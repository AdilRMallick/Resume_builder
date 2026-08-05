"""Feed fetching, parsing, and posting identity.

Everything in this module is pure except `fetch_feed`, which is the only function that
touches the network. That split is what makes identity unit-testable without fixtures
of the whole 12MB feed.

The real feed shape, confirmed against
`SimplifyJobs/New-Grad-Positions@dev:.github/scripts/listings.json` (18k records):

    {
      "source": "Simplify",              # or a GitHub handle for community submissions
      "category": "Software",            # Software | AI/ML/Data | Hardware | Quant | Product
      "company_name": "D2L",
      "id": "4f5e74ec-73cb-4c30-887b-3f97e6fdacd6",   # UUID4, unique across the file
      "title": "Software Developer - New Graduate",
      "active": true,
      "date_updated": 1765365281,        # unix seconds
      "date_posted": 1765365281,         # unix seconds
      "url": "https://...",
      "locations": ["Toronto, ON, Canada", ...],
      "company_url": "https://simplify.jobs/c/D2L",
      "is_visible": true,
      "sponsorship": "Other",            # Other | Offers Sponsorship
                                         # | Does Not Offer Sponsorship
                                         # | U.S. Citizenship is Required
      "degrees": ["Bachelor's", ...]
    }

There is no `terms` or `season` field in the new-grad feed (that is the internships
repo). Start season is therefore extracted from the title where present.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from jme.logging import get_logger

log = get_logger(__name__)

# A record missing any of these is unusable and is skipped, not fatal. `id` is
# deliberately absent: the canonical key has a documented fallback for records without
# one, which is the whole point of the fallback.
REQUIRED_FIELDS = ("company_name", "title", "url")


# --------------------------------------------------------------------------------------
# typed errors
# --------------------------------------------------------------------------------------


class FeedError(Exception):
    """Base for every ingestor feed failure."""


class FeedNotFoundError(FeedError):
    """The feed URL returned 404. The path moved, or the branch was renamed."""

    def __init__(self, url: str, status_code: int = 404) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"feed not found (HTTP {status_code}): {url}")


class FeedUnavailableError(FeedError):
    """Transport failure or a non-404 error status. Retry later."""

    def __init__(self, url: str, reason: str, status_code: int | None = None) -> None:
        self.url = url
        self.reason = reason
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code else reason
        super().__init__(f"feed unavailable ({detail}): {url}")


class FeedParseError(FeedError):
    """The body is not a JSON array of objects. Never retried, it needs a human."""

    def __init__(self, reason: str, *, source: str | None = None) -> None:
        self.reason = reason
        self.source = source
        where = f" [{source}]" if source else ""
        super().__init__(f"feed is not parseable{where}: {reason}")


# --------------------------------------------------------------------------------------
# normalization and identity
# --------------------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Punctuation becomes a *space* rather than being deleted, so that
    "Software Engineer, New Grad" and "Software Engineer - New Grad" both collapse to
    "software engineer new grad". Deleting instead would yield "engineernew" for one of
    them and silently split the identity of a reposted role.

    NFKD first, so the en-dashes, curly quotes, and accented characters that the feed is
    full of fold onto plain ASCII instead of surviving as distinct code points: "L'Oréal"
    and "L Oreal" have to reach the same key. Combining marks are dropped rather than
    turned into separators, so "Oréal" becomes "oreal" and not "ore al"; everything else
    outside `[a-z0-9]` becomes a space, which keeps unicode punctuation behaving exactly
    like its ASCII equivalent.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", stripped.lower()).strip()


def url_parts(url: str | None) -> tuple[str, str]:
    """Return (host, path) for keying. Host is lowercased with a leading `www.` dropped.

    Dropping `www.` matters: a company that reposts through `www.acme.com` after
    previously using `acme.com` is the same posting, and treating it as new would
    reset its repost history.
    """
    if not url:
        return "", ""
    split = urlsplit(url.strip())
    host = (split.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = split.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return host, path


def canonical_key(
    simplify_id: str | None, company: str | None, title: str | None, url: str | None
) -> str:
    """Stable identity for a posting.

    Prefer the Simplify id: it is a UUID4 that upstream guarantees unique across the
    file and stable across edits to title, location, and URL, which are exactly the
    fields that churn. A UUID and a 64-char sha256 hex digest can never collide, so the
    two key spaces coexist without a discriminating prefix.

    Fall back to `sha256(normalize(company) + normalize(title) + url_host + url_path)`
    per ARCHITECTURE.md. Note the query string is deliberately *not* part of the key,
    which is the documented design; on the handful of hosts that identify a posting
    purely by query parameter (`?job_id=...`) the fallback leans entirely on company and
    title to disambiguate. In the real feed every record carries an id, so the fallback
    only fires for hand-written fixtures and malformed submissions.
    """
    if simplify_id and simplify_id.strip():
        return simplify_id.strip()
    host, path = url_parts(url)
    material = f"{normalize(company)}{normalize(title)}{host}{path}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# field mapping
# --------------------------------------------------------------------------------------

# `category` in the feed -> our `role_type` slug. The long-form variants are legacy
# values still present on a few hundred older community-submitted records.
ROLE_TYPE_BY_CATEGORY = {
    "software": "swe",
    "software engineering": "swe",
    "ai/ml/data": "ai_ml_data",
    "data science, ai & machine learning": "ai_ml_data",
    "hardware": "hardware",
    "quant": "quant",
    "quantitative finance": "quant",
    "product": "pm",
    "product management": "pm",
}

_REMOTE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b", re.IGNORECASE)

_SEASONS = "spring|summer|fall|autumn|winter"
_SEASON_YEAR = re.compile(rf"\b({_SEASONS})\b[\s,'’-]*\b(20\d{{2}})\b", re.IGNORECASE)
_YEAR_SEASON = re.compile(rf"\b(20\d{{2}})\b[\s,'’-]*\b({_SEASONS})\b", re.IGNORECASE)
_BARE_YEAR = re.compile(r"\b(20[2-4]\d)\b")

_SEASON_CANON = {"autumn": "Fall"}


def role_type_for(category: str | None) -> str | None:
    if not category:
        return None
    return ROLE_TYPE_BY_CATEGORY.get(category.strip().lower(), "other")


def is_remote(locations: Iterable[str] | None) -> bool:
    """True when any location string reads as remote.

    The feed spells it half a dozen ways: "Remote", "Remote in USA", "Remote in USA (FL)".
    """
    return any(_REMOTE.search(loc) for loc in locations or () if isinstance(loc, str))


def extract_start_season(*texts: str | None) -> str | None:
    """Pull a start season out of free text, e.g. a title.

    Season words only count when a nearby year backs them up. Without that guard
    "Full-Stack Java / Spring Boot Developer" reports a Spring start, which is the exact
    kind of quiet garbage that makes a downstream eligibility filter untrustworthy.
    A bare four-digit year is accepted on its own and returned year-only.
    """
    for text in texts:
        if not text:
            continue
        cleaned = unicodedata.normalize("NFKD", text)
        match = _SEASON_YEAR.search(cleaned)
        if match:
            season, year = match.group(1).lower(), match.group(2)
            return f"{_SEASON_CANON.get(season, season.capitalize())} {year}"
        match = _YEAR_SEASON.search(cleaned)
        if match:
            year, season = match.group(1), match.group(2).lower()
            return f"{_SEASON_CANON.get(season, season.capitalize())} {year}"
        match = _BARE_YEAR.search(cleaned)
        if match:
            return match.group(1)
    return None


def parse_timestamp(value: Any) -> dt.datetime | None:
    """Feed timestamps are unix seconds. Zero and junk both mean 'unknown'."""
    if value in (None, "", 0):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedRecord:
    """One feed entry, mapped onto the columns of `posting`."""

    canonical_key: str
    simplify_id: str | None
    company: str
    title: str
    url: str
    url_host: str | None
    locations: list[str] = field(default_factory=list)
    sponsorship: str | None = None
    role_type: str | None = None
    start_season: str | None = None
    is_remote: bool = False
    posted_at: dt.datetime | None = None
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


def record_from_raw(obj: Any) -> FeedRecord:
    """Map one raw feed object. Raises FeedParseError when the object is unusable."""
    if not isinstance(obj, dict):
        raise FeedParseError(f"expected an object, got {type(obj).__name__}")

    missing = [f for f in REQUIRED_FIELDS if not str(obj.get(f) or "").strip()]
    if missing:
        raise FeedParseError(f"record missing required field(s): {', '.join(missing)}")

    company = str(obj["company_name"]).strip()
    title = str(obj["title"]).strip()
    url = str(obj["url"]).strip()
    simplify_id = str(obj.get("id") or "").strip() or None

    locations = [str(loc).strip() for loc in (obj.get("locations") or []) if str(loc).strip()]
    host, _path = url_parts(url)

    sponsorship = obj.get("sponsorship")
    sponsorship = str(sponsorship).strip() if sponsorship else None

    # `active` is the applications-open flag; `is_visible` is upstream's soft-delete.
    # Either being false means the posting should not be surfaced, so both feed into
    # the same lifecycle bit rather than being modelled separately.
    active = bool(obj.get("active", False)) and bool(obj.get("is_visible", True))

    return FeedRecord(
        canonical_key=canonical_key(simplify_id, company, title, url),
        simplify_id=simplify_id,
        company=company,
        title=title,
        url=url,
        url_host=host or None,
        locations=locations,
        sponsorship=sponsorship,
        role_type=role_type_for(obj.get("category")),
        start_season=extract_start_season(title),
        is_remote=is_remote(locations),
        posted_at=parse_timestamp(obj.get("date_posted")),
        active=active,
        raw=obj,
    )


def _dedupe(records: Iterable[FeedRecord]) -> Iterator[FeedRecord]:
    """Last write wins on a repeated canonical key.

    Postgres refuses an `ON CONFLICT` statement whose VALUES list contains the same
    conflict target twice ("cannot affect row a second time"), so duplicates have to be
    collapsed here rather than left for the database to complain about.
    """
    seen: dict[str, FeedRecord] = {}
    for record in records:
        seen[record.canonical_key] = record
    yield from seen.values()


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    records: list[FeedRecord]
    sha256: str
    skipped: int


def parse_feed(body: bytes | str, *, source: str | None = None) -> ParsedFeed:
    """Parse the feed body into records plus the digest of the exact bytes seen.

    The digest is over the raw bytes, not the parsed structure: it is a "did the feed
    change at all" marker for `ingest_run`, and re-serializing would make it depend on
    our own JSON formatting.
    """
    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise FeedParseError(f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}",
                             source=source) from exc

    if not isinstance(data, list):
        raise FeedParseError(f"expected a JSON array, got {type(data).__name__}", source=source)

    records: list[FeedRecord] = []
    skipped = 0
    for index, obj in enumerate(data):
        try:
            records.append(record_from_raw(obj))
        except FeedParseError as exc:
            # One bad row must not lose the other 18,000.
            skipped += 1
            if skipped <= 10:
                log.warning("feed_record_skipped", index=index, reason=str(exc))

    deduped = list(_dedupe(records))
    if len(deduped) != len(records):
        log.warning("feed_duplicate_keys_collapsed", dropped=len(records) - len(deduped))

    return ParsedFeed(records=deduped, sha256=digest, skipped=skipped)


# --------------------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------------------


def fetch_feed(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
    user_agent: str | None = None,
) -> bytes:
    """GET the feed. The only function in this package that touches the network."""
    headers = {"User-Agent": user_agent} if user_agent else None
    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise FeedUnavailableError(url, f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            http.close()

    if response.status_code == 404:
        raise FeedNotFoundError(url, response.status_code)
    if response.status_code >= 400:
        raise FeedUnavailableError(url, "error status", status_code=response.status_code)

    log.info("feed_fetched", url=url, bytes=len(response.content), status=response.status_code)
    return response.content


def load_feed_file(path: str | Path) -> bytes:
    """Read a local listings.json, for `--file` and for fixtures."""
    p = Path(path)
    try:
        return p.read_bytes()
    except FileNotFoundError as exc:
        raise FeedNotFoundError(str(p)) from exc
    except OSError as exc:
        raise FeedUnavailableError(str(p), f"{type(exc).__name__}: {exc}") from exc

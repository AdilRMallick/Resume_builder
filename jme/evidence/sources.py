"""Where evidence text comes from.

Two sources today, both yielding the same `SourceRecord` shape so the ingester does
not care which is which:

  * `markdown`     - a recursive directory of `.md` files (JME_EVIDENCE_DIR)
  * `repo_readme`  - the README of each repo in JME_EVIDENCE_REPOS ("owner/repo")

`source_ref` is the stable identity of a document: a POSIX-style path relative to the
evidence directory, or "owner/repo". It is half of the chunk key, so it must not
change when the process runs from a different working directory.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from jme.config import get_settings
from jme.logging import get_logger

log = get_logger(__name__)

SOURCE_MARKDOWN = "markdown"
SOURCE_REPO_README = "repo_readme"

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv", ".obsidian"}

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class SourceRecord:
    source_type: str
    source_ref: str
    text: str


# --------------------------------------------------------------------------------------
# markdown directory
# --------------------------------------------------------------------------------------


def iter_markdown_dir(root: str | Path) -> Iterator[SourceRecord]:
    """Yield every markdown file under `root`, recursively, in sorted path order.

    Sorted so a run is reproducible and diffs between runs are readable. Missing
    directory is a warning, not an error: a fresh clone has no evidence yet.
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        log.warning("evidence.dir_missing", dir=str(base))
        return

    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        rel = path.relative_to(base)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            log.warning("evidence.decode_failed", path=str(path))
            continue
        if not text.strip():
            continue
        yield SourceRecord(SOURCE_MARKDOWN, rel.as_posix(), text)


# --------------------------------------------------------------------------------------
# github readmes
# --------------------------------------------------------------------------------------


def fetch_repo_readmes(
    repos: Sequence[str],
    *,
    token: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[SourceRecord]:
    """Fetch each repo's README via the GitHub API.

    Uses `/repos/{owner}/{repo}/readme` with the raw media type, which resolves
    whatever the README is actually called and whichever branch is default. A repo
    that 404s (private, renamed, typo'd) is logged and skipped rather than failing the
    whole run: one bad entry in config should not stop the corpus from rebuilding.
    """
    if not repos:
        return []

    headers = {
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": get_settings().user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    records: list[SourceRecord] = []
    try:
        for repo in repos:
            ref = repo.strip()
            if not _REPO_RE.match(ref):
                raise ValueError(f"evidence repo must look like 'owner/repo', got {repo!r}")
            url = f"{GITHUB_API}/repos/{ref}/readme"
            try:
                response = http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("evidence.readme_fetch_failed", repo=ref, error=str(exc))
                continue
            if response.status_code == 404:
                log.warning("evidence.readme_not_found", repo=ref)
                continue
            if response.status_code in (401, 403):
                log.warning(
                    "evidence.readme_forbidden",
                    repo=ref,
                    status=response.status_code,
                    hint="set GITHUB_TOKEN for private repos or to lift rate limits",
                )
                continue
            if response.status_code >= 400:
                log.warning("evidence.readme_error", repo=ref, status=response.status_code)
                continue
            text = response.text
            if not text.strip():
                log.warning("evidence.readme_empty", repo=ref)
                continue
            records.append(SourceRecord(SOURCE_REPO_README, ref, text))
    finally:
        if owned:
            http.close()
    return records


# --------------------------------------------------------------------------------------
# combined
# --------------------------------------------------------------------------------------


def load_sources(
    *,
    directory: str | Path | None = None,
    repos: Sequence[str] | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[SourceRecord], set[str]]:
    """Load everything configured.

    Returns the records plus the set of source types that were actually *scanned*.
    The ingester needs that second value: it may only soft-delete chunks belonging to
    a source type it just looked at, otherwise running `ingest --dir` with no repos
    configured would wipe every repo README from the corpus.
    """
    settings = get_settings()
    directory = settings.evidence_dir if directory is None else directory
    repos = settings.evidence_repos if repos is None else list(repos)
    token = settings.github_token if token is None else token

    records: list[SourceRecord] = list(iter_markdown_dir(directory))
    scanned = {SOURCE_MARKDOWN}
    if repos:
        records.extend(fetch_repo_readmes(repos, token=token, client=client))
        scanned.add(SOURCE_REPO_README)
    return records, scanned

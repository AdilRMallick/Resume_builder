"""Unit tests for parsing and posting identity. No database, no network."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from _feedfixtures import ACME, EDGE_CASES, GLOBEX, INITECH, MALFORMED, RUN1, UMBRELLA

from jme.ingestor.feed import (
    FeedParseError,
    canonical_key,
    extract_start_season,
    is_remote,
    normalize,
    parse_feed,
    parse_timestamp,
    record_from_raw,
    role_type_for,
    url_parts,
)

# --------------------------------------------------------------------------------------
# normalize
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Software Engineer", "software engineer"),
        ("  Software   Engineer  ", "software engineer"),
        ("SOFTWARE ENGINEER", "software engineer"),
        # punctuation becomes a separator, never a deletion
        ("Software Engineer, New Grad", "software engineer new grad"),
        ("Software Engineer - New Grad", "software engineer new grad"),
        ("Software Engineer -- New Grad", "software engineer new grad"),
        ("Software Engineer/New Grad", "software engineer new grad"),
        ("Software Engineer (New Grad)", "software engineer new grad"),
        # unicode dashes and quotes fold to ASCII
        ("Software Engineer – New Grad", "software engineer new grad"),
        ("Software Engineer — New Grad", "software engineer new grad"),
        ("L’Oréal", "l oreal"),
        ("Zalando SE", "zalando se"),
        # company punctuation
        ("Acme Corp.", "acme corp"),
        ("Acme Corp", "acme corp"),
        ("AT&T", "at t"),
        ("Ernst & Young", "ernst young"),
        ("Booz Allen Hamilton, Inc.", "booz allen hamilton inc"),
        ("Yahoo! Inc", "yahoo inc"),
        ("Deloitte (US)", "deloitte us"),
        ("3M", "3m"),
        ("h/e/r/e", "h e r e"),
        # degenerate input
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("!!!", ""),
        ("---", ""),
        ("\t\n", ""),
        # digits survive, they are load-bearing in titles
        ("Software Engineer 1", "software engineer 1"),
        ("Software Engineer I – 2026 Start", "software engineer i 2026 start"),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize("Software Engineer, New Grad – 2026")
    assert normalize(once) == once


def test_normalize_collapses_punctuation_only_differences() -> None:
    variants = [
        "Software Engineer, New Grad",
        "Software Engineer - New Grad",
        "Software Engineer – New Grad",
        "software engineer   new  grad",
        "Software Engineer: New Grad!",
    ]
    assert len({normalize(v) for v in variants}) == 1


def test_normalize_does_not_merge_distinct_words() -> None:
    # deleting punctuation instead of replacing it would produce "engineernew" here
    assert normalize("Engineer-New") != normalize("EngineerNew")


# --------------------------------------------------------------------------------------
# url_parts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", ("boards.greenhouse.io", "/acme/jobs/123")),
        ("https://www.acme.com/careers/1", ("acme.com", "/careers/1")),
        ("https://ACME.com/Careers/1", ("acme.com", "/Careers/1")),
        ("https://acme.com/careers/1/", ("acme.com", "/careers/1")),
        ("https://acme.com/", ("acme.com", "/")),
        ("https://acme.com", ("acme.com", "")),
        ("https://acme.com/careers?job_id=7", ("acme.com", "/careers")),
        ("https://acme.com:8443/careers", ("acme.com", "/careers")),
        ("", ("", "")),
        (None, ("", "")),
    ],
)
def test_url_parts(url: str | None, expected: tuple[str, str]) -> None:
    assert url_parts(url) == expected


# --------------------------------------------------------------------------------------
# canonical_key
# --------------------------------------------------------------------------------------


def test_canonical_key_prefers_simplify_id() -> None:
    key = canonical_key("abc-123", "Acme", "Software Engineer", "https://acme.com/1")
    assert key == "abc-123"


def test_canonical_key_ignores_blank_simplify_id() -> None:
    blank = canonical_key("   ", "Acme", "Software Engineer", "https://acme.com/1")
    absent = canonical_key(None, "Acme", "Software Engineer", "https://acme.com/1")
    assert blank == absent
    assert len(blank) == 64


def test_canonical_key_fallback_matches_the_documented_formula() -> None:
    expected = hashlib.sha256(
        (normalize("Acme Corp.") + normalize("Software Engineer") + "acme.com" + "/careers/1")
        .encode("utf-8")
    ).hexdigest()
    assert canonical_key(None, "Acme Corp.", "Software Engineer", "https://acme.com/careers/1") == (
        expected
    )


def test_canonical_key_fallback_is_hex_sha256() -> None:
    key = canonical_key(None, "Acme", "SWE", "https://acme.com/1")
    assert len(key) == 64
    int(key, 16)  # raises if not hex


def test_canonical_key_never_collides_between_id_and_hash_space() -> None:
    # a Simplify id is a 36-char uuid, a fallback key is 64 hex chars
    assert len(canonical_key("aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa", "a", "b", "c")) == 36
    assert len(canonical_key(None, "a", "b", "https://x.com/y")) == 64


@pytest.mark.parametrize(
    ("company_a", "title_a", "company_b", "title_b"),
    [
        # punctuation-only differences must not split identity
        ("Acme Corp.", "Software Engineer, New Grad", "Acme Corp", "Software Engineer - New Grad"),
        ("AT&T", "Software Engineer", "AT & T", "Software  Engineer"),
        ("Ernst & Young", "Analyst (Tech)", "Ernst & Young", "Analyst - Tech"),
        ("L’Oréal", "Data Scientist", "L Oreal", "Data   Scientist"),
        ("Yahoo! Inc", "SWE – New Grad", "Yahoo Inc", "SWE - New Grad"),
        ("Booz Allen Hamilton, Inc.", "SWE I", "Booz Allen Hamilton Inc", "SWE I"),
    ],
)
def test_canonical_key_is_stable_across_punctuation(
    company_a: str, title_a: str, company_b: str, title_b: str
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/1"
    assert canonical_key(None, company_a, title_a, url) == canonical_key(
        None, company_b, title_b, url
    )


def test_canonical_key_does_not_expand_abbreviations() -> None:
    """A documented limit: the normalizer strips punctuation, it does not paraphrase.

    "Ernst & Young" and "Ernst and Young" are different keys. In practice this never
    fires, because the real feed always carries a Simplify id and the fallback is only
    reached for hand-written records.
    """
    url = "https://acme.com/1"
    assert canonical_key(None, "Ernst & Young", "Analyst", url) != canonical_key(
        None, "Ernst and Young", "Analyst", url
    )


def test_canonical_key_ignores_www_and_trailing_slash() -> None:
    a = canonical_key(None, "Acme", "SWE", "https://www.acme.com/careers/1/")
    b = canonical_key(None, "Acme", "SWE", "https://acme.com/careers/1")
    assert a == b


def test_canonical_key_differs_on_real_differences() -> None:
    base = canonical_key(None, "Acme", "Software Engineer", "https://acme.com/1")
    assert base != canonical_key(None, "Acme", "Senior Software Engineer", "https://acme.com/1")
    assert base != canonical_key(None, "Globex", "Software Engineer", "https://acme.com/1")
    assert base != canonical_key(None, "Acme", "Software Engineer", "https://acme.com/2")
    assert base != canonical_key(None, "Acme", "Software Engineer", "https://globex.com/1")


def test_canonical_key_ampersand_company_does_not_collide_with_neighbour() -> None:
    # "AT&T" and "ATT" both normalize away punctuation, but not to the same tokens
    assert normalize("AT&T") == "at t"
    assert normalize("ATT") == "att"
    url = "https://acme.com/1"
    assert canonical_key(None, "AT&T", "SWE", url) != canonical_key(None, "ATT", "SWE", url)


# --------------------------------------------------------------------------------------
# field mapping
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("Software", "swe"),
        ("software", "swe"),
        ("Software Engineering", "swe"),
        ("AI/ML/Data", "ai_ml_data"),
        ("Data Science, AI & Machine Learning", "ai_ml_data"),
        ("Hardware", "hardware"),
        ("Quant", "quant"),
        ("Quantitative Finance", "quant"),
        ("Product", "pm"),
        ("Product Management", "pm"),
        ("Something New Upstream Added", "other"),
        (None, None),
        ("", None),
    ],
)
def test_role_type_for(category: str | None, expected: str | None) -> None:
    assert role_type_for(category) == expected


@pytest.mark.parametrize(
    ("locations", "expected"),
    [
        (["Remote in USA"], True),
        (["Remote"], True),
        (["Remote in USA (FL)"], True),
        (["Detroit, MI", "Remote in Canada"], True),
        (["Detroit, MI"], False),
        (["NYC", "Chicago, IL"], False),
        ([], False),
        (None, False),
        # substring traps
        (["Remotely Village, VT"], False),
        (["Piedmonte, IT"], False),
    ],
)
def test_is_remote(locations: list[str] | None, expected: bool) -> None:
    assert is_remote(locations) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Software Engineer – New Grad Summer 2026", "Summer 2026"),
        ("Associate Software Engineer - Starting Summer 2026", "Summer 2026"),
        ("Graduate Software Engineer - 2026 Start - Chicago", "2026"),
        ("2026 Full-time - Software Engineer I", "2026"),
        ("ASIC Clocks Design Engineer – New College Grad 2025", "2025"),
        ("Fall 2027 Software Engineer", "Fall 2027"),
        ("Software Engineer, Autumn 2026", "Fall 2026"),
        ("Software Engineer 2026 Winter", "Winter 2026"),
        # the trap: a season word with no year is not a season
        ("Full-Stack Java / Spring Boot Developer", None),
        ("Software Engineer 1 - Java - Spring Boot", None),
        ("Winter Sports Analyst", None),
        ("Software Engineer", None),
        ("Software Engineer 1", None),
        (None, None),
        (""  , None),
    ],
)
def test_extract_start_season(title: str | None, expected: str | None) -> None:
    assert extract_start_season(title) == expected


def test_extract_start_season_checks_each_text_in_order() -> None:
    assert extract_start_season(None, "Software Engineer", "Summer 2027 cohort") == "Summer 2027"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1765365281, dt.datetime(2025, 12, 10, 11, 14, 41, tzinfo=dt.UTC)),
        (0, None),
        (None, None),
        ("", None),
        (-5, None),
        ("not a number", None),
    ],
)
def test_parse_timestamp(value: object, expected: dt.datetime | None) -> None:
    assert parse_timestamp(value) == expected


def test_parse_timestamp_is_timezone_aware() -> None:
    parsed = parse_timestamp(1765365281)
    assert parsed is not None and parsed.tzinfo is not None


# --------------------------------------------------------------------------------------
# record_from_raw
# --------------------------------------------------------------------------------------


def test_record_from_raw_maps_every_column() -> None:
    obj = json.loads(RUN1.read_text(encoding="utf-8"))[1]
    record = record_from_raw(obj)

    assert record.canonical_key == GLOBEX
    assert record.simplify_id == GLOBEX
    assert record.company == "Globex"
    assert record.title == "Software Engineer – New Grad Summer 2026"
    assert record.url == "https://jobs.lever.co/globex/6ce4c517-ed15-4308-b1b3-db9ed7be5a53"
    assert record.url_host == "jobs.lever.co"
    assert record.locations == ["Remote in USA"]
    assert record.sponsorship == "Other"
    assert record.role_type == "swe"
    assert record.start_season == "Summer 2026"
    assert record.is_remote is True
    assert record.posted_at == dt.datetime(2025, 12, 10, 16, 47, 55, tzinfo=dt.UTC)
    assert record.active is True
    assert record.raw == obj


def test_record_from_raw_respects_active_and_is_visible() -> None:
    base = {
        "company_name": "Acme",
        "title": "SWE",
        "url": "https://acme.com/1",
        "id": "x",
        "locations": [],
    }
    assert record_from_raw({**base, "active": True, "is_visible": True}).active is True
    assert record_from_raw({**base, "active": False, "is_visible": True}).active is False
    # upstream's soft delete is just as disqualifying as applications being closed
    assert record_from_raw({**base, "active": True, "is_visible": False}).active is False
    # a record with neither flag defaults to inactive rather than silently going live
    assert record_from_raw(base).active is False


@pytest.mark.parametrize(
    "obj",
    [
        {"title": "SWE", "url": "https://a.com/1"},
        {"company_name": "Acme", "url": "https://a.com/1"},
        {"company_name": "Acme", "title": "SWE"},
        {"company_name": "  ", "title": "SWE", "url": "https://a.com/1"},
        "not an object",
        None,
        42,
    ],
)
def test_record_from_raw_rejects_unusable_records(obj: object) -> None:
    with pytest.raises(FeedParseError):
        record_from_raw(obj)


# --------------------------------------------------------------------------------------
# parse_feed
# --------------------------------------------------------------------------------------


def test_parse_feed_reads_the_fixture() -> None:
    parsed = parse_feed(RUN1.read_bytes())
    assert len(parsed.records) == 4
    assert parsed.skipped == 0
    assert {r.canonical_key for r in parsed.records} == {ACME, GLOBEX, INITECH, UMBRELLA}
    umbrella = next(r for r in parsed.records if r.canonical_key == UMBRELLA)
    assert umbrella.active is False
    # "Spring Boot" must not read as a Spring start season
    assert umbrella.start_season is None


def test_parse_feed_sha256_is_over_the_raw_bytes() -> None:
    body = RUN1.read_bytes()
    assert parse_feed(body).sha256 == hashlib.sha256(body).hexdigest()


def test_parse_feed_sha256_is_deterministic_and_change_sensitive() -> None:
    a = parse_feed(RUN1.read_bytes()).sha256
    b = parse_feed(RUN1.read_bytes()).sha256
    c = parse_feed(RUN1.read_bytes() + b"\n").sha256
    assert a == b != c


def test_parse_feed_accepts_str_and_bytes() -> None:
    text = RUN1.read_text(encoding="utf-8")
    assert parse_feed(text).sha256 == parse_feed(text.encode("utf-8")).sha256


def test_parse_feed_raises_on_malformed_json() -> None:
    with pytest.raises(FeedParseError) as exc:
        parse_feed(MALFORMED.read_bytes(), source="fixture")
    assert "invalid JSON" in str(exc.value)
    assert exc.value.source == "fixture"


@pytest.mark.parametrize("body", [b"", b"{}", b'{"listings": []}', b"null", b"[1,2,3", b"not json"])
def test_parse_feed_rejects_non_array_documents(body: bytes) -> None:
    with pytest.raises(FeedParseError):
        parse_feed(body)


def test_parse_feed_accepts_an_empty_array() -> None:
    parsed = parse_feed(b"[]")
    assert parsed.records == []
    assert parsed.skipped == 0


def test_parse_feed_skips_bad_records_without_losing_good_ones() -> None:
    parsed = parse_feed(EDGE_CASES.read_bytes())
    keys = {r.canonical_key for r in parsed.records}

    # the record with no url and the bare string are skipped
    assert parsed.skipped == 2
    # the record with no id falls back to the sha256 key
    no_id = next(r for r in parsed.records if r.company == "No Id Co.")
    assert no_id.simplify_id is None
    assert len(no_id.canonical_key) == 64
    assert no_id.url_host == "noidco.com"

    # is_visible false is respected
    hidden = next(r for r in parsed.records if r.company == "Hidden Co")
    assert hidden.active is False
    assert hidden.role_type == "hardware"
    assert hidden.is_remote is True
    assert hidden.start_season == "2026"
    assert hidden.posted_at is None

    # the duplicate canonical key is collapsed, last one wins
    assert "77777777-7777-4777-8777-777777777777" in keys
    dupes = [r for r in parsed.records if r.canonical_key == "77777777-7777-4777-8777-777777777777"]
    assert len(dupes) == 1
    assert dupes[0].title == "Software Engineer, Second Version Wins"

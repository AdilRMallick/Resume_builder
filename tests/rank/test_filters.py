"""Stage 1, one rule at a time.

The rule that matters most here is the one that is easiest to get wrong: *missing
data must flag, not drop*. Three tests below (`test_missing_start_season_is_flagged`,
`test_unknown_sponsorship_is_flagged`, `test_missing_role_type_is_flagged`) exist
specifically to fail if someone "tidies up" a NULL check into an exclusion.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from jme.rank.filters import DROP_REASONS, apply_filters, classify_posting
from tests.rank.factories import make_settings

pytestmark = pytest.mark.integration


def _kept_ids(session: Session, settings) -> set[int]:
    return {p.posting_id for p in apply_filters(session, settings).kept}


# --------------------------------------------------------------------------------------
# inactive
# --------------------------------------------------------------------------------------


def test_inactive_posting_is_dropped(db_session, make_posting, settings):
    live = make_posting()
    dead = make_posting(inactive_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC))

    outcome = apply_filters(db_session, settings)

    assert live.id in outcome.posting_ids
    assert dead.id not in outcome.posting_ids
    assert outcome.drop_counts["inactive"] == 1
    assert outcome.total_postings == 2
    assert outcome.active_postings == 1


# --------------------------------------------------------------------------------------
# sponsorship
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_class", "expected_reason"),
    [
        ("Offers Sponsorship", "offers", None),
        ("Does Not Offer Sponsorship", "not_offered", "sponsorship_not_offered"),
        ("U.S. Citizenship is Required", "citizenship", "sponsorship_citizenship"),
        ("Active Security Clearance Required", "citizenship", "sponsorship_citizenship"),
        ("Offshore Only", "offshore", "sponsorship_offshore"),
        ("Requires U.S. Permanent Residency", "citizenship", "sponsorship_citizenship"),
    ],
)
def test_sponsorship_classification(
    db_session, make_posting, settings, value, expected_class, expected_reason
):
    posting = make_posting(sponsorship=value)

    classified, reason = classify_posting(db_session, posting.id, settings)

    assert classified is not None
    assert classified.sponsorship_class == expected_class
    assert reason == expected_reason


@pytest.mark.parametrize("value", [None, "", "Unclear, ask the recruiter", "TBD"])
def test_unknown_sponsorship_is_flagged_not_dropped(db_session, make_posting, settings, value):
    """Degrade rather than drop: a sponsorship string we cannot parse is not a rejection."""
    posting = make_posting(sponsorship=value)

    classified, reason = classify_posting(db_session, posting.id, settings)

    assert reason is None
    assert classified is not None
    assert classified.sponsorship_class == "unknown"
    assert "sponsorship_unknown" in classified.flags
    assert posting.id in _kept_ids(db_session, settings)


def test_allow_sponsorship_required_readmits_blocked_roles(db_session, make_posting):
    blocked = make_posting(sponsorship="U.S. Citizenship is Required")
    offshore = make_posting(sponsorship="Offshore Only")

    strict = _kept_ids(db_session, make_settings())
    permissive = _kept_ids(db_session, make_settings(allow_sponsorship_required=True))

    assert blocked.id not in strict and offshore.id not in strict
    assert {blocked.id, offshore.id} <= permissive


# --------------------------------------------------------------------------------------
# location
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("locations", "is_remote", "kept"),
    [
        (["Detroit, MI"], False, True),
        (["Ann Arbor, MI"], False, True),
        (["Chicago, IL"], False, True),
        (["Remote in USA"], False, True),
        (["Remote"], False, True),
        (["San Francisco, CA"], True, True),  # is_remote wins over the location string
        (["San Francisco, CA"], False, False),
        (["London, UK", "Bangalore, India"], False, False),
        (["New York, NY", "Detroit, MI"], False, True),  # any location may match
        ([], False, False),
        (None, False, False),
    ],
)
def test_location_allowlist(db_session, make_posting, settings, locations, is_remote, kept):
    posting = make_posting(locations=locations, is_remote=is_remote)

    assert (posting.id in _kept_ids(db_session, settings)) is kept


def test_location_stored_as_bare_string_is_handled(db_session, make_posting, settings):
    """`locations` is JSONB, and the feed has been known to emit a bare string."""
    posting = make_posting(locations=None)
    db_session.execute(
        __import__("sqlalchemy").text(
            "UPDATE posting SET locations = '\"Detroit, MI\"'::jsonb WHERE id = :id"
        ),
        {"id": posting.id},
    )

    assert posting.id in _kept_ids(db_session, settings)


# --------------------------------------------------------------------------------------
# role type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_type", "expected_state", "kept"),
    [
        ("swe", "allowed", True),
        ("SWE", "allowed", True),  # case insensitive
        (" swe ", "allowed", True),  # whitespace insensitive
        ("quant", "excluded", False),
        ("pm", "excluded", False),
        ("hardware", "excluded", False),
    ],
)
def test_role_type_allowlist(db_session, make_posting, settings, role_type, expected_state, kept):
    posting = make_posting(role_type=role_type)

    classified, _ = classify_posting(db_session, posting.id, settings)

    assert classified is not None
    assert classified.role_state == expected_state
    assert (posting.id in _kept_ids(db_session, settings)) is kept


@pytest.mark.parametrize("role_type", [None, "", "   ", "quantum-alchemy"])
def test_missing_or_unrecognised_role_type_is_flagged_not_dropped(
    db_session, make_posting, settings, role_type
):
    """A role type we have never seen is our gap in coverage, not the posting's fault."""
    posting = make_posting(role_type=role_type)

    classified, reason = classify_posting(db_session, posting.id, settings)

    assert reason is None
    assert classified is not None
    assert classified.role_state == "unknown"
    assert "role_type_unknown" in classified.flags
    assert posting.id in _kept_ids(db_session, settings)


# --------------------------------------------------------------------------------------
# start season
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start_season", "expected_state", "kept"),
    [
        ("Summer 2027", "eligible", True),
        ("Fall 2027", "eligible", True),
        ("Winter 2028", "eligible", True),
        ("Spring 2027", "too_early", False),  # January-March 2027, before a May 2027 grad
        ("Summer 2026", "too_early", False),
        ("Fall 2026", "too_early", False),
        ("2027", "partial", True),  # year only: cannot rule it out, so keep it
        ("2026", "too_early", False),  # year only, but the whole year is behind us
    ],
)
def test_start_season_against_grad_date(
    db_session, make_posting, settings, start_season, expected_state, kept
):
    posting = make_posting(start_season=start_season)

    classified, _ = classify_posting(db_session, posting.id, settings)

    assert classified is not None
    assert classified.start_state == expected_state
    assert (posting.id in _kept_ids(db_session, settings)) is kept


@pytest.mark.parametrize("start_season", [None, "", "ASAP", "Rolling start dates"])
def test_missing_start_season_is_flagged_not_dropped(
    db_session, make_posting, settings, start_season
):
    """ARCHITECTURE.md section 6, verbatim: flag rather than drop when absent."""
    posting = make_posting(start_season=start_season)

    classified, reason = classify_posting(db_session, posting.id, settings)

    assert reason is None
    assert classified is not None
    assert classified.start_state == "unknown"
    assert "start_season_unknown" in classified.flags
    assert posting.id in _kept_ids(db_session, settings)


def test_grad_date_is_configurable_not_hardcoded(db_session, make_posting):
    posting = make_posting(start_season="Spring 2027")

    assert posting.id not in _kept_ids(db_session, make_settings())
    assert posting.id in _kept_ids(db_session, make_settings(grad_date=dt.date(2026, 12, 1)))


# --------------------------------------------------------------------------------------
# funnel bookkeeping
# --------------------------------------------------------------------------------------


def test_drop_counts_are_exhaustive_and_attributed_once(db_session, make_posting, settings):
    """Every posting is either kept or attributed to exactly one rule."""
    make_posting()  # kept
    make_posting(inactive_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC))
    make_posting(sponsorship="U.S. Citizenship is Required")
    make_posting(sponsorship="Offshore Only")
    make_posting(sponsorship="Does Not Offer Sponsorship")
    make_posting(locations=["London, UK"])
    make_posting(role_type="quant")
    make_posting(start_season="Summer 2026")

    outcome = apply_filters(db_session, settings)

    assert set(outcome.drop_counts) == set(DROP_REASONS)
    assert sum(outcome.drop_counts.values()) + len(outcome.kept) == outcome.total_postings
    assert outcome.drop_counts == {
        "inactive": 1,
        "sponsorship_offshore": 1,
        "sponsorship_citizenship": 1,
        "sponsorship_not_offered": 1,
        "location": 1,
        "role_type": 1,
        "start_season": 1,
    }


def test_multiple_failures_are_attributed_to_the_first_rule(db_session, make_posting, settings):
    """Attribution order is fixed so the funnel is stable, not double-counted."""
    make_posting(sponsorship="Offshore Only", locations=["London, UK"], role_type="quant")

    outcome = apply_filters(db_session, settings)

    assert outcome.drop_counts["sponsorship_offshore"] == 1
    assert outcome.drop_counts["location"] == 0
    assert outcome.drop_counts["role_type"] == 0


def test_classify_posting_reports_inactive_and_missing(db_session, make_posting, settings):
    dead = make_posting(inactive_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC))

    assert classify_posting(db_session, dead.id, settings) == (None, "inactive")
    assert classify_posting(db_session, 9_999_999, settings) == (None, None)

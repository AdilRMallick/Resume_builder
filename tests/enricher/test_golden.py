"""Golden file test: five hand-labelled job descriptions, measured recall.

How the goldens work
--------------------
`golden/NN_name.txt`               a realistic new-grad SWE job description
`golden/NN_name.expected.json`     hand-labelled ground truth (raw_text + importance)
`golden/NN_name.llm.json`          the canned model response replayed by StubLLM

The `.llm.json` files are stand-ins for a recorded API response: they are deliberately
imperfect in the ways a real model is imperfect - they miss some labelled requirements,
add some the labels do not carry, and slice spans on different boundaries - so the
threshold below measures something. They were written by hand rather than recorded from a
live call because tests must never hit the API; when a real recording is captured, drop it
in as `.llm.json` and the same assertions apply unchanged.

Threshold
---------
RECALL_THRESHOLD is the number to move when the prompt changes. It is set below the
current measured recall with enough headroom to absorb span-boundary noise, but high
enough that dropping a whole category of requirement (all the preferred ones, say) fails
the build.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jme.enricher.extraction import extract_requirements, find_verbatim, normalize
from jme.enricher.prompts import PROMPT_VERSION
from jme.models import PostingRequirement

from .conftest import load_goldens

RECALL_THRESHOLD = 0.80
PER_FILE_RECALL_THRESHOLD = 0.70
IMPORTANCE_AGREEMENT_THRESHOLD = 0.75


def _key(text: str) -> str:
    return normalize(text).normalized.casefold()


def _matches(expected_text: str, extracted_text: str) -> bool:
    """One expected requirement is covered if a span overlaps it in either direction.

    Span boundaries are a judgement call ("SQL" vs "Working knowledge of SQL"), and the
    taxonomy resolver is what turns either into a skill id. Requiring exact equality would
    measure prompt phrasing, not extraction quality.
    """
    a, b = _key(expected_text), _key(extracted_text)
    return a in b or b in a


# --------------------------------------------------------------------------------------
# fixture hygiene - these run without a database
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", load_goldens(), ids=lambda c: c.name)
def test_canned_llm_spans_are_verbatim(case):
    """If a canned span is not in its JD, the golden is broken, not the extractor."""
    jd = normalize(case.jd_text)
    bad = [
        item["raw_text"]
        for item in case.llm_payload["requirements"]
        if find_verbatim(jd, item["raw_text"]) is None
    ]
    assert not bad, f"{case.name}: canned spans not found in the JD: {bad}"


@pytest.mark.parametrize("case", load_goldens(), ids=lambda c: c.name)
def test_expected_labels_are_verbatim_and_well_formed(case):
    jd = normalize(case.jd_text)
    bad = [
        item["raw_text"]
        for item in case.expected
        if find_verbatim(jd, item["raw_text"]) is None
    ]
    assert not bad, f"{case.name}: labelled spans not found in the JD: {bad}"
    assert len(case.expected) >= 15, f"{case.name}: label set is too thin to measure recall"
    assert {item["importance"] for item in case.expected} <= {
        "required",
        "preferred",
        "mentioned",
    }
    assert len(case.jd_text.split()) >= 250, f"{case.name}: JD is shorter than a real posting"


# --------------------------------------------------------------------------------------
# the measurement
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_golden_recall(db_session, make_posting, stub_llm, no_resolver, goldens, capsys):
    total_expected = 0
    total_found = 0
    total_importance_ok = 0
    lines: list[str] = []

    for case in goldens:
        posting_id = make_posting(
            db_session, case.jd_text, company=case.company, title=case.title
        )
        rows = extract_requirements(
            db_session,
            posting_id,
            case.jd_text,
            run_id="run-golden",
            resolver=no_resolver,
            company=case.company,
            title=case.title,
            llm_call=stub_llm([case.llm_payload]),
        )
        assert rows, f"{case.name}: extraction produced nothing"

        found = 0
        importance_ok = 0
        missed: list[str] = []
        for label in case.expected:
            hits = [row for row in rows if _matches(label["raw_text"], row.raw_text)]
            if hits:
                found += 1
                if any(row.importance.value == label["importance"] for row in hits):
                    importance_ok += 1
            else:
                missed.append(label["raw_text"])

        recall = found / len(case.expected)
        lines.append(
            f"  {case.name:<32} recall {recall:.3f}  "
            f"({found}/{len(case.expected)})  extracted {len(rows)}  missed: {missed}"
        )
        assert recall >= PER_FILE_RECALL_THRESHOLD, (
            f"{case.name}: recall {recall:.3f} below per-file floor "
            f"{PER_FILE_RECALL_THRESHOLD}; missed {missed}"
        )

        total_expected += len(case.expected)
        total_found += found
        total_importance_ok += importance_ok

    recall = total_found / total_expected
    agreement = total_importance_ok / total_found
    with capsys.disabled():
        print("\ngolden extraction recall")
        print("\n".join(lines))
        print(
            f"  {'AGGREGATE':<32} recall {recall:.3f}  ({total_found}/{total_expected})  "
            f"importance agreement {agreement:.3f}  (threshold {RECALL_THRESHOLD})"
        )

    assert recall >= RECALL_THRESHOLD, f"aggregate recall {recall:.3f} < {RECALL_THRESHOLD}"
    assert agreement >= IMPORTANCE_AGREEMENT_THRESHOLD


@pytest.mark.integration
def test_every_golden_row_is_verbatim_and_versioned(
    db_session, make_posting, stub_llm, no_resolver, goldens
):
    texts = {}
    for case in goldens:
        posting_id = make_posting(db_session, case.jd_text, company=case.company)
        texts[posting_id] = case.jd_text
        extract_requirements(
            db_session,
            posting_id,
            case.jd_text,
            run_id="run-golden-audit",
            resolver=no_resolver,
            llm_call=stub_llm([case.llm_payload]),
        )

    rows = list(db_session.scalars(select(PostingRequirement)))
    assert len(rows) >= 5 * 15
    for row in rows:
        assert row.prompt_version == PROMPT_VERSION
        assert row.model_id
        assert row.raw_text in texts[row.posting_id], (
            f"stored raw_text is not a literal span of posting {row.posting_id}: {row.raw_text!r}"
        )

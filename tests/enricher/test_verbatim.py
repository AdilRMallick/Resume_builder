"""Unit tests for the verbatim check. No database, no LLM, no network."""

from __future__ import annotations

import pytest

from jme.enricher.extraction import (
    Candidate,
    dedupe,
    find_verbatim,
    normalize,
    parse_candidates,
    verify,
)
from jme.models import Importance

JD = (
    "Requirements\n\n"
    "- Proficiency in at least one statically typed language such as\n"
    "  Go, Java, or C++.\n"
    "- Experience with   Kubernetes and Terraform.\n"
    "- Strong written communication skills; we’re a documentation-heavy team.\n"
)


def test_normalize_collapses_whitespace_and_keeps_offsets():
    result = normalize("a  b\n\tc")
    assert result.normalized == "a b c"
    assert result.original[result.offsets[0]] == "a"
    assert result.original[result.offsets[-1]] == "c"


def test_span_reflowed_across_a_line_break_is_verbatim():
    # The model returned the bullet as one line. That is whitespace, not paraphrase.
    span = find_verbatim(normalize(JD), "statically typed language such as Go, Java, or C++")
    assert span is not None
    assert "\n" in span  # the *source* span is returned, line break and all
    assert span in JD


def test_span_with_extra_internal_spaces_is_verbatim():
    assert find_verbatim(normalize(JD), "Experience with Kubernetes and Terraform") is not None


def test_curly_apostrophe_folded_to_straight_is_verbatim():
    span = find_verbatim(normalize(JD), "we're a documentation-heavy team")
    assert span == "we’re a documentation-heavy team"  # stored as the JD wrote it


def test_recapitalized_span_returns_the_source_capitalization():
    assert find_verbatim(normalize(JD), "strong WRITTEN communication skills") == (
        "Strong written communication skills"
    )


@pytest.mark.parametrize(
    "paraphrase",
    [
        "Proficiency in a statically typed programming language",  # reworded
        "Knowledge of Kubernetes, Terraform, and Docker",  # invented Docker
        "Go, Java, C++, or Rust",  # stitched plus invented
        "3+ years of experience",  # hallucinated whole cloth
        "   ",  # empty after normalization
    ],
)
def test_paraphrases_are_not_verbatim(paraphrase):
    assert find_verbatim(normalize(JD), paraphrase) is None


def test_verify_splits_kept_from_rejected():
    candidates = [
        Candidate("Kubernetes", Importance.preferred, 0.9),
        Candidate("familiarity with container orchestration", Importance.preferred, 0.8),
    ]
    kept, rejected = verify(candidates, normalize(JD))
    assert [c.raw_text for c in kept] == ["Kubernetes"]
    assert rejected == ["familiarity with container orchestration"]


def test_parse_candidates_skips_malformed_items():
    payload = {
        "requirements": [
            {"raw_text": "Go", "importance": "required", "confidence": 0.9},
            {"raw_text": "Java", "importance": "extremely required", "confidence": 0.9},  # bad enum
            {"raw_text": "   ", "importance": "required", "confidence": 0.9},  # empty
            {"raw_text": "C++", "importance": "mentioned", "confidence": "high"},  # bad float
            "not an object",
        ]
    }
    parsed = parse_candidates(payload)
    assert [c.raw_text for c in parsed] == ["Go", "C++"]
    assert parsed[1].confidence == 0.0


def test_parse_candidates_clamps_confidence():
    payload = {"requirements": [{"raw_text": "Go", "importance": "required", "confidence": 7.5}]}
    assert parse_candidates(payload)[0].confidence == 1.0


def test_dedupe_keeps_strongest_importance_and_highest_confidence():
    merged = dedupe(
        [
            Candidate("Go", Importance.mentioned, 0.4),
            Candidate("Go", Importance.required, 0.9),
            Candidate("Java", Importance.preferred, 0.5),
        ]
    )
    by_text = {c.raw_text: c for c in merged}
    assert len(merged) == 2
    assert by_text["Go"].importance is Importance.required
    assert by_text["Go"].confidence == 0.9

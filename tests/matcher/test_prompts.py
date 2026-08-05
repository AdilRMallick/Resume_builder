"""Prompt and schema shape. Pure string/dict assertions, no database and no model."""

from __future__ import annotations

from jme.matcher.prompts import (
    MATCH_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_corrective_prompt,
    build_user_prompt,
)
from jme.matcher.retrieval import RequirementView, RetrievedChunk
from jme.models import CitationStatus, Importance, Verdict

REQS = [
    RequirementView(1, 10, "Go", "Strong Go experience", Importance.required),
    RequirementView(2, None, None, "Ability to mentor interns", Importance.mentioned),
]
CHUNKS = [
    RetrievedChunk(101, "markdown", "a.md", "Go", "wrote a Go service", 0.2, False),
    RetrievedChunk(102, "repo_readme", "b/README.md", None, "postgres schema work", 0.4, True),
]


def test_prompt_version_is_pinned():
    assert PROMPT_VERSION == "v1"


def test_system_prompt_states_the_citation_rules():
    lowered = SYSTEM_PROMPT.lower()
    assert "mandatory when status is \"evidenced\"" in lowered.replace("'", '"')
    assert "must be one of the chunk ids supplied" in lowered
    assert "fabrication" in lowered


def test_schema_matches_the_contract():
    props = MATCH_SCHEMA["properties"]
    assert MATCH_SCHEMA["additionalProperties"] is False
    assert set(MATCH_SCHEMA["required"]) == {"requirements", "verdict", "rationale"}

    item = props["requirements"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {
        "requirement_id",
        "canonical_skill_id",
        "status",
        "evidence_chunk_id",
        "reasoning",
    }
    assert item["properties"]["status"]["enum"] == [s.value for s in CitationStatus]
    assert item["properties"]["evidence_chunk_id"]["type"] == ["integer", "null"]
    assert props["verdict"]["enum"] == [v.value for v in Verdict]


def test_user_prompt_labels_requirements_and_chunks():
    text = build_user_prompt(
        company="Acme",
        title="New Grad SWE",
        jd_text="Build backend services in Go.",
        requirements=REQS,
        chunks=CHUNKS,
    )
    assert "REQ 1 [required] canonical_skill_id=10 (Go)" in text
    assert "REQ 2 [mentioned] canonical_skill_id=null" in text
    assert "CHUNK 101 | markdown:a.md - Go" in text
    assert "manually tagged" in text  # chunk 102 carries a manual tag
    assert "Build backend services in Go." in text
    assert "# Valid evidence_chunk_id values\n101, 102" in text
    assert "(2 rows)" in text


def test_user_prompt_degrades_without_jd_text():
    text = build_user_prompt(
        company="Acme", title="New Grad SWE", jd_text=None, requirements=REQS, chunks=CHUNKS
    )
    assert "job description text unavailable" in text


def test_user_prompt_handles_an_empty_candidate_set():
    text = build_user_prompt(
        company="Acme", title="New Grad SWE", jd_text="x", requirements=REQS, chunks=[]
    )
    assert "(no evidence chunks retrieved)" in text
    assert "(none)" in text


def test_corrective_prompt_lists_the_valid_ids_and_changes_the_content_hash():
    original = build_user_prompt(
        company="Acme", title="New Grad SWE", jd_text="x", requirements=REQS, chunks=CHUNKS
    )
    corrected = build_corrective_prompt(
        original, problems=["requirement 1: evidence_chunk_id 999 was never supplied"], valid_chunk_ids=[102, 101]
    )
    assert corrected.startswith(original)
    assert corrected != original, "the retry must not be answerable from the same cache key"
    assert "CORRECTION" in corrected
    assert "101, 102" in corrected
    assert "999 was never supplied" in corrected

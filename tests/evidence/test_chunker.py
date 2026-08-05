"""Chunker unit tests. No DB, no network."""

from __future__ import annotations

import re

import pytest

from jme.evidence.chunker import (
    CHARS_PER_TOKEN,
    MAX_TOKENS,
    MIN_TOKENS,
    Chunk,
    chunk_markdown,
    estimate_tokens,
)

BULLET_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def _words(n: int, seed: str = "redis") -> str:
    """A blob of roughly `n` tokens' worth of text."""
    word = f"{seed} "
    return (word * ((n * CHARS_PER_TOKEN) // len(word) + 1)).strip()


def _bullet_lines(chunks: list[Chunk]) -> list[str]:
    out = []
    for chunk in chunks:
        out.extend(line for line in chunk.text.splitlines() if BULLET_RE.match(line))
    return out


# --------------------------------------------------------------------------------------
# estimate_tokens
# --------------------------------------------------------------------------------------


def test_estimate_tokens_is_four_chars_per_token():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * 400) == 100


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("a" * 100) < estimate_tokens("a" * 200)


# --------------------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------------------


def test_empty_document_yields_nothing():
    assert chunk_markdown("") == []
    assert chunk_markdown("\n\n   \n") == []


def test_heading_only_document_yields_nothing():
    assert chunk_markdown("# Title\n\n## Empty section\n") == []


def test_heading_is_carried_onto_every_chunk():
    doc = "# Resume\n\nIntro paragraph.\n\n## Redis\n\nBuilt a pipeline.\n"
    chunks = chunk_markdown(doc)
    assert [c.heading for c in chunks] == ["Resume", "Redis"]
    # the heading line also leads the chunk text, so an embedded chunk carries context
    assert chunks[0].text.startswith("# Resume")
    assert chunks[1].text.startswith("## Redis")


def test_chunks_never_span_a_heading_boundary():
    doc = "## A\n\nshort a.\n\n## B\n\nshort b.\n"
    chunks = chunk_markdown(doc)
    assert len(chunks) == 2
    assert "short b" not in chunks[0].text
    assert "short a" not in chunks[1].text


def test_text_without_any_heading_gets_null_heading():
    chunks = chunk_markdown("Just a paragraph of evidence.\n")
    assert len(chunks) == 1
    assert chunks[0].heading is None


def test_ordinals_are_sequential_and_deterministic():
    doc = "## A\n\n" + _words(400) + "\n\n## B\n\n" + _words(400) + "\n"
    first = chunk_markdown(doc)
    second = chunk_markdown(doc)
    assert [c.ordinal for c in first] == list(range(len(first)))
    assert first == second  # frozen dataclass equality: text, heading, ordinal, tokens


def test_ordinals_are_stable_when_a_later_section_changes():
    head = "## Stable\n\n" + _words(300) + "\n\n"
    a = chunk_markdown(head + "## Tail\n\noriginal tail text.\n")
    b = chunk_markdown(head + "## Tail\n\ncompletely different tail text.\n")
    # the leading section keeps identical ordinals and text -> no re-embed downstream
    assert a[0] == b[0]
    assert a[-1].ordinal == b[-1].ordinal
    assert a[-1].text != b[-1].text


# --------------------------------------------------------------------------------------
# bullets
# --------------------------------------------------------------------------------------


def test_never_splits_a_bullet():
    # each bullet is ~120 tokens, so packing must break between bullets, never inside
    bullets = [f"- {_words(120, seed=f'skill{i}')}" for i in range(12)]
    doc = "## Experience\n\n" + "\n".join(bullets) + "\n"
    chunks = chunk_markdown(doc)

    assert len(chunks) > 1, "expected the section to split across several chunks"
    emitted = _bullet_lines(chunks)
    assert emitted == bullets
    for chunk in chunks:
        for line in chunk.text.splitlines():
            if BULLET_RE.match(line):
                assert line in bullets


def test_oversized_single_bullet_is_emitted_whole():
    huge = f"- {_words(MAX_TOKENS * 3)}"
    chunks = chunk_markdown(f"## Big\n\n{huge}\n")
    assert len(chunks) == 1
    assert chunks[0].text.endswith(huge)
    assert chunks[0].token_estimate > MAX_TOKENS  # deliberately over budget


def test_nested_bullets_stay_with_their_parent():
    doc = (
        "## Projects\n\n"
        "- Parent bullet about Redis\n"
        "  - nested detail one\n"
        "  - nested detail two\n"
        "- Second parent bullet\n"
    )
    chunks = chunk_markdown(doc)
    assert len(chunks) == 1
    text = chunks[0].text
    assert "- Parent bullet about Redis\n  - nested detail one" in text
    assert "- Second parent bullet" in text


def test_loose_list_with_blank_lines_keeps_all_bullets():
    doc = "## Loose\n\n- one\n\n- two\n\n- three\n"
    chunks = chunk_markdown(doc)
    assert _bullet_lines(chunks) == ["- one", "- two", "- three"]


def test_numbered_bullets_are_bullets_too():
    doc = "## Steps\n\n1. first step\n2. second step\n"
    chunks = chunk_markdown(doc)
    assert _bullet_lines(chunks) == ["1. first step", "2. second step"]


def test_bullet_continuation_line_is_not_orphaned():
    doc = "## X\n\n- a bullet whose sentence\n  wraps onto the next line\n- second\n"
    chunks = chunk_markdown(doc)
    assert "a bullet whose sentence\n  wraps onto the next line" in chunks[0].text


# --------------------------------------------------------------------------------------
# size targets
# --------------------------------------------------------------------------------------


def test_long_document_respects_the_token_window():
    sections = []
    for i in range(6):
        body = "\n".join(f"- {_words(60, seed=f's{i}b{j}')}" for j in range(10))
        sections.append(f"## Section {i}\n\n{body}\n")
    chunks = chunk_markdown("\n".join(sections))

    assert len(chunks) > 6
    for chunk in chunks:
        assert chunk.token_estimate <= MAX_TOKENS

    # every chunk except the trailing one of each section should be reasonably full
    last_of_section: dict[str | None, int] = {}
    for index, chunk in enumerate(chunks):
        last_of_section[chunk.heading] = index
    tail_indexes = set(last_of_section.values())
    for index, chunk in enumerate(chunks):
        if index not in tail_indexes:
            assert chunk.token_estimate >= MIN_TOKENS


def test_small_neighbouring_blocks_are_merged():
    doc = "## Notes\n\n" + "\n\n".join(_words(40, seed=f"p{i}") for i in range(8))
    chunks = chunk_markdown(doc)
    # 8 x ~40 tokens = ~320 tokens: one chunk, not eight
    assert len(chunks) == 1
    assert MIN_TOKENS <= chunks[0].token_estimate <= MAX_TOKENS


def test_long_single_paragraph_is_hard_split_on_sentences():
    sentence = "Redis streams gave the fetcher real delivery semantics. "
    paragraph = sentence * 120  # way over the max, no bullets, no headings
    chunks = chunk_markdown(f"## Story\n\n{paragraph}\n")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_estimate <= MAX_TOKENS + estimate_tokens("## Story\n\n")
    assert all(c.heading == "Story" for c in chunks)
    # no sentence was cut in half
    rejoined = " ".join(c.text.replace("## Story\n\n", "") for c in chunks)
    assert rejoined.count("Redis streams gave") == 120


def test_single_unbroken_token_stream_still_terminates():
    blob = "x" * (MAX_TOKENS * CHARS_PER_TOKEN * 3)
    chunks = chunk_markdown(f"## Blob\n\n{blob}\n")
    assert len(chunks) >= 1
    assert "".join(c.text.replace("## Blob\n\n", "") for c in chunks) == blob


# --------------------------------------------------------------------------------------
# code fences
# --------------------------------------------------------------------------------------


def test_code_fence_is_atomic_and_never_reflowed():
    code = "```python\ndef f():\n    return 1\n```"
    chunks = chunk_markdown(f"## Snippet\n\nBefore.\n\n{code}\n\nAfter.\n")
    assert len(chunks) == 1
    assert code in chunks[0].text


def test_hash_inside_a_code_fence_is_not_a_heading():
    doc = "## Real\n\n```sh\n# not a heading\necho hi\n```\n"
    chunks = chunk_markdown(doc)
    assert [c.heading for c in chunks] == ["Real"]


def test_bullet_marker_inside_a_code_fence_is_not_a_bullet():
    doc = "## Real\n\n```yaml\n- item: one\n- item: two\n```\n"
    chunks = chunk_markdown(doc)
    assert len(chunks) == 1
    assert "```yaml\n- item: one\n- item: two\n```" in chunks[0].text


# --------------------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------------------


def test_crlf_is_normalised():
    unix = chunk_markdown("## A\r\n\r\n- one\r\n- two\r\n")
    assert unix == chunk_markdown("## A\n\n- one\n- two\n")


def test_token_estimate_matches_the_stored_text():
    chunks = chunk_markdown("## A\n\n" + _words(300))
    for chunk in chunks:
        assert chunk.token_estimate == estimate_tokens(chunk.text)


def test_min_greater_than_max_is_rejected():
    with pytest.raises(ValueError):
        chunk_markdown("# x\n\nbody", min_tokens=10, max_tokens=5)

"""Markdown chunker.

Pure function, no DB and no network, because chunking is the part of the evidence
pipeline that decides retrieval quality and therefore the part worth testing hard.

Strategy, in priority order:

1. **Headings are hard boundaries.** A chunk never spans a markdown heading. That is
   what makes the `heading` we carry onto each chunk actually true, and headings are
   the strongest semantic signal a hand-written evidence note has.
2. **Within a section, pack atomic units greedily** up to `MAX_TOKENS`. The atomic
   units are: one bullet (including its nested sub-bullets and continuation lines),
   one paragraph, one fenced code block.
3. **A bullet is never split.** Resume bullets are the highest-value evidence in the
   corpus and half a bullet is worse than useless: it reads as a different claim. An
   oversized single bullet is emitted whole, over the max, on purpose.
4. **Merge undersized neighbours** inside a section so greedy packing does not leave a
   40-token orphan behind a 480-token chunk.
5. **Hard-split only paragraphs** that individually exceed the max, on sentence
   boundaries first and whitespace as a last resort.

We deliberately do *not* merge across a section boundary, even when both sides are
small. Merging there would force a chunk to carry a heading that only describes part
of its text, and a wrong heading is worse than an undersized chunk.

Ordinals are assigned sequentially per document and are a pure function of the
document text, so re-ingesting an unedited file maps chunk-to-chunk onto the existing
rows through `uq_chunk_source_ordinal` and nothing is re-embedded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# ~4 characters per token is the usual English rule of thumb for BPE tokenizers. We
# only need it to be monotonic and cheap: it sizes chunks, it is not billed against.
CHARS_PER_TOKEN = 4

MIN_TOKENS = 200
MAX_TOKENS = 500

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+\S")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    """Estimate token count at ~4 characters per token.

    Explicit and deliberately crude: swapping in a real tokenizer later means changing
    exactly this function, and every size decision in the chunker flows through it.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True)
class Chunk:
    """One unit of evidence, ready to embed."""

    ordinal: int
    heading: str | None
    text: str
    token_estimate: int


@dataclass(frozen=True)
class _Unit:
    """An atomic piece of a section. Never split, except oversized paragraphs."""

    kind: str  # "bullet" | "paragraph" | "code"
    text: str


@dataclass(frozen=True)
class _Section:
    heading_line: str | None  # raw, e.g. "## Redis pipeline"
    title: str | None  # e.g. "Redis pipeline"
    units: tuple[_Unit, ...]


# --------------------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------------------


def chunk_markdown(
    text: str,
    *,
    min_tokens: int = MIN_TOKENS,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    """Split a markdown document into embeddable chunks.

    Deterministic: the same input always yields the same chunks with the same ordinals.
    """
    if min_tokens > max_tokens:
        raise ValueError("min_tokens must be <= max_tokens")

    chunks: list[Chunk] = []
    ordinal = 0
    for section in _parse_sections(text):
        units = _expand_oversized(section.units, max_tokens)
        for group in _merge_small(_pack(units, max_tokens), min_tokens, max_tokens):
            body = _join(group)
            full = f"{section.heading_line}\n\n{body}" if section.heading_line else body
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    heading=section.title,
                    text=full,
                    token_estimate=estimate_tokens(full),
                )
            )
            ordinal += 1
    return chunks


# --------------------------------------------------------------------------------------
# parsing: document -> sections -> atomic units
# --------------------------------------------------------------------------------------


def _parse_sections(text: str) -> list[_Section]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[_Section] = []
    heading_line: str | None = None
    title: str | None = None
    units: list[_Unit] = []

    def flush() -> None:
        if units:
            sections.append(_Section(heading_line, title, tuple(units)))
        units.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if _FENCE_RE.match(line):
            block, i = _take_fence(lines, i)
            if block.strip():
                units.append(_Unit("code", block))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            heading_line = line.strip()
            title = heading.group(2).strip() or None
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        if _BULLET_RE.match(line):
            bullets, i = _take_bullet_group(lines, i)
            units.extend(_Unit("bullet", b) for b in bullets)
            continue

        para, i = _take_paragraph(lines, i)
        if para.strip():
            units.append(_Unit("paragraph", para))

    flush()
    return sections


def _take_fence(lines: list[str], i: int) -> tuple[str, int]:
    opener = _FENCE_RE.match(lines[i])
    assert opener is not None
    marker = opener.group(1)[0] * 3
    out = [lines[i]]
    i += 1
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip().startswith(marker):
            i += 1
            break
        i += 1
    return "\n".join(out), i


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _take_bullet_group(lines: list[str], i: int) -> tuple[list[str], int]:
    """Collect a run of bullets. Each returned string is ONE atomic bullet.

    A bullet owns everything under it that is more indented than its own marker:
    nested sub-bullets and lazy continuation lines. That is what "never split a bullet"
    has to mean in practice, otherwise a nested list gets orphaned from its parent.
    """
    base = _indent(lines[i])
    bullets: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            bullets.append("\n".join(current).rstrip())
        current.clear()

    n = len(lines)
    while i < n:
        line = lines[i]

        if not line.strip():
            # A blank line continues a loose list only if a sibling bullet follows.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _BULLET_RE.match(lines[j]) and _indent(lines[j]) <= base:
                i = j
                continue
            break

        if _FENCE_RE.match(line) and _indent(line) <= base:
            break
        if _HEADING_RE.match(line):
            break

        bullet = _BULLET_RE.match(line)
        if bullet and _indent(line) <= base:
            flush()
            base = min(base, _indent(line))
            current.append(line.rstrip())
        elif current:
            # nested bullet or continuation line: belongs to the bullet above it
            current.append(line.rstrip())
        else:  # pragma: no cover - unreachable, we only enter on a bullet line
            break
        i += 1

    flush()
    return bullets, i


def _take_paragraph(lines: list[str], i: int) -> tuple[str, int]:
    out: list[str] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or _HEADING_RE.match(line) or _BULLET_RE.match(line):
            break
        if _FENCE_RE.match(line):
            break
        out.append(line.rstrip())
        i += 1
    return "\n".join(out), i


# --------------------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------------------


def _expand_oversized(units: tuple[_Unit, ...], max_tokens: int) -> list[_Unit]:
    """Hard-split paragraphs that alone blow the budget. Bullets and code stay whole."""
    out: list[_Unit] = []
    for unit in units:
        if unit.kind == "paragraph" and estimate_tokens(unit.text) > max_tokens:
            out.extend(_Unit("paragraph", piece) for piece in _hard_split(unit.text, max_tokens))
        else:
            out.append(unit)
    return out


def _hard_split(text: str, max_tokens: int) -> list[str]:
    limit = max_tokens * CHARS_PER_TOKEN
    pieces: list[str] = []
    buf = ""
    for sentence in _split_atoms(text, limit):
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if buf and len(candidate) > limit:
            pieces.append(buf)
            buf = sentence
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces or [text]


def _split_atoms(text: str, limit: int) -> list[str]:
    """Sentences, falling back to whitespace runs for a sentence that is itself huge."""
    atoms: list[str] = []
    for sentence in _SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            atoms.append(sentence)
            continue
        words = sentence.split()
        buf = ""
        for word in words:
            candidate = f"{buf} {word}".strip() if buf else word
            if buf and len(candidate) > limit:
                atoms.append(buf)
                buf = word
            else:
                buf = candidate
        if buf:
            atoms.append(buf)
    return atoms


def _pack(units: list[_Unit], max_tokens: int) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_tokens = 0
    for unit in units:
        tokens = estimate_tokens(unit.text)
        if current and current_tokens + tokens > max_tokens:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups


def _merge_small(
    groups: list[list[_Unit]], min_tokens: int, max_tokens: int
) -> list[list[_Unit]]:
    merged: list[list[_Unit]] = []
    for group in groups:
        if merged:
            prev_tokens = _group_tokens(merged[-1])
            tokens = _group_tokens(group)
            undersized = prev_tokens < min_tokens or tokens < min_tokens
            if undersized and prev_tokens + tokens <= max_tokens:
                merged[-1] = merged[-1] + group
                continue
        merged.append(group)
    return merged


def _group_tokens(group: list[_Unit]) -> int:
    return estimate_tokens(_join(group))


def _join(units: list[_Unit]) -> str:
    parts: list[str] = []
    previous: _Unit | None = None
    for unit in units:
        if previous is None:
            parts.append(unit.text)
        elif previous.kind == "bullet" and unit.kind == "bullet":
            parts.append("\n" + unit.text)
        else:
            parts.append("\n\n" + unit.text)
        previous = unit
    return "".join(parts)

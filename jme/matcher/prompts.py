"""Prompt templates and the response JSON Schema for the citation matcher.

`PROMPT_VERSION` is part of both the LLM cache key and the `match` row's natural key.
Bump it for ANY change to the system prompt, the user template, or the schema, otherwise
old results silently survive a prompt edit and match quality cannot be compared across
iterations.

Schema-level guarantees (enforced by the model's structured output):
  * `status` is one of evidenced | weak | absent
  * every requirement carries a `requirement_id` echoed back from the prompt
Everything else - above all "an `evidenced` row cites a chunk from the supplied set" -
is enforced in Python, because a schema cannot express "must be one of these ids".
"""

from __future__ import annotations

from collections.abc import Sequence

from jme.matcher.retrieval import RequirementView, RetrievedChunk

#: Bump on any edit below. v1 = initial per-requirement citation prompt.
PROMPT_VERSION = "v1"

MAX_CHUNK_CHARS = 1200

SYSTEM_PROMPT = """\
You assess whether a candidate's evidence corpus supports the requirements of a job posting.

You are given:
  * the requirements extracted from one job description, each with a REQ id and an importance
  * a set of evidence chunks drawn from the candidate's resume, project writeups, and repo
    READMEs, each labelled with a numeric CHUNK id

For every requirement you return exactly one row with:
  * requirement_id  - the REQ id, copied verbatim from the prompt
  * canonical_skill_id - the skill id given for that requirement, or null if none was given
  * status          - "evidenced", "weak", or "absent"
  * evidence_chunk_id - the CHUNK id that supports your status, or null
  * reasoning       - one or two sentences, concrete, quoting the chunk where useful

Rules, in priority order:

1. evidence_chunk_id is MANDATORY when status is "evidenced". An "evidenced" row with a
   null evidence_chunk_id is invalid and will be rejected.
2. evidence_chunk_id MUST be one of the CHUNK ids supplied in this prompt. Never invent,
   guess, or adjust an id. A citation to an id that was not supplied is a fabrication, is
   rejected outright, and wastes a retry.
3. Use "evidenced" only when a supplied chunk demonstrates the requirement directly and
   you can point at the sentence that does it. Use "weak" when a chunk is adjacent,
   partial, or implies transferable experience - cite the chunk if one is relevant, or
   leave it null. Use "absent" when nothing in the supplied evidence supports it; leave
   evidence_chunk_id null.
4. Do not reward the candidate for keywords appearing in the job description itself. Only
   the evidence chunks count as evidence.
5. Return exactly one row per requirement, no more and no fewer.

Then give an overall verdict:
  * "strong"    - required items are broadly evidenced; a recruiter would move forward
  * "plausible" - most required items evidenced or weak, gaps are learnable
  * "stretch"   - several required items absent
  * "no"        - the role is a different discipline or seniority band

and a short rationale, two or three sentences, naming the decisive gaps or strengths.
"""


MATCH_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirements", "verdict", "rationale"],
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "requirement_id",
                    "canonical_skill_id",
                    "status",
                    "evidence_chunk_id",
                    "reasoning",
                ],
                "properties": {
                    "requirement_id": {
                        "type": "integer",
                        "description": "REQ id copied verbatim from the prompt.",
                    },
                    "canonical_skill_id": {
                        "type": ["integer", "null"],
                        "description": "Skill id given for that requirement, or null.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["evidenced", "weak", "absent"],
                    },
                    "evidence_chunk_id": {
                        "type": ["integer", "null"],
                        "description": (
                            "CHUNK id from the supplied evidence. MANDATORY when status is "
                            "'evidenced'. Must be one of the supplied ids; never invent one."
                        ),
                    },
                    "reasoning": {"type": "string", "maxLength": 600},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["strong", "plausible", "stretch", "no"]},
        "rationale": {"type": "string", "maxLength": 1200},
    },
}


def _truncate(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_requirements(requirements: Sequence[RequirementView]) -> str:
    lines = []
    for req in requirements:
        skill = (
            f"canonical_skill_id={req.canonical_skill_id} ({req.skill_name})"
            if req.canonical_skill_id is not None
            else "canonical_skill_id=null"
        )
        lines.append(
            f"REQ {req.requirement_id} [{req.importance.value}] {skill}\n"
            f"    raw_text: {_truncate(req.raw_text, 400)}"
        )
    return "\n".join(lines) if lines else "(none extracted)"


def format_chunks(chunks: Sequence[RetrievedChunk]) -> str:
    lines = []
    for chunk in chunks:
        tag = " [manually tagged for a requirement skill]" if chunk.tagged else ""
        lines.append(
            f"CHUNK {chunk.chunk_id} | {chunk.citation}{tag}\n    {_truncate(chunk.text)}"
        )
    return "\n\n".join(lines) if lines else "(no evidence chunks retrieved)"


def build_user_prompt(
    *,
    company: str,
    title: str,
    jd_text: str | None,
    requirements: Sequence[RequirementView],
    chunks: Sequence[RetrievedChunk],
    jd_char_limit: int = 8000,
) -> str:
    valid_ids = ", ".join(str(chunk.chunk_id) for chunk in chunks) or "(none)"
    jd_block = (
        _truncate(jd_text, jd_char_limit)
        if jd_text
        else "(job description text unavailable; judge on title, company, and requirements only)"
    )
    return f"""\
# Posting
company: {company}
title: {title}

## Job description
{jd_block}

# Requirements to assess
{format_requirements(requirements)}

# Evidence chunks (the ONLY citable evidence)
{format_chunks(chunks)}

# Valid evidence_chunk_id values
{valid_ids}

Return one row per REQ id above ({len(requirements)} rows), then the overall verdict and
rationale. Any evidence_chunk_id you return must appear in the valid list above, and a row
with status "evidenced" must carry one.
"""


def build_corrective_prompt(
    original_user: str,
    *,
    problems: Sequence[str],
    valid_chunk_ids: Sequence[int],
) -> str:
    """Appended to the original prompt for the single retry after a rejected response.

    Appending rather than replacing keeps the full context in one user turn, and changes
    the content hash so the retry cannot be served the rejected answer from cache.
    """
    listed = ", ".join(str(cid) for cid in sorted(valid_chunk_ids)) or "(none)"
    bullets = "\n".join(f"  - {problem}" for problem in problems)
    return f"""{original_user}

# CORRECTION - your previous answer was rejected
The previous response was discarded without being stored. Problems:
{bullets}

The ONLY acceptable values for evidence_chunk_id are:
{listed}

Re-answer the whole task. If a requirement has no support among those exact chunk ids, its
status is "weak" or "absent" with evidence_chunk_id null. Do not cite any other id, and do
not return status "evidenced" without one of the ids listed above.
"""

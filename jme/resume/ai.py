"""Evidence-grounded resume rewriting through OpenAI or Anthropic.

The browser never calls either provider. It sends one request to the localhost API,
which owns the secret, asks for schema-constrained rewrites, validates every proposed
bullet, and falls back to deterministic tailoring on any failure.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from typing import Any, Literal

import httpx

from jme.config import Settings, get_settings
from jme.resume.latex import render_jake_latex

Provider = Literal["openai", "anthropic"]
ProviderCall = Callable[[Provider, str, str, dict[str, Any], Settings], tuple[dict[str, Any], str]]

SECTIONS = ("education", "experience", "projects", "leadership")
REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "text": {"type": "string"},
                    "keywords_used": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["source_id", "text", "keywords_used"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rewrites"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You tailor one-page software, cloud, and APM resumes.
Return only schema-conforming JSON. Each rewrite must remain fully supported by its one
source bullet. Preserve every employer, project, date, technology relationship, scope,
and metric. Never add a skill, responsibility, leadership claim, outcome, or number.
Use only keywords listed in that source bullet's verified_tags. Omit bullets that do not
benefit from rewriting. Keep each accepted bullet concise, specific, and ATS-readable.
Do not mention the target company, target title, application, job, or tailoring process.
"""


class AIRewriteError(RuntimeError):
    """A provider could not produce a usable structured response."""


def provider_catalog(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Public capability metadata; never returns either secret."""
    cfg = settings or get_settings()
    return [
        {"id": "verified", "label": "Verified only", "available": True, "model": None},
        {
            "id": "openai",
            "label": "AI rewrite · OpenAI",
            "available": bool(cfg.openai_api_key),
            "model": cfg.resume_openai_model,
        },
        {
            "id": "anthropic",
            "label": "AI rewrite · Claude",
            "available": bool(cfg.anthropic_api_key),
            "model": cfg.resume_anthropic_model,
        },
    ]


def _bullet_records(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for section in SECTIONS:
        for entry_index, entry in enumerate(data.get(section, [])):
            for bullet_index, bullet in enumerate(entry.get("bullets", [])):
                yield f"{section}:{entry_index}:{bullet_index}", bullet


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9+#./-]+", " ", value.lower()).strip()


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<![a-z])\d+(?:[.,]\d+)?", value.lower()))


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def _validate_candidate(
    candidate: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    *,
    target: dict[str, str],
) -> tuple[str, str] | None:
    source_id = candidate.get("source_id")
    text = str(candidate.get("text", "")).strip()
    keywords = candidate.get("keywords_used")
    source = sources.get(source_id) if isinstance(source_id, str) else None
    if source is None or not isinstance(keywords, list):
        return None
    original = str(source["text"]).strip()
    if not 40 <= len(text) <= min(420, max(100, int(len(original) * 1.45))):
        return None
    if "\n" in text or text == original or text.lower().startswith(("i ", "my ")):
        return None
    if not text.endswith((".", ";")):
        return None
    if not _numbers(text) <= _numbers(original):
        return None

    normalized_text = _normalized(text)
    normalized_original = _normalized(original)
    source_tags = {_normalized(tag) for tag in source.get("tags", []) if _normalized(tag)}
    for keyword in keywords:
        normalized_keyword = _normalized(str(keyword))
        if normalized_keyword not in source_tags or not _contains_phrase(
            normalized_text, normalized_keyword
        ):
            return None

    for value in (target.get("company", ""), target.get("title", "")):
        normalized_target = _normalized(value)
        if (
            len(normalized_target) >= 3
            and _contains_phrase(normalized_text, normalized_target)
            and not _contains_phrase(normalized_original, normalized_target)
        ):
            return None
    return source_id, text


def _openai_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    if not settings.openai_api_key:
        raise AIRewriteError("OPENAI_API_KEY is not configured")
    body = {
        "model": settings.resume_openai_model,
        "instructions": system,
        "input": user,
        "store": False,
        "max_output_tokens": 6000,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "resume_rewrites",
                "description": "Evidence-linked resume bullet rewrites",
                "schema": schema,
                "strict": True,
            }
        },
    }
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json=body,
            timeout=settings.resume_ai_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AIRewriteError(f"OpenAI request failed: {exc}") from exc

    if payload.get("status") not in (None, "completed"):
        raise AIRewriteError(f"OpenAI response status was {payload.get('status')}")
    text = next(
        (
            block.get("text")
            for item in payload.get("output", [])
            for block in item.get("content", [])
            if block.get("type") == "output_text"
        ),
        None,
    )
    if not isinstance(text, str):
        raise AIRewriteError("OpenAI returned no structured text output")
    try:
        return json.loads(text), settings.resume_openai_model
    except json.JSONDecodeError as exc:
        raise AIRewriteError("OpenAI returned invalid structured JSON") from exc


def _anthropic_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    if not settings.anthropic_api_key:
        raise AIRewriteError("ANTHROPIC_API_KEY is not configured")
    try:
        import anthropic

        response = anthropic.Anthropic(api_key=settings.anthropic_api_key).messages.create(
            model=settings.resume_anthropic_model,
            max_tokens=6000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            timeout=settings.resume_ai_timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001 - SDK exceptions change across versions
        raise AIRewriteError(f"Claude request failed: {exc}") from exc
    if response.stop_reason in {"refusal", "max_tokens"}:
        raise AIRewriteError(f"Claude stopped with {response.stop_reason}")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if not isinstance(text, str):
        raise AIRewriteError("Claude returned no structured text output")
    try:
        return json.loads(text), settings.resume_anthropic_model
    except json.JSONDecodeError as exc:
        raise AIRewriteError("Claude returned invalid structured JSON") from exc


def _call_provider(
    provider: Provider,
    system: str,
    user: str,
    schema: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    if provider == "openai":
        return _openai_call(system, user, schema, settings)
    return _anthropic_call(system, user, schema, settings)


def customize_with_ai(
    tailored: dict[str, Any],
    *,
    job_description: str,
    provider: Provider,
    settings: Settings | None = None,
    call_provider: ProviderCall | None = None,
) -> dict[str, Any]:
    """Apply only validated provider rewrites and regenerate the Jake LaTeX output."""
    cfg = settings or get_settings()
    result = copy.deepcopy(tailored)
    sources = dict(_bullet_records(result))
    evidence = [
        {"source_id": source_id, "text": bullet["text"], "verified_tags": bullet["tags"]}
        for source_id, bullet in sources.items()
    ]
    user = json.dumps(
        {
            "target_title": result["target"].get("title", ""),
            "job_description": job_description,
            "source_bullets": evidence,
        },
        ensure_ascii=False,
    )
    caller = call_provider or _call_provider
    payload, model = caller(provider, SYSTEM_PROMPT, user, REWRITE_SCHEMA, cfg)
    candidates = payload.get("rewrites")
    if not isinstance(candidates, list):
        raise AIRewriteError("provider response omitted the rewrites array")

    accepted: dict[str, str] = {}
    rejected = 0
    for candidate in candidates:
        validated = (
            _validate_candidate(candidate, sources, target=result["target"])
            if isinstance(candidate, dict)
            else None
        )
        if validated is None or validated[0] in accepted:
            rejected += 1
            continue
        accepted[validated[0]] = validated[1]

    for source_id, bullet in _bullet_records(result):
        rewritten = accepted.get(source_id)
        if rewritten is not None:
            bullet["source_text"] = bullet["text"]
            bullet["text"] = rewritten
            bullet["ai_rewritten"] = True

    result["customization"] = {
        "requested_mode": provider,
        "applied_mode": "ai" if accepted else "verified",
        "provider": provider,
        "model": model,
        "rewritten_bullets": len(accepted),
        "rejected_rewrites": rejected,
        "warning": None if accepted else "The model returned no rewrite that passed validation.",
    }
    if accepted:
        result["source_rule"] = (
            "AI-assisted wording tied to verified source bullets; review highlighted rewrites."
        )
    result["latex"] = render_jake_latex(result)
    return result

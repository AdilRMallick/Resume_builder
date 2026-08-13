"""Evidence-grounded resume rewriting through configured AI providers.

The browser never calls a provider. It sends one request to the localhost API, which
owns every secret, asks for schema-constrained rewrites, validates every proposed
bullet, and falls back to deterministic tailoring on any failure.

Providers split into two kinds. The HTTP ones (OpenAI, Anthropic, Gemini, Kimi) bill an
API key held in .env. The `claude_code` one shells out to the locally installed Claude
Code CLI, so it needs no key at all and draws on that CLI's Claude subscription
allowance instead; see jme.resume.claude_code.
"""

from __future__ import annotations

import copy
import json
import re
import ssl
from collections.abc import Callable, Iterator
from typing import Any, Literal
from urllib.parse import quote

import httpx
import truststore

from jme.config import Settings, get_settings
from jme.resume.claude_code import ClaudeCodeError, claude_code_call
from jme.resume.claude_code import is_available as claude_code_available
from jme.resume.latex import render_jake_latex

Provider = Literal["openai", "anthropic", "gemini", "kimi", "claude_code"]
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

CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assistant_message": {"type": "string"},
        "rewrites": REWRITE_SCHEMA["properties"]["rewrites"],
        "remove_source_ids": {"type": "array", "items": {"type": "string"}},
        "prioritize_source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "assistant_message",
        "rewrites",
        "remove_source_ids",
        "prioritize_source_ids",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You tailor one-page software, cloud, and APM resumes.
Return only schema-conforming JSON. Each rewrite must remain fully supported by its one
source bullet. Preserve every employer, project, date, technology relationship, scope,
and metric. Never add a skill, responsibility, leadership claim, outcome, or number.
Use only keywords listed in that source bullet's verified_tags. In keywords_used, list
only the verified_tags you wrote into that rewrite word for word, spelled exactly as the
tag is spelled. Drop any tag you paraphrased, pluralized, inflected, or left out; an
empty keywords_used is better than one unmatched entry. Never pad a sentence with tags
to make them countable. Omit bullets that do not benefit from rewriting. Keep each accepted bullet concise, specific, and ATS-readable.
Follow standing_instructions when they are compatible with this evidence policy.
Do not mention the target company, target title, application, job, or tailoring process.
"""

CHAT_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + """
You are revising an existing resume through a short chat. Follow the user's latest
instruction when it is supported by the supplied evidence. You may rewrite an existing
bullet, remove an existing bullet, or prioritize existing bullets within their current
entry. Never create a new bullet or change identity, contact information, organizations,
titles, dates, education, project names, URLs, or skills. A removal must use a source_id.
A priority list must contain source_ids in the requested order. Explain what you changed
in assistant_message. If the request would require unsupported information, make no edit
and explain what verified evidence is missing. Never claim an edit was made unless it is
present in the structured actions.
"""
)


class AIRewriteError(RuntimeError):
    """A provider could not produce a usable structured response."""


def _provider_ssl_context() -> ssl.SSLContext:
    """Use the native OS trust store while keeping certificate checks enabled."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def provider_catalog(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Public capability metadata; never returns either secret."""
    cfg = settings or get_settings()
    return [
        {"id": "verified", "label": "Verified only", "available": True, "model": None},
        {
            "id": "claude_code",
            "label": "AI rewrite · Claude Code (local, no API key)",
            "available": claude_code_available(cfg),
            "model": cfg.resume_claude_code_model or None,
        },
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
        {
            "id": "gemini",
            "label": "AI rewrite · Gemini (free tier available)",
            "available": bool(cfg.gemini_api_key),
            "model": cfg.resume_gemini_model,
        },
        {
            "id": "kimi",
            "label": "AI rewrite · Kimi",
            "available": bool(cfg.kimi_api_key),
            "model": cfg.resume_kimi_model,
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
    original = str(source.get("source_text") or source["text"]).strip()
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


def _evidence_payload(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source_id,
            "text": bullet["text"],
            "verified_source": bullet.get("source_text") or bullet["text"],
            "verified_tags": bullet["tags"],
        }
        for source_id, bullet in sources.items()
    ]


def _apply_rewrites(
    result: dict[str, Any],
    candidates: Any,
    sources: dict[str, dict[str, Any]],
) -> tuple[int, int]:
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
            bullet["source_text"] = bullet.get("source_text") or bullet["text"]
            bullet["text"] = rewritten
            bullet["ai_rewritten"] = True
    return len(accepted), rejected


def _apply_chat_structure_edits(
    result: dict[str, Any],
    *,
    remove_source_ids: Any,
    prioritize_source_ids: Any,
    valid_source_ids: set[str],
) -> tuple[int, int, int]:
    rejected = 0
    requested_removals: set[str] = set()
    if isinstance(remove_source_ids, list):
        for value in remove_source_ids:
            if isinstance(value, str) and value in valid_source_ids:
                requested_removals.add(value)
            else:
                rejected += 1
    else:
        rejected += 1

    priorities: list[str] = []
    if isinstance(prioritize_source_ids, list):
        for value in prioritize_source_ids:
            if isinstance(value, str) and value in valid_source_ids and value not in priorities:
                priorities.append(value)
            else:
                rejected += 1
    else:
        rejected += 1
    priority = {source_id: index for index, source_id in enumerate(priorities)}

    removed = 0
    prioritized = 0
    for section in SECTIONS:
        for entry_index, entry in enumerate(result.get(section, [])):
            bullets = entry.get("bullets", [])
            records = [
                (f"{section}:{entry_index}:{bullet_index}", bullet)
                for bullet_index, bullet in enumerate(bullets)
            ]
            removable = [source_id for source_id, _ in records if source_id in requested_removals]
            allowed_removals = set(removable[: max(0, len(records) - 1)])
            rejected += len(removable) - len(allowed_removals)
            records = [record for record in records if record[0] not in allowed_removals]
            removed += len(allowed_removals)
            original_order = [source_id for source_id, _ in records]
            records.sort(
                key=lambda record: (
                    record[0] not in priority,
                    priority.get(record[0], len(priority)),
                    original_order.index(record[0]),
                )
            )
            new_order = [source_id for source_id, _ in records]
            if new_order != original_order:
                prioritized += sum(
                    source_id in priority
                    for source_id, old_source_id in zip(new_order, original_order, strict=True)
                    if source_id != old_source_id
                )
            entry["bullets"] = [bullet for _, bullet in records]
    return removed, prioritized, rejected


def _chat_action_summary(
    *, rewritten: int, removed: int, prioritized: int, rejected: int
) -> str:
    actions = []
    if rewritten:
        actions.append(f"rewrote {rewritten} verified bullet{'s' if rewritten != 1 else ''}")
    if removed:
        actions.append(f"removed {removed} bullet{'s' if removed != 1 else ''}")
    if prioritized:
        actions.append(
            f"reprioritized {prioritized} bullet{'s' if prioritized != 1 else ''}"
        )
    if not actions:
        return (
            "I couldn't make that change without adding unsupported information. "
            f"{rejected} proposed action{'s were' if rejected != 1 else ' was'} rejected."
        )
    summary = f"I updated your verified resume: {', '.join(actions)}."
    if rejected:
        summary += (
            f" I also rejected {rejected} unsupported proposed "
            f"action{'s' if rejected != 1 else ''}."
        )
    return summary


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
            verify=_provider_ssl_context(),
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

        with httpx.Client(verify=_provider_ssl_context()) as http_client:
            response = anthropic.Anthropic(
                api_key=settings.anthropic_api_key, http_client=http_client
            ).messages.create(
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


def _gemini_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    if not settings.gemini_api_key:
        raise AIRewriteError("GEMINI_API_KEY is not configured")
    model = quote(settings.resume_gemini_model, safe="")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": 6000,
        },
    }
    try:
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "content-type": "application/json",
            },
            json=body,
            timeout=settings.resume_ai_timeout_sec,
            verify=_provider_ssl_context(),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AIRewriteError(f"Gemini request failed: {exc}") from exc

    candidate = next(iter(payload.get("candidates", [])), None)
    if not isinstance(candidate, dict):
        raise AIRewriteError("Gemini returned no candidate")
    finish_reason = candidate.get("finishReason")
    if finish_reason not in (None, "STOP"):
        raise AIRewriteError(f"Gemini stopped with {finish_reason}")
    text = next(
        (
            part.get("text")
            for part in candidate.get("content", {}).get("parts", [])
            if isinstance(part.get("text"), str)
        ),
        None,
    )
    if not isinstance(text, str):
        raise AIRewriteError("Gemini returned no structured text output")
    try:
        return json.loads(text), settings.resume_gemini_model
    except json.JSONDecodeError as exc:
        raise AIRewriteError("Gemini returned invalid structured JSON") from exc


def _kimi_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    if not settings.kimi_api_key:
        raise AIRewriteError("MOONSHOT_API_KEY is not configured")
    body = {
        "model": settings.resume_kimi_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "resume_rewrites",
                "schema": schema,
                "strict": True,
            },
        },
        "thinking": {"type": "disabled"},
        "max_completion_tokens": 6000,
    }
    try:
        response = httpx.post(
            "https://api.moonshot.ai/v1/chat/completions",
            headers={
                "authorization": f"Bearer {settings.kimi_api_key}",
                "content-type": "application/json",
            },
            json=body,
            timeout=settings.resume_ai_timeout_sec,
            verify=_provider_ssl_context(),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AIRewriteError(f"Kimi request failed: {exc}") from exc

    choice = next(iter(payload.get("choices", [])), None)
    if not isinstance(choice, dict):
        raise AIRewriteError("Kimi returned no completion choice")
    if choice.get("finish_reason") == "length":
        raise AIRewriteError("Kimi response was truncated")
    text = choice.get("message", {}).get("content")
    if not isinstance(text, str):
        raise AIRewriteError("Kimi returned no structured text output")
    try:
        return json.loads(text), settings.resume_kimi_model
    except json.JSONDecodeError as exc:
        raise AIRewriteError("Kimi returned invalid structured JSON") from exc


def _claude_code_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    """Adapt the local CLI's failures onto the same contract the HTTP providers use."""
    try:
        return claude_code_call(system, user, schema, settings)
    except ClaudeCodeError as exc:
        raise AIRewriteError(str(exc)) from exc


def _call_provider(
    provider: Provider,
    system: str,
    user: str,
    schema: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    if provider == "openai":
        return _openai_call(system, user, schema, settings)
    if provider == "anthropic":
        return _anthropic_call(system, user, schema, settings)
    if provider == "gemini":
        return _gemini_call(system, user, schema, settings)
    if provider == "claude_code":
        return _claude_code_call(system, user, schema, settings)
    return _kimi_call(system, user, schema, settings)


def customize_with_ai(
    tailored: dict[str, Any],
    *,
    job_description: str,
    provider: Provider,
    steering_prompt: str = "",
    settings: Settings | None = None,
    call_provider: ProviderCall | None = None,
) -> dict[str, Any]:
    """Apply only validated provider rewrites and regenerate the Jake LaTeX output."""
    cfg = settings or get_settings()
    result = copy.deepcopy(tailored)
    sources = dict(_bullet_records(result))
    user = json.dumps(
        {
            "target_title": result["target"].get("title", ""),
            "job_description": job_description,
            "standing_instructions": steering_prompt,
            "source_bullets": _evidence_payload(sources),
        },
        ensure_ascii=False,
    )
    caller = call_provider or _call_provider
    payload, model = caller(provider, SYSTEM_PROMPT, user, REWRITE_SCHEMA, cfg)
    accepted, rejected = _apply_rewrites(result, payload.get("rewrites"), sources)

    result["customization"] = {
        "requested_mode": provider,
        "applied_mode": "ai" if accepted else "verified",
        "provider": provider,
        "model": model,
        "rewritten_bullets": accepted,
        "rejected_rewrites": rejected,
        "warning": None if accepted else "The model returned no rewrite that passed validation.",
    }
    if accepted:
        result["source_rule"] = (
            "AI-assisted wording tied to verified source bullets; review highlighted rewrites."
        )
    result["latex"] = render_jake_latex(result)
    return result


def revise_with_ai(
    tailored: dict[str, Any],
    *,
    job_description: str,
    provider: Provider,
    messages: list[dict[str, str]],
    steering_prompt: str = "",
    settings: Settings | None = None,
    call_provider: ProviderCall | None = None,
) -> dict[str, Any]:
    """Apply a conversational revision while keeping every resume claim grounded."""
    cfg = settings or get_settings()
    result = copy.deepcopy(tailored)
    sources = dict(_bullet_records(result))
    user = json.dumps(
        {
            "target_title": result["target"].get("title", ""),
            "job_description": job_description,
            "standing_instructions": steering_prompt,
            "conversation": messages,
            "source_bullets": _evidence_payload(sources),
        },
        ensure_ascii=False,
    )
    caller = call_provider or _call_provider
    payload, model = caller(provider, CHAT_SYSTEM_PROMPT, user, CHAT_SCHEMA, cfg)
    rewritten, rejected = _apply_rewrites(result, payload.get("rewrites"), sources)
    removed, prioritized, structure_rejected = _apply_chat_structure_edits(
        result,
        remove_source_ids=payload.get("remove_source_ids"),
        prioritize_source_ids=payload.get("prioritize_source_ids"),
        valid_source_ids=set(sources),
    )
    rejected += structure_rejected
    applied = bool(rewritten or removed or prioritized)
    result["customization"] = {
        "requested_mode": provider,
        "applied_mode": "ai" if applied else "verified",
        "provider": provider,
        "model": model,
        "rewritten_bullets": rewritten,
        "rejected_rewrites": rejected,
        "warning": None if applied else "No requested edit passed evidence validation.",
    }
    result["chat_reply"] = _chat_action_summary(
        rewritten=rewritten,
        removed=removed,
        prioritized=prioritized,
        rejected=rejected,
    )
    result["chat_removed_bullets"] = removed
    result["chat_prioritized_bullets"] = prioritized
    if applied:
        result["source_rule"] = (
            "Chat revisions are limited to verified bullets; unsupported claims are rejected."
        )
    result["latex"] = render_jake_latex(result)
    return result

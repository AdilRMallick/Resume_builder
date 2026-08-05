"""Anthropic client wrapper: structured output, content-addressed caching, cost accounting.

Every LLM call in this system goes through `structured_call`. That gives one place to
enforce the three rules the architecture depends on:

  1. the cache key always contains prompt_version and model_id, so a prompt edit can
     never silently reuse a stale result
  2. tokens and dollars are recorded per call into run_metric
  3. responses are schema-validated before anything downstream sees them
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.logging import get_logger
from jme.metrics import record
from jme.models import LLMCache

log = get_logger(__name__)


# Anthropic list price, USD per million tokens. Update alongside model changes.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING.get(model, (5.00, 25.00))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass
class LLMResult:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool
    model: str


class LLMError(RuntimeError):
    pass


def get_client():  # pragma: no cover - thin wrapper over the SDK
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise LLMError(
            "ANTHROPIC_API_KEY is not set. Set it in .env, or run with a cached result."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def structured_call(
    session: Session,
    *,
    kind: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    prompt_version: str,
    run_id: str | None = None,
    max_tokens: int = 16000,
    model: str | None = None,
    effort: str | None = None,
    extra_key_parts: tuple[str, ...] = (),
    force_refresh: bool = False,
) -> LLMResult:
    """One structured Anthropic call, cached on content + prompt_version + model.

    `extra_key_parts` is where callers add anything else the result depends on -
    evidence_version for matching, for instance. Getting this wrong is the classic
    way a cache quietly serves the wrong answer, so it is an explicit argument
    rather than something inferred.
    """
    settings = get_settings()
    model_id = model or settings.anthropic_model
    effort_level = effort or settings.anthropic_effort

    key = cache_key(kind, system, user, json.dumps(schema, sort_keys=True),
                    prompt_version, model_id, *extra_key_parts)

    if not force_refresh:
        cached = session.get(LLMCache, key)
        if cached is not None:
            log.debug("llm_cache_hit", kind=kind, key=key[:12])
            if run_id:
                record(session, run_id, kind, "cache_hit", 1)
            return LLMResult(
                payload=cached.payload,
                input_tokens=cached.input_tokens,
                output_tokens=cached.output_tokens,
                cost_usd=float(cached.cost_usd),
                cached=True,
                model=model_id,
            )

    client = get_client()
    response = client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort_level,
            "format": {"type": "json_schema", "schema": schema},
        },
    )

    if response.stop_reason == "refusal":
        raise LLMError(f"model refused: {getattr(response, 'stop_details', None)}")
    if response.stop_reason == "max_tokens":
        raise LLMError(f"response truncated at max_tokens={max_tokens}; raise it and retry")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMError("no text block in response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"structured output was not valid JSON: {exc}") from exc

    usage = response.usage
    in_tok = int(usage.input_tokens or 0)
    out_tok = int(usage.output_tokens or 0)
    cost = estimate_cost_usd(model_id, in_tok, out_tok)

    session.merge(
        LLMCache(
            cache_key=key,
            kind=kind,
            payload=payload,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
        )
    )

    if run_id:
        record(session, run_id, kind, "input_tokens", in_tok, {"model": model_id})
        record(session, run_id, kind, "output_tokens", out_tok, {"model": model_id})
        record(session, run_id, kind, "cost_usd", cost, {"model": model_id})
        record(session, run_id, kind, "cache_hit", 0)

    log.info(
        "llm_call",
        kind=kind,
        model=model_id,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=round(cost, 6),
    )
    return LLMResult(payload, in_tok, out_tok, cost, cached=False, model=model_id)


def cached_payloads(session: Session, kind: str) -> list[dict[str, Any]]:
    rows = session.scalars(select(LLMCache).where(LLMCache.kind == kind)).all()
    return [row.payload for row in rows]

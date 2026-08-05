"""Fixtures for the enricher tests.

The one rule these tests exist to protect: **no live API calls, ever**. Every test drives
extraction through `StubLLM`, a stand-in for `jme.llm.structured_call` that replays canned
payloads and records the same token/cost metrics the real thing does. It is injected via
`llm_call=` or monkeypatched onto the `jme.enricher.extraction.call_llm` seam, so nothing
here needs an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from jme.llm import LLMResult
from jme.metrics import record
from jme.models import Posting, PostingJD

GOLDEN_DIR = Path(__file__).parent / "golden"


# --------------------------------------------------------------------------------------
# the recorded LLM layer
# --------------------------------------------------------------------------------------


@dataclass
class StubLLM:
    """Replays canned payloads in order; the last one repeats if calls outrun payloads.

    Signature-compatible with `jme.llm.structured_call`, including its side effect of
    recording tokens and cost into run_metric, so cost accounting is exercised too.
    """

    payloads: list[dict[str, Any]]
    model: str = "stub-model-v1"
    input_tokens: int = 1200
    output_tokens: int = 400
    cost_usd: float = 0.0123
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, session: Session, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        payload = self.payloads[index]

        run_id = kwargs.get("run_id")
        if run_id:
            kind = kwargs.get("kind", "extraction")
            record(session, run_id, kind, "input_tokens", self.input_tokens, {"model": self.model})
            record(
                session, run_id, kind, "output_tokens", self.output_tokens, {"model": self.model}
            )
            record(session, run_id, kind, "cost_usd", self.cost_usd, {"model": self.model})
            record(session, run_id, kind, "cache_hit", 0)

        return LLMResult(
            payload=payload,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            cached=False,
            model=self.model,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def stub_llm():
    """`stub_llm([payload_1, payload_2, ...])` -> a StubLLM."""

    def _make(payloads: list[dict[str, Any]], **kwargs: Any) -> StubLLM:
        return StubLLM(payloads=payloads, **kwargs)

    return _make


@pytest.fixture
def no_resolver():
    """A resolver that always declines. Task 5 is built in parallel; extraction must not
    depend on it, and a null canonical_skill_id is a legitimate outcome."""

    def _resolve(session: Session, raw_text: str, posting_id: int | None = None) -> int | None:
        return None

    return _resolve


# --------------------------------------------------------------------------------------
# goldens
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenCase:
    name: str
    jd_text: str
    company: str
    title: str
    expected: list[dict[str, str]]
    llm_payload: dict[str, Any]


def load_goldens() -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for jd_path in sorted(GOLDEN_DIR.glob("*.txt")):
        stem = jd_path.stem
        expected = json.loads((GOLDEN_DIR / f"{stem}.expected.json").read_text(encoding="utf-8"))
        payload = json.loads((GOLDEN_DIR / f"{stem}.llm.json").read_text(encoding="utf-8"))
        cases.append(
            GoldenCase(
                name=stem,
                jd_text=jd_path.read_text(encoding="utf-8"),
                company=expected["company"],
                title=expected["title"],
                expected=expected["requirements"],
                llm_payload=payload,
            )
        )
    return cases


@pytest.fixture(scope="session")
def goldens() -> list[GoldenCase]:
    cases = load_goldens()
    assert len(cases) >= 5, "golden corpus must hold at least 5 job descriptions"
    return cases


# --------------------------------------------------------------------------------------
# database helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def make_posting():
    """`make_posting(session, jd_text, company=..., title=...)` -> posting id."""
    counter = {"n": 0}

    def _make(
        session: Session,
        jd_text: str | None,
        *,
        company: str = "Test Co",
        title: str = "Software Engineer, New Grad",
        adapter: str = "greenhouse",
    ) -> int:
        counter["n"] += 1
        suffix = counter["n"]
        posting = Posting(
            canonical_key=f"test-key-{suffix}-{id(session)}",
            company=company,
            title=title,
            url=f"https://boards.example.com/{suffix}",
            url_host="boards.example.com",
            locations=["Remote"],
            role_type="swe",
        )
        session.add(posting)
        session.flush()
        session.add(
            PostingJD(
                posting_id=posting.id,
                adapter=adapter,
                raw_text=jd_text,
                char_count=len(jd_text or ""),
                title=title,
            )
        )
        session.flush()
        return posting.id

    return _make

from __future__ import annotations

import json

from jme.config import Settings
from jme.resume.ai import (
    _gemini_call,
    _kimi_call,
    customize_with_ai,
    provider_catalog,
    revise_with_ai,
)
from jme.resume.tailor import load_profile, tailor_profile


def _tailored() -> dict:
    return tailor_profile(
        load_profile(),
        company="Example Company",
        title="Cloud Software Engineer",
        job_description="Python FastAPI Docker Kubernetes AWS REST API " * 8,
    )


def test_ai_accepts_an_evidence_linked_rewrite_and_preserves_jake_output() -> None:
    def fake_call(provider, system, user, schema, settings):
        assert provider == "openai"
        assert "Never add" in system
        assert schema["additionalProperties"] is False
        evidence = {item["source_id"]: item for item in json.loads(user)["source_bullets"]}
        source_id = next(
            key for key, item in evidence.items() if item["text"].startswith("Architected")
        )
        return {
            "rewrites": [
                {
                    "source_id": source_id,
                    "text": (
                        "Built a containerized FastAPI inference service with Python and Docker, "
                        "separating GPU-based satellite imagery segmentation from a Kubernetes-"
                        "orchestrated ML pipeline and reducing model validation cycles from hours "
                        "to minutes."
                    ),
                    "keywords_used": ["python", "docker", "kubernetes"],
                }
            ]
        }, "test-openai"

    result = customize_with_ai(
        _tailored(),
        job_description="Python FastAPI Docker Kubernetes AWS " * 8,
        provider="openai",
        settings=Settings(OPENAI_API_KEY="test"),
        call_provider=fake_call,
    )

    assert result["customization"]["applied_mode"] == "ai"
    assert result["customization"]["rewritten_bullets"] == 1
    rewritten = [
        bullet
        for entry in result["experience"]
        for bullet in entry["bullets"]
        if bullet.get("ai_rewritten")
    ]
    assert len(rewritten) == 1
    assert rewritten[0]["source_text"].startswith("Architected")
    assert "Built a containerized" in result["latex"]
    assert "Tailored for" not in result["latex"]
    assert "Example Company" not in result["latex"]


def test_ai_rejects_new_metrics_unverified_keywords_and_target_leaks() -> None:
    def fake_call(provider, system, user, schema, settings):
        item = json.loads(user)["source_bullets"][0]
        return {
            "rewrites": [
                {
                    "source_id": item["source_id"],
                    "text": f"{item['text'][:-1]} while improving throughput by 99 percent.",
                    "keywords_used": [],
                },
                {
                    "source_id": item["source_id"],
                    "text": f"{item['text'][:-1]} using roadmap prioritization.",
                    "keywords_used": ["roadmap"],
                },
                {
                    "source_id": item["source_id"],
                    "text": f"{item['text'][:-1]} for Example Company.",
                    "keywords_used": [],
                },
            ]
        }, "test-claude"

    result = customize_with_ai(
        _tailored(),
        job_description="roadmap throughput Python " * 20,
        provider="anthropic",
        settings=Settings(ANTHROPIC_API_KEY="test"),
        call_provider=fake_call,
    )

    assert result["customization"]["applied_mode"] == "verified"
    assert result["customization"]["rewritten_bullets"] == 0
    assert result["customization"]["rejected_rewrites"] == 3
    assert result["customization"]["warning"]


def test_chat_applies_grounded_revisions_and_returns_an_explanation() -> None:
    def fake_call(provider, system, user, schema, settings):
        request = json.loads(user)
        assert provider == "gemini"
        assert "short chat" in system
        assert request["standing_instructions"] == "Keep every bullet concise."
        assert request["conversation"][-1]["content"] == "Emphasize the cloud work."
        evidence = request["source_bullets"]
        source = next(item for item in evidence if item["text"].startswith("Architected"))
        removable = next(
            item
            for item in evidence
            if item["source_id"].split(":")[:2] == source["source_id"].split(":")[:2]
            and item["source_id"] != source["source_id"]
        )
        return {
            "assistant_message": "I emphasized the verified cloud architecture and removed one lower-priority bullet.",
            "rewrites": [
                {
                    "source_id": source["source_id"],
                    "text": (
                        "Built a containerized FastAPI inference service with Python and Docker, "
                        "separating GPU-based satellite imagery segmentation from a Kubernetes-"
                        "orchestrated ML pipeline and reducing model validation cycles from hours "
                        "to minutes."
                    ),
                    "keywords_used": ["python", "docker", "kubernetes"],
                }
            ],
            "remove_source_ids": [removable["source_id"]],
            "prioritize_source_ids": [],
        }, "test-gemini"

    result = revise_with_ai(
        _tailored(),
        job_description="Python FastAPI Docker Kubernetes AWS " * 8,
        provider="gemini",
        messages=[{"role": "user", "content": "Emphasize the cloud work."}],
        steering_prompt="Keep every bullet concise.",
        settings=Settings(GEMINI_API_KEY="test"),
        call_provider=fake_call,
    )

    assert result["customization"]["applied_mode"] == "ai"
    assert result["customization"]["rewritten_bullets"] == 1
    assert result["chat_removed_bullets"] == 1
    assert "rewrote 1 verified bullet" in result["chat_reply"]
    assert "removed 1 bullet" in result["chat_reply"]
    assert "Chat revisions are limited" in result["source_rule"]
    assert "Tailored for" not in result["latex"]


def test_provider_catalog_reports_readiness_without_returning_secrets() -> None:
    providers = provider_catalog(
        Settings(
            OPENAI_API_KEY="openai-secret",
            ANTHROPIC_API_KEY=None,
            GEMINI_API_KEY="gemini-secret",
            MOONSHOT_API_KEY="kimi-secret",
        )
    )
    by_id = {provider["id"]: provider for provider in providers}
    assert by_id["verified"]["available"] is True
    assert by_id["openai"]["available"] is True
    assert by_id["anthropic"]["available"] is False
    assert by_id["gemini"]["available"] is True
    assert by_id["kimi"]["available"] is True
    assert "openai-secret" not in json.dumps(providers)
    assert "gemini-secret" not in json.dumps(providers)
    assert "kimi-secret" not in json.dumps(providers)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_gemini_uses_backend_key_and_json_schema(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, verify):
        captured.update(url=url, headers=headers, body=json, timeout=timeout, verify=verify)
        return _FakeResponse(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": '{"rewrites": []}'}]},
                    }
                ]
            }
        )

    monkeypatch.setattr("jme.resume.ai.httpx.post", fake_post)
    payload, model = _gemini_call(
        "system",
        "user",
        {"type": "object"},
        Settings(GEMINI_API_KEY="gemini-secret"),
    )
    assert payload == {"rewrites": []}
    assert model == "gemini-3.6-flash"
    assert captured["headers"]["x-goog-api-key"] == "gemini-secret"
    assert captured["verify"].verify_mode.name == "CERT_REQUIRED"
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["body"]["generationConfig"]["responseJsonSchema"] == {
        "type": "object"
    }


def test_kimi_uses_moonshot_backend_key_and_structured_output(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, verify):
        captured.update(url=url, headers=headers, body=json, timeout=timeout, verify=verify)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"rewrites": []}'},
                    }
                ]
            }
        )

    monkeypatch.setattr("jme.resume.ai.httpx.post", fake_post)
    payload, model = _kimi_call(
        "system",
        "user",
        {"type": "object"},
        Settings(MOONSHOT_API_KEY="kimi-secret"),
    )
    assert payload == {"rewrites": []}
    assert model == "kimi-k2.6"
    assert captured["url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer kimi-secret"
    assert captured["verify"].verify_mode.name == "CERT_REQUIRED"
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["body"]["thinking"] == {"type": "disabled"}

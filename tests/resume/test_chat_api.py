from __future__ import annotations

from fastapi.testclient import TestClient

from jme.api.app import create_app


def test_chat_endpoint_is_stateless_and_returns_a_rebuilt_resume(monkeypatch) -> None:
    captured = {}

    def fake_revise(result, **kwargs):
        captured.update(kwargs)
        result["chat_reply"] = "I shortened the verified cloud bullet."
        result["chat_removed_bullets"] = 0
        result["chat_prioritized_bullets"] = 1
        return result

    monkeypatch.setattr("jme.api.app.revise_with_ai", fake_revise)
    client = TestClient(create_app())
    response = client.post(
        "/resume/chat",
        json={
            "job_description": "Python FastAPI Docker Kubernetes AWS platform reliability " * 8,
            "steering_prompt": "Lead with concise cloud impact.",
            "provider": "gemini",
            "messages": [{"role": "user", "content": "Shorten the first cloud bullet."}],
            "render_pdf": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["template_id"] == "jake-gutierrez"
    assert body["chat_reply"].startswith("I shortened")
    assert body["chat_prioritized_bullets"] == 1
    assert captured["steering_prompt"] == "Lead with concise cloud impact."
    assert captured["messages"][-1]["role"] == "user"
    assert "Tailored for" not in body["latex"]

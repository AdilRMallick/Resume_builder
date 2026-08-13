from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from jme.config import Settings
from jme.resume import claude_code
from jme.resume.ai import AIRewriteError, _call_provider, provider_catalog

SCHEMA = {"type": "object", "properties": {"rewrites": {"type": "array"}}}


def _settings(tmp_path: Path, **overrides) -> Settings:
    fake_cli = tmp_path / "claude.exe"
    fake_cli.write_text("", encoding="utf-8")
    return Settings(JME_CLAUDE_CODE_CLI_PATH=str(fake_cli), **overrides)


def _envelope(**overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"rewrites": []}',
        "structured_output": {"rewrites": [{"source_id": "experience:0:0", "text": "x"}]},
        "modelUsage": {
            "claude-haiku-4-5": {"outputTokens": 15},
            "claude-sonnet-5": {"outputTokens": 400},
        },
    }
    payload.update(overrides)
    return json.dumps(payload)


def _fake_run(captured: dict, *, stdout: str = "", stderr: str = "", returncode: int = 0):
    def run(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run


def test_cli_call_is_keyless_toolless_and_schema_bound(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-reach-the-cli")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured, stdout=_envelope()))

    payload, model = claude_code.claude_code_call(
        "system text", "user text", SCHEMA, _settings(tmp_path)
    )

    args = captured["args"]
    assert payload["rewrites"][0]["source_id"] == "experience:0:0"
    # The model that did the work, not the alias, and never a dollar cost.
    assert model == "claude-sonnet-5 (subscription)"
    assert "--print" in args and args[args.index("--output-format") + 1] == "json"
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
    assert args[args.index("--system-prompt") + 1] == "system text"
    assert args[args.index("--tools") + 1] == ""
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(args)
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--effort") + 1] == "medium"
    # The prompt travels over stdin, so a long job description cannot hit an argv limit.
    assert captured["kwargs"]["input"] == "user text"
    assert "user text" not in args
    # Subscription billing only: no API key or gateway may survive into the child.
    child_env = captured["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "ANTHROPIC_BASE_URL" not in child_env
    assert child_env.get("PATH") == os.environ.get("PATH")


def test_fenced_result_text_is_used_when_structured_output_is_missing(
    tmp_path, monkeypatch
) -> None:
    envelope = _envelope(
        structured_output=None,
        result='```json\n{"rewrites": [{"source_id": "projects:0:0"}]}\n```',
    )
    monkeypatch.setattr(subprocess, "run", _fake_run({}, stdout=envelope))

    payload, _ = claude_code.claude_code_call("s", "u", SCHEMA, _settings(tmp_path))

    assert payload["rewrites"][0]["source_id"] == "projects:0:0"


def test_usage_limit_is_reported_as_a_subscription_limit(tmp_path, monkeypatch) -> None:
    envelope = _envelope(
        is_error=True,
        subtype="error_during_execution",
        result="Claude usage limit reached. Your limit will reset at 5pm.",
    )
    monkeypatch.setattr(subprocess, "run", _fake_run({}, stdout=envelope, returncode=1))

    with pytest.raises(claude_code.ClaudeCodeError, match="usage limit is reached"):
        claude_code.claude_code_call("s", "u", SCHEMA, _settings(tmp_path))


def test_signed_out_cli_points_at_the_login_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_run({}, stdout="", stderr="Invalid API key · Please run /login", returncode=1),
    )

    with pytest.raises(claude_code.ClaudeCodeError, match="claude auth login"):
        claude_code.claude_code_call("s", "u", SCHEMA, _settings(tmp_path))


def test_timeout_names_the_budget_and_the_knobs_that_change_it(tmp_path, monkeypatch) -> None:
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"model overloaded")

    monkeypatch.setattr(subprocess, "run", timeout)
    settings = _settings(tmp_path, JME_RESUME_CLAUDE_CODE_TIMEOUT_SEC=12)

    with pytest.raises(claude_code.ClaudeCodeError) as failure:
        claude_code.claude_code_call("s", "u", SCHEMA, settings)
    message = str(failure.value)
    assert "within 12s" in message
    assert "JME_RESUME_CLAUDE_CODE_EFFORT" in message
    assert "model overloaded" in message


def test_missing_cli_is_unavailable_and_never_spawns_a_process(tmp_path, monkeypatch) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the CLI must not be invoked when it is not installed")

    monkeypatch.setattr(subprocess, "run", explode)
    settings = Settings(JME_CLAUDE_CODE_CLI_PATH=str(tmp_path / "nope" / "claude.exe"))

    assert claude_code.cli_path(settings) is None
    assert not claude_code.is_available(settings)
    with pytest.raises(claude_code.ClaudeCodeError, match="JME_CLAUDE_CODE_CLI_PATH"):
        claude_code.claude_code_call("s", "u", SCHEMA, settings)


def test_disabled_provider_is_hidden_from_the_catalog(tmp_path) -> None:
    settings = _settings(tmp_path, JME_RESUME_CLAUDE_CODE_ENABLED=False)
    entry = next(
        item for item in provider_catalog(settings) if item["id"] == "claude_code"
    )
    assert entry["available"] is False


def test_catalog_offers_claude_code_without_any_api_key(tmp_path) -> None:
    settings = _settings(tmp_path)
    entry = next(
        item for item in provider_catalog(settings) if item["id"] == "claude_code"
    )
    assert entry["available"] is True
    assert entry["model"] == "sonnet"
    assert "no API key" in entry["label"]


def test_dispatch_maps_cli_failures_onto_the_shared_fallback_contract(monkeypatch) -> None:
    def fail(system, user, schema, settings):
        raise claude_code.ClaudeCodeError("cli exploded")

    monkeypatch.setattr("jme.resume.ai.claude_code_call", fail)

    with pytest.raises(AIRewriteError, match="cli exploded"):
        _call_provider("claude_code", "s", "u", SCHEMA, Settings())

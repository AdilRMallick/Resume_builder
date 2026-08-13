"""Local Claude Code CLI provider for resume rewriting.

This is the one rewrite path that needs no API key. The backend shells out to the
`claude` binary already signed in on this machine and asks it for the same
schema-constrained JSON the HTTP providers return, so the work draws down that Claude
subscription's allowance instead of Anthropic API credits and stops when the
subscription usage limit is reached. A free Claude.ai account has no CLI, so this
provider simply reports itself unavailable there.

The child process is stripped of every API-billing environment variable. That is what
keeps "subscription" honest: if `ANTHROPIC_API_KEY` leaked through, the CLI would
prefer it and silently bill metered API credits instead. The browser never reaches this
module; it only ever names the provider id on a localhost request.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jme.config import Settings, get_settings

# Anything here would redirect the CLI off subscription auth and onto metered billing
# (a direct API key, a gateway/proxy, or a cloud model provider).
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

# Windows keeps npm shims and the native installer off PATH often enough to be worth
# checking directly; a wrong guess just means the provider reports unavailable.
_FALLBACK_RELATIVE_PATHS = (
    ".local/bin/claude.exe",
    ".local/bin/claude",
    ".claude/local/claude.exe",
    ".claude/local/claude",
    "AppData/Roaming/npm/claude.cmd",
)

# Keep a console window from flashing on Windows every time a rewrite runs. POSIX
# requires this to stay 0.
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class ClaudeCodeError(RuntimeError):
    """The local CLI was missing, unauthenticated, rate limited, or unparseable."""


def cli_path(settings: Settings | None = None) -> str | None:
    """Absolute path to a usable `claude` binary, or None when it is not installed."""
    cfg = settings or get_settings()
    if cfg.claude_code_cli_path:
        configured = Path(cfg.claude_code_cli_path).expanduser()
        return str(configured) if configured.is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    for relative in _FALLBACK_RELATIVE_PATHS:
        candidate = Path.home() / relative
        if candidate.is_file():
            return str(candidate)
    return None


def is_available(settings: Settings | None = None) -> bool:
    """Report installability only. Auth is proven by the first real call, not a probe."""
    cfg = settings or get_settings()
    return bool(cfg.resume_claude_code_enabled and cli_path(cfg))


def subscription_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment minus every variable that would bill API credits."""
    child = dict(os.environ if environ is None else environ)
    for name in BILLING_ENV_VARS:
        child.pop(name, None)
    return child


def _cli_args(cli: str, system: str, schema: dict[str, Any], settings: Settings) -> list[str]:
    args = [
        cli,
        "--print",
        "--output-format",
        "json",
        # The CLI validates the model's own output against this before returning it.
        "--json-schema",
        json.dumps(schema),
        "--system-prompt",
        system,
        # A resume rewrite is pure text transformation: no file, shell, or web tools.
        "--tools",
        "",
        "--strict-mcp-config",
        # Ignore this machine's CLAUDE.md, hooks, plugins, and custom agents so the
        # prompt is exactly what this module sent.
        "--safe-mode",
        # Never write the job description or resume into the CLI's session history.
        "--no-session-persistence",
    ]
    if settings.resume_claude_code_model:
        args += ["--model", settings.resume_claude_code_model]
    # Effort trades latency against how many rewrites survive evidence validation; see
    # the measured numbers on Settings.resume_claude_code_effort.
    if settings.resume_claude_code_effort:
        args += ["--effort", settings.resume_claude_code_effort]
    return args


def _diagnose(detail: str, *, returncode: int | None = None) -> str:
    lowered = detail.lower()
    if "limit" in lowered and any(
        word in lowered for word in ("usage", "rate", "reached", "reset", "quota")
    ):
        return (
            "Claude Code stopped: this Claude subscription's usage limit is reached. "
            f"Wait for the limit to reset or pick another provider. {detail.strip()[:400]}"
        )
    if any(
        phrase in lowered
        for phrase in ("log in", "login", "not authenticated", "unauthorized", "oauth", "401")
    ):
        return (
            "Claude Code is not signed in. Run `claude auth login` in a terminal, "
            f"then retry. {detail.strip()[:400]}"
        )
    suffix = f" (exit code {returncode})" if returncode else ""
    return f"Claude Code CLI failed{suffix}: {detail.strip()[:400] or 'no diagnostic output'}"


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def _structured_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = envelope.get("structured_output")
    if isinstance(payload, dict):
        return payload
    text = envelope.get("result")
    if not isinstance(text, str):
        raise ClaudeCodeError("Claude Code returned no structured output")
    try:
        parsed = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError("Claude Code returned invalid structured JSON") from exc
    if not isinstance(parsed, dict):
        raise ClaudeCodeError("Claude Code returned a non-object result")
    return parsed


def _reported_model(envelope: dict[str, Any], settings: Settings) -> str:
    """Name the model that actually did the work, not just the requested alias."""
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        ranked = sorted(
            (
                (name, stats.get("outputTokens", 0))
                for name, stats in usage.items()
                if isinstance(stats, dict)
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if ranked:
            return f"{ranked[0][0]} (subscription)"
    return f"{settings.resume_claude_code_model or 'claude-code'} (subscription)"


def claude_code_call(
    system: str, user: str, schema: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    """Run `claude -p` locally and return its schema-validated JSON plus the model used."""
    if not settings.resume_claude_code_enabled:
        raise ClaudeCodeError("the Claude Code provider is disabled in this configuration")
    cli = cli_path(settings)
    if cli is None:
        raise ClaudeCodeError(
            "the Claude Code CLI was not found on PATH; install it and sign in with "
            "`claude auth login`, or set JME_CLAUDE_CODE_CLI_PATH"
        )

    # A throwaway working directory keeps any project context out of the session.
    with tempfile.TemporaryDirectory(prefix="jme-claude-code-") as workdir:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, prompt goes over stdin
                _cli_args(cli, system, schema, settings),
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.resume_claude_code_timeout_sec,
                cwd=workdir,
                env=subscription_env(),
                check=False,
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.TimeoutExpired as exc:
            # The CLI's own message usually explains a stall (overload, retry backoff).
            stderr = exc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise ClaudeCodeError(
                f"Claude Code did not answer within {settings.resume_claude_code_timeout_sec}s; "
                "raise JME_RESUME_CLAUDE_CODE_TIMEOUT_SEC or lower "
                f"JME_RESUME_CLAUDE_CODE_EFFORT. {(stderr or '').strip()[:200]}"
            ) from exc
        except OSError as exc:
            raise ClaudeCodeError(f"could not start the Claude Code CLI: {exc}") from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise ClaudeCodeError(_diagnose(completed.stderr or "", returncode=completed.returncode))
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError(_diagnose(stdout, returncode=completed.returncode)) from exc
    if not isinstance(envelope, dict):
        raise ClaudeCodeError("Claude Code returned an unexpected envelope")
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        detail = envelope.get("result") or envelope.get("error") or completed.stderr or ""
        raise ClaudeCodeError(_diagnose(str(detail), returncode=completed.returncode))
    return _structured_payload(envelope), _reported_model(envelope, settings)

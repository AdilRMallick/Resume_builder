"""CLI surface tests. The commands are argument parsing plus rendering, so these assert
wiring and exit codes rather than behaviour already covered elsewhere.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import httpx
import pytest
import respx
from _feedfixtures import MALFORMED, RUN1
from typer.testing import CliRunner

from jme.ingestor import cli as ingest_cli
from jme.models import IngestRun

FEED_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

runner = CliRunner()


@pytest.fixture
def cli_session(db_session, monkeypatch):
    """Point the CLI's `session_scope` at the rolled-back test session."""

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(ingest_cli, "session_scope", scope)
    # rich squeezes columns to nothing at the CliRunner's default 80 chars
    monkeypatch.setenv("COLUMNS", "220")
    return db_session


def test_app_exposes_the_expected_commands() -> None:
    names = {c.name for c in ingest_cli.app.registered_commands}
    assert {"run", "summary"} <= names


def test_top_level_cli_mounts_the_ingest_subcommand() -> None:
    from jme.cli import app as root

    result = runner.invoke(root, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "summary" in result.stdout


@pytest.mark.integration
def test_run_from_a_local_file(cli_session) -> None:
    result = runner.invoke(
        ingest_cli.app, ["run", "--file", str(RUN1), "--no-enqueue", "--no-deactivate"]
    )
    assert result.exit_code == 0, result.output
    assert "new" in result.output


@pytest.mark.integration
def test_run_dry_run_writes_nothing(cli_session) -> None:
    result = runner.invoke(
        ingest_cli.app, ["run", "--file", str(RUN1), "--no-enqueue", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output


@pytest.mark.integration
@respx.mock
def test_run_exits_2_on_404(cli_session) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(404))
    result = runner.invoke(ingest_cli.app, ["run", "--url", FEED_URL, "--no-enqueue"])
    assert result.exit_code == 2
    assert "feed not found" in result.output


@pytest.mark.integration
@respx.mock
def test_run_exits_3_on_malformed_json(cli_session) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=MALFORMED.read_bytes()))
    result = runner.invoke(ingest_cli.app, ["run", "--url", FEED_URL, "--no-enqueue"])
    assert result.exit_code == 3
    assert "malformed" in result.output


@pytest.mark.integration
@respx.mock
def test_run_exits_4_on_transport_failure(cli_session) -> None:
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(ingest_cli.app, ["run", "--url", FEED_URL, "--no-enqueue"])
    assert result.exit_code == 4


def test_run_rejects_a_missing_file() -> None:
    result = runner.invoke(ingest_cli.app, ["run", "--file", "does-not-exist.json"])
    assert result.exit_code != 0


@pytest.mark.integration
def test_summary_renders_recent_runs(cli_session) -> None:
    cli_session.add(
        IngestRun(
            run_id="ingest-20260201T090000-abc123",
            started_at=dt.datetime(2026, 2, 1, 9, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 2, 1, 9, 0, 12, tzinfo=dt.UTC),
            feed_sha256="a" * 64,
            total_seen=18005,
            new_count=12,
            updated_count=17993,
            deactivated_count=4,
            reactivated_count=1,
        )
    )
    cli_session.flush()

    result = runner.invoke(ingest_cli.app, ["summary", "-n", "5"])

    assert result.exit_code == 0, result.output
    assert "18005" in result.output


@pytest.mark.integration
def test_summary_is_graceful_when_there_are_no_runs(cli_session) -> None:
    result = runner.invoke(ingest_cli.app, ["summary"])
    assert result.exit_code == 0
    assert "no ingest runs" in result.output

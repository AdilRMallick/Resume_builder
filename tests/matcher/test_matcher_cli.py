"""`jme match ...` CLI. The database session is redirected at the session_scope seam so
the commands run against the rolled-back test transaction."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from jme.matcher import cli as cli_module
from jme.matcher.cli import app
from jme.metrics import record
from jme.models import Match
from tests.matcher.conftest import add_shortlist, payload

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def cli(db_session, monkeypatch):
    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(cli_module, "session_scope", scope)
    return runner


def test_posting_command_prints_citations_verdict_and_cost(cli, db_session, seed, install_stub):
    install_stub([payload(seed)], cost_usd=0.0042)

    result = cli.invoke(app, ["posting", str(seed.posting_id)])

    assert result.exit_code == 0, result.output
    assert "evidenced" in result.output
    assert "plausible" in result.output
    assert "$0.004200" in result.output
    assert "Go" in result.output and "Kubernetes" in result.output
    assert "chunk" in result.output


def test_posting_command_reports_a_rejected_match(cli, db_session, seed, install_stub):
    install_stub([payload(seed, chunk_ids=[999_999, None, None])])

    result = cli.invoke(app, ["posting", str(seed.posting_id)])

    assert result.exit_code == 1
    assert "FabricatedCitationError" in result.output
    assert db_session.query(Match).count() == 0


def test_shortlist_command_totals_cost(cli, db_session, seed, install_stub):
    add_shortlist(db_session, "sl-cli", [seed.posting_id])
    install_stub([payload(seed)], cost_usd=0.01)

    result = cli.invoke(app, ["shortlist", "--run-id", "sl-cli"])

    assert result.exit_code == 0, result.output
    assert "matched 1/1" in result.output
    assert "$0.01000" in result.output


def test_stale_command_is_bounded(cli, db_session, seed, install_stub):
    install_stub([payload(seed), payload(seed, verdict="strong")])
    from jme.matcher.match import match_posting

    match = match_posting(db_session, seed.posting_id, run_id="cli-stale")
    match.is_stale = True
    db_session.commit()

    result = cli.invoke(app, ["stale", "--limit", "5"])

    assert result.exit_code == 0, result.output
    assert "recomputed 1/1" in result.output


def test_cost_command_reports_tokens_dollars_and_cache_rate(cli, db_session, seed, install_stub):
    install_stub([payload(seed)], cost_usd=0.02, input_tokens=1000, output_tokens=250)
    from jme.matcher.match import match_posting

    match_posting(db_session, seed.posting_id, run_id="cli-cost")
    # the real llm layer records these per call; the stub bypasses it, so stand them in
    record(db_session, "cli-cost", "match", "input_tokens", 1000)
    record(db_session, "cli-cost", "match", "output_tokens", 250)
    record(db_session, "cli-cost", "match", "cost_usd", 0.02)
    record(db_session, "cli-cost", "match", "cache_hit", 0)
    record(db_session, "cli-cost", "match", "cache_hit", 1)
    db_session.flush()

    result = cli.invoke(app, ["cost", "--run-id", "cli-cost"])

    assert result.exit_code == 0, result.output
    assert "50.0%" in result.output
    assert "1,000" in result.output
    assert "$0.02000" in result.output
    assert "stale matches on active postings: 0" in result.output

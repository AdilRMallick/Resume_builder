"""CLI smoke tests.

`session_scope` is patched onto the rolled-back test session, so the commands run
end to end against real Postgres without committing anything and without ever
touching the developer's own database.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from jme.evidence import cli as evidence_cli
from jme.evidence import ingest as ingest_module
from jme.evidence.ingest import live_chunks
from jme.evidence.version import current_version
from tests.evidence.conftest import make_skill

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def cli(db_session, provider, monkeypatch):
    @contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(evidence_cli, "session_scope", _scope)
    monkeypatch.setattr(ingest_module, "get_provider", lambda: provider)
    return runner


def _run(cli_runner, args: list[str]):
    result = cli_runner.invoke(evidence_cli.app, args)
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


def test_dry_run_reports_without_writing(cli, db_session, evidence_dir):
    result = _run(cli, ["ingest", "--dir", str(evidence_dir), "--dry-run"])
    assert "would bump evidence_version" in result.output
    assert live_chunks(db_session) == []


def test_ingest_then_list_then_show(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    chunks = live_chunks(db_session)
    assert len(chunks) == 2

    listed = _run(cli, ["list"])
    assert "resume.md" in listed.output

    shown = _run(cli, ["show", str(chunks[0].id)])
    assert "Redis" in shown.output


def test_second_ingest_reports_no_change(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    version = current_version(db_session)
    result = _run(cli, ["ingest", "--dir", str(evidence_dir)])
    assert "no change" in result.output
    assert current_version(db_session) == version


def test_show_missing_chunk_exits_nonzero(cli):
    result = cli.invoke(evidence_cli.app, ["show", "9999999"])
    assert result.exit_code == 1


def test_tag_without_a_taxonomy_tells_the_user_what_to_run(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    chunk_id = live_chunks(db_session)[0].id

    result = cli.invoke(evidence_cli.app, ["tag", str(chunk_id), "Redis"])

    assert result.exit_code == 1
    # rich hard-wraps the console, so compare on collapsed whitespace
    assert "jme taxonomy seed" in " ".join(result.output.split())


def test_tag_and_untag_round_trip(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    chunk_id = live_chunks(db_session)[0].id
    make_skill(db_session, "Redis")

    tagged = _run(cli, ["tag", str(chunk_id), "Redis"])
    assert "tagged" in tagged.output
    version = current_version(db_session)

    untagged = _run(cli, ["untag", str(chunk_id), "Redis"])
    assert "untagged" in untagged.output
    assert current_version(db_session) == version + 1


def test_tag_with_an_unknown_skill_exits_nonzero(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    chunk_id = live_chunks(db_session)[0].id
    make_skill(db_session, "Redis")

    result = cli.invoke(evidence_cli.app, ["tag", str(chunk_id), "Kubernetes"])
    assert result.exit_code == 1
    assert "unknown canonical skill" in result.output


def test_version_command(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    result = _run(cli, ["version"])
    assert "live chunks" in result.output
    assert str(current_version(db_session)) in result.output


def test_reembed_force_bumps(cli, db_session, evidence_dir):
    _run(cli, ["ingest", "--dir", str(evidence_dir)])
    version = current_version(db_session)

    plain = _run(cli, ["reembed"])
    assert "re-embedded 0" in plain.output
    assert current_version(db_session) == version

    forced = _run(cli, ["reembed", "--force"])
    assert "re-embedded 2" in forced.output
    assert current_version(db_session) == version + 1


def test_evidence_app_is_mounted_on_the_root_cli():
    from jme.cli import app as root

    names = {group.name for group in root.registered_groups}
    assert "evidence" in names

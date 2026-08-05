"""CLI surface: the commands exist, and the table renderer survives real row objects."""

from __future__ import annotations

import decimal

import pytest
from typer.testing import CliRunner

from jme.enricher import cli
from jme.enricher.prompts import PROMPT_VERSION
from jme.models import CanonicalSkill, Importance, PostingRequirement, SkillCategory

runner = CliRunner()


def test_commands_are_registered():
    names = {command.name or command.callback.__name__ for command in cli.app.registered_commands}
    assert names == {"posting", "backlog", "worker", "cost"}


def test_help_lists_the_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in ("posting", "backlog", "worker", "cost"):
        assert name in result.stdout


@pytest.mark.integration
def test_render_prints_importance_confidence_and_skill(db_session, capsys):
    skill = CanonicalSkill(name="Kubernetes", category=SkillCategory.infra)
    db_session.add(skill)
    db_session.flush()

    rows = [
        PostingRequirement(
            posting_id=1,
            canonical_skill_id=skill.id,
            raw_text="Familiarity with Kubernetes",
            importance=Importance.preferred,
            confidence=decimal.Decimal("0.850"),
            prompt_version=PROMPT_VERSION,
            model_id="stub-model-v1",
        ),
        PostingRequirement(
            posting_id=1,
            canonical_skill_id=None,
            raw_text="Interest in payments",
            importance=Importance.mentioned,
            confidence=decimal.Decimal("0.400"),
            prompt_version=PROMPT_VERSION,
            model_id="stub-model-v1",
        ),
    ]

    cli._render(db_session, 1, rows)
    out = capsys.readouterr().out
    assert "Kubernetes" in out
    assert "unresolved" in out
    assert "0.85" in out
    assert PROMPT_VERSION in out

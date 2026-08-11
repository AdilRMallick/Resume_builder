"""The daily digest joins the three payoff views without doing new model work."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest

from jme.report.digest import (
    build_digest_report,
    digest_report_to_dict,
    render_digest_markdown,
    write_digest,
)

pytestmark = pytest.mark.integration


def test_digest_uses_latest_shortlist_and_latest_matches(db_session, seeded) -> None:
    report = build_digest_report(
        db_session,
        top_roles=3,
        top_gaps=2,
        now=dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.UTC),
    )

    assert report.shortlist_run_id == "rank-0002"
    assert report.shortlisted_count == 5
    assert report.matched_count == 4
    assert report.unmatched_count == 1
    assert report.stale_match_count == 1
    assert len(report.roles) == 3
    assert [role.rank for role in report.roles] == [1, 2, 3]
    assert report.roles[0].verdict == "plausible"
    assert report.roles[0].rationale == "seeded"
    assert report.roles[0].evidenced_count == 1
    assert [gap.skill for gap in report.gaps] == ["Kubernetes", "Go"]
    assert any("not been matched" in warning for warning in report.warnings)
    assert any("stale match" in warning for warning in report.warnings)


def test_digest_json_and_markdown_are_stable_and_grounded(db_session, seeded, tmp_path) -> None:
    report = build_digest_report(db_session, top_roles=1, top_gaps=1)
    payload = digest_report_to_dict(report)

    assert payload["schema_version"] == "1.0"
    assert payload["kind"] == "daily_digest"
    assert payload["counts"]["roles_returned"] == 1
    assert payload["counts"]["shortlisted"] == 5
    assert payload["roles"][0]["citations"][0]["source_ref"] == "resume.md"
    assert payload["gaps"][0]["skill"] == "Kubernetes"
    json.dumps(payload)

    markdown = render_digest_markdown(report)
    assert "# Job match digest" in markdown
    assert "Acme 1 — Software Engineer New Grad 1" in markdown
    assert "resume.md #" in markdown
    assert "## Top skill gaps" in markdown
    assert "## Next actions" in markdown

    escaped_report = dataclasses.replace(
        report,
        roles=[
            dataclasses.replace(
                report.roles[0],
                citations=[
                    dataclasses.replace(
                        report.roles[0].citations[0], reasoning="used A | B\nin production"
                    )
                ],
            )
        ],
    )
    assert "used A \\| B in production" in render_digest_markdown(escaped_report)

    md_path = write_digest(report, tmp_path / "nested" / "digest.md", format="markdown")
    json_path = write_digest(report, tmp_path / "digest.json", format="json")
    assert md_path.read_text(encoding="utf-8") == markdown
    assert json.loads(json_path.read_text(encoding="utf-8"))["kind"] == "daily_digest"


def test_empty_database_produces_an_actionable_digest(db_session) -> None:
    report = build_digest_report(db_session)

    assert report.shortlist_run_id is None
    assert report.roles == []
    assert report.gaps == []
    assert report.shortlisted_count == 0
    assert any("jme rank run" in warning for warning in report.warnings)
    assert any("jme evidence ingest" in warning for warning in report.warnings)
    assert "No shortlisted roles yet." in render_digest_markdown(report)

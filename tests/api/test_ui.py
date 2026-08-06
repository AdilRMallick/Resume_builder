"""The dashboard.

Two of these tests are about the page's *contract with the rest of the system* rather
than its appearance: every path it fetches must exist on the app, and it must not
reference anything off-origin. A dashboard that silently 404s half its panels, or that
needs the network to render, is worse than no dashboard.
"""

from __future__ import annotations

import re

import pytest

from jme.api.app import DASHBOARD, create_app
from jme.api.schemas import (
    GapCounts,
    GapReportOut,
    GapSkill,
    Health,
    PostingSummary,
    QueueStatus,
    ShortlistItem,
    ShortlistOut,
    StreamStatus,
)

pytestmark = pytest.mark.integration


def test_dashboard_is_served_at_the_root(client, seed) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Job Match Engine</title>" in response.text


def test_dashboard_does_not_shadow_the_api(client, seed) -> None:
    """Serving the page at `/` must not swallow the JSON routes underneath it."""
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/gaps").status_code == 200
    assert client.get("/shortlist").status_code == 200


def test_every_path_the_page_fetches_exists_on_the_app() -> None:
    """The page can only render what the API serves. Keep the two from drifting."""
    html = DASHBOARD.read_text(encoding="utf-8")
    # Pull every quoted path out of each load(...) call rather than just the first
    # argument: one call picks its path with a ternary, and a regex that only matched
    # the simple form would quietly stop checking it.
    fetched = {
        path.split("?")[0]  # "/gaps?top=25" is the "/gaps" route
        for call in re.findall(r"load\((.*?)\);", html, re.S)
        for path in re.findall(r"""["'](/[^"']*)["']""", call)
    }
    # A regex that silently matches nothing would make this test vacuous, and an
    # under-matching one would make it weaker than it looks. Pin the expected set.
    assert fetched == {"/health", "/gaps", "/shortlist", "/ops/queue"}, (
        f"unexpected set of fetched paths: {sorted(fetched)}"
    )

    routes = {route.path for route in create_app().routes}
    missing = fetched - routes
    assert not missing, f"dashboard fetches paths the API does not serve: {sorted(missing)}"


#: Every response field the dashboard reads, by the model that owns it. Checking paths
#: alone is not enough: the first draft of this page read `eligible_postings` off the top
#: level of /gaps, where it does not live, and rendered "undefined" while every path test
#: passed. Rename a field in the API and this list tells you the page needs updating.
DASHBOARD_FIELDS = {
    Health: ["status", "database", "evidence_version", "error"],
    GapReportOut: ["evidence_version", "counts", "query_seconds", "gaps"],
    GapCounts: ["eligible_postings", "taxonomy_coverage", "requirements_unmapped", "gaps", "covered"],
    GapSkill: [
        "skill", "category", "required_count", "preferred_count", "posting_count",
        "status", "evidenced_citations", "weak_citations", "absent_citations",
    ],
    ShortlistOut: ["run_id", "count", "items"],
    ShortlistItem: ["rank", "coarse_score", "posting"],
    PostingSummary: ["company", "title", "url", "locations", "is_remote", "has_jd", "jd_adapter"],
    QueueStatus: ["streams"],
    StreamStatus: [
        "stream", "group", "depth", "pending", "consumers", "oldest_pending_age_sec",
        "dead_letter_depth", "error",
    ],
}


@pytest.mark.parametrize(("model", "fields"), DASHBOARD_FIELDS.items(), ids=lambda v: getattr(v, "__name__", ""))
def test_dashboard_reads_fields_that_actually_exist(model, fields) -> None:
    missing = [name for name in fields if name not in model.model_fields]
    assert not missing, f"{model.__name__} has no field(s) {missing}, but the dashboard reads them"


def test_dashboard_references_nothing_off_origin() -> None:
    """No CDN, no fonts, no analytics.

    The whole system is built to run offline - the test suite never touches the network
    and the default embedding provider is a local hash. A dashboard that needs a CDN to
    render would quietly break that promise, and on a locked-down machine it would break
    the page.
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    external = re.findall(r"""(?:src|href)\s*=\s*["'](https?://[^"']+)""", html)
    assert not external, f"dashboard references external resources: {external}"

    # rel="noreferrer" links out to job postings at runtime, which is fine - those URLs
    # come from the data, not from the page. What matters is that no *asset* is remote.
    assert "<script src" not in html
    assert "@import" not in html


def test_dashboard_reports_a_missing_asset_instead_of_a_blank_500(client, monkeypatch) -> None:
    missing = DASHBOARD.with_name("does-not-exist.html")
    monkeypatch.setattr("jme.api.app.DASHBOARD", missing)
    response = client.get("/")
    assert response.status_code == 500
    assert "dashboard asset missing" in response.json()["detail"]

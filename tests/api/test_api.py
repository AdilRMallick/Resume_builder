"""Every endpoint, over a seeded transactional session."""

from __future__ import annotations

import base64

import pytest

from .conftest import EVIDENCE_VERSION, LATEST_RUN, OLDER_RUN

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------


def test_health_reports_db_and_evidence_version(client, seed) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["evidence_version"] == EVIDENCE_VERSION
    assert body["error"] is None


def test_resume_profile_contains_verified_bullet_bank(client) -> None:
    response = client.get("/resume/profile")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Adil R. Mallick"
    assert len(body["experience"]) == 3
    assert len(body["projects"]) >= 3
    assert all(bullet["tags"] for entry in body["projects"] for bullet in entry["bullets"])
    serialized = response.text.lower()
    assert "confirm:" not in serialized
    assert "734-999-7794" not in serialized


def test_resume_tailor_is_stateless_and_never_rewrites_bullets(client) -> None:
    profile = client.get("/resume/profile").json()
    source_bullets = {
        bullet["text"]
        for section in ("education", "experience", "projects", "leadership")
        for entry in profile[section]
        for bullet in entry["bullets"]
    }
    response = client.post(
        "/resume/tailor",
        json={
            "company": "Example",
            "title": "Python Backend Engineer",
            "url": "https://example.com/job",
            "job_description": "Python FastAPI Redis PostgreSQL Docker REST API AWS " * 8,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target"]["title"] == "Python Backend Engineer"
    assert body["template_id"] == "jake-gutierrez"
    assert body["role_focus"] == "swe"
    assert body["projects"][0]["organization"] == "Job Match Engine"
    assert body["source_rule"].endswith("no bullet was rewritten.")
    assert body["customization"]["applied_mode"] == "verified"
    assert body["latex"].startswith("%-------------------------")
    assert "Tailored for" not in body["latex"]
    output_bullets = {
        bullet["text"]
        for section in ("education", "experience", "projects", "leadership")
        for entry in body[section]
        for bullet in entry["bullets"]
    }
    assert output_bullets <= source_bullets


def test_resume_tailor_rejects_an_empty_job_description(client) -> None:
    response = client.post("/resume/tailor", json={"job_description": "too short"})
    assert response.status_code == 422


def test_resume_tailor_can_return_a_locally_compiled_pdf(client, monkeypatch) -> None:
    from jme.resume.pdf import CompiledPDF

    monkeypatch.setattr(
        "jme.api.app.compile_one_page_resume",
        lambda result, **kwargs: (
            result,
            CompiledPDF(b"%PDF-1.7\ncompiled with LaTeX", 1),
            2,
        ),
    )
    response = client.post(
        "/resume/tailor",
        json={
            "render_pdf": True,
            "job_description": "Python FastAPI PostgreSQL Redis Docker REST API AWS " * 8,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert base64.b64decode(body["pdf_base64"]).startswith(b"%PDF-1.7")
    assert body["pdf_error"] is None
    assert body["pdf_pages"] == 1
    assert body["pdf_omitted_bullets"] == 2


def test_resume_provider_status_never_exposes_keys(client, monkeypatch) -> None:
    from jme.config import Settings

    monkeypatch.setattr(
        "jme.resume.ai.get_settings",
        lambda: Settings(OPENAI_API_KEY="do-not-return-me", ANTHROPIC_API_KEY=None),
    )
    response = client.get("/resume/providers")
    assert response.status_code == 200
    body = response.json()
    assert {item["id"] for item in body["providers"]} == {
        "verified",
        "openai",
        "anthropic",
        "gemini",
        "kimi",
    }
    assert next(item for item in body["providers"] if item["id"] == "openai")[
        "available"
    ]
    assert "do-not-return-me" not in response.text


@pytest.mark.parametrize("mode", ["openai", "anthropic", "gemini", "kimi"])
def test_resume_ai_failure_falls_back_to_verified_output(
    client, monkeypatch, mode
) -> None:
    from jme.resume.ai import AIRewriteError

    def fail(*args, **kwargs):
        raise AIRewriteError("test provider unavailable")

    monkeypatch.setattr("jme.api.app.customize_with_ai", fail)
    response = client.post(
        "/resume/tailor",
        json={
            "customization_mode": mode,
            "title": "Cloud Engineer",
            "job_description": "Python AWS Docker Kubernetes Terraform " * 10,
        },
    )
    assert response.status_code == 200
    customization = response.json()["customization"]
    assert customization["requested_mode"] == mode
    assert customization["applied_mode"] == "verified"
    assert "used verified-only" in customization["warning"]


# --------------------------------------------------------------------------------------
# postings
# --------------------------------------------------------------------------------------


def test_postings_are_paginated(client, seed) -> None:
    response = client.get("/postings", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    assert set(body["items"][0]) >= {"id", "company", "title", "url", "has_jd", "jd_adapter"}

    page_two = client.get("/postings", params={"limit": 2, "offset": 2}).json()
    assert len(page_two["items"]) == 1
    first_ids = {item["id"] for item in body["items"]}
    assert first_ids.isdisjoint({item["id"] for item in page_two["items"]})


@pytest.mark.parametrize(
    ("params", "expected_total"),
    [
        ({"active": True}, 2),
        ({"active": False}, 1),
        ({"has_jd": True}, 1),
        ({"has_jd": False}, 2),
        ({"company": "globex"}, 1),
        ({"role_type": "SWE"}, 3),
        ({"role_type": "quant"}, 0),
        ({"q": "backend"}, 3),
        ({"q": "initech"}, 1),
        ({"q": "nothing matches this"}, 0),
    ],
)
def test_posting_filters(client, seed, params, expected_total) -> None:
    response = client.get("/postings", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == expected_total


def test_posting_detail_includes_jd_requirements_and_latest_match(client, seed) -> None:
    response = client.get(f"/postings/{seed.with_jd_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == seed.with_jd_id
    assert body["jd"]["adapter"] == "greenhouse"
    assert body["jd"]["fetch_status"] == "ok"
    assert body["jd"]["has_text"] is True
    # the JD body itself is not part of the contract; char_count is
    assert "raw_text" not in body["jd"]

    assert len(body["requirements"]) == 4
    assert body["requirements"][0]["importance"] == "required"  # required sorts first
    unresolved = [r for r in body["requirements"] if r["canonical_skill_id"] is None]
    assert len(unresolved) == 1 and unresolved[0]["skill"] is None

    match = body["latest_match"]
    assert match["prompt_version"] == "v1"  # the newer of the two matches
    assert match["is_stale"] is False
    assert match["evidence_version"] == EVIDENCE_VERSION
    statuses = {c["skill"]: c["status"] for c in match["citations"]}
    assert statuses == {"Python": "evidenced", "Kubernetes": "absent"}
    assert next(c for c in match["citations"] if c["skill"] == "Python")["evidence_chunk_id"]


def test_posting_detail_without_jd_or_match(client, seed) -> None:
    body = client.get(f"/postings/{seed.without_jd_id}").json()
    assert body["jd"] is None
    assert body["latest_match"] is None
    assert body["requirements"] == []
    assert body["has_jd"] is False


def test_posting_detail_404s_cleanly_for_an_unknown_id(client, seed) -> None:
    response = client.get(f"/postings/{seed.missing_id}")
    assert response.status_code == 404
    assert str(seed.missing_id) in response.json()["detail"]


def test_posting_detail_422s_on_a_non_integer_id(client, seed) -> None:
    assert client.get("/postings/not-an-id").status_code == 422


# --------------------------------------------------------------------------------------
# shortlist
# --------------------------------------------------------------------------------------


def test_shortlist_defaults_to_the_latest_run(client, seed) -> None:
    body = client.get("/shortlist").json()
    assert body["run_id"] == LATEST_RUN
    assert body["count"] == 2
    assert [item["rank"] for item in body["items"]] == [1, 2]
    assert body["items"][0]["posting"]["id"] == seed.with_jd_id
    assert body["items"][0]["coarse_score"] == pytest.approx(0.9)


def test_shortlist_accepts_an_explicit_run_id(client, seed) -> None:
    body = client.get("/shortlist", params={"run_id": OLDER_RUN}).json()
    assert body["run_id"] == OLDER_RUN
    assert body["count"] == 1


def test_shortlist_is_empty_not_404_when_no_run_exists(client, db_session) -> None:
    response = client.get("/shortlist")
    assert response.status_code == 200
    assert response.json() == {"run_id": None, "created_at": None, "count": 0, "items": []}


# --------------------------------------------------------------------------------------
# gaps
# --------------------------------------------------------------------------------------


def test_gaps_returns_the_report_schema(client, seed) -> None:
    response = client.get("/gaps", params={"top": 5})
    assert response.status_code == 200
    body = response.json()

    assert body["schema_version"] == "1.0"
    assert body["kind"] == "gap_report"
    assert body["evidence_version"] == EVIDENCE_VERSION
    assert body["counts"]["eligible_postings"] == 2
    assert body["counts"]["requirements_unmapped"] == 1

    skills = [g["skill"] for g in body["gaps"]]
    assert skills == ["Kubernetes"]  # Python is evidenced, Communication is not actionable
    assert body["gaps"][0]["status"] == "absent"
    assert body["covered"] is None


def test_gaps_can_include_covered_skills(client, seed) -> None:
    body = client.get("/gaps", params={"include_covered": True}).json()
    assert [c["skill"] for c in body["covered"]] == ["Python"]


def test_gaps_rejects_a_nonsense_top(client, seed) -> None:
    assert client.get("/gaps", params={"top": 0}).status_code == 422


# --------------------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------------------


def test_digest_combines_shortlist_matches_and_gaps(client, seed) -> None:
    response = client.get("/digest", params={"top_roles": 1, "top_gaps": 1})
    assert response.status_code == 200
    body = response.json()

    assert body["schema_version"] == "1.0"
    assert body["kind"] == "daily_digest"
    assert body["shortlist_run_id"] == LATEST_RUN
    assert body["counts"]["shortlisted"] == 2
    assert body["counts"]["roles_returned"] == 1
    assert body["counts"]["matched"] == 1
    assert body["counts"]["unmatched"] == 1
    assert body["roles"][0]["posting_id"] == seed.with_jd_id
    assert body["roles"][0]["verdict"] == "plausible"
    assert body["roles"][0]["evidenced_count"] == 1
    assert body["roles"][0]["absent_count"] == 1
    assert body["gaps"][0]["skill"] == "Kubernetes"
    assert body["next_actions"]


def test_digest_rejects_zero_limits(client, seed) -> None:
    assert client.get("/digest", params={"top_roles": 0}).status_code == 422
    assert client.get("/digest", params={"top_gaps": 0}).status_code == 422


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


def test_skills_carry_counts(client, seed) -> None:
    response = client.get("/skills")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    by_name = {s["name"]: s for s in body["items"]}
    assert by_name["Kubernetes"]["required_count"] == 1
    assert by_name["Kubernetes"]["posting_count"] == 1
    assert by_name["Python"]["required_count"] == 0
    assert by_name["Python"]["requirement_count"] == 1
    assert by_name["Excellent Communication"]["is_actionable"] is False


def test_skills_filters(client, seed) -> None:
    assert client.get("/skills", params={"actionable": False}).json()["total"] == 1
    assert client.get("/skills", params={"q": "kube"}).json()["total"] == 1
    assert client.get("/skills", params={"category": "language"}).json()["total"] == 1


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------


def test_metrics_are_listable_and_filterable(client, seed) -> None:
    body = client.get("/metrics").json()
    assert body["total"] == 2
    assert {m["stage"] for m in body["items"]} == {"rank", "fetch"}

    fetch = client.get("/metrics", params={"stage": "fetch"}).json()
    assert fetch["total"] == 1
    assert fetch["items"][0]["metric"] == "adapter_success_rate"
    assert fetch["items"][0]["value"] == pytest.approx(0.5)
    assert fetch["items"][0]["labels"] == {"adapter": "greenhouse"}

    assert client.get("/metrics", params={"run_id": "nope"}).json()["total"] == 0


# --------------------------------------------------------------------------------------
# ops
# --------------------------------------------------------------------------------------


def test_ops_queue_returns_both_streams(client) -> None:
    response = client.get("/ops/queue")
    assert response.status_code == 200
    body = response.json()
    assert [s["stream"] for s in body["streams"]] == ["jme:fetch", "jme:enrich"]
    for stream in body["streams"]:
        # depth is populated when Redis answers; a missing consumer group is reported
        # per-stream rather than failing the request
        assert stream["depth"] is not None or stream["error"] is not None


def test_ops_queue_degrades_gracefully_when_redis_is_unreachable(client, monkeypatch) -> None:
    """Redis being down is a fact about one dependency, not a reason to 500."""
    import jme.api.app as app_module
    from jme.config import Settings

    broken = Settings(JME_REDIS_URL="redis://127.0.0.1:1/0")
    monkeypatch.setattr(app_module, "get_settings", lambda: broken)

    response = client.get("/ops/queue")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    assert [s["stream"] for s in body["streams"]] == ["jme:fetch", "jme:enrich"]
    assert all(s["error"] == "redis unreachable" for s in body["streams"])
    assert all(s["depth"] is None for s in body["streams"])

    # and the rest of the API is unaffected
    assert client.get("/health").status_code == 200


def test_only_stateless_resume_tailoring_accepts_a_post(client) -> None:
    """The extension's compute-only route is the sole non-read verb."""
    from jme.api.app import app as real_app

    non_read_routes = {}
    for route in real_app.routes:
        methods = getattr(route, "methods", set()) or set()
        non_read = methods & {"POST", "PUT", "PATCH", "DELETE"}
        if non_read:
            non_read_routes[route.path] = non_read
    assert non_read_routes == {"/resume/tailor": {"POST"}}
    assert client.get("/resume/tailor").status_code == 405

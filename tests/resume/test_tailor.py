from __future__ import annotations

from jme.resume.tailor import load_profile, load_rules, tailor_profile


def test_tailoring_prioritizes_relevant_verified_projects_without_rewriting() -> None:
    profile = load_profile()
    original_bullets = {
        bullet["text"]
        for section in ("education", "experience", "projects", "leadership")
        for entry in profile[section]
        for bullet in entry["bullets"]
    }

    result = tailor_profile(
        profile,
        company="Example Cloud",
        title="Backend Python Engineer",
        job_description=(
            "We need a Python engineer with FastAPI, PostgreSQL, Redis, Docker, REST API, "
            "distributed systems, CI/CD, and AWS experience. " * 3
        ),
    )

    assert result["projects"][0]["organization"] == "Job Match Engine"
    assert result["template_id"] == "jake-gutierrez"
    assert result["role_focus"] == "swe"
    assert {"python", "fastapi", "postgresql", "redis", "docker"} <= set(
        result["matched_skills"]
    )
    output_bullets = {
        bullet["text"]
        for section in ("education", "experience", "projects", "leadership")
        for entry in result[section]
        for bullet in entry["bullets"]
    }
    assert output_bullets <= original_bullets
    assert sum(len(project["bullets"]) for project in result["projects"]) <= 5
    assert all(len(project["bullets"]) <= 2 for project in result["projects"])


def test_short_tags_match_words_not_substrings() -> None:
    profile = load_profile()
    result = tailor_profile(
        profile,
        job_description="MongoDB governance and database operations " * 5,
    )
    assert "go" not in result["matched_skills"]


def test_apm_tailoring_prioritizes_product_evidence_without_adding_claims() -> None:
    result = tailor_profile(
        load_profile(),
        title="Associate Product Manager",
        job_description=(
            "Own product discovery and roadmap prioritization, learn from users, analyze "
            "customer needs, coordinate cross-functional delivery, and measure outcomes. " * 3
        ),
    )
    assert result["role_focus"] == "apm"
    assert result["projects"][0]["organization"] == "Half-Full"
    assert "product" in result["matched_skills"]


def test_cloud_tailoring_puts_verified_cloud_keywords_first() -> None:
    result = tailor_profile(
        load_profile(),
        title="Cloud Infrastructure Engineer",
        job_description="AWS Azure Terraform Docker Kubernetes CI/CD cloud infrastructure " * 4,
    )
    assert result["role_focus"] == "cloud"
    assert result["skills"]["Cloud and DevOps"][:5] == [
        "AWS",
        "Azure",
        "Terraform",
        "Docker",
        "Kubernetes",
    ]


def test_jake_latex_has_canonical_sections_and_never_renders_target_job() -> None:
    result = tailor_profile(
        load_profile(),
        company="Never Render Incorporated",
        title="Secret Target Job",
        job_description="Python FastAPI PostgreSQL Redis Docker REST API AWS " * 8,
    )
    latex = result["latex"]
    assert "Never Render Incorporated" not in latex
    assert "Secret Target Job" not in latex
    assert "Tailored for" not in latex
    assert "\\pdfgentounicode=1" in latex
    assert "\\documentclass[letterpaper,11pt]{article}" in latex
    positions = [
        latex.index(f"\\section{{{section}}}")
        for section in load_rules()["section_order"]
    ]
    assert positions == sorted(positions)


def test_template_rules_forbid_header_banners_and_unverified_keywords() -> None:
    rules = load_rules()
    assert rules["header"]["allowed"] == ["name", "contact"]
    assert "target_job" in rules["header"]["forbidden"]
    assert rules["keyword_policy"]["mode"] == "verified_only"

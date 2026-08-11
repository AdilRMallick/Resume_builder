from __future__ import annotations

from jme.resume.tailor import load_profile, tailor_profile


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


def test_short_tags_match_words_not_substrings() -> None:
    profile = load_profile()
    result = tailor_profile(
        profile,
        job_description="MongoDB governance and database operations " * 5,
    )
    assert "go" not in result["matched_skills"]

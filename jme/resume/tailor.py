"""Deterministic resume tailoring from a verified bullet bank.

Tailoring means selection and ordering, never rewriting. That constraint matters more
than clever prose: every sentence in the output remains byte-for-byte traceable to the
curated profile, so the browser extension cannot manufacture a claim to chase a keyword.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jme.resume.latex import render_jake_latex

PROFILE_PATH = Path(__file__).with_name("profile.json")
RULES_PATH = Path(__file__).with_name("jake_template_rules.json")


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    """Load the package-owned, verified resume profile."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    """Load the permanent Jake-template and keyword-selection contract."""
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9+#./-]+", " ", value.lower()).strip()


def _contains(context: str, tag: str) -> bool:
    normalized = _normalize(tag)
    if not normalized:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", context) is not None


def _bullet_score(bullet: dict[str, Any], context: str) -> int:
    return sum(4 for tag in bullet.get("tags", []) if _contains(context, tag))


def _order_bullets(entry: dict[str, Any], context: str) -> dict[str, Any]:
    result = copy.deepcopy(entry)
    indexed = list(enumerate(result.get("bullets", [])))
    indexed.sort(key=lambda item: (-_bullet_score(item[1], context), item[0]))
    result["bullets"] = [bullet for _, bullet in indexed]
    return result


def _entry_score(entry: dict[str, Any], context: str) -> int:
    return sum(_bullet_score(bullet, context) for bullet in entry.get("bullets", []))


def _matched_tags(profile: dict[str, Any], context: str) -> list[str]:
    counts: Counter[str] = Counter()
    for section in ("education", "experience", "projects", "leadership"):
        for entry in profile.get(section, []):
            for bullet in entry.get("bullets", []):
                for tag in bullet.get("tags", []):
                    if _contains(context, tag):
                        counts[tag] += 1
    return [tag for tag, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _order_skills(skills: dict[str, list[str]], context: str) -> dict[str, list[str]]:
    return {
        category: sorted(values, key=lambda value: (not _contains(context, value), values.index(value)))
        for category, values in skills.items()
    }


def _detect_role(context: str, rules: dict[str, Any]) -> str:
    scores = {
        role: sum(1 for signal in signals if _contains(context, signal))
        for role, signals in rules["role_signals"].items()
    }
    return max(scores, key=lambda role: (scores[role], role == "swe"))


def _select_projects(
    projects: list[dict[str, Any]], context: str, rules: dict[str, Any]
) -> list[dict[str, Any]]:
    limits = rules["project_limits"]
    ranked = list(enumerate(projects))
    ranked.sort(key=lambda item: (-_entry_score(item[1], context), item[0]))
    selected: list[dict[str, Any]] = []
    remaining = int(limits["bullets_total"])
    for _, entry in ranked[: int(limits["entries"])]:
        ordered = _order_bullets(entry, context)
        take = min(int(limits["bullets_per_entry"]), remaining)
        ordered["bullets"] = ordered["bullets"][:take]
        selected.append(ordered)
        remaining -= take
        if remaining <= 0:
            break
    return selected


def tailor_profile(
    profile: dict[str, Any],
    *,
    job_description: str,
    title: str = "",
    company: str = "",
    url: str = "",
) -> dict[str, Any]:
    """Return a one-page-oriented profile ordered for a target job description."""
    context = _normalize(f"{title}\n{job_description}")
    rules = load_rules()
    result = {
        "template_id": rules["template_id"],
        "role_focus": _detect_role(context, rules),
        "target": {"company": company, "title": title, "url": url},
        "matched_skills": _matched_tags(profile, context),
        "name": profile["name"],
        "headline": profile["headline"],
        "contact": copy.deepcopy(profile.get("contact", [])),
        "education": [
            _order_bullets(entry, context) for entry in profile.get("education", [])
        ],
        "experience": [
            _order_bullets(entry, context) for entry in profile.get("experience", [])
        ],
        "projects": _select_projects(profile.get("projects", []), context, rules),
        "leadership": [
            _order_bullets(entry, context) for entry in profile.get("leadership", [])
        ],
        "skills": _order_skills(profile.get("skills", {}), context),
        "certifications": copy.deepcopy(profile.get("certifications", [])),
        "source_rule": "Selected and reordered from the verified profile; no bullet was rewritten.",
    }
    result["latex"] = render_jake_latex(result)
    return result

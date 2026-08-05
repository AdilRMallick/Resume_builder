"""Validation of data/taxonomy/skills.yaml itself. No database.

This is the curation guard rail: the seed file is hand-edited, and every failure mode
here (a typo'd category, a duplicated skill, an alias claimed twice) silently corrupts
every aggregate number downstream.
"""

from __future__ import annotations

from collections import Counter

import pytest

from jme.models import SkillCategory
from jme.taxonomy.normalize import normalize
from jme.taxonomy.seed import (
    MAX_SKILLS,
    MIN_SKILLS,
    SeedError,
    SkillSpec,
    check_size,
    validate_specs,
)

# categories that must actually be represented; a taxonomy missing one of these is not
# describing US new-grad SWE postings
REQUIRED_CATEGORIES = {c for c in SkillCategory}

# a sample of things that genuinely appear in new-grad job descriptions
MUST_EXIST = [
    "Python",
    "Java",
    "Go",
    "C++",
    "C#",
    "Rust",
    "TypeScript",
    "JavaScript",
    "Kotlin",
    "Swift",
    "Scala",
    "Ruby",
    "SQL",
    "Bash",
    "PostgreSQL",
    "MySQL",
    "MongoDB",
    "Redis",
    "DynamoDB",
    "Cassandra",
    "Elasticsearch",
    "Snowflake",
    "BigQuery",
    "AWS",
    "Google Cloud Platform",
    "Microsoft Azure",
    "AWS Lambda",
    "Amazon S3",
    "Amazon EC2",
    "Docker",
    "Kubernetes",
    "Terraform",
    "CI/CD",
    "Apache Kafka",
    "gRPC",
    "REST APIs",
    "Microservices",
    "Linux",
    "Git",
    "Observability",
    "PyTorch",
    "TensorFlow",
    "LLMs",
    "RAG",
    "NLP",
    "Computer Vision",
    "scikit-learn",
    "React",
    "Django",
    "Spring Boot",
    "Flask",
    "FastAPI",
    "Node.js",
    "Next.js",
    ".NET",
    "Ruby on Rails",
    "Unit Testing",
    "Code Review",
    "Agile",
    "Distributed Systems",
    "System Design",
    "Data Structures & Algorithms",
    "Application Security",
    "Communication",
    "Teamwork",
    "Leadership",
]


def test_size_within_curation_target(specs):
    check_size(specs)
    assert MIN_SKILLS <= len(specs) <= MAX_SKILLS


def test_every_category_is_a_valid_enum_member(specs):
    for spec in specs:
        assert isinstance(spec.category, SkillCategory)


def test_every_category_is_represented(specs):
    present = {spec.category for spec in specs}
    assert present == REQUIRED_CATEGORIES, f"missing: {REQUIRED_CATEGORIES - present}"


def test_no_duplicate_skill_names(specs):
    counts = Counter(normalize(spec.name) for spec in specs)
    dupes = [name for name, n in counts.items() if n > 1]
    assert not dupes, f"duplicate skill names: {dupes}"


def test_no_ambiguous_aliases(specs):
    owner: dict[str, str] = {}
    clashes: list[str] = []
    for spec in specs:
        for norm in spec.desired_aliases():
            if norm in owner and owner[norm] != spec.name:
                clashes.append(f"{norm!r}: {owner[norm]} vs {spec.name}")
            owner[norm] = spec.name
    assert not clashes, f"ambiguous aliases: {clashes}"


def test_identity_alias_is_generated_for_every_skill(specs):
    for spec in specs:
        assert normalize(spec.name) in spec.desired_aliases()


def test_aliases_fit_the_column(specs):
    for spec in specs:
        assert len(spec.name) <= 128
        for norm, display in spec.desired_aliases().items():
            assert 0 < len(norm) <= 128
            assert 0 < len(display) <= 128


@pytest.mark.parametrize("name", MUST_EXIST)
def test_expected_skill_is_present(specs, name):
    assert any(spec.name == name for spec in specs), f"{name} missing from the taxonomy"


def test_soft_skills_are_not_actionable(specs):
    offenders = [s.name for s in specs if s.category is SkillCategory.soft and s.is_actionable]
    assert not offenders, f"soft skills must be excluded from the gap report: {offenders}"


def test_actionable_skills_dominate(specs):
    """The taxonomy exists to produce actionable gaps; most entries must be actionable."""
    actionable = [s for s in specs if s.is_actionable]
    assert len(actionable) > len(specs) * 0.6


def test_technical_categories_are_actionable(specs):
    technical = {
        SkillCategory.language,
        SkillCategory.database,
        SkillCategory.infra,
        SkillCategory.framework,
        SkillCategory.ml,
    }
    non_actionable = [s.name for s in specs if s.category in technical and not s.is_actionable]
    # "Cloud Computing" style catch-alls live in `cloud`, which is deliberately excluded
    assert non_actionable == []


@pytest.mark.parametrize(
    ("alias", "skill"),
    [
        ("Golang", "Go"),
        ("Postgres", "PostgreSQL"),
        ("psql", "PostgreSQL"),
        ("K8s", "Kubernetes"),
        ("JS", "JavaScript"),
        ("TS", "TypeScript"),
        ("CS fundamentals", "Data Structures & Algorithms"),
        ("DSA", "Data Structures & Algorithms"),
        ("Amazon Web Services", "AWS"),
        ("GCP", "Google Cloud Platform"),
        ("NodeJS", "Node.js"),
        ("node", "Node.js"),
        ("dotnet", ".NET"),
        ("cpp", "C++"),
        ("csharp", "C#"),
        ("mongo", "MongoDB"),
        ("ML", "Machine Learning"),
        ("large language models", "LLMs"),
        ("retrieval augmented generation", "RAG"),
        ("continuous integration", "CI/CD"),
        ("rails", "Ruby on Rails"),
        ("SRE", "Reliability Engineering"),
        ("TDD", "Test-Driven Development"),
        ("OOP", "Object-Oriented Programming"),
    ],
)
def test_real_world_alias_is_curated(specs, alias, skill):
    by_name = {spec.name: spec for spec in specs}
    assert skill in by_name, f"{skill} missing"
    assert normalize(alias) in by_name[skill].desired_aliases()


def test_validate_specs_rejects_duplicate_names():
    specs = [
        SkillSpec(name="Go", category=SkillCategory.language),
        SkillSpec(name="go", category=SkillCategory.language),
    ]
    with pytest.raises(SeedError, match="duplicate skill names"):
        validate_specs(specs)


def test_validate_specs_rejects_ambiguous_aliases():
    specs = [
        SkillSpec(name="Go", category=SkillCategory.language, aliases=("golang",)),
        SkillSpec(name="Golang Fan Club", category=SkillCategory.soft, aliases=("Golang",)),
    ]
    with pytest.raises(SeedError, match="ambiguous aliases"):
        validate_specs(specs)


def test_check_size_rejects_a_tiny_taxonomy():
    with pytest.raises(SeedError, match="expected between"):
        check_size([SkillSpec(name="Go", category=SkillCategory.language)])

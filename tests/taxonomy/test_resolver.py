"""Resolution of tricky raw strings against the real seeded taxonomy.

The table below is the contract for Task 6 (requirement extraction): whatever the LLM
hands back as a `raw_text` span goes through `resolve()`, and these are the cases that
decide whether the aggregate gap report counts things correctly or invents a "Go" gap
every time a posting mentions Django.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from jme.models import CandidateStatus, CanonicalSkill, SkillAliasCandidate
from jme.taxonomy import resolver

pytestmark = pytest.mark.integration

# (raw text, expected canonical skill name or None)
CASES: list[tuple[str, str | None]] = [
    # --- the "Go" substring traps -----------------------------------------------
    ("Go", "Go"),
    ("go", "Go"),
    ("Golang", "Go"),
    ("golang", "Go"),
    ("Go (Golang)", "Go"),
    ("Experience with Go and Kubernetes preferred", "Go"),
    ("Backend services written in Go.", "Backend Development"),
    ("Django", "Django"),
    ("Django REST Framework", "Django"),
    ("MongoDB", "MongoDB"),
    ("Mongo", "MongoDB"),
    ("Mongoose ODM", None),
    ("ongoing work", None),
    ("ongoing maintenance of internal tooling", None),
    ("Go-to-market strategy", None),
    ("go to market experience", None),
    ("Lego robotics club", "Robotics"),
    # --- C family ----------------------------------------------------------------
    ("C", "C"),
    ("C++", "C++"),
    ("C#", "C#"),
    ("c#", "C#"),
    ("C-Sharp", "C#"),
    ("C/C++", "C++"),
    ("C++/Java", "C++"),
    ("Objective-C", "Objective-C"),
    ("C-Suite exposure", None),
    ("Modern C++ (C++17)", "C++"),
    # --- dots --------------------------------------------------------------------
    (".NET", ".NET"),
    (".NET Core", ".NET"),
    ("ASP.NET Core", ".NET"),
    ("Node.js", "Node.js"),
    ("NodeJS", "Node.js"),
    ("node", "Node.js"),
    ("Node", "Node.js"),
    ("Next.js", "Next.js"),
    ("Vue.js", "Vue.js"),
    ("Experience with Node.js.", "Node.js"),
    # --- databases ---------------------------------------------------------------
    ("Postgres", "PostgreSQL"),
    ("PostgreSQL", "PostgreSQL"),
    ("postgresql", "PostgreSQL"),
    ("psql", "PostgreSQL"),
    ("MySQL", "MySQL"),
    ("SQL", "SQL"),
    ("SQL Server", "Microsoft SQL Server"),
    ("NoSQL databases", "NoSQL"),
    ("Redis", "Redis"),
    ("DynamoDB", "DynamoDB"),
    ("Elasticsearch", "Elasticsearch"),
    ("Snowflake", "Snowflake"),
    ("BigQuery", "BigQuery"),
    # --- cloud and infra ---------------------------------------------------------
    ("AWS", "AWS"),
    ("Amazon Web Services", "AWS"),
    ("Amazon Web Services (AWS)", "AWS"),
    ("GCP", "Google Cloud Platform"),
    ("Google Cloud Platform", "Google Cloud Platform"),
    ("Azure", "Microsoft Azure"),
    ("AWS Lambda", "AWS Lambda"),
    ("lambda expressions in Java", "Java"),
    ("K8s", "Kubernetes"),
    ("Kubernetes (K8s)", "Kubernetes"),
    ("Docker containers", "Docker"),
    ("Terraform", "Terraform"),
    ("CI/CD", "CI/CD"),
    ("continuous integration and continuous delivery", "CI/CD"),
    ("GitHub Actions", "GitHub Actions"),
    ("Git", "Git"),
    ("Kafka", "Apache Kafka"),
    ("gRPC", "gRPC"),
    ("RESTful APIs", "REST APIs"),
    ("REST", "REST APIs"),
    ("the rest of the team", None),
    ("data at rest encryption", "Cryptography"),
    ("Micro-services", "Microservices"),
    ("microservices architecture", "Microservices"),
    ("Linux/Unix", "Linux"),
    # --- languages and frameworks ------------------------------------------------
    ("JS", "JavaScript"),
    ("TS", "TypeScript"),
    ("JavaScript/TypeScript", "JavaScript"),
    ("Python 3", "Python"),
    ("Java 17", "Java"),
    ("Rust", "Rust"),
    ("Kotlin", "Kotlin"),
    ("Swift/SwiftUI", "Swift"),
    ("React", "React"),
    ("React Native", "React Native"),
    ("Spring Boot", "Spring Boot"),
    ("Spring 2027 start date", None),
    ("FastAPI", "FastAPI"),
    ("Ruby on Rails", "Ruby on Rails"),
    ("R", "R"),
    ("R&D experience", None),
    # --- ml ----------------------------------------------------------------------
    ("Machine Learning", "Machine Learning"),
    ("ML/AI", "Machine Learning"),
    ("LLMs", "LLMs"),
    ("large language models", "LLMs"),
    ("RAG", "RAG"),
    ("retrieval-augmented generation", "RAG"),
    ("PyTorch", "PyTorch"),
    ("scikit-learn", "scikit-learn"),
    ("NLP", "NLP"),
    # --- practices and soft skills ----------------------------------------------
    ("CS fundamentals", "Data Structures & Algorithms"),
    ("DSA", "Data Structures & Algorithms"),
    ("strong fundamentals in data structures and algorithms", "Data Structures & Algorithms"),
    ("system design", "System Design"),
    ("distributed systems", "Distributed Systems"),
    ("unit testing", "Unit Testing"),
    ("TDD", "Test-Driven Development"),
    ("Agile/Scrum", "Agile"),
    ("excellent written and verbal communication skills", "Communication"),
    ("Bachelor's degree in Computer Science", "Computer Science Degree"),
    # --- genuine non-matches -----------------------------------------------------
    ("", None),
    ("   ", None),
    ("3+ years of experience", None),
    ("blockchain wizardry", None),
    ("quantum annealing", None),
    ("underwater basket weaving", None),
]


@pytest.mark.parametrize(("raw", "expected"), CASES, ids=[c[0] or "<empty>" for c in CASES])
def test_resolve_case(tx, skill_names, raw, expected):
    skill_id = resolver.resolve(tx, raw, record_candidate=False)
    got = skill_names.get(skill_id) if skill_id is not None else None
    assert got == expected


def test_at_least_fifty_cases():
    assert len(CASES) >= 50


@pytest.mark.parametrize(
    "variants",
    [
        ("Postgres", "PostgreSQL", "psql", "Postgres SQL", "postgres"),
        ("Go", "Golang", "go lang", "GOLANG"),
        ("Kubernetes", "k8s", "K8S", "kube"),
        ("Node.js", "NodeJS", "node js", "node"),
        (".NET", "dotnet", ".net core", "ASP.NET"),
    ],
)
def test_variants_collapse_onto_one_node(tx, variants):
    ids = {resolver.resolve(tx, v, record_candidate=False) for v in variants}
    assert len(ids) == 1 and None not in ids, f"{variants} resolved to {ids}"


def test_c_family_are_three_distinct_nodes(tx):
    c = resolver.resolve(tx, "C", record_candidate=False)
    cpp = resolver.resolve(tx, "C++", record_candidate=False)
    csharp = resolver.resolve(tx, "C#", record_candidate=False)
    assert len({c, cpp, csharp}) == 3
    assert None not in {c, cpp, csharp}


def test_longest_alias_wins(tx, skill_names):
    """Overlapping aliases resolve to the most specific skill."""
    assert skill_names[resolver.resolve(tx, "React Native", record_candidate=False)] == (
        "React Native"
    )
    assert skill_names[resolver.resolve(tx, "SQL Server", record_candidate=False)] == (
        "Microsoft SQL Server"
    )
    assert skill_names[resolver.resolve(tx, "GitHub Actions", record_candidate=False)] == (
        "GitHub Actions"
    )
    assert skill_names[resolver.resolve(tx, "Google Cloud Platform", record_candidate=False)] == (
        "Google Cloud Platform"
    )


def test_scan_returns_every_skill_in_a_span(tx, skill_names):
    matches = resolver.scan(tx, "Experience with Go, Kubernetes and PostgreSQL is preferred")
    assert [skill_names[m.skill_id] for m in matches] == ["Go", "Kubernetes", "PostgreSQL"]
    assert [m.start for m in matches] == sorted(m.start for m in matches)


def test_resolve_all_batches(tx, skill_names):
    ids = resolver.resolve_all(tx, ["Golang", "K8s", "not a skill at all"], record_candidates=False)
    assert [skill_names.get(i) if i else None for i in ids] == ["Go", "Kubernetes", None]


# --------------------------------------------------------------------------------------
# the resolver may never create a canonical skill
# --------------------------------------------------------------------------------------


def _skill_count(session) -> int:
    return session.scalar(select(func.count(CanonicalSkill.id)))


UNKNOWN = [
    "blockchain wizardry",
    "quantum annealing",
    "underwater basket weaving",
    "COBOL on the mainframe circa 1974",
    "vibes-based programming",
]


def test_resolver_never_creates_a_canonical_skill(tx):
    before = _skill_count(tx)
    resolver.resolve_all(tx, UNKNOWN, posting_id=None)
    resolver.resolve_all(tx, UNKNOWN, posting_id=None)
    tx.flush()
    assert _skill_count(tx) == before


def test_resolver_module_cannot_reference_canonical_skill():
    """Structural, not aspirational: the module has no handle on the table."""
    assert not hasattr(resolver, "CanonicalSkill")
    source = __import__("inspect").getsource(resolver)
    assert "CanonicalSkill" not in source.split('"""', 2)[2]


def test_unknown_text_lands_in_the_candidate_queue(tx):
    resolver.resolve(tx, "vibes-based programming")
    tx.flush()
    row = tx.scalar(
        select(SkillAliasCandidate).where(
            SkillAliasCandidate.raw_norm == "vibes based programming"
        )
    )
    assert row is not None
    assert row.raw_text == "vibes-based programming"
    assert row.seen_count == 1
    assert row.status is CandidateStatus.pending


def test_candidate_seen_count_increments(tx):
    for _ in range(4):
        resolver.resolve(tx, "COBOL on the mainframe circa 1974")
    tx.flush()
    row = tx.scalar(
        select(SkillAliasCandidate).where(
            SkillAliasCandidate.raw_norm == "cobol on the mainframe circa 1974"
        )
    )
    assert row.seen_count == 4


def test_candidate_records_the_first_posting_only(tx, db_engine):
    from jme.models import Posting

    posting = Posting(canonical_key="k-taxonomy-test", company="Acme", title="SWE", url="http://x")
    tx.add(posting)
    tx.flush()

    resolver.resolve(tx, "quantum annealing", posting_id=posting.id)
    resolver.resolve(tx, "quantum annealing", posting_id=None)
    tx.flush()
    row = tx.scalar(
        select(SkillAliasCandidate).where(SkillAliasCandidate.raw_norm == "quantum annealing")
    )
    assert row.seen_count == 2
    assert row.example_posting_id == posting.id


def test_known_text_creates_no_candidate(tx):
    before = tx.scalar(select(func.count(SkillAliasCandidate.id)))
    resolver.resolve_all(tx, ["Golang", "PostgreSQL", "Kubernetes", "React"])
    tx.flush()
    assert tx.scalar(select(func.count(SkillAliasCandidate.id))) == before


def test_candidate_status_is_not_reset_by_a_later_sighting(tx):
    resolver.resolve(tx, "blockchain wizardry")
    tx.flush()
    row = tx.scalar(
        select(SkillAliasCandidate).where(SkillAliasCandidate.raw_norm == "blockchain wizardry")
    )
    row.status = CandidateStatus.rejected
    tx.flush()
    resolver.resolve(tx, "blockchain wizardry")
    tx.flush()
    tx.refresh(row)
    assert row.status is CandidateStatus.rejected
    assert row.seen_count == 2


# --------------------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------------------


def test_index_is_cached_and_invalidatable(tx):
    first = resolver.get_index(tx)
    assert resolver.get_index(tx) is first
    resolver.invalidate_cache()
    assert resolver.get_index(tx) is not first


def test_index_covers_the_whole_alias_table(tx):
    from jme.models import SkillAlias

    index = resolver.get_index(tx)
    assert index.size == tx.scalar(select(func.count(SkillAlias.id)))
    assert index.max_tokens >= 3

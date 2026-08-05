"""Seed a database with synthetic postings so `EXPLAIN ANALYZE` means something.

A ten-row table always sequential-scans, so a plan recorded against one proves
nothing about index choice. This script fills `posting`, `posting_requirement` and
`evidence_chunk` with a few thousand plausible rows, resolves the requirement spans
through the taxonomy, writes synthetic `match` and `match_citation` rows so the gap
rollup has both sides of its join, then `ANALYZE`s everything so the planner has real
statistics to work with.

Run `jme taxonomy seed` first: with no alias index, nothing resolves and the gap
rollup has nothing to group on. Nothing here calls an API - the resolver is
deterministic and the embeddings come from whichever provider is configured (`hash`
by default, which is offline).

Every row it writes is tagged `canonical_key LIKE 'synthetic-%'` (and evidence rows
`source_type = 'synthetic'`) so `--purge` can remove exactly what it created.

Usage::

    python tests/rank/seed_synthetic.py --postings 5000
    python tests/rank/seed_synthetic.py --postings 5000 --no-matches
    python tests/rank/seed_synthetic.py --purge
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.embeddings import get_provider

#: every table this script writes to or reports on, in dependency order
_TABLES = ("posting", "posting_requirement", "evidence_chunk", "match", "match_citation")

COMPANIES = [
    "Stripe", "Datadog", "Cloudflare", "Snowflake", "Rivian", "Ford", "GM", "Duo Security",
    "Grainger", "Morningstar", "Groupon", "Braintree", "Epic", "Palantir", "Ramp", "Plaid",
    "Two Sigma", "Citadel", "Jane Street", "Optiver", "Blue Origin", "SpaceX", "Anduril",
    "Roblox", "Databricks", "Confluent", "HashiCorp", "MongoDB", "Elastic", "Redis Labs",
]

TITLES = [
    "Software Engineer, New Grad", "Backend Engineer (New Grad)", "Platform Engineer I",
    "Data Infrastructure Engineer", "Site Reliability Engineer, University Grad",
    "Distributed Systems Engineer", "Full Stack Engineer, Early Career",
    "Machine Learning Engineer, New Grad", "Quantitative Developer",
    "Associate Product Manager", "Security Engineer I", "Systems Engineer, New Grad",
]

LOCATIONS = [
    "Detroit, MI", "Ann Arbor, MI", "Chicago, IL", "Remote in USA", "New York, NY",
    "San Francisco, CA", "Seattle, WA", "Austin, TX", "Boston, MA", "London, UK",
    "Toronto, ON", "Bangalore, India", "Michigan", "Remote", "Dearborn, MI",
]

SPONSORSHIPS = [
    "Offers Sponsorship", "Does Not Offer Sponsorship", "U.S. Citizenship is Required",
    "", "Offers Sponsorship", "Active Security Clearance Required", "Offshore Only",
]

ROLE_TYPES = ["swe", "swe", "swe", "quant", "pm", "hardware", "", "data", "wizardry"]

# Array lengths are deliberately coprime-ish with the other arrays (8 seasons vs 7
# sponsorships vs 9 role types): equal lengths would lock the modulo strides together
# and every posting with a given sponsorship would get the same season, which makes the
# funnel counts meaningless.
SEASONS = [
    "Summer 2027", "Fall 2027", "Spring 2027", "Summer 2026", "2027", "", "Winter 2028",
    "Fall 2026",
]

SKILLS = [
    "Proficiency in Python or Go", "Experience with PostgreSQL and query optimization",
    "Familiarity with Redis or another in-memory data store",
    "Experience building distributed systems at scale",
    "Strong understanding of data structures and algorithms",
    "Hands-on experience with Kubernetes and Docker",
    "Experience with AWS, GCP, or Azure", "Familiarity with CI/CD pipelines",
    "Experience with React and modern JavaScript", "Knowledge of SQL and relational modeling",
    "Exposure to machine learning frameworks such as PyTorch",
    "Experience writing and maintaining automated tests",
    "Understanding of message queues and event-driven architecture",
    "Excellent written and verbal communication skills",
    "Bachelor's degree in Computer Science or related field",
]

EVIDENCE_TEXTS = [
    "Built a Redis Streams consumer group harness in Go with XAUTOCLAIM-based reclaim of "
    "messages abandoned by dead workers, plus a dead letter stream after N attempts.",
    "Designed a Postgres schema with pgvector embeddings and an HNSW index, and tuned the "
    "active-feed query with EXPLAIN ANALYZE until it used a composite btree index.",
    "Wrote a per-host token bucket rate limiter backed by Redis so concurrent workers share "
    "one budget, with exponential backoff and a circuit breaker per host.",
    "Implemented ATS adapters for Greenhouse and Lever against their public JSON APIs, with "
    "a readability-based fallback extractor and typed error classes.",
    "Shipped a Python service using SQLAlchemy 2.0 and Alembic migrations, containerised with "
    "docker compose for local Postgres and Redis.",
    "Trained and evaluated PyTorch models for a course project, tracking precision and recall "
    "against a hand-labelled golden set.",
    "Built a React dashboard over a FastAPI backend, with server-side pagination and a typed "
    "OpenAPI client.",
    "Automated deployment with GitHub Actions, running pytest, ruff and mypy on every push and "
    "publishing container images.",
    "Ran Kubernetes workloads on AWS EKS, wrote Helm charts, and debugged a noisy-neighbour "
    "CPU throttling issue with cgroup metrics.",
    "Contributed a bug fix to the dapr project in Go, including a regression test and a design "
    "note in the pull request.",
]

_SEED_POSTINGS = """
WITH params AS (
    SELECT
        :companies ::text[]    AS companies,
        :titles ::text[]       AS titles,
        :locations ::text[]    AS locs,
        :sponsorships ::text[] AS sponsorships,
        :role_types ::text[]   AS roles,
        :seasons ::text[]      AS seasons
)
INSERT INTO posting (
    canonical_key, company, title, url, url_host, locations, sponsorship, role_type,
    start_season, is_remote, posted_at, first_seen_at, last_seen_at, inactive_at, repost_count
)
SELECT
    'synthetic-' || g,
    p.companies[1 + (g * 7)  % array_length(p.companies, 1)],
    p.titles   [1 + (g * 3)  % array_length(p.titles, 1)],
    'https://boards.greenhouse.io/synthetic/jobs/' || g,
    'boards.greenhouse.io',
    CASE WHEN g % 13 = 0 THEN NULL
         ELSE to_jsonb(ARRAY[
                p.locs[1 + (g * 11) % array_length(p.locs, 1)],
                p.locs[1 + (g * 29) % array_length(p.locs, 1)]
              ])
    END,
    NULLIF(p.sponsorships[1 + (g * 5)  % array_length(p.sponsorships, 1)], ''),
    NULLIF(p.roles       [1 + (g * 13) % array_length(p.roles, 1)], ''),
    NULLIF(p.seasons     [1 + (g * 17) % array_length(p.seasons, 1)], ''),
    (g % 9 = 0),
    now() - ((g % 120) || ' days')::interval,
    now() - ((g % 120) || ' days')::interval,
    now(),
    CASE WHEN g % 5 = 0 THEN now() - interval '3 days' ELSE NULL END,
    g % 3
FROM generate_series(1, :n) AS g, params p
ON CONFLICT (canonical_key) DO NOTHING
"""

_SEED_REQUIREMENTS = """
WITH params AS (SELECT :skills ::text[] AS skills)
INSERT INTO posting_requirement (
    posting_id, canonical_skill_id, raw_text, importance, confidence, prompt_version, created_at
)
SELECT
    p.id,
    NULL,
    params.skills[1 + ((p.id * 5 + k * 7) % array_length(params.skills, 1))],
    (ARRAY['required', 'required', 'preferred', 'mentioned']::importance[])[1 + (k % 4)],
    0.800,
    'v1',
    now()
FROM posting p, generate_series(1, :per_posting) AS k, params
WHERE p.canonical_key LIKE 'synthetic-%' AND p.id % 3 <> 0
ON CONFLICT DO NOTHING
"""


_RESOLVE_REQUIREMENTS = """
UPDATE posting_requirement SET canonical_skill_id = :skill_id
WHERE raw_text = :raw_text AND canonical_skill_id IS NULL
"""

# A match on every fourth eligible posting, one in nine of them stale. The stale ones
# matter: the rollup has to ignore them, and a plan recorded against a table with none
# would not show the filter doing anything.
_SEED_MATCHES = """
INSERT INTO match (posting_id, evidence_version, prompt_version, model_id, jd_sha256,
                   score, verdict, rationale, is_stale, computed_at,
                   input_tokens, output_tokens, cost_usd)
SELECT p.id, 1, 'v1', 'synthetic', md5(p.canonical_key), 0.72,
       'plausible'::verdict, '{"summary": "synthetic"}'::jsonb,
       (p.id % 9 = 0), now(), 2400, 600, 0.012
FROM posting p
WHERE p.canonical_key LIKE 'synthetic-%' AND p.inactive_at IS NULL AND p.id % 4 = 0
ON CONFLICT DO NOTHING
"""

# One citation per distinct skill the posting asks for, cycling evidenced/weak/absent
# so the "best status ever" aggregate has all three to choose between.
_SEED_CITATIONS = """
INSERT INTO match_citation (match_id, canonical_skill_id, evidence_chunk_id, status, reasoning)
SELECT m.id,
       r.canonical_skill_id,
       CASE WHEN (m.id + r.canonical_skill_id) % 3 = 0 THEN c.id ELSE NULL END,
       (ARRAY['evidenced','weak','absent']::citation_status[])[
           1 + ((m.id + r.canonical_skill_id) % 3)],
       'synthetic'
FROM match m
JOIN (
    SELECT DISTINCT posting_id, canonical_skill_id
    FROM posting_requirement
    WHERE canonical_skill_id IS NOT NULL
) r ON r.posting_id = m.posting_id
CROSS JOIN LATERAL (SELECT id FROM evidence_chunk ORDER BY id LIMIT 1) c
ON CONFLICT DO NOTHING
"""


def resolve_requirements(session: Session) -> int:
    """Attach canonical skills to the seeded spans, the way the enricher would.

    Without this every `canonical_skill_id` is NULL, the gap rollup groups on nothing
    and its recorded plan is a plan over an empty join. Nothing here is an API call:
    the resolver is the deterministic alias index from the taxonomy seed. Candidate
    recording is off, because a synthetic span that does not resolve is not a real
    review-queue item.
    """
    from jme.taxonomy import resolver

    raws = [
        r[0]
        for r in session.execute(
            text("SELECT DISTINCT raw_text FROM posting_requirement WHERE canonical_skill_id IS NULL")
        )
    ]
    if not raws:
        return 0
    updated = 0
    for raw, skill_id in zip(raws, resolver.resolve_all(session, raws, record_candidates=False),
                             strict=True):
        if skill_id is None:
            continue
        updated += session.execute(
            text(_RESOLVE_REQUIREMENTS), {"skill_id": skill_id, "raw_text": raw}
        ).rowcount
    return updated


def seed_matches(session: Session) -> None:
    """Synthetic match/citation rows so the gap rollup has both sides of its join."""
    session.execute(text(_SEED_MATCHES))
    session.execute(text(_SEED_CITATIONS))


def seed(
    session: Session, postings: int, per_posting: int, chunks: int, matches: bool = True
) -> dict[str, int]:
    session.execute(
        text(_SEED_POSTINGS),
        {
            "n": postings,
            "companies": COMPANIES,
            "titles": TITLES,
            "locations": LOCATIONS,
            "sponsorships": SPONSORSHIPS,
            "role_types": ROLE_TYPES,
            "seasons": SEASONS,
        },
    )
    session.execute(text(_SEED_REQUIREMENTS), {"per_posting": per_posting, "skills": SKILLS})

    provider = get_provider()
    texts = [EVIDENCE_TEXTS[i % len(EVIDENCE_TEXTS)] + f" (variant {i})" for i in range(chunks)]
    vectors = provider.embed(texts)
    session.execute(
        text(
            """
            INSERT INTO evidence_chunk (
                source_type, source_ref, heading, ordinal, text, text_sha256, token_estimate,
                embedding, embedding_model, evidence_version, created_at, updated_at
            )
            VALUES (
                'synthetic', :source_ref, :heading, :ordinal, :text, md5(:text), :tokens,
                :embedding ::vector, :model, 1, now(), now()
            )
            ON CONFLICT (source_type, source_ref, ordinal) DO NOTHING
            """
        ),
        [
            {
                "source_ref": f"synthetic/evidence-{i // 10}.md",
                "heading": f"Project {i // 10}",
                "ordinal": i,
                "text": body,
                "tokens": len(body) // 4,
                "embedding": "[" + ",".join(f"{v:.6g}" for v in vec) + "]",
                "model": provider.name,
            }
            for i, (body, vec) in enumerate(zip(texts, vectors, strict=True))
        ],
    )
    session.execute(
        text(
            """
            INSERT INTO evidence_version (id, version, bumped_at, reason)
            VALUES (1, 1, now(), 'synthetic seed')
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    session.commit()

    if matches:
        resolve_requirements(session)
        seed_matches(session)
        session.commit()

    for table in _TABLES:
        session.execute(text(f"ANALYZE {table}"))
    session.commit()

    return _counts(session)


def purge(session: Session) -> dict[str, int]:
    # match and match_citation hang off posting with ON DELETE CASCADE, so deleting the
    # synthetic postings takes the synthetic matches with them.
    session.execute(text("DELETE FROM posting WHERE canonical_key LIKE 'synthetic-%'"))
    session.execute(text("DELETE FROM evidence_chunk WHERE source_type = 'synthetic'"))
    session.commit()
    for table in _TABLES:
        session.execute(text(f"ANALYZE {table}"))
    session.commit()
    return _counts(session)


def _counts(session: Session) -> dict[str, int]:
    return {
        table: int(session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in _TABLES
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="defaults to settings.database_url")
    parser.add_argument("--postings", type=int, default=5000)
    parser.add_argument("--requirements-per-posting", type=int, default=6)
    parser.add_argument("--chunks", type=int, default=120)
    parser.add_argument("--purge", action="store_true", help="delete synthetic rows and exit")
    parser.add_argument(
        "--no-matches",
        dest="matches",
        action="store_false",
        help="skip taxonomy resolution and synthetic match/citation rows",
    )
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    engine = create_engine(url, future=True)
    with Session(engine) as session:
        counts = (
            purge(session)
            if args.purge
            else seed(
                session,
                args.postings,
                args.requirements_per_posting,
                args.chunks,
                matches=args.matches,
            )
        )
    print(f"{'purged' if args.purge else 'seeded'} {url}")
    for table, n in counts.items():
        print(f"  {table:22s} {n:>8d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

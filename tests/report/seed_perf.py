"""Seed a realistic-volume database so the gap-report timing means something.

A 2-second budget measured against a 24-row fixture is not a measurement, it is a
formality. This builds a database the size the real system will actually reach - a few
thousand postings, tens of thousands of requirements, a real taxonomy, and thousands of
match citations - and then times the rollup against it.

Deliberately writes to its own database (default `jme_perf`) so it never collides with
the dev database or with another engineer's test run.

    ./.venv/Scripts/python.exe tests/report/seed_perf.py --reset
    ./.venv/Scripts/python.exe tests/report/seed_perf.py --explain --runs 5

Shape of the generated data, chosen to look like the real feed rather than to be easy:
  * skill demand follows a Zipf-ish curve: a handful of skills in most postings, a long
    tail in a few. A uniform distribution would make every plan look like a clean hash
    aggregate over evenly sized groups.
  * 12% of requirements have no canonical skill, matching the taxonomy-coverage hole a
    real extraction run leaves behind.
  * ~15% of postings are inactive and ~8% fail the eligibility filters, so the eligible
    CTE actually filters something.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import time

from sqlalchemy import create_engine, insert, text
from sqlalchemy.orm import Session

from jme.models import (
    Base,
    CanonicalSkill,
    EvidenceChunk,
    EvidenceVersion,
    Match,
    MatchCitation,
    Posting,
    PostingJD,
    PostingRequirement,
    RunMetric,
)
from jme.report.gap import build_gap_report, explain_gap_query

DEFAULT_URL = "postgresql+psycopg://jme:jme@localhost:5433/jme_perf"

CATEGORIES = ["language", "database", "cloud", "infra", "ml", "framework", "practice", "domain"]
ADAPTERS = ["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "fallback"]
ADAPTER_SUCCESS = {
    "greenhouse": 0.97,
    "lever": 0.95,
    "ashby": 0.88,
    "smartrecruiters": 0.82,
    "workday": 0.35,
    "fallback": 0.55,
}
IMPORTANCES = ["required"] * 40 + ["preferred"] * 35 + ["mentioned"] * 25
EVIDENCE_VERSION = 4


def _ensure_database(url: str) -> None:
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
        ).scalar()
        if not exists:
            conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
    admin.dispose()


def seed(
    session: Session,
    *,
    postings: int,
    skills: int,
    reqs_per_posting: int,
    matched_postings: int,
    rng: random.Random,
) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    counts: dict[str, int] = {}

    session.add(EvidenceVersion(id=1, version=EVIDENCE_VERSION, reason="perf seed"))
    session.flush()

    skill_rows = [
        {
            "name": f"Skill {i:03d}",
            "category": rng.choice(CATEGORIES) if i % 7 else "soft",
            # ~14% non-actionable, the soft-skill share of a real taxonomy
            "is_actionable": i % 7 != 0,
        }
        for i in range(1, skills + 1)
    ]
    session.execute(insert(CanonicalSkill), skill_rows)
    skill_ids = [r[0] for r in session.execute(text("SELECT id FROM canonical_skill ORDER BY id"))]
    counts["canonical_skill"] = len(skill_ids)

    # Zipf-ish weights: skill 1 appears in most postings, skill 200 in a handful
    weights = [1.0 / (rank**0.85) for rank in range(1, len(skill_ids) + 1)]

    posting_rows = []
    for i in range(1, postings + 1):
        first_seen = now - dt.timedelta(days=rng.uniform(0, 60), hours=rng.uniform(0, 24))
        inactive = rng.random() < 0.15
        posting_rows.append(
            {
                "canonical_key": f"perf-{i:06d}",
                "simplify_id": f"simplify-{i:06d}",
                "company": f"Company {i % 800:03d}",
                "title": f"Software Engineer, New Grad {i}",
                "url": f"https://boards.greenhouse.io/c{i % 800}/jobs/{i}",
                "url_host": "boards.greenhouse.io",
                "locations": rng.choice(
                    [
                        ["Remote"],
                        ["Chicago, IL"],
                        ["Detroit, MI", "Remote"],
                        ["San Francisco, CA"],
                        ["New York, NY", "Austin, TX"],
                    ]
                ),
                "sponsorship": rng.choices(
                    [None, "Offers sponsorship", "Does not offer sponsorship", "U.S. Citizenship required"],
                    weights=[0.5, 0.42, 0.05, 0.03],
                )[0],
                "role_type": rng.choices(["swe", "quant", "pm"], weights=[0.92, 0.05, 0.03])[0],
                "is_remote": rng.random() < 0.3,
                "posted_at": first_seen,
                "first_seen_at": first_seen,
                "last_seen_at": now - dt.timedelta(days=rng.uniform(0, 3)),
                "inactive_at": (first_seen + dt.timedelta(days=rng.uniform(1, 20)))
                if inactive
                else None,
                "repost_count": rng.choices([0, 1, 2, 5], weights=[0.8, 0.12, 0.06, 0.02])[0],
            }
        )
    session.execute(insert(Posting), posting_rows)
    posting_ids = [r[0] for r in session.execute(text("SELECT id FROM posting ORDER BY id"))]
    counts["posting"] = len(posting_ids)

    jd_rows = []
    for pid in posting_ids:
        if rng.random() > 0.88:  # 12% never even got a fetch attempt recorded
            continue
        adapter = rng.choices(ADAPTERS, weights=[0.35, 0.25, 0.12, 0.08, 0.12, 0.08])[0]
        ok = rng.random() < ADAPTER_SUCCESS[adapter]
        jd_rows.append(
            {
                "posting_id": pid,
                "adapter": adapter,
                "raw_text": ("job description text " * 120) if ok else None,
                "text_sha256": f"sha-{pid:08d}",
                "fetch_status": "ok" if ok else rng.choice(["permanent_error", "not_found"]),
                "char_count": 2400 if ok else 0,
                "attempts": 1 if ok else 3,
                "extracted_at": now if ok else None,
                "updated_at": now,
            }
        )
    session.execute(insert(PostingJD), jd_rows)
    counts["posting_jd"] = len(jd_rows)

    req_rows = []
    for pid in posting_ids:
        n = max(4, int(rng.gauss(reqs_per_posting, 3)))
        chosen = rng.choices(skill_ids, weights=weights, k=n)
        seen: set[str] = set()
        for j, sid in enumerate(chosen):
            unmapped = rng.random() < 0.12
            raw = f"requirement {j} for skill {sid if not unmapped else 'x'}"
            if raw in seen:  # uq_requirement_posting_raw_prompt
                continue
            seen.add(raw)
            req_rows.append(
                {
                    "posting_id": pid,
                    "canonical_skill_id": None if unmapped else sid,
                    "raw_text": raw,
                    "importance": rng.choice(IMPORTANCES),
                    "confidence": round(rng.uniform(0.5, 0.99), 3),
                    "prompt_version": "v1",
                    "model_id": "claude-opus-5",
                    "created_at": now,
                }
            )
    for start in range(0, len(req_rows), 5000):
        session.execute(insert(PostingRequirement), req_rows[start : start + 5000])
    counts["posting_requirement"] = len(req_rows)

    chunk_rows = [
        {
            "source_type": "markdown",
            "source_ref": f"evidence/note-{i:03d}.md",
            "ordinal": i,
            "text": f"evidence chunk {i}",
            "text_sha256": f"chunk-{i:06d}",
            "token_estimate": 300,
            "evidence_version": EVIDENCE_VERSION,
            "created_at": now,
            "updated_at": now,
        }
        for i in range(300)
    ]
    session.execute(insert(EvidenceChunk), chunk_rows)
    chunk_ids = [r[0] for r in session.execute(text("SELECT id FROM evidence_chunk ORDER BY id"))]
    counts["evidence_chunk"] = len(chunk_ids)

    matched = rng.sample(posting_ids, min(matched_postings, len(posting_ids)))
    match_rows = [
        {
            "posting_id": pid,
            "evidence_version": EVIDENCE_VERSION,
            "prompt_version": "v1",
            "model_id": "claude-opus-5",
            "jd_sha256": f"jd-{pid:08d}",
            "score": round(rng.uniform(0.1, 0.95), 4),
            "verdict": rng.choice(["strong", "plausible", "stretch", "no"]),
            "is_stale": rng.random() < 0.2,
            "input_tokens": rng.randint(2000, 9000),
            "output_tokens": rng.randint(200, 900),
            "cost_usd": round(rng.uniform(0.005, 0.04), 6),
            "computed_at": now - dt.timedelta(days=rng.uniform(0, 10)),
        }
        for pid in matched
    ]
    session.execute(insert(Match), match_rows)
    match_ids = [r[0] for r in session.execute(text("SELECT id FROM match ORDER BY id"))]
    counts["match"] = len(match_ids)

    # citations skew towards the head of the skill distribution, like real matches do
    citation_rows = []
    for mid in match_ids:
        for sid in rng.choices(skill_ids, weights=weights, k=rng.randint(5, 12)):
            status = rng.choices(["evidenced", "weak", "absent"], weights=[0.2, 0.3, 0.5])[0]
            citation_rows.append(
                {
                    "match_id": mid,
                    "canonical_skill_id": sid,
                    "evidence_chunk_id": rng.choice(chunk_ids) if status == "evidenced" else None,
                    "status": status,
                    "reasoning": "seeded",
                }
            )
    for start in range(0, len(citation_rows), 5000):
        session.execute(insert(MatchCitation), citation_rows[start : start + 5000])
    counts["match_citation"] = len(citation_rows)

    session.execute(
        insert(RunMetric),
        [
            {
                "run_id": f"perf-{i // 20:04d}",
                "stage": rng.choice(["ingest", "fetch", "enrich", "rank", "match", "report"]),
                "metric": rng.choice(["duration_sec", "rows", "cost_usd", "cache_hit_rate"]),
                "value": round(rng.uniform(0, 100), 6),
                "labels": {"seeded": True},
                "recorded_at": now - dt.timedelta(hours=i),
            }
            for i in range(400)
        ],
    )
    counts["run_metric"] = 400
    session.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_URL)
    parser.add_argument("--postings", type=int, default=3000)
    parser.add_argument("--skills", type=int, default=200)
    parser.add_argument("--reqs-per-posting", type=int, default=13)
    parser.add_argument("--matched-postings", type=int, default=800)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    parser.add_argument("--skip-seed", action="store_true", help="only measure, do not insert")
    parser.add_argument("--explain", action="store_true", help="print EXPLAIN ANALYZE")
    parser.add_argument("--runs", type=int, default=3, help="how many timed report runs")
    args = parser.parse_args()

    _ensure_database(args.database_url)
    engine = create_engine(args.database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    if args.reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    if not args.skip_seed:
        started = time.perf_counter()
        with Session(engine) as session:
            counts = seed(
                session,
                postings=args.postings,
                skills=args.skills,
                reqs_per_posting=args.reqs_per_posting,
                matched_postings=args.matched_postings,
                rng=random.Random(args.seed),
            )
        print(f"seeded in {time.perf_counter() - started:.1f}s")
        for table, n in counts.items():
            print(f"  {table:24s} {n:>8,}")
        with engine.begin() as conn:
            conn.exec_driver_sql("ANALYZE")

    with Session(engine) as session:
        timings = []
        for _ in range(args.runs):
            report = build_gap_report(session)
            timings.append(report.query_seconds)
        print()
        print(f"eligible postings        {report.eligible_posting_count:>8,}")
        print(f"requirements (eligible)  {report.total_requirement_count:>8,}")
        print(f"taxonomy coverage        {report.taxonomy_coverage:>8.1%}")
        print(f"actionable skills        {report.actionable_skill_count:>8,}")
        print(f"ranked gaps              {len(report.gaps):>8,}")
        print(
            "query seconds            "
            f"min {min(timings):.3f}  median {sorted(timings)[len(timings) // 2]:.3f}  "
            f"max {max(timings):.3f}"
        )
        print()
        print("top 10 gaps:")
        for i, gap in enumerate(report.top_gaps(10), start=1):
            print(
                f"  {i:>2}. {gap.skill:<12} required={gap.required_count:<5} "
                f"postings={gap.posting_count:<5} status={gap.status}"
            )
        if args.explain:
            print()
            print(explain_gap_query(session))


if __name__ == "__main__":
    main()

# Job Match Engine

[![CI](https://github.com/AdilRMallick/Resume_builder/actions/workflows/ci.yml/badge.svg)](https://github.com/AdilRMallick/Resume_builder/actions/workflows/ci.yml)

Personal system that ingests the SimplifyJobs new-grad feed, resolves job descriptions,
matches them against an evidence corpus of my actual work, and reports aggregate skill
gaps.

The point is not "a job board scraper". The point is the last step: **a ranked list of
skills that appear as `required` across the roles I am actually eligible for, and that
have no evidence backing them in my corpus.** That list is a study plan.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and the reasoning behind it, and
[BUILD_PLAN.md](BUILD_PLAN.md) for the milestone breakdown.

---

## Architecture

Three processes plus an API. No shared code between services; the contract is Redis
Streams plus Postgres.

```
                    ┌───────────────────────────────────────────────┐
   cron ───────────▶│ ingestor (Python)                             │
                    │ listings.json → upsert posting → XADD fetch    │
                    └───────────────────┬───────────────────────────┘
                                        │
                              Redis Stream  jme:fetch
                                        │  (consumer group: fetchers)
                                        ▼
                    ┌───────────────────────────────────────────────┐
                    │ fetcher (Go)                                  │
                    │ per-host token bucket, circuit breaker,       │
                    │ robots.txt, ATS adapters → posting_jd         │
                    └───────────────────┬───────────────────────────┘
                                        │
                              Redis Stream  jme:enrich
                                        │  (consumer group: enrichers)
                                        ▼
                    ┌───────────────────────────────────────────────┐
                    │ enricher (Python)                             │
                    │ requirement extraction → taxonomy resolve →   │
                    │ embed → filter+rank → LLM match → gap rollup  │
                    └───────────────────┬───────────────────────────┘
                                        │
                    ┌───────────────────▼───────────────────────────┐
                    │ Postgres 16 + pgvector                        │
                    │ posting · posting_jd · canonical_skill ·      │
                    │ posting_requirement · evidence_chunk ·        │
                    │ match · match_citation · run_metric           │
                    └───────────────────┬───────────────────────────┘
                                        │
                              api (FastAPI, read-only)
```

**Why Redis Streams and not a list queue.** A list gives no record that a message was
ever handed out — if the worker holding it dies, the work is gone. A stream's consumer
group keeps a Pending Entries List: `XACK` marks completion only after the database
write commits, `XAUTOCLAIM` transfers messages idle past a visibility timeout to a live
worker, and `XPENDING` shows what is stuck and for how long. Delivery is at-least-once
by choice — the alternative is at-most-once, which silently drops work — so every
handler is idempotent and every write is an upsert.

**Why Go for the fetcher only.** Fetching a few hundred URLs across a dozen hosts with
per-host rate limits, timeouts, and cancellation is the shape Go is good at. It is a
clean service boundary, so the language split costs nothing architecturally.

---

## Quick start

Requires Docker, Python 3.11+, and Go 1.23+.

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY if you want LLM stages
make up                       # Postgres (pgvector) on :5433, Redis on :6380
make install                  # venv + editable install with dev extras
make migrate                  # apply Alembic migrations
jme status                    # verify connectivity
```

Nothing above needs an API key. The default embedding provider is `hash` — a
deterministic offline hash embedder — so the whole pipeline runs, and the whole test
suite passes, with no credentials and no network.

**No Docker?** `sudo make dev-setup` installs Postgres with pgvector and Redis natively
on Debian/Ubuntu and binds them to the same ports docker-compose publishes (5433 and
6380), so nothing else has to change. `make verify-services` checks both are reachable.
Use it on CI runners and cloud sandboxes; on a normal dev machine prefer `make up`.

---

## The pipeline, end to end

```bash
jme taxonomy seed             # load the curated canonical skill vocabulary
jme ingest run                # pull the feed, upsert postings, enqueue fetch jobs
make fetcher && ./bin/fetcher # resolve job descriptions (Go worker)
jme enrich worker             # extract structured requirements from JD text
jme evidence ingest           # chunk + embed your evidence corpus
jme rank run                  # hard filters, then vector ranking → shortlist
jme match shortlist           # LLM match with citations back to evidence chunks
jme report gap                # ← the payoff
```

Every stage is independently runnable and independently useful.

For the same numbers in a browser:

```bash
jme serve start               # then open http://127.0.0.1:8000
```

The dashboard at `/` is a read-only view of the gap report, the shortlist, and queue
depth. It is one HTML file with no build step, no framework and no CDN, and it holds no
data of its own — every panel fetches the same JSON endpoints the CLI uses, so it cannot
show a number the API disagrees with.

---

## Layout

```
jme/                  Python services
  config.py           all tunables, one place, env-driven
  models.py           SQLAlchemy schema — the contract every service codes against
  queue.py            Redis Streams producer + consumer group (Python side)
  llm.py              the only Anthropic entry point: structured output, cache, cost
  embeddings.py       pluggable embedding providers (hash | voyage)
  ingestor/           feed ingestion and posting lifecycle
  taxonomy/           canonical skills, alias resolution, review queue
  enricher/           LLM requirement extraction
  evidence/           corpus chunking, embedding, versioning
  rank/               hard filters + vector ranking
  matcher/            LLM match with mandatory citations
  report/             the aggregate gap report
  api/                read-only FastAPI over Postgres

fetcher/              Go fetch service
  internal/domain/    types shared across the process boundary
  internal/queue/     Redis Streams consumer group (Go side)
  internal/httpx/     per-host token bucket, backoff, circuit breaker, robots.txt
  internal/adapters/  ATS adapters + registry
  internal/store/     Postgres write path

migrations/           Alembic — every schema change, no manual DDL
docs/explain/         committed EXPLAIN ANALYZE output for the load-bearing queries
```

---

## Design decisions worth knowing about

**Postings are never hard deleted.** A posting has `first_seen_at`, `last_seen_at`, and
`inactive_at`. A canonical key that disappears from the feed is deactivated; if it comes
back, `inactive_at` clears and `repost_count` increments. Repost count is itself signal —
roles that repost repeatedly are either high churn or perpetually open.

**The LLM does not get to create canonical skills.** Extraction maps raw requirement
text onto a curated taxonomy through a deterministic resolver. Anything with no
confident match goes to a `skill_alias_candidate` review queue for batch approval.
Auto-creation causes taxonomy drift within a week and silently breaks every aggregate
number in the report.

**Requirements store the literal span.** `posting_requirement.raw_text` must be a
verbatim substring of the job description, verified after the model returns. A
paraphrase is rejected and retried. Without this the report is unauditable.

**Cache keys include `prompt_version`.** Every LLM cache key is
`sha256(input) + evidence_version + prompt_version + model_id`. Without the prompt
version, editing a prompt silently reuses stale results and match quality cannot be
compared across iterations.

**Evidence has a monotonic version.** Any corpus change bumps it and marks matches for
active postings stale. Recompute is lazy and bounded. Without this the system keeps
reporting gaps that have already been closed.

**Fetching is deliberately polite.** Honest user agent with a contact URL, per-host
token bucket in Redis shared across all worker processes, exponential backoff with
jitter, circuit breaker per host, robots.txt respected, and a permanent cache by posting
id — a successful resolution is never refetched.

---

## Instrumentation

Everything lands in `run_metric`, so every claim about this project has a number behind
it:

| Metric | Where |
|---|---|
| Adapter success rate, overall and per adapter | `jme report coverage` |
| Fetch latency and outcome, per host | fetcher → `run_metric` |
| Queue depth and pending message age | `GET /ops/queue` |
| Dead letter rate | `jme:fetch:dead`, `jme:enrich:dead` |
| LLM cache hit rate | `jme match cost` |
| Tokens and dollars per run, per stage | `jme enrich cost`, `jme match cost` |
| Stage-by-stage funnel counts | `jme rank funnel` |
| Gap report query runtime | `docs/explain/gap_report.md` |

---

## Testing

```bash
make test          # both suites
make test-py       # pytest
make test-go       # go test (needs `make up` first)
```

No test hits a paid API or the live network. LLM calls are stubbed at a single named
seam, HTTP is served from recorded fixtures, and embeddings default to the offline hash
provider. The Go queue and rate-limiter tests do use the real Redis from
`docker compose`, because the guarantees they assert — reclaiming a dead worker's
messages, two processes sharing one token bucket — are not meaningfully testable
against a fake.

---

## Explicitly out of scope

Auto-apply, resume keyword injection, multi-user hosting, and auth. See
[ARCHITECTURE.md](ARCHITECTURE.md) section 10 for why.

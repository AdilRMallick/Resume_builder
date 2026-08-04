# Build Plan

Companion to ARCHITECTURE.md. Milestones are ordered so each one is independently
useful. Task briefs are written to be handed directly to a coding agent.

**Division of labor**: give agents the mechanical, well-specified work (adapters,
schema migrations, parsers, test harnesses). Keep for yourself the work that
generates interview stories (queue semantics, index design, taxonomy curation,
prompt iteration). If an agent writes the Redis consumer group logic, you cannot
credibly claim to understand it.

---

## M0: Feed ingestion
**Ship criteria**: cron pulls the feed, new postings land in Postgres, `git log`
style diff shows what changed since last run.

Useful on day one even with nothing else built.

## M1: Fetch pipeline
**Ship criteria**: Greenhouse and Lever postings resolve to JD text, workers survive
being killed mid-job, adapter coverage percentage is reported.

## M2: Evidence corpus and ranking
**Ship criteria**: postings ranked by relevance to my actual evidence, hard filters
applied, top 20 surfaced.

## M3: LLM matching
**Ship criteria**: structured match with per-skill citations back to evidence chunks.

## M4: Aggregate gap report
**Ship criteria**: ranked list of canonical skills that appear as `required` across
eligible postings with no evidence backing. The payoff.

## M5: Optional extensions
Workday adapter, daily digest email, HackerRank scorer as second-opinion signal,
web UI.

---

# Agent task briefs

Each brief is self-contained. Paste one at a time. Do not batch them.

---

## TASK 1: Feed ingestor

**Goal**: Poll the SimplifyJobs new-grad feed and upsert postings into Postgres.

**Context**: `SimplifyJobs/New-Grad-Positions` publishes a `listings.json` updated by
a GitHub Action several times daily. Locate the file's actual path in the repo before
writing code; do not assume it. Do not parse the README, it is a 545KB markdown file
and parsing it is wasted effort.

**Requirements**
- Fetch `listings.json` from the raw GitHub URL. Handle 404 and malformed JSON.
- Compute a canonical key per posting: prefer the Simplify id, else
  `sha256(normalize(company) + normalize(title) + url_host + url_path)`.
- Upsert into `posting`. On conflict update `last_seen_at` and mutable fields.
- Set `inactive_at` for canonical keys present in the last run but absent now.
- If a key reappears with `inactive_at` set, clear it and increment `repost_count`.
- Never hard delete.
- Emit a run summary: new, updated, deactivated, reactivated counts.

**Files**: `ingestor/`, Alembic migration for `posting`.

**Acceptance**
- Running twice in a row produces zero new rows on the second run.
- Unit test with a fixture where a posting disappears and then returns, asserting
  `repost_count` increments and `inactive_at` clears.
- Idempotent under concurrent execution.

**Out of scope**: fetching job description text, any LLM call, any UI.

---

## TASK 2: ATS adapter interface plus Greenhouse and Lever

**Goal**: Given a posting URL, return structured job description text.

**Requirements**
- Define an adapter interface: `Detect(url) bool` and
  `Fetch(ctx, url) (JobDescription, error)`.
- `JobDescription` carries: raw text, title, location, and the adapter name.
- Implement Greenhouse and Lever using their public JSON APIs. Do not scrape HTML
  for these two.
- Implement a `FallbackAdapter` using HTTP fetch plus readability-style main content
  extraction.
- Registry that dispatches by URL host, falling back when no adapter matches.
- Every adapter respects a passed-in `context.Context` for timeout and cancellation.
- Return typed errors distinguishing: not found, rate limited, transient, permanent.

**Files**: `fetcher/adapters/`.

**Acceptance**
- Table-driven tests per adapter using recorded HTTP fixtures, no live network in
  tests.
- Test that a cancelled context aborts an in-flight fetch.
- Adding a new adapter requires touching only the registry.

**Out of scope**: the queue, rate limiting, Workday, database writes.

---

## TASK 3: Rate limiter and HTTP client

**Goal**: Shared per-host rate limiting across multiple concurrent workers.

**Requirements**
- Token bucket per URL host, state in Redis so limits hold across worker processes.
- Default 1 request per 2 seconds per host, configurable per host.
- Exponential backoff with jitter on 429 and 5xx.
- Circuit breaker: open a host after N consecutive failures, half-open after a
  cooldown.
- Honest user agent with a contact URL, set from config.
- Check and cache robots.txt per host, respect disallow rules.

**Files**: `fetcher/httpx/`.

**Acceptance**
- Integration test with a real Redis (testcontainers or a local instance) showing two
  concurrent workers against the same host collectively respect the limit.
- Circuit breaker test asserting requests are rejected without a network call while
  open.

**Out of scope**: adapters, queue consumption.

---

## TASK 4: Redis Streams consumer group harness

**NOTE: consider writing this one yourself.** This is the component with the best
interview story in the project. If an agent writes it, read every line and be able to
explain `XAUTOCLAIM` semantics from memory before you claim it.

**Goal**: Reliable job consumption with recovery from dead workers.

**Requirements**
- Producer: `XADD` to a stream with a job payload.
- Consumer group with a per-worker consumer name.
- `XREADGROUP` loop with a blocking read.
- `XACK` only after successful processing and commit.
- Periodic `XAUTOCLAIM` sweep to reclaim messages pending longer than the visibility
  timeout.
- Retry counter per message; after N attempts move to a dead letter stream with the
  failure reason.
- Idempotency: processing the same message twice must not duplicate rows.
- Graceful shutdown on SIGTERM: stop reading, finish in-flight work, then exit.

**Files**: `fetcher/queue/`.

**Acceptance**
- Test that `SIGKILL`ing a worker mid-job results in the message being reclaimed and
  completed by another worker.
- Test that a permanently failing job lands in the dead letter stream after exactly N
  attempts.
- Test that redelivery does not duplicate database rows.

**Out of scope**: adapters, matching.

---

## TASK 5: Taxonomy seed and alias matching

**Goal**: Curated canonical skill vocabulary with deterministic alias resolution.

**Requirements**
- Seed `canonical_skill` with 150 to 250 entries across categories: language,
  database, cloud, infra, ml, framework, practice, soft.
- Mark `is_actionable` false for entries like "strong communication" so they can be
  excluded from the gap report.
- Seed `skill_alias` with common variants. "Golang" maps to "Go", "Postgres" and
  "PostgreSQL" map to one node, and so on.
- Resolver function: raw text in, canonical skill id or null out. Case insensitive,
  punctuation insensitive, word boundary aware so "Go" does not match "Django" or
  "Mongo".
- On no match, insert or increment a `skill_alias_candidate` row.
- CLI to review candidates: list by `seen_count` descending, approve to an existing
  canonical skill, promote to a new canonical skill, or reject.

**Acceptance**
- Test suite of at least 50 tricky raw strings with expected resolutions, including
  the "Go" substring cases.
- The resolver never creates a canonical skill. Only the CLI can.

**Out of scope**: LLM extraction, which is Task 6.

---

## TASK 6: Requirement extraction

**Goal**: Turn JD text into structured requirement rows.

**Requirements**
- Anthropic API call with structured output: list of
  `{raw_text, importance, confidence}` where importance is `required`, `preferred`,
  or `mentioned`.
- `raw_text` must be a literal span from the JD, verified by substring check.
  Reject and retry once if the model paraphrases.
- Pass each `raw_text` through the Task 5 resolver to attach `canonical_skill_id`.
- Store `prompt_version` on every row.
- Cache by `sha256(jd_text) + prompt_version + model_id`.
- Record tokens and cost per call into `run_metric`.

**Acceptance**
- Golden file test: 5 real JDs with hand-labelled expected requirements, asserting
  recall above a threshold you set after seeing baseline output.
- Verify no extraction result is missing `prompt_version`.

**Out of scope**: matching against evidence.

---

## TASK 7: Evidence corpus ingestion and embedding

**Goal**: Build and embed the personal evidence corpus.

**Requirements**
- Ingest from: a directory of markdown files, plus GitHub README files for a
  configured list of repos.
- Chunk on semantic boundaries (headings, bullet groups), target 200 to 500 tokens,
  never split a bullet.
- Embed and store in `evidence_chunk` with pgvector.
- Bump `evidence_version` on any insert, update, or delete.
- On version bump, mark all `match` rows for active postings `is_stale = true`.
- CLI to manually tag a chunk with canonical skills.

**Acceptance**
- Re-running ingestion with unchanged inputs does not bump `evidence_version`.
- Test that a single chunk edit marks matches stale but does not delete them.

**Out of scope**: retrieval or ranking.

---

## TASK 8: Filter and rank pipeline

**Goal**: Reduce all active postings to a ranked shortlist.

**Requirements**
- Stage 1, SQL hard filters: `inactive_at IS NULL`, sponsorship allowlist, location
  allowlist plus remote, role type, start season where extractable.
- Stage 2, vector similarity between posting requirements and evidence chunks,
  producing a coarse relevance score.
- Weight `required` requirements above `preferred` in the score.
- Output the top N, N configurable, default 20.
- Emit stage counts into `run_metric`: total, after filters, after ranking.

**Acceptance**
- `EXPLAIN ANALYZE` output for the stage 1 query committed to the repo with a note
  on which indexes it used.
- Deterministic output given fixed inputs and a fixed evidence version.

**Out of scope**: LLM matching.

---

## TASK 9: LLM match with citations

**Goal**: Per-posting structured match grounded in specific evidence.

**Requirements**
- For each shortlisted posting, retrieve the top K evidence chunks per requirement.
- Single structured call returning, per requirement:
  `{canonical_skill_id, status, evidence_chunk_id, reasoning}` where status is
  `evidenced`, `weak`, or `absent`.
- An `evidence_chunk_id` is mandatory when status is `evidenced`. Reject responses
  that claim evidence without citing a chunk.
- Overall verdict plus a short rationale.
- Cache by `sha256(jd_text) + evidence_version + prompt_version + model_id`.
- Write `match` and `match_citation` rows in one transaction.

**Acceptance**
- Test asserting a fabricated citation (chunk id not in the retrieved set) is
  rejected.
- Cost per match recorded and under a configured ceiling.

**Out of scope**: the aggregate report.

---

## TASK 10: Aggregate gap report

**Goal**: The payoff feature.

**Requirements**
- Across all eligible active postings, aggregate requirements by canonical skill.
- Filter to `is_actionable = true`.
- For each skill compute: postings requiring it, postings preferring it, and my
  status derived from `match_citation` (`evidenced`, `weak`, `absent`).
- Rank by `required_count` among skills where my status is `absent` or `weak`.
- Output as a table plus a JSON export.
- Include a trend view: how each skill's frequency changed over the last 30 days.

**Acceptance**
- Report runs against a seeded test database with known counts and produces the
  expected ranking.
- Query runtime under 2 seconds on the full dataset, with `EXPLAIN ANALYZE` recorded.

**Out of scope**: any UI beyond terminal output and JSON.

---

# Instrumentation checklist

Wire from M1 forward. Every claim about this project should have a number.

- Adapter success rate, per adapter and overall
- Fetch latency p50 and p99, per host
- Queue depth and pending message age
- Dead letter rate
- Cache hit rate, per cache
- Tokens and dollars per run, per stage
- Postings processed per run, and stage-by-stage funnel counts
- Gap report query runtime

# Repo hygiene

- `.env.example` committed, `.env` never
- Alembic migrations for every schema change, no manual DDL
- `docker-compose.yml` with Postgres, pgvector, and Redis for local dev
- README with an architecture diagram and the current adapter coverage number

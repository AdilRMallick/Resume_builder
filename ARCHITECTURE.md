# Job Match Engine: Architecture

Personal system that ingests the SimplifyJobs new-grad feed, resolves job descriptions,
matches them against an evidence corpus of my actual work, and reports aggregate skill gaps.

Primary goal: a daily-useful tool.
Secondary goal: deep, defensible experience with Redis, Postgres, Go, and retrieval.

---

## 1. Service boundaries

Three processes, no shared code, contract is Redis Streams plus Postgres.

```
  cron ──> [ingestor: Python]  reads listings.json, upserts postings,
                               XADD fetch jobs to stream

       ┌─> [fetcher: Go]       consumer group on fetch stream,
       │                       per-host rate limit, ATS adapters,
       │                       writes raw JD, XADD enrich jobs
       │
  Redis Streams
       │
       └─> [enricher: Python]  consumer group on enrich stream,
                               requirement extraction, embeddings,
                               matching, gap rollup

            [api: FastAPI]     read-only over Postgres, serves UI + digest
```

Why Redis Streams rather than a list-based queue: consumer groups give real
delivery semantics. `XACK` for completion, `XAUTOCLAIM` for jobs abandoned by a dead
worker, `XPENDING` for visibility into stuck work. This is the difference between
"I used Redis" and being able to answer what happens when a worker dies mid-job.

Why Go for the fetcher only: fetching a few hundred URLs across a dozen hosts with
per-host rate limits, timeouts, and cancellation is the exact shape Go is good at.
It is a clean service boundary so the language split costs nothing architecturally,
and it gives Go a second artifact beyond the dapr PR.

---

## 2. Posting identity and lifecycle

The feed is not stable. Companies repost, edit titles, and pull listings. Naive
inserts produce duplicates and naive deletes destroy history.

**Canonical key**: prefer the Simplify posting id when present. Fall back to
`sha256(normalize(company) + normalize(title) + url_host + url_path)`.
Normalization lowercases, strips punctuation, and collapses whitespace.

**Lifecycle**: never hard delete. A posting has `first_seen_at`, `last_seen_at`,
`inactive_at`. When a canonical key disappears from the feed or flips inactive, set
`inactive_at`. If the same key reappears, clear `inactive_at` and increment
`repost_count`. Repost count is itself signal: roles that repost repeatedly are
either high churn or perpetually open.

---

## 3. The requirement taxonomy problem

This is the single decision that determines whether the aggregate gap report works.

If requirements are stored as free text, aggregation fails. "Go", "Golang",
"Go (Golang)", and "experience with Go" become four rows and none of them cross the
threshold to show up in a ranked report.

**Design**: a curated `canonical_skill` table with an alias list. Extraction maps
raw requirement text onto a canonical skill id.

Critically, the LLM does **not** get to create canonical skills. When extraction
finds something with no confident alias match, it writes to a
`skill_alias_candidate` review queue with the raw text and the posting it came from.
I approve or reject in batch. Auto-creation causes taxonomy drift within a week and
silently breaks every aggregate number.

Seed the taxonomy manually with roughly 150 to 250 entries covering languages,
databases, cloud, infra, ML, and the common soft requirements. Categories matter for
reporting: a gap in "Kubernetes" is actionable, a gap in "excellent communication"
is not.

Each extracted requirement stores:
- `canonical_skill_id`
- `raw_text` (the literal span from the JD, for auditability)
- `importance` enum: `required` | `preferred` | `mentioned`
- `confidence` float

Importance matters enormously for the gap report. A skill that is `required` in 40
postings is a very different signal from one that is `preferred` in 40.

---

## 4. Evidence corpus and staleness

The corpus is chunked from: resume bullets, repo READMEs, project writeups, course
descriptions, and any hand-written evidence notes.

Each chunk has an embedding and an optional set of manually tagged canonical skills.
Manual tagging on a small number of chunks meaningfully improves retrieval precision
over pure similarity.

**Versioning**: the corpus has a monotonic `evidence_version`. Any insert, edit, or
delete bumps it. Match results are keyed partly on evidence version, so when I add a
new project every prior match becomes stale and eligible for recompute. Without this
the system quietly reports gaps I already closed.

Recompute is lazy and bounded: on version bump, mark matches stale, recompute only
for active postings, only on next scheduled run.

---

## 5. Match caching and cost control

LLM matching is the only expensive operation. Cache key:

```
sha256(jd_text) + evidence_version + prompt_version + model_id
```

`prompt_version` is required. Without it, prompt edits silently reuse stale results
and I cannot compare match quality across prompt iterations.

**Two-stage funnel** to keep cost bounded:
1. Hard filters in SQL (location, sponsorship, seniority, start season). Cheap.
2. Vector similarity over evidence to rank the survivors. Cheap.
3. LLM structured match on the top N only, N configurable, default 20.

Log tokens and cost per match into a `run_metric` table. Every interview claim about
this project should have a real number behind it.

---

## 6. Eligibility filters

Encoded as config, not hardcoded:
- `grad_date`: May 2027. New-grad postings vary in start season; extract start
  season where present and flag rather than drop when absent.
- `sponsorship`: the feed carries a sponsorship field. Filter out
  offshore-only and citizenship-required roles unless explicitly included.
- `locations`: allowlist plus remote. Detroit, Chicago, and Michigan get a boost.
- `role_type`: SWE and adjacent. The feed also carries Quant and PM.

---

## 7. Fetching etiquette

Reading public job postings is fine. Being rude about it gets the IP blocked and is
an avoidable self-own.

- Honest user agent string with a contact URL.
- Per-host token bucket in Redis, shared across all workers. Default 1 request per
  2 seconds per host, conservative by design.
- Permanent cache by posting id. Job descriptions do not change; never refetch a
  successful resolution.
- Exponential backoff on 429 and 5xx, circuit break a host after repeated failures.
- Respect robots.txt.

---

## 8. ATS adapter design

Interface, implemented per ATS, with detection by URL host:

| ATS | Difficulty | Approach |
|---|---|---|
| Greenhouse | Easy | Public JSON API by board token |
| Lever | Easy | Public JSON API |
| Ashby | Medium | Public JSON endpoint |
| SmartRecruiters | Medium | Public API |
| Workday | Hard | Client-rendered, needs headless browser or internal JSON endpoint |
| Unknown | Fallback | Fetch plus readability text extraction |

**Adapter coverage is a first-class metric.** Track percent of active postings
successfully resolved to JD text, broken down by adapter. This number is the honest
measure of whether the system works, and it is the most interesting thing to talk
about in an interview.

Failures degrade rather than drop: a posting with no JD text still appears in the
queue, matched on title and company only, flagged as low confidence.

---

## 9. Postgres schema (draft)

```sql
-- feed
posting(id, canonical_key UNIQUE, simplify_id, company, title, url, locations JSONB,
        sponsorship, posted_at, first_seen_at, last_seen_at, inactive_at,
        repost_count, role_type)

posting_jd(posting_id PK FK, adapter, raw_text, extracted_at, fetch_status,
           char_count)

-- taxonomy
canonical_skill(id, name UNIQUE, category, is_actionable BOOL)
skill_alias(id, canonical_skill_id FK, alias UNIQUE)
skill_alias_candidate(id, raw_text, seen_count, example_posting_id, status)

posting_requirement(id, posting_id FK, canonical_skill_id FK NULL, raw_text,
                    importance, confidence, prompt_version)

-- evidence
evidence_chunk(id, source_type, source_ref, text, embedding VECTOR(1536),
               created_at, evidence_version)
evidence_skill(evidence_chunk_id FK, canonical_skill_id FK)  -- manual tags

-- matching
match(id, posting_id FK, evidence_version, prompt_version, model_id,
      score NUMERIC, verdict, rationale JSONB, is_stale BOOL, computed_at)
match_citation(match_id FK, canonical_skill_id FK, evidence_chunk_id FK NULL,
               status)  -- status: evidenced | weak | absent

-- ops
run_metric(id, run_id, stage, metric, value, recorded_at)
```

Indexes to add deliberately, and be able to say why:
- `posting(inactive_at, posted_at DESC)` for the active feed query
- `posting_requirement(canonical_skill_id, importance)` for the gap rollup
- ivfflat or hnsw on `evidence_chunk.embedding`
- `match(is_stale, posting_id)` for the recompute sweep

Run `EXPLAIN ANALYZE` on the gap rollup query at least once and record the result.

---

## 10. Explicitly out of scope

- **Auto-apply.** Mass submission produces worse outcomes than twenty targeted
  applications and violates most ATS terms of service. Simplify's own autofill
  extension is the ceiling of reasonable.
- **Resume keyword injection.** The system reports gaps so I can close them, not
  fabricate them. Anything on a resume has to survive an interview.
- **Multi-user or hosting.** Single user, runs locally or on one small box. No auth,
  no tenancy, no billing. Adding these costs weeks and teaches nothing new.
- **The HackerRank scorer, until M5.** It is a second-opinion signal on the profile
  side, not the centerpiece.

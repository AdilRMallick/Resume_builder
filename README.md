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

## Easiest setup: resume-tailoring Chrome extension

This is the shortest path if you only want to open a job listing and generate a
tailored Jake-template resume. It needs **Python 3.11+ and Chrome or Edge**. It does
not need Docker, Postgres, Redis, Go, or an API key.

First, use GitHub's **Code → Download ZIP** and extract it, or clone the repository.
Open the extracted repository folder—the one containing this README—in PowerShell.

### 1. Install and start the local backend (Windows PowerShell)

Open PowerShell in the repository folder and run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
powershell -ExecutionPolicy Bypass -File .\scripts\install-tectonic.ps1
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\jme.exe serve start --port 8002
```

Keep that PowerShell window open. When it says Uvicorn is running, the backend is
ready at <http://127.0.0.1:8002>.

The setup command creates `.env` only when it is missing, so rerunning it will not erase
any API keys you added. Tectonic is the local LaTeX engine: its first PDF compile downloads
the TeX support bundle, then later compiles use the cache. Resume data is not uploaded to
an online LaTeX service.

### 2. Load the extension once

1. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
2. Turn on **Developer mode**.
3. Click **Load unpacked**.
4. Select this repository's `extension` folder.
5. Pin **JME Resume Tailor** from the browser's Extensions menu.

### 3. Tailor a resume

1. Open a job listing.
2. Click **JME Resume Tailor** to open its side panel.
3. Click **Use current job page**, or paste/upload the job description.
4. Optionally enter a standing prompt under **Always follow these instructions**. It is
   saved in the extension and reused automatically for every future resume.
5. Leave **Verified selection** selected, or choose a configured AI provider.
6. Click **Build tailored resume**.
7. With an AI provider selected, use **Ask for a change** below the preview for iterative
   rewrites, removals, or reprioritization. Each reply recompiles the PDF.
8. Review the real compiled PDF, then copy its text or download the `.tex` or `.pdf` file.

Verified mode works immediately and never calls an AI provider. To enable AI rewriting,
open `.env`, add exactly one key, save the file, stop the server with `Ctrl+C`, and run
the start command again:

```dotenv
GEMINI_API_KEY=your-key-here
# or OPENAI_API_KEY=your-key-here
# or ANTHROPIC_API_KEY=your-key-here
# or MOONSHOT_API_KEY=your-key-here
```

```powershell
.\.venv\Scripts\jme.exe serve start --port 8002
```

For later use, you only need to open PowerShell in the repository, run that final start
command, and click the extension. You do not need to reinstall it each time. After the
extension code changes, click its **Reload** button on `chrome://extensions`.

<details>
<summary>macOS/Linux commands</summary>

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
# Install Tectonic from https://tectonic-typesetting.github.io/book/latest/installation/
[ -f .env ] || cp .env.example .env
./.venv/bin/jme serve start --port 8002
```

</details>

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

## Full job-match pipeline setup

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
jme report digest             # daily brief: roles + citations + gaps + actions
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

For a scheduled or inbox-friendly artifact, `jme report digest` combines the latest
shortlist, each role's newest grounded match, its evidence citations, and the current
gap ranking without making another LLM call. Use `--format json` for automation or
`--output daily.md` to write the Markdown digest. The same JSON contract is available
at `GET /digest`.

### Browser extension

`extension/` contains an unpacked Chrome/Edge extension with two halves: a **Workday
autofill** engine that runs entirely in the browser, and the **resume tailor** that talks
to the local API.

#### Workday autofill

Roughly half of a typical application pipeline runs through Workday, and each of those
forms is twenty minutes of retyping the same work history into custom dropdowns,
typeaheads, three-part date fields, and repeating panels that browser autofill cannot
touch. The extension fills them from a profile you enter once.

Your profile lives in `chrome.storage.local` and never leaves the device: the engine runs
inside the Workday tab, and no file under `extension/autofill/` is allowed to make a
network call — `tests/extension/test_extension.py` enforces that. There is no account, no
license, and no fill limit.

Open the side panel on a Workday application and press **Fill this page**, or use the
floating button the extension puts in the page itself. It fills one step at a time and
**never submits and never presses Next** — you review each page and advance it yourself.

What it handles:

| Widget | How |
| --- | --- |
| Text and textarea | Native value setter so React's change tracker fires |
| Dropdowns | Opens the portal-rendered listbox, scores every option, clicks the best |
| Multi-selects | Types each value, waits for the prompt options, clicks the match |
| Dates | Fills the month/day/year spinbuttons, or a single `MM/DD/YYYY` input |
| Repeating sections | Clicks Add until there are enough panels, then fills each in scope |
| Resume upload | Attaches the stored file through a synthetic `DataTransfer` |

Fields are found by `data-automation-id` first, then by accessible name, then by the
surrounding group — tenants customise their Workday instances, so no single selector
works everywhere. Anything it cannot match with confidence is reported back to you
rather than guessed at.

`tests/extension/dom_checks.js` drives the engine against synthetic Workday forms to keep
that honest. It needs jsdom, so it is opt-in — `npm install` in `tests/extension` and
`pytest tests/extension` picks it up. The rest of the extension suite runs unconditionally.

Two behaviours are off by default, in **Edit profile**:

- **Voluntary disclosures** (gender, race, veteran, disability). These are optional on
  every application, so the extension leaves those pages blank until you fill in the
  answers you want and turn them on.
- **Overwrite existing values.** A value Workday parsed out of your uploaded resume is
  usually more current than a stale profile entry, so filled fields are left alone.

**Edit profile → Import from JME** seeds your work history, education, and skills from
`jme/resume/profile.json`, so both halves of the extension draw on the same verified
facts. It only fills blanks; anything you have already typed is kept.

#### Resume tailor

While a job listing is open, click **Use current job page** to capture its visible
description, or paste or upload a text/Markdown/HTML description. The local API always
selects relevant verified bullets from `jme/resume/profile.json`. You can keep that
deterministic wording or ask OpenAI, Claude, Gemini, or Kimi to propose evidence-linked
rewrites; numeric, keyword, source, and target-banner checks run before any rewrite is
accepted. The result uses Jake's canonical LaTeX layout and is compiled locally into the
PDF shown in the extension. **Use this PDF for autofill uploads** stores that compiled
PDF as the file the autofill engine attaches on your next application.

AI is optional. Put one provider key in `.env` and restart the API:

```dotenv
OPENAI_API_KEY=your-key
# or
ANTHROPIC_API_KEY=your-key
# or
GEMINI_API_KEY=your-key
# or (Kimi)
MOONSHOT_API_KEY=your-key
```

Gemini offers a limited free API tier. Google states that free-tier content may be used
to improve its products; use a paid tier if that tradeoff is not acceptable for resume
data. Kimi API usage is billed separately by Moonshot.

```bash
jme serve start --port 8002
```

Then open `chrome://extensions` (or `edge://extensions`), enable Developer mode, choose
**Load unpacked**, and select the repository's `extension/` directory. Autofill works
without the backend running; only the tailor needs it.

The extension can reach exactly two places: Workday application hosts, and the local API
on port 8002. It never receives a provider key. Verified mode sends nothing externally;
AI mode sends the job description and selected evidence to the provider you choose.

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

extension/            unpacked Chrome/Edge extension
  autofill/           Workday fill engine: DOM utils, widget drivers, field map, runner
  profile/            profile schema and the editor page it is entered on
  sidepanel.*         the panel: autofill controls and the resume tailor

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

**Dedicated ATS adapters use public JSON APIs.** Greenhouse, Lever, Ashby, and
SmartRecruiters posting URLs bypass page scraping and preserve structured description
sections. Unknown hosts still degrade to the readability fallback rather than dropping
the posting.

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

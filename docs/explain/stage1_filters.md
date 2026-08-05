# Stage 1 filters -- recorded plan

`jme rank explain` -- the query in `jme/rank/filters.py`, verbatim, run through
`EXPLAIN (ANALYZE, BUFFERS)`.

<!-- Recorded by hand, not generated on every run. Re-record it when the query, the
     indexes, or the dataset shape changes, and say what changed. -->

## How this was recorded

```bash
docker compose up -d                              # Postgres 16.14 + pgvector
make migrate                                      # schema at a5be80babd8c
python -m jme.cli taxonomy seed                   # 229 canonical skills, 1284 aliases
python tests/rank/seed_synthetic.py --postings 5000   # also resolves + seeds matches
```

Dataset the numbers below describe:

| table | rows | note |
|---|---|---|
| `posting` | 5,000 | 4,000 active, 1,000 with `inactive_at` set |
| `posting_requirement` | 19,998 | all resolved, to 11 distinct canonical skills |
| `match` / `match_citation` | 1,000 / 3,996 | synthetic, 112 matches marked stale |
| `canonical_skill` | 229 | 194 actionable |
| `evidence_chunk` | 137 | 120 synthetic, hash-embedded offline provider |

Every table was `ANALYZE`d before recording, so the planner had real statistics.
Timings come from a laptop under Docker Desktop on Windows, warm cache -- treat the
*shape* of the plan as the durable claim and the milliseconds as indicative.

## The plan

```
Sort  (cost=7509.87..7509.92 rows=20 width=1775) (actual time=68.268..68.306 rows=486 loops=1)
  Sort Key: a.id
  Sort Method: quicksort  Memory: 128kB
  Buffers: shared hit=338
  CTE active
    ->  Seq Scan on posting a_1  (cost=0.00..418.00 rows=4000 width=207) (actual time=0.012..8.602 rows=4000 loops=1)
          Filter: (inactive_at IS NULL)
          Rows Removed by Filter: 1000
          Buffers: shared hit=338
  ->  CTE Scan on active a  (cost=0.12..7091.44 rows=20 width=1775) (actual time=0.164..67.998 rows=486 loops=1)
        Filter: (CASE WHEN (CASE WHEN (sponsorship_norm = ''::text) THEN 'unknown'::text WHEN (sponsorship_norm ~ '(offshore|outside the us|non us only)'::text) THEN 'offshore'::text WHEN (sponsorship_norm ~ '(citizen|clearance|green card|permanent resid)'::text) THEN 'citizenship'::text WHEN (sponsorship_norm ~ '(does not offer|no sponsorship|not offer sponsorship|sponsorship not (offered|available)|without sponsorship)'::text) THEN 'not_offered'::text WHEN (sponsorship_norm ~ 'offers sponsorship'::text) THEN 'offers'::text ELSE 'unknown'::text END = 'offshore'::text) THEN 'sponsorship_offshore'::text WHEN (CASE WHEN (sponsorship_norm = ''::text) THEN 'unknown'::text WHEN (sponsorship_norm ~ '(offshore|outside the us|non us only)'::text) THEN 'offshore'::text WHEN (sponsorship_norm ~ '(citizen|clearance|green card|permanent resid)'::text) THEN 'citizenship'::text WHEN (sponsorship_norm ~ '(does not offer|no sponsorship|not offer sponsorship|sponsorship not (offered|available)|without sponsorship)'::text) THEN 'not_offered'::text WHEN (sponsorship_norm ~ 'offers sponsorship'::text) THEN 'offers'::text ELSE 'unknown'::text END = 'citizenship'::text) THEN 'sponsorship_citizenship'::text WHEN (CASE WHEN (sponsorship_norm = ''::text) THEN 'unknown'::text WHEN (sponsorship_norm ~ '(offshore|outside the us|non us only)'::text) THEN 'offshore'::text WHEN (sponsorship_norm ~ '(citizen|clearance|green card|permanent resid)'::text) THEN 'citizenship'::text WHEN (sponsorship_norm ~ '(does not offer|no sponsorship|not offer sponsorship|sponsorship not (offered|available)|without sponsorship)'::text) THEN 'not_offered'::text WHEN (sponsorship_norm ~ 'offers sponsorship'::text) THEN 'offers'::text ELSE 'unknown'::text END = 'not_offered'::text) THEN 'sponsorship_not_offered'::text WHEN ((NOT is_remote) AND (NOT (locations @> '["Remote"]'::jsonb)) AND (NOT (SubPlan 2)) AND ((jsonb_typeof(locations) <> 'string'::text) OR ((locations #>> '{}'::text[]) !~~* ALL ('{"%ann arbor%",%chicago%,%detroit%,%michigan%,%remote%}'::text[])))) THEN 'location'::text WHEN (CASE WHEN ((role_type IS NULL) OR (btrim((role_type)::text) = ''::text)) THEN 'unknown'::text WHEN (lower(btrim((role_type)::text)) = ANY ('{swe}'::text[])) THEN 'allowed'::text WHEN (lower(btrim((role_type)::text)) = ANY ('{swe,software,"software engineering",engineering,quant,"quantitative finance",pm,product,"product management",hardware,data,"data science",ml,ai,research,design,ux,security,it,business,finance,consulting,other}'::text[])) THEN 'excluded'::text ELSE 'unknown'::text END = 'excluded'::text) THEN 'role_type'::text WHEN (CASE WHEN ((start_season IS NULL) OR (btrim((start_season)::text) = ''::text)) THEN 'unknown'::text WHEN ("substring"((start_season)::text, '(20[0-9]{2})'::text) IS NULL) THEN 'unknown'::text WHEN (make_date(("substring"((start_season)::text, '(20[0-9]{2})'::text))::integer, CASE WHEN ((start_season)::text ~* 'spring'::text) THEN 3 WHEN ((start_season)::text ~* 'summer'::text) THEN 6 WHEN ((start_season)::text ~* '(fall|autumn)'::text) THEN 9 WHEN ((start_season)::text ~* 'winter'::text) THEN 1 ELSE 12 END, 1) >= '2027-03-17'::date) THEN CASE WHEN ((start_season)::text ~* '(spring|summer|fall|autumn|winter)'::text) THEN 'eligible'::text ELSE 'partial'::text END ELSE 'too_early'::text END = 'too_early'::text) THEN 'start_season'::text ELSE NULL::text END IS NULL)
        Rows Removed by Filter: 3514
        Buffers: shared hit=338
        SubPlan 2
          ->  Function Scan on jsonb_array_elements_text loc  (cost=0.01..1.63 rows=1 width=0) (actual time=0.004..0.004 rows=1 loops=1261)
                Filter: (v ~~* ANY ('{"%ann arbor%",%chicago%,%detroit%,%michigan%,%remote%}'::text[]))
                Rows Removed by Filter: 1
Planning Time: 0.268 ms
Execution Time: 68.534 ms
```

**486 of 5,000 postings survive in 69 ms.**

## Reading it

**The driving scan is sequential, and that is correct here.** `inactive_at IS NULL`
matches 4,000 of 5,000 rows. At 80% selectivity no planner will pick an index: the
index scan has to visit essentially every heap page anyway, plus the index pages. The
note in `filters.py` says the predicate is *usable* as an index qualifier, not that it
will always be used, and those are different claims. Forcing the issue with
`SET enable_seqscan = off` shows the index is in fact usable and what it costs:

```
Sort  (cost=7579.15..7579.20 rows=20 width=1775) (actual time=73.071..73.109 rows=486 loops=1)
  Sort Key: a.id
  Sort Method: quicksort  Memory: 128kB
  Buffers: shared hit=181
  CTE active
    ->  Bitmap Heap Scan on posting a_1  (cost=79.28..487.28 rows=4000 width=207) (actual time=0.129..9.410 rows=4000 loops=1)
          Recheck Cond: (inactive_at IS NULL)
          Heap Blocks: exact=171
          Buffers: shared hit=181
          ->  Bitmap Index Scan on ix_posting_active_feed  (cost=0.00..78.28 rows=4000 width=0) (actual time=0.101..0.101 rows=4000 loops=1)
                Index Cond: (inactive_at IS NULL)
                Buffers: shared hit=10
  ->  CTE Scan on active a  (cost=0.12..7091.44 rows=20 width=1775) (actual time=0.266..72.793 rows=486 loops=1)
        Filter: <the sponsorship/location/role/season CASE, elided - identical to the plan above>
        Rows Removed by Filter: 3514
        Buffers: shared hit=181
        SubPlan 2
          ->  Function Scan on jsonb_array_elements_text loc  (cost=0.01..1.63 rows=1 width=0) (actual time=0.004..0.004 rows=1 loops=1261)
                Filter: (v ~~* ANY ('{"%ann arbor%",%chicago%,%detroit%,%michigan%,%remote%}'::text[]))
                Rows Removed by Filter: 1
Planning Time: 0.276 ms
Execution Time: 73.275 ms
```

`Bitmap Index Scan on ix_posting_active_feed / Index Cond: (inactive_at IS NULL)` --
Postgres b-trees index NULLs, so `IS NULL` is an index condition rather than a
post-scan filter. It reads 10 index buffers and 171 heap blocks -- 181 buffers against
the seq scan's 338, because it only visits pages that contain an active posting -- but
its estimated cost is 487 against the seq scan's 418, and the measured execution is
73.3 ms against 68.5 ms. Fewer buffers, more time: the bitmap has to be built and the
heap visited in a different order, and at 80% selectivity that overhead is not repaid.
The planner is right by a small margin at this size and this active fraction. The
margin grows in the index's favour as the archive of inactive postings grows: the
interesting number is not row count, it is the *active fraction*. At 5,000 rows and 80% active the seq scan wins; at 50,000 rows and 15%
active it will not.

**All of the cost is above the scan, in the CTE filter.** 8.6 ms to produce the 4,000
active rows, another 59 ms to evaluate the eligibility CASE over them. That is the
shape the design asks for: one indexable predicate in the driving `WHERE`, everything
else row-local and unindexable by construction. The sponsorship classifier is five regexes
over a free-text column, the role and season rules parse strings; no index can answer
any of it, and building one would mean materialising a classification that changes
whenever the rules do.

**The location arm short-circuits in the right order.** `SubPlan 2` --
`jsonb_array_elements_text` -- runs 1,261 times, not 4,000. The cheap arms in front of
it (`is_remote`, then `locations @> '["Remote"]'::jsonb`) accept most survivors before
the expensive unnest is reached. The ILIKE arm that follows is a substring test --
"Detroit, MI" has to match the allowlist entry "Detroit" -- and is deliberately last.

One correction to the module docstring in `filters.py`, which describes the `@>` arm
as the one a GIN index can answer: **no such index exists in the schema today** --
`a5be80babd8c` creates `ix_posting_active_feed`, `ix_posting_last_seen`,
`ix_posting_role_type`, `ix_posting_simplify_id` and `ix_posting_url_host` on
`posting`, and nothing on `locations`. Nor would this query use one if it did: the
containment test sits inside a CASE expression over a CTE, evaluated row by row on
rows the driving scan has already produced. Leading with `@>` is a cheap-operator-first
ordering that keeps the option open for a future query that filters on location
directly; it is not, right now, an index access path.

**`Rows Removed by Filter: 3514` is the funnel.** 4,000 active in, 486 out. The
per-rule attribution `jme rank funnel` reports is generated from the same CTE text as
this query, so the drop counts and the survivor set cannot drift apart.

## What to watch

* If the seq scan ever appears *with* a small active fraction, the planner's statistics
  are stale -- `ANALYZE posting`.
* If `SubPlan 2`'s loop count approaches the active row count, the cheap location arms
  have stopped accepting and the allowlist or the feed's location format has changed.
* The 59 ms filter is linear in active postings. At 50,000 active it is roughly a
  second, which is the point at which the classification wants to be a stored generated
  column rather than an expression.

## What this dataset does not prove

The postings are synthetic: 5,000 rows generated from a fixed set of companies,
titles, locations and sponsorship strings, with requirement spans drawn from a list
of 20 templates that resolve to 11 distinct canonical skills. That is enough to make
the planner choose real strategies over real statistics, and enough to show which
index each query lands on. It is not enough to predict selectivity on the live feed,
where skill demand has a long tail and location strings are far messier.

# Gap report rollup -- recorded plan

`jme report explain` -- the rollup in `jme/report/gap.py`, verbatim, run through
`EXPLAIN (ANALYZE, BUFFERS, VERBOSE)`.

**Acceptance criterion: under 2 seconds on the full dataset. Recorded at 15.3 ms of
planner-reported execution, 52 ms end to end through `jme report gap`.**

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
Sort  (cost=8921.36..8921.78 rows=169 width=179) (actual time=15.189..15.195 rows=11 loops=1)
  Output: ($3), ($4), ($5), ($6), (COALESCE($7, 0)), s.id, s.name, ((s.category)::text), (count(*) FILTER (WHERE (r.importance = 'required'::importance))), (count(*) FILTER (WHERE (r.importance = 'preferred'::importance))), (count(*) FILTER (WHERE (r.importance = 'mentioned'::importance))), (count(DISTINCT r.posting_id)), (CASE COALESCE((max(CASE c.status WHEN 'evidenced'::citation_status THEN 3 WHEN 'weak'::citation_status THEN 2 ELSE 1 END)), 1) WHEN 3 THEN 'evidenced'::text WHEN 2 THEN 'weak'::text ELSE 'absent'::text END), (COALESCE((count(*) FILTER (WHERE (c.status = 'evidenced'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'weak'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'absent'::citation_status))), '0'::bigint)), (COALESCE((count(DISTINCT c.match_id)), '0'::bigint))
  Sort Key: (count(*) FILTER (WHERE (r.importance = 'required'::importance))) DESC NULLS LAST, (count(*) FILTER (WHERE (r.importance = 'preferred'::importance))) DESC NULLS LAST, (count(*) FILTER (WHERE (r.importance = 'mentioned'::importance))) DESC NULLS LAST, (count(DISTINCT r.posting_id)) DESC NULLS LAST, s.name
  Sort Method: quicksort  Memory: 26kB
  Buffers: shared hit=1214
  CTE eligible
    ->  Bitmap Heap Scan on public.posting p  (cost=78.32..7051.32 rows=141 width=8) (actual time=0.122..7.062 rows=588 loops=1)
          Output: p.id
          Recheck Cond: (p.inactive_at IS NULL)
          Filter: (((p.role_type IS NULL) OR (lower((p.role_type)::text) = ANY ('{swe}'::text[]))) AND ((p.sponsorship IS NULL) OR ((p.sponsorship)::text !~~* ALL ('{"%does not offer%","%no sponsorship%","%not provide sponsorship%",%citizen%,%clearance%,%offshore%}'::text[]))) AND (p.is_remote OR (p.locations IS NULL) OR (jsonb_typeof(p.locations) <> 'array'::text) OR (jsonb_array_length(p.locations) = 0) OR (SubPlan 1)))
          Rows Removed by Filter: 3412
          Heap Blocks: exact=171
          Buffers: shared hit=181
          ->  Bitmap Index Scan on ix_posting_active_feed  (cost=0.00..78.28 rows=4000 width=0) (actual time=0.083..0.084 rows=4000 loops=1)
                Index Cond: (p.inactive_at IS NULL)
                Buffers: shared hit=10
          SubPlan 1
            ->  Function Scan on pg_catalog.jsonb_array_elements_text loc  (cost=0.00..1.63 rows=1 width=0) (actual time=0.003..0.003 rows=1 loops=530)
                  Function Call: jsonb_array_elements_text(p.locations)
                  Filter: (loc.name ~~* ANY ('{%Remote%,%Detroit%,"%Ann Arbor%",%Michigan%,%Chicago%}'::text[]))
                  Rows Removed by Filter: 1
  CTE req
    ->  Hash Join  (cost=4.58..1226.01 rows=846 width=16) (actual time=7.510..10.407 rows=2376 loops=1)
          Output: r_1.canonical_skill_id, r_1.importance, r_1.posting_id
          Hash Cond: (r_1.posting_id = e.id)
          Buffers: shared hit=1119
          ->  Seq Scan on public.posting_requirement r_1  (cost=0.00..1137.98 rows=19998 width=16) (actual time=0.032..1.338 rows=19998 loops=1)
                Output: r_1.id, r_1.posting_id, r_1.canonical_skill_id, r_1.raw_text, r_1.importance, r_1.confidence, r_1.prompt_version, r_1.model_id, r_1.created_at
                Buffers: shared hit=938
          ->  Hash  (cost=2.82..2.82 rows=141 width=8) (actual time=7.303..7.303 rows=588 loops=1)
                Output: e.id
                Buckets: 1024  Batches: 1  Memory Usage: 31kB
                Buffers: shared hit=181
                ->  CTE Scan on eligible e  (cost=0.00..2.82 rows=141 width=8) (actual time=0.123..7.159 rows=588 loops=1)
                      Output: e.id
                      Buffers: shared hit=181
  InitPlan 4 (returns $3)
    ->  Aggregate  (cost=3.17..3.18 rows=1 width=8) (actual time=0.050..0.050 rows=1 loops=1)
          Output: count(*)
          ->  CTE Scan on eligible  (cost=0.00..2.82 rows=141 width=0) (actual time=0.001..0.028 rows=588 loops=1)
                Output: eligible.id
  InitPlan 5 (returns $4)
    ->  Aggregate  (cost=19.04..19.05 rows=1 width=8) (actual time=0.194..0.194 rows=1 loops=1)
          Output: count(*)
          ->  CTE Scan on req  (cost=0.00..16.92 rows=846 width=0) (actual time=0.000..0.109 rows=2376 loops=1)
                Output: req.canonical_skill_id, req.importance, req.posting_id
  InitPlan 6 (returns $5)
    ->  Aggregate  (cost=19.03..19.04 rows=1 width=8) (actual time=0.241..0.241 rows=1 loops=1)
          Output: count(*)
          ->  CTE Scan on req req_1  (cost=0.00..16.92 rows=842 width=0) (actual time=0.001..0.156 rows=2376 loops=1)
                Output: req_1.canonical_skill_id, req_1.importance, req_1.posting_id
                Filter: (req_1.canonical_skill_id IS NOT NULL)
  InitPlan 7 (returns $6)
    ->  Aggregate  (cost=16.93..16.94 rows=1 width=8) (actual time=0.091..0.091 rows=1 loops=1)
          Output: count(*)
          ->  CTE Scan on req req_2  (cost=0.00..16.92 rows=4 width=0) (actual time=0.090..0.090 rows=0 loops=1)
                Output: req_2.canonical_skill_id, req_2.importance, req_2.posting_id
                Filter: (req_2.canonical_skill_id IS NULL)
                Rows Removed by Filter: 2376
  InitPlan 8 (returns $7)
    ->  Index Scan using evidence_version_pkey on public.evidence_version v  (cost=0.15..8.17 rows=1 width=4) (actual time=0.010..0.011 rows=1 loops=1)
          Output: v.version
          Index Cond: (v.id = 1)
          Buffers: shared hit=2
  ->  Nested Loop Left Join  (cost=436.34..571.40 rows=169 width=179) (actual time=14.286..15.174 rows=11 loops=1)
        Output: $3, $4, $5, $6, COALESCE($7, 0), s.id, s.name, (s.category)::text, (count(*) FILTER (WHERE (r.importance = 'required'::importance))), (count(*) FILTER (WHERE (r.importance = 'preferred'::importance))), (count(*) FILTER (WHERE (r.importance = 'mentioned'::importance))), (count(DISTINCT r.posting_id)), (CASE COALESCE((max(CASE c.status WHEN 'evidenced'::citation_status THEN 3 WHEN 'weak'::citation_status THEN 2 ELSE 1 END)), 1) WHEN 3 THEN 'evidenced'::text WHEN 2 THEN 'weak'::text ELSE 'absent'::text END), (COALESCE((count(*) FILTER (WHERE (c.status = 'evidenced'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'weak'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'absent'::citation_status))), '0'::bigint)), (COALESCE((count(DISTINCT c.match_id)), '0'::bigint))
        Buffers: shared hit=1214
        ->  Result  (cost=0.00..0.01 rows=1 width=0) (actual time=0.000..0.001 rows=1 loops=1)
        ->  Hash Join  (cost=436.34..568.86 rows=169 width=115) (actual time=13.693..14.576 rows=11 loops=1)
              Output: (count(*) FILTER (WHERE (r.importance = 'required'::importance))), (count(*) FILTER (WHERE (r.importance = 'preferred'::importance))), (count(*) FILTER (WHERE (r.importance = 'mentioned'::importance))), (count(DISTINCT r.posting_id)), s.id, s.name, s.category, (CASE COALESCE((max(CASE c.status WHEN 'evidenced'::citation_status THEN 3 WHEN 'weak'::citation_status THEN 2 ELSE 1 END)), 1) WHEN 3 THEN 'evidenced'::text WHEN 2 THEN 'weak'::text ELSE 'absent'::text END), (COALESCE((count(*) FILTER (WHERE (c.status = 'evidenced'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'weak'::citation_status))), '0'::bigint)), (COALESCE((count(*) FILTER (WHERE (c.status = 'absent'::citation_status))), '0'::bigint)), (COALESCE((count(DISTINCT c.match_id)), '0'::bigint))
              Inner Unique: true
              Hash Cond: (r.canonical_skill_id = s.id)
              Buffers: shared hit=1212
              ->  Merge Left Join  (cost=429.63..560.76 rows=200 width=100) (actual time=13.637..14.515 rows=11 loops=1)
                    Output: (count(*) FILTER (WHERE (r.importance = 'required'::importance))), (count(*) FILTER (WHERE (r.importance = 'preferred'::importance))), (count(*) FILTER (WHERE (r.importance = 'mentioned'::importance))), (count(DISTINCT r.posting_id)), r.canonical_skill_id, CASE COALESCE((max(CASE c.status WHEN 'evidenced'::citation_status THEN 3 WHEN 'weak'::citation_status THEN 2 ELSE 1 END)), 1) WHEN 3 THEN 'evidenced'::text WHEN 2 THEN 'weak'::text ELSE 'absent'::text END, COALESCE((count(*) FILTER (WHERE (c.status = 'evidenced'::citation_status))), '0'::bigint), COALESCE((count(*) FILTER (WHERE (c.status = 'weak'::citation_status))), '0'::bigint), COALESCE((count(*) FILTER (WHERE (c.status = 'absent'::citation_status))), '0'::bigint), COALESCE((count(DISTINCT c.match_id)), '0'::bigint)
                    Inner Unique: true
                    Merge Cond: (r.canonical_skill_id = c.canonical_skill_id)
                    Buffers: shared hit=1210
                    ->  GroupAggregate  (cost=57.83..78.78 rows=200 width=36) (actual time=11.466..11.781 rows=11 loops=1)
                          Output: r.canonical_skill_id, count(*) FILTER (WHERE (r.importance = 'required'::importance)), count(*) FILTER (WHERE (r.importance = 'preferred'::importance)), count(*) FILTER (WHERE (r.importance = 'mentioned'::importance)), count(DISTINCT r.posting_id)
                          Group Key: r.canonical_skill_id
                          Buffers: shared hit=1119
                          ->  Sort  (cost=57.83..59.94 rows=842 width=16) (actual time=11.446..11.533 rows=2376 loops=1)
                                Output: r.canonical_skill_id, r.importance, r.posting_id
                                Sort Key: r.canonical_skill_id, r.posting_id
                                Sort Method: quicksort  Memory: 189kB
                                Buffers: shared hit=1119
                                ->  CTE Scan on req r  (cost=0.00..16.92 rows=842 width=16) (actual time=7.513..10.868 rows=2376 loops=1)
                                      Output: r.canonical_skill_id, r.importance, r.posting_id
                                      Filter: (r.canonical_skill_id IS NOT NULL)
                                      Buffers: shared hit=1119
                    ->  GroupAggregate  (cost=371.80..478.35 rows=11 width=40) (actual time=2.166..2.724 rows=11 loops=1)
                          Output: c.canonical_skill_id, max(CASE c.status WHEN 'evidenced'::citation_status THEN 3 WHEN 'weak'::citation_status THEN 2 ELSE 1 END), count(*) FILTER (WHERE (c.status = 'evidenced'::citation_status)), count(*) FILTER (WHERE (c.status = 'weak'::citation_status)), count(*) FILTER (WHERE (c.status = 'absent'::citation_status)), count(DISTINCT c.match_id)
                          Group Key: c.canonical_skill_id
                          Buffers: shared hit=91
                          ->  Sort  (cost=371.80..380.67 rows=3548 width=16) (actual time=2.112..2.262 rows=3996 loops=1)
                                Output: c.canonical_skill_id, c.status, c.match_id
                                Sort Key: c.canonical_skill_id, c.match_id
                                Sort Method: quicksort  Memory: 253kB
                                Buffers: shared hit=91
                                ->  Hash Join  (cost=42.10..162.59 rows=3548 width=16) (actual time=0.213..1.078 rows=3996 loops=1)
                                      Output: c.canonical_skill_id, c.status, c.match_id
                                      Inner Unique: true
                                      Hash Cond: (c.match_id = m.id)
                                      Buffers: shared hit=91
                                      ->  Seq Scan on public.match_citation c  (cost=0.00..109.96 rows=3996 width=16) (actual time=0.022..0.331 rows=3996 loops=1)
                                            Output: c.id, c.match_id, c.canonical_skill_id, c.evidence_chunk_id, c.status, c.reasoning
                                            Filter: (c.canonical_skill_id IS NOT NULL)
                                            Buffers: shared hit=70
                                      ->  Hash  (cost=31.00..31.00 rows=888 width=8) (actual time=0.186..0.186 rows=888 loops=1)
                                            Output: m.id
                                            Buckets: 1024  Batches: 1  Memory Usage: 43kB
                                            Buffers: shared hit=21
                                            ->  Seq Scan on public.match m  (cost=0.00..31.00 rows=888 width=8) (actual time=0.003..0.109 rows=888 loops=1)
                                                  Output: m.id
                                                  Filter: (NOT m.is_stale)
                                                  Rows Removed by Filter: 112
                                                  Buffers: shared hit=21
              ->  Hash  (cost=4.29..4.29 rows=194 width=19) (actual time=0.051..0.051 rows=194 loops=1)
                    Output: s.id, s.name, s.category
                    Buckets: 1024  Batches: 1  Memory Usage: 18kB
                    Buffers: shared hit=2
                    ->  Seq Scan on public.canonical_skill s  (cost=0.00..4.29 rows=194 width=19) (actual time=0.008..0.026 rows=194 loops=1)
                          Output: s.id, s.name, s.category
                          Filter: s.is_actionable
                          Rows Removed by Filter: 35
                          Buffers: shared hit=2
Planning:
  Buffers: shared hit=12
Planning Time: 0.651 ms
Execution Time: 15.287 ms
```

## Reading it

**The eligibility CTE lands on the index.** `Bitmap Index Scan on
ix_posting_active_feed / Index Cond: (p.inactive_at IS NULL)`, 10 index buffers, then
169 heap blocks. This is the same predicate as stage 1 but the planner picks the index
here, because the rollup's CTE is a bare `SELECT p.id` -- 8 bytes wide against stage
1's 207 -- so the bitmap heap scan is cheap relative to reading whole rows. 588
postings survive eligibility out of 4,000 active.

**The requirement join is a hash join, driven by the small side.** 19,998
`posting_requirement` rows are scanned once and probed against a 588-row hash of
eligible posting ids, yielding 2,376 rows. `ix_posting_requirement_posting_id` exists
and the planner declines to use it, correctly: the rollup touches *most* of the table,
and 938 buffers of sequential read beat 2,376 random index lookups. That index earns
its keep on single-posting lookups (`jme rank show`), not on this rollup.

**Citations aggregate separately, then merge.** The `match_citation` side filters
`NOT m.is_stale` first (112 of 1,000 matches dropped -- stale matches must not close a
gap, or a corpus edit would silently mark a still-open gap as covered), aggregates
3,996 citations to 11 skills, and merge-joins against the requirement aggregate on
`canonical_skill_id`. Both sides are already sorted on that key by their
`GroupAggregate`s, so the merge is free.

**`max(CASE status ...)` is the "best status ever" rule, in the plan.** `evidenced` 3,
`weak` 2, `absent` 1, `max` over all non-stale citations for the skill, mapped back to
a label. One aggregate, no correlated subquery per skill.

**The final `Sort` is 11 rows.** Ranking happens after aggregation, on a set the size
of the taxonomy, not the size of the corpus. That is why the 2-second ceiling has so
much headroom: everything expensive is a single pass over a few tens of thousands of
rows, and everything after the aggregate is tiny.

## Where the time actually goes

Elapsed is cumulative at each node, as `EXPLAIN` reports it:

| step | elapsed at that node, ms |
|---|---|
| eligibility CTE (5,000 postings -> 588) | 7.1 |
| requirement hash join (19,998 -> 2,376) | 10.4 |
| requirement aggregate (2,376 -> 11 skills) | 11.8 |
| citation aggregate (3,996 -> 11 skills) | 2.7 (independent branch) |
| **total execution** | **15.3** |

Planning takes another 0.7 ms. At this data size the query is effectively free; the
2-second ceiling is there to catch a future regression, not to describe today.

## What to watch

* `Rows Removed by Filter` on the `match` seq scan is the stale-match count. If it
  climbs toward the total, the evidence corpus is being edited faster than matches are
  being recomputed and the report is reading mostly-empty citation data.
* The requirement hash join is the term that grows with the feed. It is linear and
  cheap, but if `posting_requirement` reaches the millions the eligible-posting hash
  should be pushed down into an index scan on `(posting_id, canonical_skill_id)`.
* `taxonomy_coverage` in the report output is the honest companion to this plan: a fast
  query over requirements that never resolved to a canonical skill is still a report
  that cannot see them.

## What this dataset does not prove

The postings are synthetic: 5,000 rows generated from a fixed set of companies,
titles, locations and sponsorship strings, with requirement spans drawn from a list
of 20 templates that resolve to 11 distinct canonical skills. That is enough to make
the planner choose real strategies over real statistics, and enough to show which
index each query lands on. It is not enough to predict selectivity on the live feed,
where skill demand has a long tail and location strings are far messier.

On this seeded dataset the report itself returns **0 gaps and 11 covered skills** --
the synthetic citations give every skill an `evidenced` row somewhere, so nothing
qualifies as absent or weak. The plan is what this file is recording; the ranking
correctness is asserted against a hand-computed fixture in `tests/report/test_gap.py`,
where every expected count is derivable by reading the fixture.

# Job Match Engine

Source: implementation and documentation in this repository.

## Problem and architecture

- Built a local-first job-match engine that ingests job feeds, fetches postings, extracts structured requirements, retrieves résumé evidence, produces cited match assessments, and reports skill gaps.
- Split the system into a Python control plane and a Go fetcher. PostgreSQL stores durable application data and pgvector embeddings; Redis Streams provides the fetch queue and consumer-group delivery semantics.
- Kept the evidence corpus versioned so a real evidence change invalidates stale matches, while an unchanged ingestion run is a no-op.

## Reliable fetching

- Implemented dedicated public-JSON adapters for Greenhouse, Lever, Ashby, and SmartRecruiters, with a generic HTML fallback for other hosts.
- Added per-host rate limiting, robots-policy handling, retries, a circuit breaker, a dead-letter path, and stale-message recovery for failed workers.
- Used Redis consumer groups and explicit acknowledgements so an interrupted worker does not silently lose an in-flight posting.

## Evidence-grounded matching and reporting

- Chunked Markdown evidence by heading and bullet boundaries so citations remain self-contained and defensible.
- Added deterministic shortlist filters, vector retrieval, cached LLM extraction and matching, evidence citations, gap aggregation, and a daily digest available through both the CLI and API.
- Built a dependency-free dashboard whose panels fail independently and whose tests verify that every fetched route and rendered API field exists.

## Verification and performance

- Added Python and Go test suites, Ruff and Go vet checks, database migration checks, and GitHub Actions matrices for Python 3.11 and 3.13.
- Recorded reproducible `EXPLAIN ANALYZE` evidence for shortlist filtering and gap reporting. The synthetic gap report completed in 15.3 milliseconds against a two-second target.
- Protected the pgvector HNSW index from an Alembic autogeneration issue that otherwise proposed deleting the index used by similarity search.

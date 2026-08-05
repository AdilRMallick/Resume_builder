"""Feed ingestion: pull the SimplifyJobs new-grad feed and upsert postings.

Layering, deliberately kept thin:

  * `feed`   - pure parsing and identity. No database, no network state. Unit tested.
  * `upsert` - all SQL. Takes a Session, returns counts. Integration tested.
  * `run`    - orchestration: fetch, upsert, sweep, record metrics, enqueue.
  * `cli`    - typer surface, mounted by `jme.cli` as `jme ingest`.
"""

from __future__ import annotations

from jme.ingestor.feed import (
    FeedError,
    FeedNotFoundError,
    FeedParseError,
    FeedRecord,
    FeedUnavailableError,
    canonical_key,
    fetch_feed,
    load_feed_file,
    normalize,
    parse_feed,
)
from jme.ingestor.run import IngestSummary, ingest
from jme.ingestor.upsert import UpsertResult, deactivate_missing, upsert_postings

__all__ = [
    "FeedError",
    "FeedNotFoundError",
    "FeedParseError",
    "FeedRecord",
    "FeedUnavailableError",
    "IngestSummary",
    "UpsertResult",
    "canonical_key",
    "deactivate_missing",
    "fetch_feed",
    "ingest",
    "load_feed_file",
    "normalize",
    "parse_feed",
    "upsert_postings",
]

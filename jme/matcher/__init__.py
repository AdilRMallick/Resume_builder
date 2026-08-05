"""LLM match with citations (Task 9).

The matcher takes a shortlisted posting, retrieves evidence chunks per requirement,
asks the model for a per-requirement verdict grounded in a specific chunk id, and
persists `match` + `match_citation` in one transaction.

The load-bearing rule: a citation the model invented is never written to the database.
See `jme.matcher.match.validate_response`.
"""

from __future__ import annotations

from jme.matcher.match import (
    CostCeilingExceeded,
    FabricatedCitationError,
    MatchError,
    MatchOutcome,
    MatchValidationError,
    MissingCitationError,
    NoRequirementsError,
    RunBudgetExceeded,
    ShortlistReport,
    match_posting,
    match_shortlist,
    recompute_stale,
)
from jme.matcher.prompts import MATCH_SCHEMA, PROMPT_VERSION
from jme.matcher.retrieval import RetrievalSet, RetrievedChunk, retrieve_for_posting

__all__ = [
    "MATCH_SCHEMA",
    "PROMPT_VERSION",
    "CostCeilingExceeded",
    "FabricatedCitationError",
    "MatchError",
    "MatchOutcome",
    "MatchValidationError",
    "MissingCitationError",
    "NoRequirementsError",
    "RetrievalSet",
    "RetrievedChunk",
    "RunBudgetExceeded",
    "ShortlistReport",
    "match_posting",
    "match_shortlist",
    "recompute_stale",
    "retrieve_for_posting",
]

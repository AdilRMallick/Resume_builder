"""Stage 1 (SQL hard filters) plus stage 2 (coarse vector relevance) of the funnel.

The point of this package is cost control: the LLM matcher (Task 9) is the only
expensive operation in the system, so everything here exists to hand it a small,
ranked, defensible shortlist. Both stages are observable -- every posting that is
dropped is counted against the rule that dropped it, and every posting that survives
carries the flags that let a human argue with the decision.
"""

from __future__ import annotations

from jme.rank.filters import FilteredPosting, FilterOutcome, apply_filters, stage1_sql
from jme.rank.pipeline import RankResult, run
from jme.rank.score import PostingScore, RequirementMatch, score_postings

__all__ = [
    "FilterOutcome",
    "FilteredPosting",
    "PostingScore",
    "RankResult",
    "RequirementMatch",
    "apply_filters",
    "run",
    "score_postings",
    "stage1_sql",
]

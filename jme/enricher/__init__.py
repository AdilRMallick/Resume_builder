"""Requirement extraction: job description text in, structured requirement rows out.

Public surface:
  * `jme.enricher.prompts.PROMPT_VERSION` - bump alongside any prompt or schema edit
  * `jme.enricher.extraction.extract_requirements` - the one entry point worth calling
  * `jme.enricher.worker.run_worker` - consumer of the `jme:enrich` stream
"""

from __future__ import annotations

from jme.enricher.prompts import PROMPT_VERSION

__all__ = ["PROMPT_VERSION"]

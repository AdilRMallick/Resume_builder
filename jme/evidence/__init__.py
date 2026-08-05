"""Evidence corpus: sources, chunking, embedding, versioning.

The corpus is the "me" side of the match. Everything downstream (ranking, LLM
matching, the gap report) cites an `evidence_chunk` id, so chunk identity has to be
stable across re-ingestion, and the corpus version has to move only when the corpus
actually changes.

Modules:
  * `sources`  - markdown directory + GitHub README loaders
  * `chunker`  - pure, deterministic markdown chunking
  * `ingest`   - diff/upsert, hash-gated embedding, soft delete, manual tags
  * `version`  - the monotonic corpus version and the match staleness sweep
  * `cli`      - `jme evidence ...`

Deliberately no re-exports: `jme.evidence.ingest` must keep resolving to the module,
not to a function of the same name.
"""

from __future__ import annotations

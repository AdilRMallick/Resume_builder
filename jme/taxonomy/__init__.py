"""Canonical skill taxonomy: seed data, normalization, and deterministic resolution.

The taxonomy is the join key for the whole system. Free-text requirements aggregate to
nothing; "Go", "Golang" and "experience with Go" have to collapse onto one id before the
gap report can rank anything.

Public surface::

    from jme.taxonomy import normalize, resolve, resolve_all
    from jme.taxonomy import seed          # the module; seed.seed(session) loads the YAML

Only :mod:`jme.taxonomy.cli` may create a canonical skill. :func:`resolve` writes
unmatched spans to the ``skill_alias_candidate`` review queue instead.
"""

from __future__ import annotations

from jme.taxonomy.normalize import normalize, tokenize
from jme.taxonomy.resolver import invalidate_cache, resolve, resolve_all, scan
from jme.taxonomy.seed import SeedError, SeedReport, SkillSpec, load_specs

__all__ = [
    "SeedError",
    "SeedReport",
    "SkillSpec",
    "invalidate_cache",
    "load_specs",
    "normalize",
    "resolve",
    "resolve_all",
    "scan",
    "tokenize",
]

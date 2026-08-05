"""Plain helpers shared by the rank tests. Deliberately not `conftest.py`: importing a
conftest module by name gives pytest two copies of the same fixtures.
"""

from __future__ import annotations

import datetime as dt

from jme.config import Settings


def make_settings(**overrides: object) -> Settings:
    """Config pinned to explicit values, never inheriting the developer's `.env`.

    `Settings(field=...)` would silently do nothing: every field carries a `JME_*`
    alias and the model is `extra="ignore"`, so keyword arguments given by field name
    are dropped on the floor. `model_copy(update=...)` is the honest way to override.
    """
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    pinned: dict[str, object] = {
        "grad_date": dt.date(2027, 5, 1),
        "location_allowlist": ["Remote", "Detroit", "Ann Arbor", "Michigan", "Chicago"],
        "location_boost": ["Detroit", "Ann Arbor", "Michigan", "Chicago"],
        "role_types": ["swe"],
        "allow_sponsorship_required": False,
        "shortlist_size": 20,
        "embedding_provider": "hash",
    }
    pinned.update(overrides)
    return base.model_copy(update=pinned)

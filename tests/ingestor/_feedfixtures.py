"""Paths and canonical keys for the ingestor fixtures.

A plain module rather than `conftest.py` so test modules can import the constants at
module scope (parametrize needs them before fixtures exist) without shadowing the
project-level `tests/conftest.py`.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

RUN1 = FIXTURES / "listings_run1.json"
RUN2_GLOBEX_GONE = FIXTURES / "listings_run2_globex_gone.json"
RUN3_GLOBEX_BACK = FIXTURES / "listings_run3_globex_back.json"
MALFORMED = FIXTURES / "listings_malformed.json"
EDGE_CASES = FIXTURES / "listings_edge_cases.json"

# canonical keys of the fixture postings, which are their Simplify ids
ACME = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
GLOBEX = "bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb"
INITECH = "cccccccc-3333-4ccc-8ccc-cccccccccccc"
UMBRELLA = "dddddddd-4444-4ddd-8ddd-dddddddddddd"
HOOLI = "eeeeeeee-5555-4eee-8eee-eeeeeeeeeeee"

RUN1_KEYS = {ACME, GLOBEX, INITECH, UMBRELLA}

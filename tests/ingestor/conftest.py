"""Fixtures local to the ingestor suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _feedfixtures import FIXTURES, RUN1  # noqa: E402


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def run1_bytes() -> bytes:
    return RUN1.read_bytes()

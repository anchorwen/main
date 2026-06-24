"""Shared fixtures for data-layer tests (Tier 1 — capital path).

Fixtures for WAL, lifecycle manager, and event writer tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Isolated data directory for WAL / lifecycle manager tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def wal_tmp_path(tmp_path: Path) -> Path:
    """Temporary WAL file path — no real data dir pollution."""
    return tmp_path / "wal_test.jsonl"

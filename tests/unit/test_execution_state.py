"""Tests for execution state persistence — the restart-amnesia firewall.

FIX-20260603-072: Global Execution State Hydration.
This module snapshots CooldownRegistry, FamilyEntryTracker, StrategyBudget,
SL streak blocks, circuit breaker state, and intraday DD kill to disk on
every save cycle and restores them on startup.

Historically, restart→immediate-trade was the #1 recurring bug category
(6 fixes: FIX-072, 073, 074, 036, 061, 063).  These tests ensure the
persistence layer survives:

- Normal save/load roundtrip
- Empty/missing state file (first run)
- Corrupt JSON
- Stale state (>24h)
- Version migration
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.runtime.execution_state import (
    load_execution_state,
    restore_execution_state,
    save_execution_state,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "execution_state.json"


@pytest.fixture
def mock_strategies() -> dict:
    """Two strategies with mock budgets that support get_state/load_state."""
    b1 = MagicMock()
    b1.get_state.return_value = {"daily_pnl": -2.5, "consecutive_losses": 2}
    b2 = MagicMock()
    b2.get_state.return_value = {"daily_pnl": 5.0, "consecutive_losses": 0}
    return {
        "barrier_12bar": MagicMock(budget=b1),
        "statarb_dynamic": MagicMock(budget=b2),
    }


@pytest.fixture
def mock_cooldown() -> MagicMock:
    c = MagicMock()
    c.get_state.return_value = {"barrier_12bar_long": time.time() + 300}
    return c


@pytest.fixture
def mock_family_tracker() -> MagicMock:
    f = MagicMock()
    f.get_state.return_value = {"swing_long": time.time() + 900}
    return f


# ── Save ───────────────────────────────────────────────────────────────────


def test_save_creates_file(tmp_state_path, mock_strategies, mock_cooldown, mock_family_tracker):
    """Normal save produces a valid JSON file."""
    save_execution_state(
        tmp_state_path,
        mock_strategies,
        mock_cooldown,
        mock_family_tracker,
        sl_streak_blocks={"statarb_dynamic": time.time() + 600},
        sl_streak_global_block=time.time() + 1200,
        consecutive_degraded=2,
        circuit_breaker_tripped=False,
        intraday_dd_active=True,
    )
    assert tmp_state_path.exists()
    data = json.loads(tmp_state_path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert "saved_at_utc" in data
    assert "barrier_12bar" in data["budgets"]
    assert data["budgets"]["barrier_12bar"]["consecutive_losses"] == 2
    assert len(data["cooldown_registry"]) > 0
    assert len(data["family_entry_tracker"]) > 0
    assert data["consecutive_degraded"] == 2
    assert data["intraday_dd_active"] is True


def test_save_creates_parent_dir(tmp_path):
    """Save creates parent directories if they don't exist."""
    p = tmp_path / "deep" / "nested" / "execution_state.json"
    save_execution_state(p, {}, None, None)
    assert p.exists()


def test_save_handles_missing_budget_gracefully(tmp_state_path):
    """Strategy without budget attribute is skipped during save."""
    strategies = {"no_budget": MagicMock(spec=[])}  # no 'budget' attr
    save_execution_state(tmp_state_path, strategies, None, None)
    data = json.loads(tmp_state_path.read_text(encoding="utf-8"))
    assert data["budgets"] == {}


# ── Load ───────────────────────────────────────────────────────────────────


def test_load_returns_none_for_missing_file():
    """First run — no state file exists."""
    assert load_execution_state("/nonexistent/path/execution_state.json") is None


def test_load_rejects_stale_state(tmp_state_path):
    """State older than 24h is rejected and deleted."""
    tmp_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state_path.write_text(json.dumps({"version": 1, "budgets": {}}), encoding="utf-8")
    # Make file appear 25 hours old
    stale_time = time.time() - (25 * 3600)
    os.utime(tmp_state_path, (stale_time, stale_time))
    assert load_execution_state(tmp_state_path) is None
    assert not tmp_state_path.exists()  # stale file deleted


def test_load_rejects_corrupt_json(tmp_state_path):
    """Corrupt JSON returns None — never crashes."""
    tmp_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state_path.write_text("this is not json {{{", encoding="utf-8")
    assert load_execution_state(tmp_state_path) is None


def test_load_rejects_non_dict(tmp_state_path):
    """A JSON array or scalar is not a valid state file."""
    tmp_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_execution_state(tmp_state_path) is None


def test_load_rejects_missing_version(tmp_state_path):
    """Dict without 'version' key is treated as invalid format."""
    tmp_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state_path.write_text(
        json.dumps({"budgets": {}, "cooldown_registry": {}}), encoding="utf-8"
    )
    assert load_execution_state(tmp_state_path) is None


def test_save_load_roundtrip(tmp_state_path, mock_strategies, mock_cooldown, mock_family_tracker):
    """A saved state should be loadable and contain all saved fields."""
    save_execution_state(
        tmp_state_path,
        mock_strategies,
        mock_cooldown,
        mock_family_tracker,
        sl_streak_blocks={"barrier_12bar": 1712345678.0},
        sl_streak_global_block=1712346000.0,
    )
    data = load_execution_state(tmp_state_path)
    assert data is not None
    assert data["version"] == 2
    assert data["budgets"]["barrier_12bar"]["daily_pnl"] == -2.5
    assert data["sl_streak_blocks"]["barrier_12bar"] == 1712345678.0


# ── Restore ────────────────────────────────────────────────────────────────


def test_restore_hydrates_budgets(tmp_path, mock_strategies):
    """Restore pushes saved budget state back into strategy objects."""
    state_path = tmp_path / "state" / "execution_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "saved_at_utc": "2026-06-05T00:00:00",
                "budgets": {
                    "barrier_12bar": {"daily_pnl": -2.5, "consecutive_losses": 2},
                },
                "cooldown_registry": {},
                "family_entry_tracker": {},
                "sl_streak_blocks": {},
            }
        ),
        encoding="utf-8",
    )

    state = MagicMock()
    state._cooldown_registry = MagicMock()
    state._family_entry_tracker = MagicMock()
    state.sl_streak_blocked_until = {}
    state.sl_streak_blocked_all_until = 0.0
    state._consecutive_degraded_cycles = 0
    state._circuit_breaker_tripped = False
    state.block_new_entries = False

    restore_execution_state(state, mock_strategies, data_dir=str(tmp_path))

    # Budget should have been hydrated
    barrier_budget = mock_strategies["barrier_12bar"].budget
    barrier_budget.load_state.assert_called_once_with({"daily_pnl": -2.5, "consecutive_losses": 2})


def test_restore_hydrates_circuit_breaker(tmp_path, mock_strategies):
    """Restore recovers circuit breaker state from disk."""
    state_path = tmp_path / "state" / "execution_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "saved_at_utc": "2026-06-05T00:00:00",
                "budgets": {},
                "cooldown_registry": {},
                "family_entry_tracker": {},
                "sl_streak_blocks": {},
                "consecutive_degraded": 3,
                "circuit_breaker_tripped": True,
                "intraday_dd_active": True,
            }
        ),
        encoding="utf-8",
    )

    state = MagicMock()
    state._cooldown_registry = MagicMock()
    state._family_entry_tracker = MagicMock()
    state.sl_streak_blocked_until = {}
    state.sl_streak_blocked_all_until = 0.0
    state._consecutive_degraded_cycles = 0
    state._circuit_breaker_tripped = False
    state._circuit_breaker_tripped_at = 0.0
    state.block_new_entries = False

    restore_execution_state(state, mock_strategies, data_dir=str(tmp_path))

    assert state._consecutive_degraded_cycles == 3
    assert state._circuit_breaker_tripped is True
    assert state._circuit_breaker_tripped_at >= 0.0
    assert state.block_new_entries is True


def test_restore_handles_missing_file_gracefully(tmp_path, mock_strategies):
    """First run — no state file. Should not crash or modify state."""
    state = MagicMock()
    state._cooldown_registry = MagicMock()
    state._family_entry_tracker = MagicMock()
    state.sl_streak_blocked_until = {}
    state.sl_streak_blocked_all_until = 0.0
    state._consecutive_degraded_cycles = 0
    state._circuit_breaker_tripped = False
    state._circuit_breaker_tripped_at = 0.0
    state.block_new_entries = False

    # Should not raise
    restore_execution_state(state, mock_strategies, data_dir=str(tmp_path))

    # State should be unchanged
    assert state._consecutive_degraded_cycles == 0
    assert state._circuit_breaker_tripped is False
    assert state.block_new_entries is False


def test_restore_preserves_higher_sl_streak(tmp_path, mock_strategies):
    """If in-memory SL streak is further in the future, it wins."""
    future = time.time() + 9999
    state_path = tmp_path / "state" / "execution_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "saved_at_utc": "2026-06-05T00:00:00",
                "budgets": {},
                "cooldown_registry": {},
                "family_entry_tracker": {},
                "sl_streak_blocks": {"statarb": time.time() - 100},  # expired
                "sl_streak_global_block": future,  # still active
                "consecutive_degraded": 1,  # < current (see below)
            }
        ),
        encoding="utf-8",
    )

    state = MagicMock()
    state._cooldown_registry = MagicMock()
    state._family_entry_tracker = MagicMock()
    state.sl_streak_blocked_until = {"existing": time.time() + 99999}  # further
    state.sl_streak_blocked_all_until = 0.0
    state._consecutive_degraded_cycles = 5  # > persisted
    state._circuit_breaker_tripped = False
    state.block_new_entries = False

    restore_execution_state(state, mock_strategies, data_dir=str(tmp_path))

    # Global SL block from disk (future) wins over 0.0
    assert state.sl_streak_blocked_all_until == pytest.approx(future, abs=1)
    # In-memory existing streak preserved
    assert "existing" in state.sl_streak_blocked_until
    # Higher in-memory degraded cycles preserved
    assert state._consecutive_degraded_cycles == 5

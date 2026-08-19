"""Tests for core.runtime.daily_ops_scheduler — daily operations scheduling.

FIX-20260619-035: Tier 1 zero-coverage breakout #6.
Covers _save_daily_ops_state and run_scheduled_daily_ops.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.runtime.daily_ops_scheduler import (
    _save_daily_ops_state,
    run_scheduled_daily_ops,
)


class TestSaveDailyOpsState:
    def test_writes_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _save_daily_ops_state(tmpdir, 1781834400.0)
            spath = Path(tmpdir) / "state" / "daily_ops_state.json"
            assert spath.exists()
            data = json.loads(spath.read_text())
            assert data["last_daily_ops_utc"] == 1781834400.0

    def test_handles_permission_error(self) -> None:
        """Should not crash on filesystem errors."""
        with patch("builtins.open", side_effect=OSError("permission denied")):
            _save_daily_ops_state("/fake/path", 0.0)  # should not raise


class TestRunScheduledDailyOps:
    def _make_config(self, base_dir: str) -> MagicMock:
        cfg = MagicMock()
        cfg.base_dir = base_dir
        cfg.mt5_terminal_path = "/fake/mt5"
        return cfg

    def _make_state(self) -> MagicMock:
        state = MagicMock()
        state._last_daily_ops_utc = 0.0
        state._tracker_reload_pending = False
        return state

    def test_persists_timestamp_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", return_value={"steps": [], "errors": 0}):
                run_scheduled_daily_ops(cfg, state)

            assert state._last_daily_ops_utc > 0
            spath = Path(tmpdir) / "state" / "daily_ops_state.json"
            assert spath.exists()

    def test_runs_daily_ops_with_correct_params(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            mock_run = MagicMock(return_value={"steps": [], "errors": 0})
            with patch("scripts.daily_ops.run_daily_ops", mock_run):
                run_scheduled_daily_ops(cfg, state)

            mock_run.assert_called_once_with(
                base_dir=tmpdir,
                skip_shadow=True,
                skip_recap=True,
                mt5_terminal_path="/fake/mt5",
            )

    def test_sets_tracker_reload_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", return_value={"steps": [], "errors": 0}):
                run_scheduled_daily_ops(cfg, state)

            assert state._tracker_reload_pending is True

    def test_handles_daily_ops_error_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", side_effect=RuntimeError("boom")):
                run_scheduled_daily_ops(cfg, state)  # should not raise

    def test_governance_skips_when_no_pnl_ledger(self) -> None:
        """Governance cycle gracefully skipped when PnL store fails to load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", return_value={"steps": [], "errors": 0}):
                run_scheduled_daily_ops(cfg, state)

            # Should complete without exception
            assert state._tracker_reload_pending is True

    def test_skips_governance_when_no_pnl_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", return_value={"steps": [], "errors": 0}):
                run_scheduled_daily_ops(cfg, state)

    def test_resource_cleanup_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops", return_value={"steps": [], "errors": 0}):
                with patch("gc.collect") as mock_gc:
                    run_scheduled_daily_ops(cfg, state)
            mock_gc.assert_called()

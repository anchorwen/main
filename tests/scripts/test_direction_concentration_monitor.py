"""Tests for scripts/_monitor_direction_concentration — FIX-20260804-008.

Locks the three defects fixed by DQAF-20260804-008:
1. ``normalize_direction`` — case + BUY/SELL normalization (was matching
   uppercase against lowercase golden_master values → always silent).
2. ``_extract_gm_direction`` — nested ``outputs.<strategy>.direction``
   extraction (was reading a top-level ``direction`` field that never exists).
3. ``_scheduled_monitor`` — monitors ALL asset dirs (was hardcoded to
   ``data_btc`` → XAU degeneracy never checked).
"""

from __future__ import annotations

import scripts._monitor_direction_concentration as mon


class TestNormalizeDirection:
    def test_lowercase_short(self) -> None:
        assert mon.normalize_direction("short") == "SHORT"

    def test_uppercase_long(self) -> None:
        assert mon.normalize_direction("LONG") == "LONG"

    def test_buy_normalized_to_long(self) -> None:
        assert mon.normalize_direction("BUY") == "LONG"

    def test_sell_normalized_to_short(self) -> None:
        assert mon.normalize_direction("SELL") == "SHORT"

    def test_neutral_excluded(self) -> None:
        assert mon.normalize_direction("neutral") == ""

    def test_empty_excluded(self) -> None:
        assert mon.normalize_direction("") == ""


class TestExtractGmDirection:
    def test_nested_outputs_first_strategy(self) -> None:
        row = {
            "outputs": {
                "h1_swing": {"direction": "short"},
                "m15_swing": {"direction": "neutral"},
            }
        }
        assert mon._extract_gm_direction(row) == "SHORT"

    def test_nested_outputs_all_neutral(self) -> None:
        row = {"outputs": {"h1_swing": {"direction": "neutral"}}}
        assert mon._extract_gm_direction(row) == ""

    def test_top_level_fallback(self) -> None:
        assert mon._extract_gm_direction({"direction": "long"}) == "LONG"

    def test_predicted_direction_fallback(self) -> None:
        assert mon._extract_gm_direction({"predicted_direction": "BUY"}) == "LONG"

    def test_missing_fields(self) -> None:
        assert mon._extract_gm_direction({}) == ""


class TestScheduledMonitor:
    def _fake_result(self, status: str = "balanced") -> dict:
        return {
            "status": status,
            "ratio": 0.5,
            "dominant": "SHORT",
            "total_signals": 10,
            "total_trades": 3,
            "signals_long": 5,
            "signals_short": 5,
            "window_hours": 4,
            "detail": {},
        }

    def test_monitors_all_asset_dirs(self, monkeypatch) -> None:
        """Scheduler wrapper must check every asset dir, not just data_btc."""
        checked: list[str] = []

        def fake_run(data_dir: str, window_hours: int) -> dict:
            checked.append(data_dir)
            return self._fake_result()

        monkeypatch.setattr(mon, "run_monitor", fake_run)
        mon._scheduled_monitor()
        assert set(checked) == set(mon.DEFAULT_ASSET_DIRS)

    def test_worst_status_wins(self, monkeypatch) -> None:
        """XAU critical must override BTC balanced in the aggregated result."""
        states = iter(["balanced", "critical"])

        def fake_run(data_dir: str, window_hours: int) -> dict:
            return self._fake_result(status=next(states))

        monkeypatch.setattr(mon, "run_monitor", fake_run)
        worst = mon._scheduled_monitor()
        assert worst == "critical"

    def test_default_asset_dirs_include_xau(self) -> None:
        assert "data" in mon.DEFAULT_ASSET_DIRS
        assert "data_btc" in mon.DEFAULT_ASSET_DIRS

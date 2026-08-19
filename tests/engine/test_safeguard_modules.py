"""Smoke tests for the three execution safeguard modules (Pitfall 1-3).

Exercises core lifecycle without requiring MT5 — validates state machines,
retry logic, persistence, and integration with the shadow-mode pipeline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from core.execution.exit_watchdog import (
    ExitWatchdog,
    ExitWatchdogResult,
)
from core.execution.limit_order_monitor import (
    MAX_WAIT_BARS,
    LimitOrderMonitor,
)
from core.protocol.event_bar_sync import (
    BarSyncPoller,
)

# ═══════════════════════════════════════════════════════════════════════════
# LimitOrderMonitor
# ═══════════════════════════════════════════════════════════════════════════


class TestLimitOrderMonitor:
    def test_place_creates_pending_order(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        intent = monitor.place(
            signal_bar=100,
            direction="long",
            signal_close=2643.50,
            entry_atr=8.2,
            spread_points=15,
        )
        assert monitor.pending_count() == 1
        assert monitor.has_pending()
        assert intent.direction == "long"
        assert intent.signal_close == 2643.50
        assert intent.status == "pending"
        assert intent.limit_price < intent.signal_close  # long: limit below market

    def test_short_limit_above_market(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        intent = monitor.place(
            signal_bar=100,
            direction="short",
            signal_close=2643.50,
            entry_atr=8.2,
            spread_points=15,
        )
        assert intent.direction == "short"
        assert intent.limit_price > intent.signal_close  # short: limit above market

    def test_long_fills_when_price_drops(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        monitor.place(
            signal_bar=100,
            direction="long",
            signal_close=2643.50,
            entry_atr=8.2,
            spread_points=15,
            current_bar=100,
        )
        # Price drops below limit → fill
        fill = monitor.check_fill(
            current_bar=101,
            bid=2642.00,
            ask=2642.30,
            spread_points=18,
            low=2641.50,
        )
        assert fill.filled
        assert fill.fill_price is not None
        assert monitor.pending_count() == 0

    def test_short_fills_when_price_rises(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        monitor.place(
            signal_bar=100,
            direction="short",
            signal_close=2643.50,
            entry_atr=8.2,
            spread_points=15,
            current_bar=100,
        )
        fill = monitor.check_fill(
            current_bar=101,
            bid=2644.20,
            ask=2644.50,
            spread_points=18,
            high=2645.00,
        )
        assert fill.filled

    def test_expires_after_max_wait_bars(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        monitor.place(
            signal_bar=100,
            direction="long",
            signal_close=2643.50,
            entry_atr=8.2,
            current_bar=100,
        )
        # Jump ahead past MAX_WAIT_BARS
        fill = monitor.check_fill(
            current_bar=100 + MAX_WAIT_BARS + 2,
            bid=2645.00,
            ask=2645.30,
        )
        assert not fill.filled
        assert fill.should_cancel
        assert "ttf_exceeded" in fill.cancel_reason
        assert monitor.pending_count() == 0

    def test_cancel_all(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        for i in range(3):
            monitor.place(
                signal_bar=100 + i,
                direction="long",
                signal_close=2643.50,
                entry_atr=8.2,
                current_bar=100 + i,
            )
        assert monitor.pending_count() == 3
        n = monitor.cancel_all(reason="emergency")
        assert n == 3
        assert monitor.pending_count() == 0

    def test_get_stats(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        monitor.place(
            signal_bar=100, direction="long", signal_close=2643.50, entry_atr=8.2, current_bar=100
        )
        monitor.check_fill(current_bar=101, bid=2642.00, ask=2642.30, low=2641.50)
        stats = monitor.get_stats()
        assert stats["total_placed"] == 1
        assert stats["filled"] == 1
        assert stats["fill_rate"] == 1.0
        assert stats["avg_ttf_bars"] == 1.0

    def test_jsonl_persistence(self, tmp_path: Path) -> None:
        monitor = LimitOrderMonitor(data_dir=str(tmp_path))
        monitor.place(
            signal_bar=100, direction="long", signal_close=2643.50, entry_atr=8.2, current_bar=100
        )
        monitor.check_fill(current_bar=101, bid=2642.00, ask=2642.30, low=2641.50)
        # Verify log file exists and contains valid JSON
        log_files = list(Path(monitor.data_dir).glob("*.jsonl"))
        assert len(log_files) > 0
        for lf in log_files:
            for line in lf.read_text(encoding="utf-8").strip().split("\n"):
                rec = json.loads(line)
                assert "event" in rec
                assert "intent_id" in rec


# ═══════════════════════════════════════════════════════════════════════════
# ExitWatchdog
# ═══════════════════════════════════════════════════════════════════════════


class TestExitWatchdog:
    def test_successful_dispatch_on_first_attempt(self, tmp_path: Path) -> None:
        watchdog = ExitWatchdog(
            data_dir=str(tmp_path), ack_poll_interval=0.01, ack_poll_timeout=0.1
        )

        def dispatch_ok(payload: dict) -> dict:
            # Write the ACK file so _poll_ack finds it — use the same date format as watchdog
            from datetime import UTC
            from datetime import datetime as dt

            today = dt.now(UTC).strftime("%Y-%m-%d")
            ack_dir = Path(tmp_path) / "receipts" / today / "exec_bridge"
            ack_dir.mkdir(parents=True, exist_ok=True)
            intent_id = "ok-" + str(payload.get("position_ticket", ""))
            ack_path = ack_dir / f"{intent_id}.ack.json"
            ack_path.write_text(json.dumps({"ack_status": "accepted"}), encoding="utf-8")
            return {"dispatched": True, "intent_id": intent_id}

        result = watchdog.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="test_exit",
            dispatch_fn=dispatch_ok,
        )
        assert result.success
        assert result.final_status == "closed"
        assert result.total_attempts == 1

    def test_retry_on_dispatch_failure(self, tmp_path: Path) -> None:
        call_count = [0]

        def dispatch_fail_twice(payload: dict) -> dict:
            from datetime import UTC
            from datetime import datetime as dt

            call_count[0] += 1
            if call_count[0] <= 2:
                return {"dispatched": False, "reason": "mt5_busy"}
            # Third attempt: succeed
            today = dt.now(UTC).strftime("%Y-%m-%d")
            ack_dir = Path(tmp_path) / "receipts" / today / "exec_bridge"
            ack_dir.mkdir(parents=True, exist_ok=True)
            intent_id = "retry-" + str(payload.get("position_ticket", ""))
            ack_path = ack_dir / f"{intent_id}.ack.json"
            ack_path.write_text(json.dumps({"ack_status": "accepted"}), encoding="utf-8")
            return {"dispatched": True, "intent_id": intent_id}

        watchdog = ExitWatchdog(
            data_dir=str(tmp_path), ack_poll_interval=0.01, ack_poll_timeout=0.1
        )
        result = watchdog.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="test_exit",
            dispatch_fn=dispatch_fail_twice,
        )
        assert result.success
        assert result.total_attempts >= 2  # retried

    def test_escalation_after_all_retries_exhausted(self, tmp_path: Path) -> None:
        def dispatch_never_ok(payload: dict) -> dict:
            return {"dispatched": False, "reason": "permanent_failure"}

        watchdog = ExitWatchdog(
            data_dir=str(tmp_path),
            max_retries=3,
            max_total_duration=999.0,
            ack_poll_interval=0.01,
            ack_poll_timeout=0.05,
        )
        result = watchdog.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="test_exit",
            dispatch_fn=dispatch_never_ok,
        )
        assert not result.success
        assert result.final_status == "escalated"
        assert result.total_attempts == 3
        assert len(result.alerts) >= 1
        assert "ESCALATED" in result.alerts[0]

    def test_critical_timeout(self, tmp_path: Path) -> None:
        def dispatch_slow(payload: dict) -> dict:
            time.sleep(0.3)
            return {"dispatched": False, "reason": "slow"}

        watchdog = ExitWatchdog(
            data_dir=str(tmp_path),
            max_retries=10,
            max_total_duration=0.3,  # very short — triggers timeout on attempt 2
            ack_poll_interval=0.01,
            ack_poll_timeout=0.05,
        )
        result = watchdog.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="test_exit",
            dispatch_fn=dispatch_slow,
        )
        assert not result.success
        assert result.final_status == "critical_timeout"
        assert "CRITICAL" in (result.alerts[0] if result.alerts else "")

    def test_alert_persistence(self, tmp_path: Path) -> None:
        watchdog = ExitWatchdog(data_dir=str(tmp_path))

        def dispatch_fail(payload: dict) -> dict:
            return {"dispatched": False}

        watchdog.execute_exit(
            position_ticket=999,
            volume=0.05,
            side="short",
            reason="persist_test",
            dispatch_fn=dispatch_fail,
        )
        alert_path = Path(tmp_path) / "reports" / "exit_watchdog_alerts.jsonl"
        assert alert_path.exists()
        lines = alert_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        alert = json.loads(lines[-1])
        assert alert["ticket"] == 999
        assert alert["reason"] == "persist_test"

    def test_is_healthy_default(self, tmp_path: Path) -> None:
        watchdog = ExitWatchdog(data_dir=str(tmp_path))
        assert watchdog.is_healthy()

    def test_slippage_escalation(self, tmp_path: Path) -> None:
        watchdog = ExitWatchdog(data_dir=str(tmp_path))
        # Attempt 1-2: normal (20)
        assert watchdog._slippage_for_attempt(1) == 20
        assert watchdog._slippage_for_attempt(2) == 20
        # Attempt 3-4: escalated (50)
        assert watchdog._slippage_for_attempt(3) == 50
        assert watchdog._slippage_for_attempt(4) == 50
        # Attempt 5: emergency (200)
        assert watchdog._slippage_for_attempt(5) == 200
        # Beyond: emergency stays
        assert watchdog._slippage_for_attempt(6) == 200

    def test_dispatches_long_side_close_payload(self, tmp_path: Path) -> None:
        payload = ExitWatchdog._build_close_payload(
            position_ticket=42,
            volume=0.03,
            side="long",
            reason="z_reversion",
            magic=90001,
            brain_ids=["brain_a", "brain_b"],
        )
        assert payload["action"] == "close"
        assert payload["position_ticket"] == 42
        assert payload["volume"] == 0.03
        assert payload["side"] == "long"
        assert "z_reversion" in payload["comment"]
        assert payload["magic"] == 90001
        assert payload["brain_ids"] == ["brain_a", "brain_b"]


# ═══════════════════════════════════════════════════════════════════════════
# BarSyncPoller (without real MT5)
# ═══════════════════════════════════════════════════════════════════════════


class TestBarSyncPoller:
    def test_state_save_and_load(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "sync_state"
        state_dir.mkdir()
        poller = BarSyncPoller(state_dir=str(state_dir), terminal_path=None)
        # Manually set state as if we've seen bars
        poller._state.last_bar_time = 1715700000
        poller._state.total_bars_seen = 42
        poller._state.lag_count = 1
        poller._save_state()

        # New poller loads the saved state
        poller2 = BarSyncPoller(state_dir=str(state_dir), terminal_path=None)
        assert poller2._state.last_bar_time == 1715700000
        assert poller2._state.total_bars_seen == 42
        assert poller2._state.lag_count == 1

    def test_bar_seconds_for(self) -> None:
        assert BarSyncPoller._bar_seconds_for("M1") == 60
        assert BarSyncPoller._bar_seconds_for("M5") == 300
        assert BarSyncPoller._bar_seconds_for("M15") == 900
        assert BarSyncPoller._bar_seconds_for("H1") == 3600
        assert BarSyncPoller._bar_seconds_for("H4") == 14400
        assert BarSyncPoller._bar_seconds_for("D1") == 86400
        assert BarSyncPoller._bar_seconds_for("UNKNOWN") == 300  # default

    def test_timeframe_map(self) -> None:
        try:
            import MetaTrader5 as mt5  # noqa: F401

            assert BarSyncPoller._timeframe_map("M5") == getattr(mt5, "TIMEFRAME_M5", 5)
        except ImportError:
            pytest.skip("MetaTrader5 not available")

    def test_get_state_initially_empty(self, tmp_path: Path) -> None:
        poller = BarSyncPoller(state_dir=str(tmp_path), terminal_path=None)
        state = poller.get_state()
        assert state["last_bar_time"] == 0
        assert state["total_bars_seen"] == 0
        assert state["lag_count"] == 0

    def test_reset_lag(self, tmp_path: Path) -> None:
        poller = BarSyncPoller(state_dir=str(tmp_path), terminal_path=None)
        poller._state.lag_count = 5
        poller.reset_lag()
        assert poller._state.lag_count == 0

    @patch("core.protocol.event_bar_sync.BarSyncPoller._init_mt5")
    def test_wait_for_new_bar_returns_none_when_mt5_unavailable(
        self, mock_init: MagicMock, tmp_path: Path
    ) -> None:
        """When MT5 init fails, wait_for_new_bar should sleep fallback and return None."""
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            fallback_interval=0.01,
            timeout_seconds=0.05,
            poll_interval=0.01,
        )
        poller._mt5_available = False  # simulate failed init
        result = poller.wait_for_new_bar(timeout_seconds=0.05)
        assert result is None  # fallback path

    @patch("core.protocol.event_bar_sync.BarSyncPoller._init_mt5")
    def test_wait_for_new_bar_timeout(self, mock_init: MagicMock, tmp_path: Path) -> None:
        """With MT5 up but no new bar, should time out and return None."""
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            poll_interval=0.01,
            timeout_seconds=0.05,
        )
        poller._mt5_available = True
        # Set last_bar_time to match the mock data → no new bar detected → timeout
        poller._state.last_bar_time = 1715700000
        # mock copy_rates_from_pos to always return the same bar
        with patch("core.protocol.event_bar_sync.BarSyncPoller._timeframe_map", return_value=5):
            mock_mt5 = MagicMock()
            mock_mt5.copy_rates_from_pos.return_value = [
                {
                    "time": 1715700000,
                    "open": 2640.0,
                    "high": 2645.0,
                    "low": 2639.0,
                    "close": 2643.0,
                    "tick_volume": 1000,
                    "spread": 8,
                    "real_volume": 0,
                },
                {
                    "time": 1715700000,
                    "open": 2640.0,
                    "high": 2645.0,
                    "low": 2639.0,
                    "close": 2643.0,
                    "tick_volume": 1000,
                    "spread": 8,
                    "real_volume": 0,
                },
            ]
            with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
                import core.protocol.event_bar_sync as bsync

                cast(
                    Any, bsync.BarSyncPoller
                )._init_mt5 = MagicMock()  # TECH_DEBT-009: method-assign 规避 (A3)
                result = poller.wait_for_new_bar(timeout_seconds=0.05)
                assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Integration: dry-run through the entry/exit paths
# ═══════════════════════════════════════════════════════════════════════════


class TestSafeguardIntegration:
    """Validates that all three modules can coexist without import or API conflicts."""

    def test_all_three_instantiate_side_by_side(self, tmp_path: Path) -> None:
        lm = LimitOrderMonitor(data_dir=str(tmp_path / "limit_orders"))
        ew = ExitWatchdog(data_dir=str(tmp_path / "exit_watchdog"))
        bs = BarSyncPoller(state_dir=str(tmp_path / "bar_sync"), terminal_path=None)

        assert lm.pending_count() == 0
        assert ew.is_healthy()
        assert bs.get_state()["total_bars_seen"] == 0

    def test_live_cycle_state_fields(self) -> None:
        """Verify LiveCycleState has the required fields."""
        from core.runtime.live_cycle import LiveCycleState

        state = LiveCycleState()
        assert state.exit_watchdog is None
        assert state.limit_monitor is None

    def test_limit_monitor_place_then_watchdog_close_flow(self, tmp_path: Path) -> None:
        """Simulate: entry recorded → exit dispatched via watchdog."""
        lm = LimitOrderMonitor(data_dir=str(tmp_path / "limit_orders"))

        # Entry: record a limit-equivalent order
        lm.place(
            signal_bar=100,
            direction="long",
            signal_close=2643.50,
            entry_atr=8.2,
            spread_points=15,
            current_bar=100,
        )

        # Price drops — fill
        fill = lm.check_fill(current_bar=101, bid=2642.00, ask=2642.30, low=2641.50)
        assert fill.filled

        # Now pretend we need to close — watchdog
        ew = ExitWatchdog(
            data_dir=str(tmp_path / "exit_watchdog"),
            ack_poll_interval=0.01,
            ack_poll_timeout=0.05,
        )

        def dispatch_ok(payload: dict) -> dict:
            assert payload["action"] == "close"
            assert payload["position_ticket"] == fill.fill_bar  # bar=101 used as ticket
            return {"dispatched": True, "intent_id": "test-close-1"}

        # Note: in real life, position_ticket comes from MT5; here we use fill_bar as placeholder
        result = ew.execute_exit(
            position_ticket=fill.fill_bar or 0,
            volume=0.05,
            side="long",
            reason="z_reversion",
            dispatch_fn=dispatch_ok,
        )
        # Without ACK file, this won't succeed — but shouldn't crash
        assert isinstance(result, ExitWatchdogResult)

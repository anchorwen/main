"""FIX-20260820-001 (TECH_DEBT-013): watchdog heartbeat pulse regression locks.

Daily XAU market close (21:00-22:00 UTC) leaves bar_sync blocking for the
next M5 bar that never forms (up to bar_period + 10s = 310s).  The in-process
watchdog in live_intent_loop.py hard-kills (os._exit(1)) after 300s without a
heartbeat refresh -- 11-14 restarts/day (watchdog_kill.log 2026-08-19
21:00:06-21:54:47 eleven consecutive kills, elapsed ~307.6s each).

FIX-20260820-001 wires a ``heartbeat_refresh`` pulse into BarSyncPoller so
the watchdog sees "alive" during legitimate waits.  Regression locks:
  1. Pulse fires repeatedly during the poll loop (market-close blocking).
  2. Pulse fires before the session-off fallback sleep (weekend path).
  3. Degraded deadline capped below watchdog 300s when NO pulse is wired;
     extends to bar-boundary (310s) when a pulse is present -- the M5
     300s-bar-period == 300s-watchdog paradox (lower-component timeout cannot
     be compressed below the guardian for M5; the pulse is the enabler).
  4. Pulse callback failure never blocks bar_sync (BLE001:FOG).
  5. BTC control: crypto_24_7 never returns risk_tier="off"; the pulse works
     during continuous crypto polling (no session-gate skip, no mis-kill).
  6. Config alignment: live.yaml == live_btc.yaml bar_sync_timeout (IC order).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from core.protocol.event_bar_sync import BarSyncPoller

# -- helpers --


def _pulse() -> tuple[MagicMock, Callable[[], None]]:
    """A heartbeat_refresh callback backed by a MagicMock (cast-not-ignore).

    TECH_DEBT-009: warn_unused_ignores=true makes `# type: ignore` fatal in
    isolated mode -- cast() is the dual-mode-clean pattern.
    """
    m = MagicMock()
    return m, cast(Callable[[], None], m)


def _stale_bars(t: int = 1715700000) -> list[dict[str, Any]]:
    """Two identical bars -- the market-close / no-new-bar scenario."""
    return [
        {
            "time": t,
            "open": 2640.0,
            "high": 2645.0,
            "low": 2639.0,
            "close": 2643.0,
            "tick_volume": 1000,
            "spread": 8,
            "real_volume": 0,
        },
        {
            "time": t,
            "open": 2640.0,
            "high": 2645.0,
            "low": 2639.0,
            "close": 2643.0,
            "tick_volume": 1000,
            "spread": 8,
            "real_volume": 0,
        },
    ]


def _mock_mt5_module() -> MagicMock:
    mock_mt5 = MagicMock()
    mock_mt5.copy_rates_from_pos.return_value = _stale_bars()
    return mock_mt5


# -- regression locks --


class TestBarSyncHeartbeatPulse:
    """1. Market-close blocking: pulse must keep the watchdog asleep."""

    def test_pulse_fires_repeatedly_during_poll_loop_market_close(self, tmp_path: Path) -> None:
        """Daily-close scenario: stale bar forever, gate returns 'caution'
        (not 'off' -- the exact gate failure that caused the kill cluster).
        BarSyncPoller must pulse per poll iteration so the watchdog never
        sees a 300s stall.
        """
        pulse, pulse_fn = _pulse()
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            poll_interval=0.01,
            fallback_interval=0.01,
            heartbeat_refresh=pulse_fn,
        )
        poller._mt5_available = True
        poller._state.last_bar_time = 1715700000

        with patch(
            "core.execution.pre_trade_guards.detect_session",
            return_value={
                "session_name": "pre_close",
                "volume_mult": 0.5,
                "sl_expand_mult": 1.5,
                "risk_tier": "caution",  # daily close -- gate does NOT skip
            },
        ):
            with patch.dict("sys.modules", {"MetaTrader5": _mock_mt5_module()}):
                result = poller.wait_for_new_bar(timeout_seconds=0.05)

        assert result is None  # timed out waiting for the (never-arriving) bar
        assert pulse.call_count >= 2  # pulsed across poll iterations

    def test_pulse_fires_before_session_off_fallback_sleep(self, tmp_path: Path) -> None:
        """Weekend path (risk_tier='off'): gate sleeps fallback_interval and
        returns None -- the pulse must fire before that sleep too."""
        pulse, pulse_fn = _pulse()
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            fallback_interval=0.01,
            heartbeat_refresh=pulse_fn,
        )

        with patch(
            "core.execution.pre_trade_guards.detect_session",
            return_value={
                "session_name": "weekend",
                "volume_mult": 0.0,
                "sl_expand_mult": 1.0,
                "risk_tier": "off",
            },
        ):
            result = poller.wait_for_new_bar(timeout_seconds=0.05)

        assert result is None
        pulse.assert_called_once()  # pulsed before the fallback sleep

    def test_degraded_deadline_capped_below_watchdog_without_pulse(self, tmp_path: Path) -> None:
        """No pulse wired: degraded must fire BEFORE the watchdog's 300s stall
        threshold so a mis-wired caller degrades gracefully instead of being
        hard-killed.  M5: min(300 + 10, 270) = 270 < 300."""
        poller = BarSyncPoller(state_dir=str(tmp_path), terminal_path=None)
        assert poller._degraded_wait_seconds() == 270.0
        assert poller._degraded_wait_seconds() < 300.0  # watchdog threshold

    def test_degraded_deadline_bar_boundary_with_pulse(self, tmp_path: Path) -> None:
        """Pulse wired (production): degraded stays at bar-boundary
        (bar_period + 10s buffer) -- the pulse keeps the watchdog asleep, so
        normal trading never sees a premature degraded wakeup before the real
        M5 bar forms at 300s."""
        pulse, pulse_fn = _pulse()
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            heartbeat_refresh=pulse_fn,
        )
        assert poller._degraded_wait_seconds() == 310.0  # 300 + 10s buffer

    def test_pulse_failure_never_blocks_bar_sync(self, tmp_path: Path) -> None:
        """BLE001:FOG contract: a failing heartbeat callback must be swallowed
        -- bar_sync must never block the main loop on pulse failure."""
        calls: list[int] = []

        def _boom() -> None:
            calls.append(1)
            raise RuntimeError("pulse backend down")

        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            poll_interval=0.01,
            heartbeat_refresh=_boom,
        )
        poller._mt5_available = True
        poller._state.last_bar_time = 1715700000

        with patch(
            "core.execution.pre_trade_guards.detect_session",
            return_value={"risk_tier": "normal"},
        ):
            with patch.dict("sys.modules", {"MetaTrader5": _mock_mt5_module()}):
                result = poller.wait_for_new_bar(timeout_seconds=0.05)

        assert result is None  # polling continued despite pulse failures
        assert len(calls) >= 1  # pulse was attempted


class TestBarSyncBtcControl:
    """5. BTC control (IC mandate): crypto_24_7 must be untouched by the fix."""

    def test_btc_crypto_24_7_never_returns_off(self) -> None:
        """BTC is continuous -- detect_session must NEVER return risk_tier
        'off', so the session gate never skips (no market-close kill cluster
        exists for BTC).  This is the control proving the fix is XAU-scoped."""
        from core.execution.pre_trade_guards import detect_session

        assert detect_session(market_type="crypto_24_7")["risk_tier"] != "off"

    def test_btc_control_pulse_works_during_crypto_polling(self, tmp_path: Path) -> None:
        """BTC continuous polling also pulses -- the pulse is harmless to the
        24/7 path (no session gate, no degraded-cap regression)."""
        pulse, pulse_fn = _pulse()
        poller = BarSyncPoller(
            state_dir=str(tmp_path),
            terminal_path=None,
            market_type="crypto_24_7",
            poll_interval=0.01,
            heartbeat_refresh=pulse_fn,
        )
        poller._mt5_available = True
        poller._state.last_bar_time = 1715700000

        with patch(
            "core.execution.pre_trade_guards.detect_session",
            return_value={
                "session_name": "crypto_continuous",
                "volume_mult": 1.0,
                "sl_expand_mult": 1.0,
                "risk_tier": "normal",
            },
        ):
            with patch.dict("sys.modules", {"MetaTrader5": _mock_mt5_module()}):
                result = poller.wait_for_new_bar(timeout_seconds=0.05)

        assert result is None
        assert pulse.call_count >= 2  # crypto polling pulsed normally


class TestBarSyncConfigAlignment:
    """6. Config alignment (IC order): bar_sync_timeout synced across assets."""

    def test_bar_sync_timeout_aligned_across_assets(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[2]  # d:\future
        live = yaml.safe_load((root / "configs" / "live.yaml").read_text(encoding="utf-8"))
        live_btc = yaml.safe_load((root / "configs" / "live_btc.yaml").read_text(encoding="utf-8"))
        xau_timeout = live["live_trading"]["bar_sync_timeout"]
        btc_timeout = live_btc["live_trading"]["bar_sync_timeout"]
        assert xau_timeout == btc_timeout
        assert xau_timeout == 240  # FIX-20260820-001 alignment target

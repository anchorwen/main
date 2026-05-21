"""Pitfall 2 safeguard: Event-driven M5 bar synchronization.

Replaces blind ``time.sleep(interval_seconds)`` with an event-driven loop
that waits for a genuine new M5 bar to form before triggering the next
live cycle.  Eliminates the "bar-closing synchronization" pitfall where
the Python brain reads incomplete / stale bar data because it ran its
cycle too early or too late relative to the MT5 bar close.

Architecture:
  - ``BarSyncPoller``: lightweight poller that blocks until MT5 reports
    a new bar time.  Uses ``copy_rates_from_pos`` which returns the bar
    array directly from the terminal — no custom indicator needed.
  - ``BarSyncState``: persistent state (last known bar time, lag counter)
    stored as JSON on disk so restarts recover without re-syncing from
    scratch.

Usage (in live_cycle.py or live_launcher.py)::

    sync = BarSyncPoller(symbol="XAUUSDc", timeframe="M5",
                         terminal_path="C:/...")
    while True:
        new_bar = sync.wait_for_new_bar(timeout_seconds=120)
        if new_bar is None:
            # Timeout — MT5 may be down; fall back to poll-based cycle
            time.sleep(60)
        else:
            # Genuine new bar at new_bar["time"]
            execute_live_cycle()

Failsafe: if MT5 connection is lost or times out, falls back to a
configurable poll interval rather than blocking forever.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# -- Constants --

DEFAULT_TIMEOUT_SECONDS = 120  # max wait for new bar before fallback
DEFAULT_POLL_INTERVAL = 2.0  # seconds between MT5 rate checks
DEFAULT_FALLBACK_INTERVAL = 60  # seconds when MT5 is unreachable
MAX_LAG_BARS = 3  # consecutive missed bars before CRITICAL alert


# -- Data types --


@dataclass
class BarSyncState:
    """Persistent state for bar-sync across restarts."""

    last_bar_time: int = 0  # unix timestamp of last seen bar
    last_bar_open: float = 0.0
    last_bar_close: float = 0.0
    lag_count: int = 0  # consecutive missed-bar counter
    total_bars_seen: int = 0
    last_sync_utc: str = ""


# -- Bar Sync Poller --


class BarSyncPoller:
    """Event-driven bar detector using MT5 terminal rates.

    Blocks until a new M5 bar appears (detected by change in the
    most recent bar's timestamp), then returns the new bar's data.

    Features:
      - Event-driven: only runs a cycle when a new bar truly exists
      - Timeout + fallback: if MT5 goes down, falls back to poll interval
      - State persistence: survives restarts without losing sync
      - Lag detection: alerts if cycles are falling behind bar formation
    """

    def __init__(
        self,
        *,
        symbol: str = "XAUUSDc",
        timeframe: str = "M5",
        terminal_path: str | None = None,
        state_dir: str = "data",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        fallback_interval: float = DEFAULT_FALLBACK_INTERVAL,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.terminal_path = terminal_path
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.fallback_interval = fallback_interval

        self._state_path = Path(state_dir) / "bar_sync_state.json"
        self._state = BarSyncState()
        self._mt5_available = False
        self._load_state()

    # -- Public API --

    def wait_for_new_bar(self, timeout_seconds: float | None = None) -> dict[str, Any] | None:
        """Block until a new bar forms, or timeout.

        Returns the new bar as a dict with keys: time, open, high, low, close,
        tick_volume, spread, real_volume.  Returns None on timeout (caller
        should use fallback interval).
        """
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        deadline = time.monotonic() + timeout

        # Ensure MT5 is initialized
        if not self._mt5_available:
            self._init_mt5()

        if not self._mt5_available:
            # MT5 unreachable — fall back immediately
            time.sleep(self.fallback_interval)
            return None

        while time.monotonic() < deadline:
            try:
                import MetaTrader5 as mt5

                # Fetch the most recent 2 bars to detect new bar formation
                rates = mt5.copy_rates_from_pos(
                    self.symbol,
                    self._timeframe_map(self.timeframe),
                    0,  # start_pos: 0 = most recent
                    2,  # count: 2 bars (current + previous)
                )
                if rates is None or len(rates) < 2:
                    time.sleep(self.poll_interval)
                    continue

                current_bar = rates[-1]
                bar_time = int(current_bar["time"])

                # Detect new bar: timestamp changed from our last known bar
                if bar_time != self._state.last_bar_time:
                    # Check if we missed bars (lag detection)
                    expected_next = self._state.last_bar_time + self._bar_seconds()
                    if self._state.last_bar_time > 0 and bar_time > expected_next:
                        missed = (bar_time - expected_next) // self._bar_seconds()
                        self._state.lag_count += missed
                        self._log_event(
                            "BAR_LAG",
                            {
                                "missed_bars": missed,
                                "total_lag": self._state.lag_count,
                                "expected_time": expected_next,
                                "actual_time": bar_time,
                            },
                        )
                        if self._state.lag_count >= MAX_LAG_BARS:
                            self._log_event(
                                "BAR_LAG_CRITICAL",
                                {
                                    "consecutive_lag": self._state.lag_count,
                                    "action": "consider_fallback_or_restart",
                                },
                            )

                    # Update state
                    self._state.last_bar_time = bar_time
                    self._state.last_bar_open = float(current_bar["open"])
                    self._state.last_bar_close = float(current_bar["close"])
                    self._state.total_bars_seen += 1
                    self._state.lag_count = max(0, self._state.lag_count - 1)
                    self._state.last_sync_utc = datetime.now(UTC).isoformat()
                    self._save_state()

                    return {
                        "time": bar_time,
                        "open": float(current_bar["open"]),
                        "high": float(current_bar["high"]),
                        "low": float(current_bar["low"]),
                        "close": float(current_bar["close"]),
                        "tick_volume": int(current_bar.get("tick_volume", 0)),
                        "spread": int(current_bar.get("spread", 0)),
                        "real_volume": int(current_bar.get("real_volume", 0)),
                    }

                # Same bar — wait and poll again
                time.sleep(self.poll_interval)

            except Exception:
                self._mt5_available = False
                self._log_event("MT5_ERROR", {"action": "fallback_to_poll"})
                time.sleep(self.fallback_interval)
                return None

        # Timeout — no new bar within the window
        self._log_event(
            "BAR_TIMEOUT",
            {
                "timeout_seconds": timeout,
                "last_bar_time": self._state.last_bar_time,
            },
        )
        return None

    def fetch_synthetic_bar(self, mt5: Any = None) -> dict[str, Any] | None:
        """Fallback: aggregate last 6 M1 bars into a synthetic M5 bar.

        Called when ``wait_for_new_bar`` times out — instead of sleeping the
        main loop, we reconstruct the most recent 5-minute window from M1
        bars.  This eliminates the 120s blind-wait and keeps the perception
        layer aligned to real-time market conditions.

        Accepts an optional *mt5* module reference from the caller (launcher)
        to bypass any internal MT5 connection issues.  When None, imports
        ``MetaTrader5`` directly.

        Returns a dict with the same shape as ``wait_for_new_bar`` (time,
        open, high, low, close, tick_volume, spread, real_volume), or None
        if no M1 data is available.
        """
        _mt5: Any = mt5
        if _mt5 is None:
            try:
                import MetaTrader5 as _mt5_mod

                _mt5 = _mt5_mod
            except Exception:
                self._log_event("BAR_SYNTHETIC_FAILED", {"error": "import_error"})
                return None

        try:
            m1_rates = _mt5.copy_rates_from_pos(
                self.symbol,
                _mt5.TIMEFRAME_M1,
                0,
                6,  # last 6 × M1 bars cover a full M5 window
            )
            if m1_rates is None or len(m1_rates) < 2:
                return None

            # Aggregate M1 bars into a synthetic M5 bar
            highs = [float(r["high"]) for r in m1_rates]
            lows = [float(r["low"]) for r in m1_rates]
            closes = [float(r["close"]) for r in m1_rates]
            opens = [float(r["open"]) for r in m1_rates]
            volumes = [int(r.get("tick_volume", 0)) for r in m1_rates]
            spreads = [int(r.get("spread", 0)) for r in m1_rates]
            real_volumes = [int(r.get("real_volume", 0)) for r in m1_rates]

            synthetic_time = int(m1_rates[-1]["time"])
            synthetic_bar = {
                "time": synthetic_time,
                "open": opens[0],
                "high": max(highs),
                "low": min(lows),
                "close": closes[-1],
                "tick_volume": sum(volumes),
                "spread": int(sum(spreads) / len(spreads)) if spreads else 0,
                "real_volume": sum(real_volumes),
                "_synthetic": True,
            }

            # Update sync state so lag detection doesn't fire spuriously
            self._state.last_bar_time = synthetic_time
            self._state.last_bar_open = synthetic_bar["open"]
            self._state.last_bar_close = synthetic_bar["close"]
            self._state.total_bars_seen += 1
            self._state.last_sync_utc = datetime.now(UTC).isoformat()
            self._save_state()

            self._log_event(
                "BAR_SYNTHETIC",
                {
                    "m1_bars_used": len(m1_rates),
                    "synthetic_time": synthetic_time,
                    "synthetic_close": synthetic_bar["close"],
                },
            )
            return synthetic_bar

        except Exception:
            self._mt5_available = False
            self._log_event("BAR_SYNTHETIC_FAILED", {"error": "mt5_unreachable"})
            return None

    def get_state(self) -> dict[str, Any]:
        """Return current sync state for health monitoring."""
        return {
            "last_bar_time": self._state.last_bar_time,
            "last_bar_close": self._state.last_bar_close,
            "total_bars_seen": self._state.total_bars_seen,
            "lag_count": self._state.lag_count,
            "last_sync_utc": self._state.last_sync_utc,
            "mt5_available": self._mt5_available,
        }

    def reset_lag(self) -> None:
        """Reset lag counter after manual intervention."""
        self._state.lag_count = 0
        self._save_state()

    # -- Internal --

    def _init_mt5(self) -> None:
        """Attempt to initialize MT5 connection."""
        try:
            import MetaTrader5 as mt5

            kwargs: dict[str, Any] = {}
            if self.terminal_path:
                p = Path(self.terminal_path)
                if p.exists():
                    kwargs["path"] = str(p)

            if not mt5.initialize(**kwargs):
                self._log_event(
                    "MT5_INIT_FAILED",
                    {
                        "error": str(mt5.last_error()),
                    },
                )
                self._mt5_available = False
                return

            # Verify symbol is available
            info = mt5.symbol_info(self.symbol)
            if info is None:
                self._log_event("MT5_SYMBOL_MISSING", {"symbol": self.symbol})
                self._mt5_available = False
                return
            if not info.visible:
                mt5.symbol_select(self.symbol, True)

            self._mt5_available = True
            self._log_event("MT5_INIT_OK", {"symbol": self.symbol})

        except Exception as exc:
            self._log_event("MT5_INIT_EXCEPTION", {"error": str(exc)})
            self._mt5_available = False

    @staticmethod
    def _timeframe_map(tf: str) -> int:
        """Map string timeframe to MT5 constant."""
        import MetaTrader5 as mt5

        mapping = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        return mapping.get(tf, mt5.TIMEFRAME_M5)

    @staticmethod
    def _bar_seconds_for(tf: str) -> int:
        """Return the number of seconds in one bar for the given timeframe."""
        mapping = {
            "M1": 60,
            "M5": 300,
            "M15": 900,
            "M30": 1800,
            "H1": 3600,
            "H4": 14400,
            "D1": 86400,
        }
        return mapping.get(tf, 300)

    def _bar_seconds(self) -> int:
        return self._bar_seconds_for(self.timeframe)

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._state = BarSyncState(
                    last_bar_time=int(data.get("last_bar_time", 0)),
                    last_bar_open=float(data.get("last_bar_open", 0)),
                    last_bar_close=float(data.get("last_bar_close", 0)),
                    lag_count=int(data.get("lag_count", 0)),
                    total_bars_seen=int(data.get("total_bars_seen", 0)),
                    last_sync_utc=str(data.get("last_sync_utc", "")),
                )
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(
                    {
                        "last_bar_time": self._state.last_bar_time,
                        "last_bar_open": self._state.last_bar_open,
                        "last_bar_close": self._state.last_bar_close,
                        "lag_count": self._state.lag_count,
                        "total_bars_seen": self._state.total_bars_seen,
                        "last_sync_utc": self._state.last_sync_utc,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _log_event(self, event: str, detail: dict[str, Any]) -> None:
        """Log a bar-sync event to the health log."""
        try:
            log_path = Path("data") / "reports" / "bar_sync_events.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "event": event,
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                **detail,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

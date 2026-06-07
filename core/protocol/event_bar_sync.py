"""Pitfall 2 safeguard: Event-driven M5 bar synchronization.

Replaces blind ``time.sleep(interval_seconds)`` with an event-driven loop
that waits for a genuine new M5 bar to form before triggering the next
live cycle.  Eliminates the "bar-closing synchronization" pitfall where
the Python brain reads incomplete / stale bar data because it ran its
cycle too early or too late relative to the MT5 bar close.

Architecture:
  - ``BarSyncPoller``: lightweight poller that blocks until MT5 reports
    a new bar time.  Uses ``copy_rates_from_pos`` which returns the bar
    array directly from the terminal -- no custom indicator needed.
  - ``BarSyncState``: persistent state (last known bar time, lag counter)
    stored as JSON on disk so restarts recover without re-syncing from
    scratch.
  - When an ``MT5Worker`` is provided, all MT5 calls are delegated to the
    worker thread -- no second ``mt5.initialize()`` call competes with the
    main loop's worker.  Without a worker, falls back to self-managed MT5
    connection (standalone / testing).

Usage (in live_cycle.py or live_launcher.py)::

    sync = BarSyncPoller(symbol="XAUUSDc", timeframe="M5",
                         terminal_path="C:/...")
    while True:
        new_bar = sync.wait_for_new_bar(timeout_seconds=120)
        if new_bar is None:
            # Timeout -- MT5 may be down; fall back to poll-based cycle
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# -- MT5 timeframe constants (hardcoded -- no thread-affinity requirement) --
MT5_TIMEFRAME_M1 = 1
MT5_TIMEFRAME_M5 = 5
MT5_TIMEFRAME_M15 = 15
MT5_TIMEFRAME_M30 = 30
MT5_TIMEFRAME_H1 = 16385
MT5_TIMEFRAME_H4 = 16388
MT5_TIMEFRAME_D1 = 16408

# -- Constants --

DEFAULT_TIMEOUT_SECONDS = 360  # absolute floor -- dynamic floor = max(this, int(bar_seconds×1.5))
DEFAULT_POLL_INTERVAL = 1.0  # seconds between MT5 rate checks
DEFAULT_FALLBACK_INTERVAL = 60  # seconds when MT5 is unreachable
MAX_LAG_BARS = 3  # consecutive missed bars before CRITICAL alert
MAX_MT5_ERROR_RETRIES = 3  # re-init + retry before returning None on transient MT5 errors
MAX_CONSECUTIVE_EMPTY_POLLS = 5  # consecutive None/empty copy_rates before re-init attempt


# -- Data types --


@dataclass
class BarSyncState:
    """Persistent state for bar-sync across restarts.

    FIX-20260601-042: ``lag_count`` is retained as a cumulative counter
    for backward compatibility.  Use ``BarSyncPoller.current_lag_bars()``
    for a real-time lag estimate computed from wall-clock vs last_bar_time.
    """

    last_bar_time: int = 0  # unix timestamp of last seen bar
    last_bar_open: float = 0.0
    last_bar_close: float = 0.0
    lag_count: int = 0  # cumulative missed-bar counter (historical)
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
        mt5_worker: MT5Worker | None = None,
        market_type: str = "forex_24_5",  # FIX-20260601-042: session-aware bar sync
        strict_mode: bool = False,  # Architect directive: refuse direct MT5 in production
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.terminal_path = terminal_path
        self._market_type = market_type
        # Dynamic timeout floor: max(provided, int(bar_period_seconds × 1.5))
        # M5=450s, M15=1350s, H1=5400s, H4=21600s -- eliminates timeframe coupling
        _bar_secs = self._bar_seconds_for(timeframe)
        _dynamic_floor = max(DEFAULT_TIMEOUT_SECONDS, int(_bar_secs * 1.5))
        self.timeout_seconds = max(timeout_seconds, _dynamic_floor)
        self.poll_interval = poll_interval
        self.fallback_interval = fallback_interval
        self._mt5_worker = mt5_worker
        self._strict_mode = strict_mode

        self._state_path = Path(state_dir) / "bar_sync_state.json"
        self._state = BarSyncState()
        self._mt5_available = mt5_worker is not None  # worker pre-initialised by caller
        self._load_state()

    # -- Public API --

    def wait_for_new_bar(self, timeout_seconds: float | None = None) -> dict[str, Any] | None:
        """Block until a new bar forms, timeout, or degraded wakeup.

        Returns a bar dict (possibly with ``_degraded: True`` sentinel) on
        success/degradation, or None on timeout.  Callers must treat any
        truthy return as "run the next cycle now".

        FIX-20260601-042: Session-aware -- skips polling when market is closed
        (e.g. XAU forex_24_5 on weekends).  BTC crypto_24_7 always polls.
        """
        # ── Session gate: skip polling during market close (FIX-20260601-042) ──
        try:
            from core.execution.pre_trade_guards import detect_session
            _session = detect_session(market_type=self._market_type)
            if _session.get("risk_tier", "normal") == "off":
                self._log_event(
                    "BAR_SESSION_OFF",
                    {"market_type": self._market_type, "fallback_sleep": self.fallback_interval},
                )
                time.sleep(self.fallback_interval)
                return None
        except Exception:  # noqa: BLE001
            pass  # Session detection is advisory -- never block the main loop

        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        _start = time.monotonic()
        deadline = _start + timeout
        _bar_secs = self._bar_seconds()
        # Degrade only after bar_period + 10s buffer -- ensures at least one
        # poll after the bar boundary before giving up.
        _degraded_deadline = _start + _bar_secs + 10.0
        _degraded = False

        # Ensure MT5 is initialized
        if not self._mt5_available:
            self._init_mt5()

        if not self._mt5_available:
            # MT5 unreachable -- fall back immediately
            time.sleep(self.fallback_interval)
            return None

        _error_count = 0
        _consecutive_empty = 0  # consecutive empty poll counter for silent-failure recovery
        while time.monotonic() < deadline:
            try:
                # Fetch the most recent 2 bars to detect new bar formation
                if self._mt5_worker is not None:
                    rates = self._mt5_worker.copy_rates_from_pos(
                        self.symbol,
                        self._timeframe_map(self.timeframe),
                        0,  # start_pos: 0 = most recent
                        2,  # count: 2 bars (current + previous)
                    )
                elif self._strict_mode:
                    raise RuntimeError(
                        "BarSyncPoller strict_mode: cannot fetch bars without MT5Worker. "
                        "Worker became unavailable mid-cycle."
                    )
                else:
                    import MetaTrader5 as mt5

                    rates = mt5.copy_rates_from_pos(
                        self.symbol,
                        self._timeframe_map(self.timeframe),
                        0,
                        2,
                    )
                if rates is None or len(rates) < 2:
                    _consecutive_empty += 1
                    # ── Silent-failure recovery ──
                    # copy_rates can return None after MT5 IPC hiccups (e.g.
                    # after shutdown+re-init) without throwing an exception.
                    # Without recovery, the poll loop spins silently for the
                    # remainder of the degraded window -- never detecting the
                    # new bar that forms minutes later.
                    # Re-init MT5 after N consecutive empty polls, same as the
                    # exception-handler path (FIX-20260522-028).
                    if _consecutive_empty >= MAX_CONSECUTIVE_EMPTY_POLLS:
                        self._log_event(
                            "BAR_EMPTY_POLLS_REINIT",
                            {
                                "consecutive_empty": _consecutive_empty,
                                "elapsed": round(time.monotonic() - _start, 1),
                                "bar_period": _bar_secs,
                                "mt5_available": self._mt5_available,
                            },
                        )
                        self._mt5_available = False
                        if self._mt5_worker is not None:
                            try:
                                self._mt5_worker.reconnect()
                                self._mt5_available = True
                            except Exception as _exc:  # noqa: BLE001
                                self._log_event(
                                    "BAR_RECONNECT_FAILED",
                                    {"error": str(_exc)[:200], "path": "worker"},
                                )
                        else:
                            try:
                                mt5.shutdown()
                            except Exception as _exc:  # noqa: BLE001
                                self._log_event(
                                    "BAR_RECONNECT_FAILED",
                                    {"error": str(_exc)[:200], "path": "shutdown"},
                                )
                            try:
                                self._init_mt5()
                            except Exception as _exc:  # noqa: BLE001
                                self._log_event(
                                    "BAR_RECONNECT_FAILED",
                                    {"error": str(_exc)[:200], "path": "init_mt5"},
                                )
                        _consecutive_empty = 0
                        time.sleep(self.poll_interval * 2)
                        continue
                    time.sleep(self.poll_interval)
                    if not _degraded and time.monotonic() >= _degraded_deadline:
                        _degraded = True
                        self._log_event(
                            "BAR_DEGRADED_WAKEUP",
                            {
                                "elapsed": round(time.monotonic() - _start, 1),
                                "bar_period": _bar_secs,
                                "last_bar_time": self._state.last_bar_time,
                                "mt5_available": self._mt5_available,
                                "error_count": _error_count,
                            },
                        )
                        return {
                            "time": self._state.last_bar_time,
                            "open": self._state.last_bar_open,
                            "high": self._state.last_bar_close,
                            "low": self._state.last_bar_close,
                            "close": self._state.last_bar_close,
                            "tick_volume": 0,
                            "spread": 0,
                            "real_volume": 0,
                            "_degraded": True,
                        }
                    continue

                # Valid poll -- reset empty-poll counter
                _consecutive_empty = 0

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

                    # Build bar data before updating state -- if construction
                    # fails, state stays unchanged so next poll retries cleanly.
                    _bar_data = {
                        "time": bar_time,
                        "open": float(current_bar["open"]),
                        "high": float(current_bar["high"]),
                        "low": float(current_bar["low"]),
                        "close": float(current_bar["close"]),
                        "tick_volume": int(current_bar["tick_volume"]),
                        "spread": int(current_bar["spread"]),
                        "real_volume": int(current_bar["real_volume"]),
                    }
                    # Update state (only after bar_data built successfully)
                    self._state.last_bar_time = bar_time
                    self._state.last_bar_open = _bar_data["open"]
                    self._state.last_bar_close = _bar_data["close"]
                    self._state.total_bars_seen += 1
                    self._state.lag_count = max(0, self._state.lag_count - 1)
                    self._state.last_sync_utc = datetime.now(UTC).isoformat()
                    self._save_state()
                    _error_count = 0  # reset on success

                    return _bar_data

                # Same bar -- wait and poll again
                _error_count = 0  # successful poll, reset error streak
                time.sleep(self.poll_interval)
                # Degraded wakeup: poll succeeded but no new bar after bar_period + buffer.
                # Check after the poll so the bar-boundary poll always runs first.
                if not _degraded and time.monotonic() >= _degraded_deadline:
                    _degraded = True
                    self._log_event(
                        "BAR_DEGRADED_WAKEUP",
                        {
                            "elapsed": round(time.monotonic() - _start, 1),
                            "bar_period": _bar_secs,
                            "last_bar_time": self._state.last_bar_time,
                            "mt5_available": self._mt5_available,
                            "error_count": _error_count,
                        },
                    )
                    return {
                        "time": self._state.last_bar_time,
                        "open": self._state.last_bar_open,
                        "high": self._state.last_bar_close,
                        "low": self._state.last_bar_close,
                        "close": self._state.last_bar_close,
                        "tick_volume": 0,
                        "spread": 0,
                        "real_volume": 0,
                        "_degraded": True,
                        "_data_incomplete": True,  # FIX-20260601-042: mark placeholder data
                    }

            except Exception as exc:  # noqa: BLE001
                _error_count += 1
                self._log_event(
                    "MT5_ERROR",
                    {
                        "action": "retry"
                        if _error_count <= MAX_MT5_ERROR_RETRIES
                        else "fallback_to_poll",
                        "error_count": _error_count,
                        "max_retries": MAX_MT5_ERROR_RETRIES,
                        "exception": str(exc),
                        "exception_type": type(exc).__name__,
                    },
                )
                if _error_count <= MAX_MT5_ERROR_RETRIES:
                    # Transient error -- clean up stale connection and re-init
                    self._mt5_available = False
                    if self._mt5_worker is not None:
                        try:
                            self._mt5_worker.reconnect()
                            self._mt5_available = True
                        except Exception as _exc:  # noqa: BLE001
                            self._log_event(
                                "BAR_MT5_ERROR_RECONNECT_FAILED",
                                {"error": str(_exc)[:200], "path": "worker"},
                            )
                    else:
                        try:
                            import MetaTrader5 as _mt5_mod

                            _mt5_mod.shutdown()
                        except Exception as _exc:  # noqa: BLE001
                            self._log_event(
                                "BAR_MT5_ERROR_RECONNECT_FAILED",
                                {"error": str(_exc)[:200], "path": "shutdown"},
                            )
                        try:
                            self._init_mt5()
                        except Exception as _exc:  # noqa: BLE001
                            self._log_event(
                                "BAR_MT5_ERROR_RECONNECT_FAILED",
                                {"error": str(_exc)[:200], "path": "init_mt5"},
                            )
                    time.sleep(self.poll_interval * 2)
                    continue
                # Persistent error -- give up for this cycle
                self._mt5_available = False
                time.sleep(self.fallback_interval)
                return None

        # Timeout -- no new bar within the window
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

        Called when wait_for_new_bar times out.  Prefers the worker when
        available; falls back to direct MetaTrader5 import for standalone use.

        Returns a dict with the same shape as wait_for_new_bar, or None.
        """
        m1_rates = None
        if self._mt5_worker is not None:
            try:
                m1_rates = self._mt5_worker.copy_rates_from_pos(self.symbol, MT5_TIMEFRAME_M1, 0, 6)
            except Exception:  # noqa: BLE001
                self._mt5_available = False
                self._log_event("BAR_SYNTHETIC_FAILED", {"error": "mt5_unreachable"})
                return None
        else:
            _mt5: Any = mt5
            if _mt5 is None:
                try:
                    import MetaTrader5 as _mt5_mod

                    _mt5 = _mt5_mod
                except Exception:  # noqa: BLE001
                    self._log_event("BAR_SYNTHETIC_FAILED", {"error": "import_error"})
                    return None

            try:
                m1_rates = _mt5.copy_rates_from_pos(
                    self.symbol,
                    _mt5.TIMEFRAME_M1,
                    0,
                    6,  # last 6 × M1 bars cover a full M5 window
                )
            except Exception:  # noqa: BLE001
                self._mt5_available = False
                self._log_event("BAR_SYNTHETIC_FAILED", {"error": "mt5_unreachable"})
                return None

        if m1_rates is None or len(m1_rates) < 2:
            return None

        # Aggregate M1 bars into a synthetic M5 bar
        highs = [float(r["high"]) for r in m1_rates]
        lows = [float(r["low"]) for r in m1_rates]
        closes = [float(r["close"]) for r in m1_rates]
        opens = [float(r["open"]) for r in m1_rates]
        volumes = [int(r["tick_volume"]) for r in m1_rates]
        spreads = [int(r["spread"]) for r in m1_rates]
        real_volumes = [int(r["real_volume"]) for r in m1_rates]

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

    def get_state(self) -> dict[str, Any]:
        """Return current sync state for health monitoring."""
        return {
            "last_bar_time": self._state.last_bar_time,
            "last_bar_close": self._state.last_bar_close,
            "total_bars_seen": self._state.total_bars_seen,
            "lag_count": self._state.lag_count,
            "current_lag_bars": self.current_lag_bars(),  # FIX-20260601-042
            "last_sync_utc": self._state.last_sync_utc,
            "mt5_available": self._mt5_available,
        }

    def current_lag_bars(self) -> int:
        """Real-time lag estimate from wall clock vs last known bar time.

        Unlike ``lag_count`` (cumulative counter), this returns the number
        of bars the system is currently behind based on the wall clock.
        Zero or negative means the system is caught up.
        """
        if self._state.last_bar_time <= 0:
            return 0
        _bar_secs = self._bar_seconds()
        if _bar_secs <= 0:
            return 0
        return max(0, int((time.time() - self._state.last_bar_time) / _bar_secs))

    def reset_lag(self) -> None:
        """Reset lag counter after manual intervention."""
        self._state.lag_count = 0
        self._save_state()

    # -- Internal --

    def _init_mt5(self) -> None:
        """Attempt to initialize MT5 connection (standalone mode only).

        When a worker is provided, the caller owns the MT5 lifecycle and this
        method is not called -- ``_mt5_available`` is set from the worker in
        ``__init__``.

        In strict_mode (production), direct MT5 access is forbidden -- the
        caller MUST provide a worker.  Raises RuntimeError if no worker.
        """
        if self._mt5_worker is not None:
            self._mt5_available = True
            return

        if self._strict_mode:
            raise RuntimeError(
                "BarSyncPoller strict_mode: MT5Worker required. "
                "Direct mt5.initialize() forbidden in production. "
                "Pass mt5_worker= to BarSyncPoller constructor."
            )

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

        except Exception as exc:  # noqa: BLE001
            self._log_event("MT5_INIT_EXCEPTION", {"error": str(exc)})
            self._mt5_available = False

    @staticmethod
    def _timeframe_map(tf: str) -> int:
        """Map string timeframe to MT5 constant (hardcoded -- no import needed)."""
        mapping = {
            "M1": MT5_TIMEFRAME_M1,
            "M5": MT5_TIMEFRAME_M5,
            "M15": MT5_TIMEFRAME_M15,
            "M30": MT5_TIMEFRAME_M30,
            "H1": MT5_TIMEFRAME_H1,
            "H4": MT5_TIMEFRAME_H4,
            "D1": MT5_TIMEFRAME_D1,
        }
        return mapping.get(tf, MT5_TIMEFRAME_M5)

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
        except (OSError, json.JSONDecodeError, ValueError) as _exc:
            # FIX-20260601-042: log instead of silent pass
            try:  # noqa: SIM105
                print(
                    json.dumps(
                        {
                            "event": "bar_sync_state_load_failed",
                            "timestamp_utc": datetime.now(UTC).isoformat(),
                            "symbol": self.symbol,
                            "error": str(_exc)[:200],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception:  # noqa: BLE001
                pass

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            import os as _os

            tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "schema_version": "bar_sync_state.v1",  # FIX-20260601-045
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
            _os.replace(tmp_path, self._state_path)
        except OSError as _exc:
            self._log_event(
                "BAR_STATE_SAVE_FAILED",
                {"error": str(_exc)[:200]},
            )

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
            # FIX-20260601-042: bar_sync event logging is best-effort -- never block the main loop
            pass

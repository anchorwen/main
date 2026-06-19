"""Dynamic session detector — tick-frequency-based market state probe.

TECH_DEBT-005: Replaces hardcoded _SESSIONS time-window table with a
physical-state probe.  Instead of asking "what time is it?" we ask
"is the market actually trading right now?"

Design:
  - Zero external dependencies (no calendar APIs, no web scraping)
  - Broker is the Single Source of Truth (tick_time from MT5 server)
  - Self-healing: automatically detects market re-open via tick resumption
  - Compatible output: same {session_name, volume_mult, sl_expand_mult, risk_tier}
    dict format as the static _SESSIONS table

State machine:
  NORMAL    — tick_time updating, market active
  ROLLOVER  — tick_time updating but with wider spreads (daily rollover)
  CLOSED    — tick_time stalled > STALL_THRESHOLD (market physically shut)

Usage::

    from core.execution.session_detector import SessionDetector

    detector = SessionDetector()
    session = detector.probe(tick_time_unix=1781800000.0, market_type="forex_24_5")
    # session["risk_tier"] → "normal" | "reduced" | "off"
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# ── Thresholds ──────────────────────────────────────────────────────────

# tick_time (MT5 server Unix timestamp) must change within this window
# to be considered "live".  MT5 ticks arrive sub-second in active markets.
# 120s allows for brief MT5 reconnection without triggering CLOSED.
TICK_STALL_SECONDS: float = 120.0

# After tick resumes, this many seconds of consecutive live ticks are
# required before transitioning from CLOSED → NORMAL (hysteresis).
REOPEN_CONFIRM_SECONDS: float = 60.0

# Consecutive seconds of stalled tick before CLOSED is declared.
# 900s = 15 min — covers daily rollover (typically <5 min) with margin.
CLOSED_STALL_SECONDS: float = 900.0


# ── Detector ────────────────────────────────────────────────────────────


@dataclass
class SessionDetector:
    """Tick-frequency-based market session detector.

    Maintains internal state across calls.  Thread-safe: all mutable
    state is updated under explicit attribute assignment (no locks needed
    for single-threaded live_cycle usage).
    """

    # Internal state
    _last_tick_time: float = 0.0
    _stall_start: float = 0.0  # time.monotonic() when stall began
    _resume_start: float = 0.0  # time.monotonic() when tick resumed after close
    _current_state: str = "normal"  # "normal" | "rollover" | "closed"
    _consecutive_live_seconds: float = 0.0
    # Result cache — when tick_time=0, return the most recent valid result
    # instead of treating missing tick as a stall.
    _last_result: dict[str, Any] | None = None

    # ── Public API ──────────────────────────────────────────────────────

    def probe(
        self,
        tick_time: float,
        market_type: str = "forex_24_5",
    ) -> dict[str, Any]:
        """Return session dict for the current tick state.

        Args:
            tick_time: MT5 server tick timestamp (Unix seconds), or 0.0 if
                       tick is unavailable.  This is the ``_tick_time`` value
                       returned by ``_mid_and_prices()``.
            market_type: "forex_24_5" or "crypto_24_7" — crypto always normal.

        Returns:
            Dict with session_name, volume_mult, sl_expand_mult, risk_tier.
            Same format as the static _SESSIONS table in pre_trade_guards.py.
        """
        # Crypto 24/7 — never closed, never degraded
        if market_type == "crypto_24_7":
            return {
                "session_name": "crypto_continuous",
                "volume_mult": 1.0,
                "sl_expand_mult": 1.0,
                "risk_tier": "normal",
                "_source": "dynamic_probe",
            }

        # ── No tick data available — return cached result ──
        # Call sites deep in the cycle may not have access to _tick_time.
        # Use the last known state rather than treating absence as a stall.
        if tick_time <= 0:
            if self._last_result is not None:
                return self._last_result
            # No prior probe yet — return conservative normal
            return self._make_result("unknown", "normal", 1.0, 1.0)

        now = time.monotonic()

        # ── Tick is live ──
        if tick_time > 0 and tick_time != self._last_tick_time:
            self._last_tick_time = tick_time
            self._stall_start = 0.0  # reset stall

            if self._current_state == "closed":
                # Market was closed — confirm re-open with hysteresis
                if self._resume_start == 0.0:
                    self._resume_start = now
                self._consecutive_live_seconds = now - self._resume_start
                if self._consecutive_live_seconds >= REOPEN_CONFIRM_SECONDS:
                    self._current_state = "normal"
                    self._resume_start = 0.0
                    self._consecutive_live_seconds = 0.0
                    return self._make_result("market_reopen", "normal", 1.0, 1.0)
                else:
                    # Still confirming — treat as caution
                    return self._make_result(
                        "market_reopening", "caution", 0.50, 1.50
                    )
            else:
                self._current_state = "normal"
                self._resume_start = 0.0
                self._consecutive_live_seconds = 0.0
                return self._make_result("live_trading", "normal", 1.0, 1.0)

        # ── Tick is stalled ──
        if self._stall_start == 0.0:
            self._stall_start = now

        stall_duration = now - self._stall_start

        if stall_duration >= CLOSED_STALL_SECONDS:
            self._current_state = "closed"
            return self._make_result("market_closed", "off", 0.0, 2.0)

        if stall_duration >= TICK_STALL_SECONDS:
            self._current_state = "rollover"
            return self._make_result(
                "daily_rollover", "reduced", 0.40, 1.50
            )

        # Brief stall (< 120s) — normal, MT5 reconnection
        return self._make_result("live_trading", "normal", 1.0, 1.0)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _make_result(
        self,
        session_name: str,
        risk_tier: str,
        volume_mult: float,
        sl_expand_mult: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_name": session_name,
            "volume_mult": volume_mult,
            "sl_expand_mult": sl_expand_mult,
            "risk_tier": risk_tier,
            "_source": "dynamic_probe",
        }
        self._last_result = result
        return result

    def reset(self) -> None:
        """Reset internal state (for testing)."""
        self._last_tick_time = 0.0
        self._stall_start = 0.0
        self._resume_start = 0.0
        self._current_state = "normal"
        self._consecutive_live_seconds = 0.0

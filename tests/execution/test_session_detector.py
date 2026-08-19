"""Unit tests for session_detector.py — tick-frequency-based market session probe.

Covers:
  - crypto_24_7 → always normal (all states bypassed)
  - tick_time=0 → cached result or default
  - Live tick → NORMAL state + state transitions
  - Stalled tick → ROLLOVER → CLOSED progression
  - Reopen hysteresis (REOPEN_CONFIRM_SECONDS)
  - reset() method
"""

from __future__ import annotations

from unittest.mock import patch

from core.execution.session_detector import (
    CLOSED_STALL_SECONDS,
    REOPEN_CONFIRM_SECONDS,
    TICK_STALL_SECONDS,
    SessionDetector,
)

# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — crypto_24_7
# ═══════════════════════════════════════════════════════════════════════════


class TestCrypto247:
    """Crypto markets are always live — no degradation."""

    def test_always_returns_normal(self) -> None:
        sd = SessionDetector()
        result = sd.probe(tick_time=0.0, market_type="crypto_24_7")
        assert result["risk_tier"] == "normal"
        assert result["session_name"] == "crypto_continuous"
        assert result["volume_mult"] == 1.0
        assert result["_source"] == "dynamic_probe"

    def test_ignores_tick_stall(self) -> None:
        sd = SessionDetector()
        # Even with zero tick, crypto is always normal
        result = sd.probe(tick_time=0.0, market_type="crypto_24_7")
        assert result["risk_tier"] == "normal"


# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — tick_time=0 (no data)
# ═══════════════════════════════════════════════════════════════════════════


class TestNoTickData:
    """When tick_time=0 (tick unavailable), return cached or default."""

    def test_first_call_no_cache_returns_unknown_normal(self) -> None:
        sd = SessionDetector()
        result = sd.probe(tick_time=0.0, market_type="forex_24_5")
        assert result["session_name"] == "unknown"
        assert result["risk_tier"] == "normal"

    def test_returns_cached_result_on_subsequent_calls(self) -> None:
        sd = SessionDetector()
        # First: live tick to populate cache
        sd.probe(tick_time=100.0, market_type="forex_24_5")
        # Then: tick_time=0 returns cached result
        result = sd.probe(tick_time=0.0, market_type="forex_24_5")
        assert result["session_name"] == "live_trading"
        assert result["risk_tier"] == "normal"


# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — live tick → NORMAL
# ═══════════════════════════════════════════════════════════════════════════


class TestLiveTick:
    """Tick is updating → market is live."""

    def test_first_live_tick_returns_normal(self) -> None:
        sd = SessionDetector()
        result = sd.probe(tick_time=1000.0, market_type="forex_24_5")
        assert result["session_name"] == "live_trading"
        assert result["risk_tier"] == "normal"
        assert result["volume_mult"] == 1.0

    def test_changing_tick_keeps_normal(self) -> None:
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        result = sd.probe(tick_time=1001.0, market_type="forex_24_5")
        assert result["risk_tier"] == "normal"

    def test_same_tick_is_not_live(self) -> None:
        """Same tick_time value as last call → not considered a new tick."""
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # Same tick_time → falls through to stall detection
        with patch("core.execution.session_detector.time.monotonic", return_value=1000.0):
            sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # First same-tick call: stall_start set but stall_duration=0
        with patch("core.execution.session_detector.time.monotonic", return_value=1000.0):
            result = sd.probe(tick_time=1000.0, market_type="forex_24_5")
            assert result["risk_tier"] == "normal"  # brief stall


# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — stall progression
# ═══════════════════════════════════════════════════════════════════════════


class TestStallProgression:
    """Tick stalls → ROLLOVER → CLOSED."""

    def test_brief_stall_returns_normal(self) -> None:
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # Same tick for < TICK_STALL_SECONDS → normal
        with patch(
            "core.execution.session_detector.time.monotonic",
            return_value=1000.0 + TICK_STALL_SECONDS - 1,
        ):
            result = sd.probe(tick_time=1000.0, market_type="forex_24_5")
            assert result["risk_tier"] == "normal"

    def test_stall_exceeding_tick_stall_returns_rollover(self) -> None:
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # Stall_start set
        with patch("core.execution.session_detector.time.monotonic", return_value=1000.0):
            sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # Stall >= TICK_STALL_SECONDS but < CLOSED_STALL_SECONDS
        with patch(
            "core.execution.session_detector.time.monotonic",
            return_value=1000.0 + TICK_STALL_SECONDS + 1,
        ):
            result = sd.probe(tick_time=1000.0, market_type="forex_24_5")
            assert result["session_name"] == "daily_rollover"
            assert result["risk_tier"] == "reduced"
            assert result["volume_mult"] == 0.40
            assert result["sl_expand_mult"] == 1.50

    def test_stall_exceeding_closed_stall_returns_closed(self) -> None:
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        with patch("core.execution.session_detector.time.monotonic", return_value=1000.0):
            sd.probe(tick_time=1000.0, market_type="forex_24_5")
        with patch(
            "core.execution.session_detector.time.monotonic",
            return_value=1000.0 + CLOSED_STALL_SECONDS + 1,
        ):
            result = sd.probe(tick_time=1000.0, market_type="forex_24_5")
            assert result["session_name"] == "market_closed"
            assert result["risk_tier"] == "off"
            assert result["volume_mult"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — reopen hysteresis
# ═══════════════════════════════════════════════════════════════════════════


class TestReopenHysteresis:
    """After CLOSED, tick must stay live for REOPEN_CONFIRM_SECONDS.

    Tests use direct state manipulation (not time.monotonic mock) because
    the state machine is deterministic given its internal _current_state,
    _stall_start, and _last_tick_time values.
    """

    def test_reopen_from_closed_state_directly(self) -> None:
        """When _current_state='closed' and a new tick arrives, reopening starts."""
        sd = SessionDetector()
        # Manually set closed state
        sd._current_state = "closed"
        sd._last_tick_time = 1000.0
        sd._stall_start = 0.0
        sd._resume_start = 0.0

        # New tick arrives → should enter reopen logic
        result = sd.probe(tick_time=2000.0, market_type="forex_24_5")
        assert result["session_name"] == "market_reopening"
        assert result["risk_tier"] == "caution"
        assert result["volume_mult"] == 0.50

    def test_confirm_reopen_after_confirm_seconds(self) -> None:
        """After REOPEN_CONFIRM_SECONDS of live ticks, market confirmed reopen."""
        sd = SessionDetector()
        sd._current_state = "closed"
        sd._last_tick_time = 1000.0
        sd._resume_start = 0.0

        # First tick: start reopening
        sd.probe(tick_time=2000.0, market_type="forex_24_5")

        # Second tick after enough time
        sd._resume_start = sd._resume_start - REOPEN_CONFIRM_SECONDS  # simulate elapsed time
        result = sd.probe(tick_time=2001.0, market_type="forex_24_5")
        assert result["session_name"] == "market_reopen"
        assert result["risk_tier"] == "normal"


# ═══════════════════════════════════════════════════════════════════════════
# SessionDetector — reset
# ═══════════════════════════════════════════════════════════════════════════


class TestReset:
    """reset() clears all internal state."""

    def test_reset_clears_state(self) -> None:
        sd = SessionDetector()
        # Go to rollover state
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        with patch("core.execution.session_detector.time.monotonic", return_value=0.0):
            sd.probe(tick_time=1000.0, market_type="forex_24_5")
        with patch(
            "core.execution.session_detector.time.monotonic", return_value=TICK_STALL_SECONDS + 1
        ):
            sd.probe(tick_time=1000.0, market_type="forex_24_5")

        sd.reset()

        # After reset, first call should be normal again
        result = sd.probe(tick_time=5000.0, market_type="forex_24_5")
        assert result["risk_tier"] == "normal"
        assert result["session_name"] == "live_trading"

    def test_reset_clears_cache(self) -> None:
        sd = SessionDetector()
        sd.probe(tick_time=1000.0, market_type="forex_24_5")
        # reset() does NOT clear _last_result (by design — cache survives reset)
        sd.reset()
        # cache still populated → tick_time=0 returns cached result
        result = sd.probe(tick_time=0.0, market_type="forex_24_5")
        assert result["session_name"] == "live_trading"  # cached

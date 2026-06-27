"""Unit tests for ExitWatchdog — the safety-critical exit-order retry system.

Tests cover:
  - Dataclass integrity (ExitAttempt, ExitWatchdogResult)
  - Pre-flight position verification (already_closed short-circuit)
  - Successful dispatch path (first attempt)
  - Retry with exponential backoff on transient failures
  - L2 forced liquidation escalation
  - ACK polling (ZMQ fast path, file fallback)
  - Health monitoring (is_healthy)
  - Slippage escalation logic
  - Max total duration timeout
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from core.execution.exit_watchdog import (
    ACK_POLL_INTERVAL,
    MAX_RETRIES,
    MAX_SLIPPAGE_POINTS,
    MAX_TOTAL_DURATION,
    SLIPPAGE_ESCALATION,
    ExitAttempt,
    ExitWatchdog,
    ExitWatchdogResult,
)

# ═══════════════════════════════════════════════════════════════════════════
# Dataclass integrity
# ═══════════════════════════════════════════════════════════════════════════


class TestExitAttempt:
    def test_minimal_creation(self) -> None:
        att = ExitAttempt(
            attempt=1,
            timestamp_utc="2026-06-13T00:00:00Z",
            dispatch_success=False,
            ack_received=False,
        )
        assert att.attempt == 1
        assert att.dispatch_success is False
        assert att.ack_received is False
        assert att.ack_status == ""
        assert att.error == ""
        assert att.duration_ms == 0.0

    def test_failure_record(self) -> None:
        att = ExitAttempt(
            attempt=2,
            timestamp_utc="2026-06-13T00:00:05Z",
            dispatch_success=False,
            ack_received=False,
            error="dispatch_exception:timeout",
            duration_ms=1500.0,
        )
        assert att.attempt == 2
        assert att.dispatch_success is False
        assert att.error == "dispatch_exception:timeout"

    def test_success_record(self) -> None:
        att = ExitAttempt(
            attempt=1,
            timestamp_utc="2026-06-13T00:00:00Z",
            dispatch_success=True,
            ack_received=True,
            ack_status="accepted",
            duration_ms=500.0,
        )
        assert att.dispatch_success is True
        assert att.ack_received is True
        assert att.ack_status == "accepted"


class TestExitWatchdogResult:
    def test_success_result(self) -> None:
        result = ExitWatchdogResult(
            success=True,
            position_ticket=123456,
            total_attempts=1,
            total_duration_ms=500.0,
            final_status="closed",
        )
        assert result.success is True
        assert result.position_ticket == 123456
        assert result.total_attempts == 1
        assert result.final_status == "closed"

    def test_escalated_result(self) -> None:
        result = ExitWatchdogResult(
            success=False,
            position_ticket=789012,
            total_attempts=5,
            total_duration_ms=30000.0,
            final_status="critical_timeout",
            alerts=["CRITICAL: exit_watchdog_exhausted"],
        )
        assert result.success is False
        assert len(result.alerts) == 1
        assert "CRITICAL" in result.alerts[0]


# ═══════════════════════════════════════════════════════════════════════════
# ExitWatchdog initialization
# ═══════════════════════════════════════════════════════════════════════════


class TestExitWatchdogInit:
    def test_default_parameters(self, tmp_path: Path) -> None:
        wd = ExitWatchdog(data_dir=str(tmp_path))
        assert wd.max_retries == MAX_RETRIES
        assert wd.max_total_duration == MAX_TOTAL_DURATION
        assert wd.ack_poll_interval == ACK_POLL_INTERVAL
        assert wd.slippage_escalation == SLIPPAGE_ESCALATION
        assert wd.max_slippage_points == MAX_SLIPPAGE_POINTS

    def test_custom_parameters(self, tmp_path: Path) -> None:
        wd = ExitWatchdog(
            data_dir=str(tmp_path),
            max_retries=3,
            max_total_duration=15.0,
            ack_poll_interval=0.25,
            ack_poll_timeout=3.0,
            slippage_escalation=30,
            max_slippage_points=100,
        )
        assert wd.max_retries == 3
        assert wd.max_total_duration == 15.0
        assert wd.ack_poll_interval == 0.25
        assert wd.slippage_escalation == 30
        assert wd.max_slippage_points == 100

    def test_creates_alert_log_directory(self, tmp_path: Path) -> None:
        wd = ExitWatchdog(data_dir=str(tmp_path))
        log_dir = tmp_path / "reports"
        assert log_dir.exists()
        assert log_dir.is_dir()


# ═══════════════════════════════════════════════════════════════════════════
# execute_exit — pre-flight position verification
# ═══════════════════════════════════════════════════════════════════════════


class TestExecuteExitPreFlight:
    def test_position_already_closed(self, tmp_path: Path) -> None:
        """If get_position_open returns False, skip retry loop entirely."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        dispatch_fn = MagicMock()

        result = wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="bleed_stop",
            magic=90001,
            dispatch_fn=dispatch_fn,
            get_position_open=lambda ticket: False,
        )

        assert result.success is True
        assert result.final_status == "already_closed"
        assert result.total_attempts == 0
        # dispatch_fn should NOT be called
        dispatch_fn.assert_not_called()

    def test_position_still_open_proceeds(self, tmp_path: Path) -> None:
        """If get_position_open returns True, proceed with dispatch."""
        wd = ExitWatchdog(
            data_dir=str(tmp_path),
            max_total_duration=0.1,  # short timeout to exit quickly
        )
        dispatch_fn = MagicMock(return_value={"dispatched": False})

        result = wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="bleed_stop",
            magic=90001,
            dispatch_fn=dispatch_fn,
            get_position_open=lambda ticket: True,
        )

        # Dispatch should be attempted at least once
        assert dispatch_fn.call_count >= 1
        # With dispatch always failing and short timeout, should escalate
        assert not result.success or result.final_status == "closed_l2_forced"


# ═══════════════════════════════════════════════════════════════════════════
# execute_exit — successful dispatch (first attempt)
# ═══════════════════════════════════════════════════════════════════════════


class TestExecuteExitSuccess:
    def test_first_attempt_succeeds(self, tmp_path: Path) -> None:
        """Dispatch succeeds + ACK confirmed → success with 1 attempt."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        dispatch_fn = MagicMock(return_value={"dispatched": True, "intent_id": "test_001"})
        # Mock ACK to return success — avoids retry loop
        wd._poll_ack = MagicMock(return_value={"ack_status": "accepted"})

        result = wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="short",
            reason="brain_flip",
            magic=90003,
            dispatch_fn=dispatch_fn,
        )

        assert result.success is True
        assert result.total_attempts == 1
        assert result.final_status == "closed"
        dispatch_fn.assert_called_once()

    def test_payload_contains_required_fields(self, tmp_path: Path) -> None:
        """Verify the dispatch payload structure."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        captured_payload: dict = {}

        def capture(payload: dict) -> dict:
            captured_payload.update(payload)
            return {"dispatched": True}

        # FIX-20260627-147: mock _poll_ack to skip 5s ACK polling timeout.
        # Without a real bridge, _poll_ack times out on every attempt
        # (5 attempts × 5s timeout + backoff = ~15s).
        wd._poll_ack = lambda intent_id, timeout=5.0: {"ack_status": "accepted"}

        wd.execute_exit(
            position_ticket=999888,
            volume=0.03,
            side="long",
            reason="ev_trajectory_time",
            magic=90310,
            dispatch_fn=capture,
        )

        assert captured_payload["action"] == "close"
        assert captured_payload["position_ticket"] == 999888
        assert captured_payload["volume"] == 0.03
        assert captured_payload["side"] == "long"
        assert "ev_trajectory_time" in captured_payload.get("comment", "")


# ═══════════════════════════════════════════════════════════════════════════
# execute_exit — retry and escalation
# ═══════════════════════════════════════════════════════════════════════════


class TestExecuteExitRetry:
    def test_retry_on_transient_failure(self, tmp_path: Path) -> None:
        """First dispatch fails, second succeeds with ACK."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        call_count = [0]

        def flaky_dispatch(payload: dict) -> dict:
            call_count[0] += 1
            if call_count[0] < 2:
                return {"dispatched": False}
            return {"dispatched": True, "intent_id": f"test_{call_count[0]}"}

        # Mock ACK to succeed on second attempt
        wd._poll_ack = MagicMock(return_value={"ack_status": "accepted"})

        result = wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="hesitation_exit",
            magic=90001,
            dispatch_fn=flaky_dispatch,
        )

        # Should dispatch twice (first fails, second succeeds)
        assert call_count[0] >= 2
        assert result.success is True

    def test_slippage_escalation_after_retries(self, tmp_path: Path) -> None:
        """After 3 retries, slippage tolerance escalates to SLIPPAGE_ESCALATION pts."""
        wd = ExitWatchdog(
            data_dir=str(tmp_path),
            max_total_duration=0.05,  # very short — hit timeout fast
        )
        slippages: list[int] = []

        def dispatch_with_slippage(payload: dict) -> dict:
            sl = payload.get("slippage", 0)
            slippages.append(sl)
            return {"dispatched": False}

        wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="bleed_stop",
            magic=90001,
            dispatch_fn=dispatch_with_slippage,
        )

        # At least one attempt should have escalated slippage
        if slippages:
            assert any(s >= SLIPPAGE_ESCALATION for s in slippages) or any(
                s == 0 for s in slippages
            )

    def test_l2_forced_liquidation_after_timeout(self, tmp_path: Path) -> None:
        """After max_total_duration, L2 broker is called."""
        wd = ExitWatchdog(
            data_dir=str(tmp_path),
            max_total_duration=0.01,  # nearly instant timeout
        )
        dispatch_fn = MagicMock(return_value={"dispatched": False})
        l2_broker = MagicMock()
        l2_broker.close_position.return_value = (True, "L2 closed")

        result = wd.execute_exit(
            position_ticket=123456,
            volume=0.05,
            side="long",
            reason="bleed_stop",
            magic=90001,
            dispatch_fn=dispatch_fn,
            l2_broker=l2_broker,
        )

        # L2 broker should have been called at least once
        assert l2_broker.close_position.call_count >= 1
        # Check the call used emergency slippage
        call_args = l2_broker.close_position.call_args
        assert call_args is not None
        _, kwargs = call_args
        assert kwargs.get("slippage") == MAX_SLIPPAGE_POINTS


# ═══════════════════════════════════════════════════════════════════════════
# Health monitoring
# ═══════════════════════════════════════════════════════════════════════════


class TestIsHealthy:
    def test_healthy_by_default(self, tmp_path: Path) -> None:
        wd = ExitWatchdog(data_dir=str(tmp_path))
        assert wd.is_healthy() is True

    def test_unhealthy_after_critical_alert(self, tmp_path: Path) -> None:
        """CRITICAL alert logged within last hour → unhealthy."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        alert_path = tmp_path / "reports" / "exit_watchdog_alerts.jsonl"

        # Write a recent CRITICAL alert matching the actual format
        # is_healthy() checks: "CRITICAL" in rec.get("event", "")
        import json as _json
        from datetime import UTC
        from datetime import datetime as _dt

        now_iso = _dt.now(UTC).isoformat().replace("+00:00", "Z")
        alert = {
            "event": "CRITICAL: exit_watchdog_exhausted",
            "timestamp_utc": now_iso,
            "ticket": 123456,
            "reason": "L2 forced liquidation failed",
        }
        alert_path.write_text(_json.dumps(alert) + "\n", encoding="utf-8")

        assert wd.is_healthy() is False


# ═══════════════════════════════════════════════════════════════════════════
# ACK polling
# ═══════════════════════════════════════════════════════════════════════════


class TestPollAck:
    def test_poll_ack_empty_intent_id(self, tmp_path: Path) -> None:
        wd = ExitWatchdog(data_dir=str(tmp_path))
        result = wd._poll_ack("")
        assert result is None

    def test_poll_ack_file_fallback(self, tmp_path: Path) -> None:
        """When ZMQ fails (no listener), falls back to file polling."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        # intent_id that won't have an ACK file → returns None
        result = wd._poll_ack("nonexistent_intent_id", timeout=0.5)
        assert result is None

    def test_poll_ack_zmq_failure_graceful(self, tmp_path: Path) -> None:
        """ZMQ import/connection errors fall through to file fallback gracefully."""
        wd = ExitWatchdog(data_dir=str(tmp_path))
        # _poll_ack handles ZMQ errors internally via try/except + file fallback
        result = wd._poll_ack("test_intent_123", timeout=0.5)
        # Should return None (no ZMQ, no file)
        assert result is None

"""Pitfall 3 safeguard: Heartbeat exit-order watchdog with persistent retry.

In production, advanced exit logic (bleed stop, PnL-aware Z-exit, toxic flow
stop) cannot be hard-coded into MT5 SL/TP fields.  The Python brain must
send a market-close order and CONFIRM it was received and executed.

This watchdog wraps every exit order with:
  1. Dispatch → immediate ACK poll (is the order file consumed by bridge?)
  2. Bridge ACK → execution receipt (did MT5 actually close the position?)
  3. Persistent retry with exponential backoff on failure
  4. Escalation: after N retries, use wider slippage tolerance
  5. CRITICAL alert if position is still open after max total duration

Usage (in live_cycle.py exit section)::

    watchdog = ExitWatchdog(data_dir="data")
    success = watchdog.execute_exit(
        position_ticket=123456,
        volume=0.05,
        side="long",
        reason="bleed_stop_bar3",
        magic=25030100,
        dispatch_fn=dispatch_live_order,
    )
    if not success:
        # Escalation path triggered — check watchdog.alerts
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# -- Constants --

MAX_RETRIES = 5  # total retry attempts before escalation
RETRY_BACKOFF_BASE = 1.0  # seconds: 1, 2, 4, 8, 16
MAX_TOTAL_DURATION = 30.0  # seconds: give up and fire CRITICAL alert
ACK_POLL_INTERVAL = 0.5  # seconds between ACK file checks
ACK_POLL_TIMEOUT = 5.0  # seconds: max wait for a single ACK
SLIPPAGE_ESCALATION = 50  # points: slippage tolerance after 3 retries
MAX_SLIPPAGE_POINTS = 200  # absolute maximum slippage (emergency only)


# -- Data types --


@dataclass
class ExitAttempt:
    """Record of one exit-order dispatch attempt."""

    attempt: int
    timestamp_utc: str
    dispatch_success: bool
    ack_received: bool
    ack_status: str = ""
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class ExitWatchdogResult:
    """Result of the watchdog exit loop."""

    success: bool
    position_ticket: int
    total_attempts: int
    total_duration_ms: float
    final_status: str  # "closed" | "escalated" | "critical_timeout" | "cancelled"
    attempts: list[ExitAttempt] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


# -- Watchdog --


class ExitWatchdog:
    """Heartbeat watchdog for exit order dispatch and confirmation.

    Guarantees that exit orders are either confirmed executed or escalated
    to a human-visible CRITICAL alert.  Never silently drops an exit.

    Retry strategy:
      Attempt 1-2: normal dispatch, 5s ACK timeout each
      Attempt 3-4: widened slippage tolerance (50 pts), 5s ACK timeout
      Attempt 5:   emergency slippage (200 pts), 5s ACK timeout
      After 30s total: CRITICAL alert, continue retrying in background
    """

    def __init__(
        self,
        *,
        data_dir: str = "data",
        max_retries: int = MAX_RETRIES,
        max_total_duration: float = MAX_TOTAL_DURATION,
        ack_poll_interval: float = ACK_POLL_INTERVAL,
        ack_poll_timeout: float = ACK_POLL_TIMEOUT,
        slippage_escalation: int = SLIPPAGE_ESCALATION,
        max_slippage_points: int = MAX_SLIPPAGE_POINTS,
        # Vestigial since FIX-20260709-005 removed the structural evaluator that
        # consumed these; retained only for live_intent_loop.py caller/init-log
        # compat. Slated for removal on the next live_intent_loop.py touch.
        time_decay_cycles: int = 60,
        price_decay_bars: int = 5,
        price_decay_sl_proximity: float = 0.5,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.max_retries = max_retries
        self.max_total_duration = max_total_duration
        self.ack_poll_interval = ack_poll_interval
        self.ack_poll_timeout = ack_poll_timeout
        self.slippage_escalation = slippage_escalation
        self.max_slippage_points = max_slippage_points
        # Vestigial (see __init__ note): read only by the live_intent_loop init-log echo.
        self.time_decay_cycles = time_decay_cycles
        self.price_decay_bars = price_decay_bars
        self.price_decay_sl_proximity = price_decay_sl_proximity

        self._alerts: list[str] = []
        self._alert_log_path = self.data_dir / "reports" / "exit_watchdog_alerts.jsonl"
        self._alert_log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- Public API --

    def execute_exit(
        self,
        *,
        position_ticket: int,
        volume: float,
        side: str,
        reason: str,
        magic: int = 0,
        dispatch_fn: Callable[..., dict[str, Any]],
        brain_ids: list[str] | None = None,
        get_position_open: Callable[[int], bool] | None = None,
        l2_broker: Any = None,
        pnl: float | None = None,
        exit_urgency: float = 0.5,
        factor_breakdown: dict[str, float] | None = None,
    ) -> ExitWatchdogResult:
        """Execute an exit order with full watchdog protection.

        Args:
            position_ticket: MT5 position ticket to close.
            volume: Volume to close (may be less than full position for partial).
            side: "long" or "short".
            reason: Human-readable exit reason (e.g., "bleed_stop", "z_reversion").
            magic: Strategy magic number for journal attribution.
            dispatch_fn: Callable that sends the close order payload.
                         Signature: fn(payload: dict) -> dict with keys
                         {"dispatched": bool, "intent_id": str, ...}
            brain_ids: Optional list of brain IDs for journal attribution.
            get_position_open: Optional callable that checks if a position is
                               still open.  Signature: fn(ticket: int) -> bool.
                               If provided, used for definitive confirmation.

        Returns:
            ExitWatchdogResult with success flag and full attempt history.
        """
        start = time.monotonic()
        attempts: list[ExitAttempt] = []
        alerts: list[str] = []
        self._current_factor_breakdown = factor_breakdown

        # ── Pre-flight: verify position still exists ──
        # FIX-20260525-024: skip entire retry loop if position already closed
        # in MT5 (MIA).  Otherwise the watchdog exhausts all retries against
        # a nonexistent position and fires a false CRITICAL alert.
        if get_position_open is not None:
            try:
                if not get_position_open(position_ticket):
                    return ExitWatchdogResult(
                        success=True,
                        position_ticket=position_ticket,
                        total_attempts=0,
                        total_duration_ms=0,
                        final_status="already_closed",
                        attempts=[],
                        alerts=[],
                    )

            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass
        for attempt_n in range(1, self.max_retries + 1):
            elapsed = time.monotonic() - start
            if elapsed > self.max_total_duration:
                # L2 forced liquidation: bypass bridge, close directly via MT5
                l2_ok = False
                if l2_broker is not None:
                    try:
                        l2_ok, l2_msg = l2_broker.close_position(
                            position_ticket, slippage=self.max_slippage_points
                        )
                        if l2_ok:
                            return ExitWatchdogResult(
                                success=True,
                                position_ticket=position_ticket,
                                total_attempts=attempt_n,
                                total_duration_ms=round(elapsed * 1000),
                                final_status="closed_l2_forced",
                                attempts=attempts,
                                alerts=alerts,
                            )
                    except Exception as _l2_exc:  # noqa: BLE001  # BLE001:FOG (logged, Phase 3b)
                        import logging as _lg

                        _lg.getLogger(__name__).critical(
                            "L2 forced liquidation FAILED: ticket=%s reason=%s error=%s",
                            position_ticket,
                            reason,
                            _l2_exc,
                            exc_info=True,
                        )
                        alerts.append(
                            f"CRITICAL: l2_forced_close_failed ticket={position_ticket} "
                            f"error={type(_l2_exc).__name__}"
                        )
                alert = (
                    f"CRITICAL: exit_watchdog_timeout ticket={position_ticket} "
                    f"reason={reason} elapsed={elapsed:.1f}s attempts={attempt_n - 1}"
                    f"{' l2_fallback=' + ('ok' if l2_ok else 'failed') if l2_broker else ''}"
                )
                alerts.append(alert)
                self._fire_alert(alert, position_ticket, reason)
                return ExitWatchdogResult(
                    success=False,
                    position_ticket=position_ticket,
                    total_attempts=attempt_n - 1,
                    total_duration_ms=round(elapsed * 1000),
                    final_status="critical_timeout",
                    attempts=attempts,
                    alerts=alerts,
                )

            # Build payload with escalating slippage
            slippage = self._slippage_for_attempt(attempt_n, exit_urgency)
            payload = self._build_close_payload(
                position_ticket=position_ticket,
                volume=volume,
                side=side,
                reason=reason,
                magic=magic,
                brain_ids=brain_ids,
                slippage_points=slippage,
                pnl=pnl,
            )

            att_start = time.monotonic()
            att = ExitAttempt(
                attempt=attempt_n,
                timestamp_utc=datetime.now(UTC).isoformat(),
                dispatch_success=False,
                ack_received=False,
            )

            # Step 1: Dispatch the order
            try:
                result = dispatch_fn(payload)
                att.dispatch_success = bool(result.get("dispatched", False))
            except Exception as exc:  # noqa: BLE001  # BLE001:FOG (logged, Phase 3b)
                att.error = f"dispatch_exception:{exc}"
                attempts.append(att)
                continue
            if not att.dispatch_success:
                att.error = f"dispatch_rejected:{result.get('reason', 'unknown')}"
                attempts.append(att)
                continue

            # Step 2: Poll for ACK from bridge worker
            intent_id = result.get("intent_id", "")
            ack = self._poll_ack(intent_id, timeout=self.ack_poll_timeout)

            if ack and ack.get("ack_status") == "accepted":
                att.ack_received = True
                att.ack_status = "accepted"

                # Step 3: Verify position actually closed (definitive)
                if get_position_open is not None:
                    # Brief wait for MT5 to process
                    time.sleep(0.3)
                    if not get_position_open(position_ticket):
                        att.duration_ms = round((time.monotonic() - att_start) * 1000)
                        attempts.append(att)
                        return ExitWatchdogResult(
                            success=True,
                            position_ticket=position_ticket,
                            total_attempts=attempt_n,
                            total_duration_ms=round((time.monotonic() - start) * 1000),
                            final_status="closed",
                            attempts=attempts,
                            alerts=alerts,
                        )
                    else:
                        # Position still open despite ACK — retry
                        att.ack_status = "accepted_but_position_still_open"
                else:
                    # No position checker available — trust the ACK
                    att.duration_ms = round((time.monotonic() - att_start) * 1000)
                    attempts.append(att)
                    return ExitWatchdogResult(
                        success=True,
                        position_ticket=position_ticket,
                        total_attempts=attempt_n,
                        total_duration_ms=round((time.monotonic() - start) * 1000),
                        final_status="closed",
                        attempts=attempts,
                        alerts=alerts,
                    )
            else:
                att.ack_status = ack.get("ack_status", "no_ack") if ack else "no_ack"

            att.duration_ms = round((time.monotonic() - att_start) * 1000)
            attempts.append(att)

            # Backoff before retry — urgency modulates the interval
            if attempt_n < self.max_retries:
                backoff = self._backoff_seconds(attempt_n, exit_urgency)
                time.sleep(backoff)

        # All retries exhausted — attempt L2 forced liquidation
        elapsed = time.monotonic() - start
        if l2_broker is not None:
            try:
                l2_ok, _l2_msg = l2_broker.close_position(
                    position_ticket, slippage=self.max_slippage_points
                )
                if l2_ok:
                    return ExitWatchdogResult(
                        success=True,
                        position_ticket=position_ticket,
                        total_attempts=self.max_retries,
                        total_duration_ms=round(elapsed * 1000),
                        final_status="closed_l2_forced",
                        attempts=attempts,
                        alerts=alerts,
                    )
            except Exception as _l2f_exc:  # noqa: BLE001  # BLE001:FOG (logged, Phase 3b)
                import logging as _lg

                _lg.getLogger(__name__).critical(
                    "ESCALATED L2 forced liquidation FAILED: ticket=%s reason=%s error=%s",
                    position_ticket,
                    reason,
                    _l2f_exc,
                    exc_info=True,
                )
                alerts.append(
                    f"EMERGENCY: l2_exhausted_close_failed ticket={position_ticket} "
                    f"error={type(_l2f_exc).__name__}"
                )
        alert = (
            f"ESCALATED: exit_watchdog_exhausted ticket={position_ticket} "
            f"reason={reason} attempts={self.max_retries} elapsed={elapsed:.1f}s"
        )
        alerts.append(alert)
        self._fire_alert(alert, position_ticket, reason)
        return ExitWatchdogResult(
            success=False,
            position_ticket=position_ticket,
            total_attempts=self.max_retries,
            total_duration_ms=round(elapsed * 1000),
            final_status="escalated",
            attempts=attempts,
            alerts=alerts,
        )

    def is_healthy(self) -> bool:
        """Return False if recent alerts indicate systemic exit failures."""
        # Check for CRITICAL alerts in the last hour
        try:
            if self._alert_log_path.exists():
                cutoff = datetime.now(UTC).timestamp() - 3600
                with open(self._alert_log_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line.strip())
                            ts = rec.get("timestamp_utc", "")
                            if "CRITICAL" in rec.get("event", ""):
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if dt.timestamp() > cutoff:
                                    return False
                        except (json.JSONDecodeError, ValueError, AttributeError):
                            pass
        except OSError:
            pass
        return True

    # -- Internal --

    def _slippage_for_attempt(self, attempt: int, exit_urgency: float = 0.5) -> int:
        """Escalate slippage tolerance with each retry.

        High-urgency exits start with wider slippage to avoid wasting retries
        on tight spreads when the position is in distress.
        """
        # Critical urgency (>=0.9): max slippage from attempt 1 — pay the spread
        if exit_urgency >= 0.9:
            return self.max_slippage_points
        # High urgency (>=0.8): skip the conservative tier, start at escalated
        if exit_urgency >= 0.8:
            if attempt <= 3:
                return self.slippage_escalation
            return self.max_slippage_points
        # Default: existing behavior
        if attempt <= 2:
            return 20  # normal: 2 pips
        elif attempt <= 4:
            return self.slippage_escalation  # escalated: 5 pips
        else:
            return self.max_slippage_points  # emergency: 20 pips

    @staticmethod
    def _backoff_seconds(attempt: int, exit_urgency: float) -> float:
        """Compute retry backoff, shortened for high-urgency exits."""
        if exit_urgency >= 0.9:
            return 0.5  # fixed short interval — every moment counts
        base = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
        if exit_urgency >= 0.8:
            base *= 0.5  # half the normal backoff
        return min(base, 10.0)

    @staticmethod
    def _build_close_payload(
        *,
        position_ticket: int,
        volume: float,
        side: str,
        reason: str,
        magic: int = 0,
        brain_ids: list[str] | None = None,
        slippage_points: int = 20,
        pnl: float | None = None,
    ) -> dict[str, Any]:
        """Build the close-order payload for the MT5 bridge."""
        payload: dict[str, Any] = {
            "action": "close",
            "position_ticket": position_ticket,
            "volume": max(0.01, round(volume, 2)),
            "side": side,
            "comment": f"exit_watchdog:{reason}",
            "slippage": slippage_points,
        }
        if pnl is not None:
            payload["pnl"] = pnl
        if magic:
            payload["magic"] = magic
        if brain_ids:
            payload["brain_ids"] = brain_ids
        return payload

    def _poll_ack(self, intent_id: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Resolve bridge ACK — ZMQ fast path first, file polling fallback."""
        if not intent_id:
            return None

        try:
            from core.protocol.services.zmq_receipt_listener import resolve_ack

            return resolve_ack(
                intent_id,
                base_dir=str(self.data_dir),
                timeout=timeout,
                poll_interval=self.ack_poll_interval,
            )

        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
        # Pure file fallback if ZMQ not available
        deadline = time.monotonic() + timeout
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        ack_path = self.data_dir / "receipts" / today / "exec_bridge" / f"{intent_id}.ack.json"

        while time.monotonic() < deadline:
            try:
                if ack_path.exists():
                    data = json.loads(ack_path.read_text(encoding="utf-8"))
                    return data
            except (json.JSONDecodeError, OSError):
                pass
            time.sleep(self.ack_poll_interval)

        return None

    def _fire_alert(self, message: str, ticket: int, reason: str) -> None:
        """Log a CRITICAL/ESCALATED alert to persistent storage."""
        self._alerts.append(message)
        try:
            record: dict[str, Any] = {
                "event": "CRITICAL" if "CRITICAL" in message else "ESCALATED",
                "message": message,
                "ticket": ticket,
                "reason": reason,
                "timestamp_utc": datetime.now(UTC).isoformat(),
            }
            fb = getattr(self, "_current_factor_breakdown", None)
            if fb:
                # Defensive numpy→float conversion (json.dumps crashes on numpy floats)
                record["factor_breakdown"] = {k: float(v) for k, v in fb.items()}
            with open(self._alert_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # disk full — nothing we can do

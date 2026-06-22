"""Staggered execution queue for multi-strategy dispatch.

When multiple strategy lines produce trade decisions in the same cycle, they
must be sent to MT5 sequentially — not simultaneously — to avoid:
  - Slippage stacking (multiple orders hitting the market at the same second)
  - MT5 order rejection due to concurrent access
  - Price impact from clustered entries

The queue processes decisions in order of priority (fastest strategy first)
with a short stagger delay between dispatches.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from typing import Any

# DQAF-20260622-059 / P2: sentinel magic guard
from core.contracts.strategy_magic import UnattributedOrderRejected
from core.runtime.fault_handler import fail_open_guard

logger = logging.getLogger(__name__)


class ExecutionQueueFatalError(Exception):
    """Fatal dispatch pipeline error — circuit breaker MUST be tripped.

    Raised by :meth:`ExecutionQueue.flush` when an unhandled exception
    occurs inside the dispatch loop.  The caller (live_cycle.py) catches
    this and trips the global circuit breaker to prevent new entries
    while the pipeline is in an unknown state (FIX-20260607-140).
    """


# Default priority: Micro > Barrier > StatArb (shortest hold time first)
DEFAULT_PRIORITY = {
    "micro_3bar": 0,
    "barrier_12bar": 1,
    "statarb_dynamic": 2,
}


@dataclass(frozen=True)
class QueuedDecision:
    """Immutable entry in the execution queue."""

    strategy_name: str
    priority: int
    decision: Any  # StrategyDecision from strategy_line.py
    risk_result: Any  # RiskResult from portfolio_risk.py


@dataclass(frozen=True)
class DispatchResult:
    """Immutable result of a dispatch attempt."""

    strategy_name: str
    magic: int
    dispatched: bool
    direction: str = ""
    reason: str = ""
    journal_entry: dict[str, Any] | None = None
    net_out_ticket_update: dict[str, Any] | None = None
    pnl: float | None = None  # FIX-138-Phase3: estimated PnL for trade notifications
    volume: float = 0.0  # trade volume (for notifications)
    price: float | None = None  # fill price (for notifications)


class ExecutionQueue:
    """Serializes multi-strategy dispatch with stagger delay.

    Blind Spot 3 (2026-06-13): entry-in-flight lock prevents duplicate open
    orders when MT5 ACK is slow (>5s).  Without this, a second management
    cycle sees no position yet and dispatches a duplicate — doubling exposure.
    Pattern mirrors the proven _pending_close lock in position_manager.py.
    """

    # ── Entry-in-flight constants ──
    PENDING_OPEN_TIMEOUT_SEC: float = 30.0  # auto-expire lock after 30s
    PENDING_OPEN_FLOOD_THRESHOLD: int = 3  # permanent-lock after 3 attempts

    def __init__(
        self,
        *,
        stagger_seconds: float = 5.0,
        priority_map: dict[str, int] | None = None,
    ):
        self.stagger_seconds = stagger_seconds
        self.priority_map = priority_map or DEFAULT_PRIORITY
        self._last_dispatch_time: float = 0.0
        self._queue: list[QueuedDecision] = []
        # ── Entry-in-flight lock (Blind Spot 3) ──
        self._pending_open: dict[str, float] = {}  # strategy_name → monotonic time
        self._open_attempt_count: dict[str, int] = {}  # cumulative attempts per strategy
        self._open_flood_locked: set[str] = set()  # permanent-locked strategies
        # ── DQAF-20260622-059 / P2: UnattributedOrderRejected self-protection ──
        self._unattributed_blocked: set[str] = set()  # strategies blocked due to sentinel magic 90401

    # ── Entry-in-flight API (Blind Spot 3) ──────────────────────────────

    def is_pending_open(self, strategy_name: str) -> bool:
        """Check whether an open order is still in-flight for this strategy.

        Returns True if the strategy has a pending open that hasn't timed out
        or been permanently flood-locked.
        """
        if strategy_name in self._open_flood_locked:
            return True
        dispatched_at = self._pending_open.get(strategy_name)
        if dispatched_at is None:
            return False
        if _time.monotonic() - dispatched_at > self.PENDING_OPEN_TIMEOUT_SEC:
            # Auto-expire stale lock
            del self._pending_open[strategy_name]
            self._open_attempt_count.pop(strategy_name, None)
            return False
        return True

    def _mark_pending_open(self, strategy_name: str) -> None:
        """Record that an open order was dispatched for this strategy."""
        self._pending_open[strategy_name] = _time.monotonic()
        self._open_attempt_count[strategy_name] = self._open_attempt_count.get(strategy_name, 0) + 1
        if self._open_attempt_count[strategy_name] >= self.PENDING_OPEN_FLOOD_THRESHOLD:
            self._open_flood_locked.add(strategy_name)
            import logging as _flood_log

            _flood_log.getLogger(__name__).critical(
                "FATAL: strategy=%s hit open flood threshold (%d attempts) — permanent-locked. "
                "Manual intervention required.",
                strategy_name,
                self._open_attempt_count[strategy_name],
            )

    def _clear_pending_open(self, strategy_name: str) -> None:
        """Clear the entry-in-flight lock on confirmed dispatch or rejection."""
        self._pending_open.pop(strategy_name, None)
        self._open_attempt_count.pop(strategy_name, None)

    # ── DQAF-20260622-059 / P2: UnattributedOrderRejected self-protection ──

    def is_unattributed_blocked(self, strategy_name: str) -> bool:
        """Check whether a strategy is blocked due to sentinel magic (90401).

        Once blocked, the strategy MUST NOT send any more OPEN orders until
        manual intervention clears the block (requires system restart).
        """
        return strategy_name in self._unattributed_blocked

    # ── End unattributed-block API ────────────────────────────────────────

    # ── End entry-in-flight API ─────────────────────────────────────────

    def enqueue(self, strategy_name: str, decision: Any, risk_result: Any) -> None:
        """Add a decision to the queue."""
        priority = self.priority_map.get(strategy_name, 99)
        self._queue.append(QueuedDecision(strategy_name, priority, decision, risk_result))

    def flush(
        self,
        dispatch_fn,
        *,
        journal_path: Any = None,
        mt5_terminal_path: str = "",
        symbol: str = "XAUUSDc",
        base_dir: str = "data",
        ignore_protection_flag: bool = False,
        protection_flag_path: str = "",
        broker: Any = None,
        close_dispatch_fn: Any = None,
        adapter_name: str = "mt5",
    ) -> list[DispatchResult]:
        """Process all queued decisions in priority order with stagger delay.

        Args:
            dispatch_fn: Callable for open-order dispatch.
            broker: Optional BrokerAdapter for pre-dispatch price validation.
            close_dispatch_fn: Optional callable for net-out close dispatch.
                Signature: fn(payload: dict) -> dict.  When provided, net-out
                close orders are routed through this instead of bare
                ``dispatch_live_order``.  Used by live_cycle to inject
                ExitWatchdog wrapping without coupling ExecutionQueue to
                watchdog types (陷阱三: upper-layer interception).

        Returns:
            List of DispatchResult, one per queued decision.
        """
        if not self._queue:
            return []

        # ── FIX-20260607-140: Fail-Closed dispatch wrapper ───────────────
        # Any unhandled exception inside flush() is a FATAL error — the
        # dispatch pipeline is broken and the system MUST NOT continue
        # opening new positions (Fail-Open → Fail-Closed).
        # The caller catches ExecutionQueueFatalError and trips the
        # circuit breaker, blocking all new entries.
        try:
            return self._flush_unsafe(
                dispatch_fn,
                journal_path=journal_path,
                mt5_terminal_path=mt5_terminal_path,
                symbol=symbol,
                base_dir=base_dir,
                ignore_protection_flag=ignore_protection_flag,
                protection_flag_path=protection_flag_path,
                broker=broker,
                close_dispatch_fn=close_dispatch_fn,
                adapter_name=adapter_name,
            )
        except Exception as _fatal_exc:  # BLE001:FOG (logged, Phase 3b)
            with fail_open_guard("execution_queue:flush"):
                import logging as _fatal_log

                _fatal_log.getLogger(__name__).critical(
                    "FATAL: ExecutionQueue flush() crashed — dispatch pipeline broken. "
                    "Circuit breaker MUST be tripped by caller. Error: %s",
                    _fatal_exc,
                    exc_info=True,
                )
                raise ExecutionQueueFatalError(
                    f"Dispatch pipeline fatal error: {_fatal_exc}"
                ) from _fatal_exc
    def _flush_unsafe(
        self,
        dispatch_fn,
        *,
        journal_path: Any = None,
        mt5_terminal_path: str = "",
        symbol: str = "XAUUSDc",
        base_dir: str = "data",
        ignore_protection_flag: bool = False,
        protection_flag_path: str = "",
        broker: Any = None,
        close_dispatch_fn: Any = None,
        adapter_name: str = "mt5",
    ) -> list[DispatchResult]:
        """Internal flush implementation — wrapped by fail-closed guard."""
        # Sort by priority (lowest first)
        self._queue.sort(key=lambda q: q.priority)
        results: list[DispatchResult] = []

        # Pre-fetch current price for validation if broker is available
        _current_price: float | None = None
        if broker is not None:
            with fail_open_guard("ExecutionQueue:PriceFetch"):
                _mid, _bid, _ask = broker.fetch_prices(symbol)
                _current_price = _mid
        else:
            logger.warning(
                "ExecutionQueue.flush: no broker provided — price guard validation skipped"
            )

        for i, queued in enumerate(self._queue):
            decision = queued.decision
            risk = queued.risk_result

            if risk.verdict.value == "rejected":
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=False,
                        direction=decision.direction,
                        reason=f"risk_rejected:{risk.reason}",
                    )
                )
                continue

            # Stagger: ensure minimum gap between dispatches
            if i > 0:
                elapsed = _time.monotonic() - self._last_dispatch_time
                if elapsed < self.stagger_seconds:
                    _time.sleep(self.stagger_seconds - elapsed)

            # ── Price guard: validate SL/TP before dispatch ──
            if _current_price is not None and _current_price > 0:
                try:
                    if decision.direction == "long":
                        if not (decision.sl < _current_price < decision.tp):
                            results.append(
                                DispatchResult(
                                    strategy_name=queued.strategy_name,
                                    magic=decision.magic,
                                    dispatched=False,
                                    direction=decision.direction,
                                    reason=f"price_guard_failed: sl={decision.sl} < price={_current_price} < tp={decision.tp}",
                                )
                            )
                            self._last_dispatch_time = _time.monotonic()
                            continue
                    else:  # short
                        if not (decision.tp < _current_price < decision.sl):
                            results.append(
                                DispatchResult(
                                    strategy_name=queued.strategy_name,
                                    magic=decision.magic,
                                    dispatched=False,
                                    direction=decision.direction,
                                    reason=f"price_guard_failed: tp={decision.tp} < price={_current_price} < sl={decision.sl}",
                                )
                            )
                            self._last_dispatch_time = _time.monotonic()
                            continue
                except Exception as _pg_exc:  # BLE001:FOG (logged, Phase 3b)
                    with fail_open_guard("execution_queue:_flush_unsafe"):
                        logger.error(
                            "price_guard validation exception for strategy=%s: %s",
                            queued.strategy_name,
                            _pg_exc,
                        )
                        results.append(
                            DispatchResult(
                                strategy_name=queued.strategy_name,
                                magic=decision.magic,
                                dispatched=False,
                                direction=decision.direction,
                                reason=f"price_guard_exception:{_pg_exc}",
                            )
                        )
                        self._last_dispatch_time = _time.monotonic()
                        continue
            # If NET_OUT or REDUCED, handle opposing position first
            _net_out_ticket_update: dict[str, Any] | None = None
            _close_result: dict[str, Any] | None = None  # FIX-138-Phase3: init before branch
            if risk.verdict.value in ("net_out", "reduced") and risk.net_out_ticket:
                # Close/reduce existing opposing position
                _close_confirmed = False
                try:
                    from core.execution.live_order_sender import dispatch_live_order

                    _close_vol = (
                        risk.net_out_close_volume
                        if risk.net_out_close_volume > 0
                        else risk.adjusted_volume
                    )
                    _close_payload: dict[str, Any] = {
                        "action": "close",
                        "position_ticket": risk.net_out_ticket,
                        "volume": _close_vol if _close_vol > 0 else 0.01,
                        "comment": f"net_out:{queued.strategy_name}",
                        "magic": decision.magic,
                        "side": "short" if decision.direction == "long" else "long",
                    }
                    # Forward PnL if computed upstream
                    _net_out_pnl = getattr(risk, "net_out_pnl", None)
                    if _net_out_pnl is not None:
                        _close_payload["pnl"] = _net_out_pnl
                    # Carry brain_ids from the position being closed (or aggressor as fallback)
                    _aggressor_brain_ids = getattr(queued.decision, "brain_ids", None)
                    _net_out_brain_ids = getattr(risk, "net_out_brain_ids", None)
                    if _net_out_brain_ids:
                        _close_payload["brain_ids"] = _net_out_brain_ids
                    elif _aggressor_brain_ids:
                        _close_payload["brain_ids"] = _aggressor_brain_ids
                    # 陷阱三: upper-layer interception — use callback if available
                    if close_dispatch_fn is not None:
                        _close_result = close_dispatch_fn(_close_payload)
                    else:
                        _close_result = dispatch_live_order(
                            base_dir=base_dir,
                            broker=None,
                            symbol=symbol,
                            execution_payload=_close_payload,
                            skip_price_guard=True,
                            ignore_protection_flag=ignore_protection_flag,
                            protection_flag_path=protection_flag_path,
                            adapter_name=adapter_name,
                            extensions={"mt5_terminal_path": mt5_terminal_path},
                        )
                    # ── Close confirmation poll (timeout 30 s, max 120 iterations) ──
                    if isinstance(_close_result, dict):
                        _intent_id = _close_result.get("intent_id")
                        if _intent_id:
                            try:
                                from core.protocol.services.zmq_receipt_listener import resolve_ack

                                _ack = resolve_ack(
                                    _intent_id,
                                    base_dir=str(base_dir),
                                    timeout=30.0,
                                    poll_interval=0.5,
                                )
                                if _ack and _ack.get("ack_status") == "accepted":
                                    _close_confirmed = True
                                    _ack_detail = _ack.get("detail", {})
                                    if _ack_detail.get("new_ticket"):
                                        _net_out_ticket_update = {
                                            "old_ticket": _ack_detail["old_ticket"],
                                            "new_ticket": _ack_detail["new_ticket"],
                                            "close_volume": _close_vol,
                                        }
                            except Exception as _ack_exc:  # BLE001:FOG (logged, Phase 3b)
                                with fail_open_guard("execution_queue:_flush_unsafe"):
                                    logger.warning(
                                        "ACK resolve error for intent_id=%s: %s",
                                        _intent_id,
                                        _ack_exc,
                                    )
                        else:
                            # When intent_id is empty, honour the dispatched flag from the
                            # close result.  Net-out closes routed through ExitWatchdog
                            # return no intent_id even on successful dispatch; blind
                            # confirmation would open a new position against a still-open
                            # opposing position when the close actually failed.
                            _close_confirmed = bool(_close_result.get("dispatched", False))
                except Exception as _net_out_exc:  # BLE001:FOG (logged, Phase 3b)
                    with fail_open_guard("execution_queue:_flush_unsafe"):
                        logger.error(
                            "net-out close dispatch failed for strategy=%s ticket=%s: %s",
                            queued.strategy_name,
                            risk.net_out_ticket if hasattr(risk, "net_out_ticket") else "unknown",
                            _net_out_exc,
                        )
                if not _close_confirmed:
                    results.append(
                        DispatchResult(
                            strategy_name=queued.strategy_name,
                            magic=decision.magic,
                            dispatched=False,
                            direction=decision.direction,
                            reason="net_out_close_not_confirmed",
                        )
                    )
                    self._last_dispatch_time = _time.monotonic()
                    continue

            # Dispatch the actual order (price guard already validated above)
            # Retry once on transient MT5 failures (1.5 s delay)
            # Stable intent_id across retries for idempotency
            import uuid as _uuid

            _intent_id = f"eq_{queued.strategy_name}_{_uuid.uuid4().hex[:12]}"
            _max_attempts = 2
            _dispatched = False
            _last_error = ""
            _journal_entry = None

            # ── Blind Spot 3: mark entry-in-flight BEFORE first attempt ──
            self._mark_pending_open(queued.strategy_name)

            for _attempt in range(_max_attempts):
                try:
                    _journal_entry = dispatch_fn(
                        side=decision.direction,
                        stop_loss=decision.sl,
                        take_profit=decision.tp,
                        intent_id=_intent_id,
                        volume=risk.adjusted_volume
                        if risk.adjusted_volume > 0
                        else decision.volume,
                        magic=decision.magic,
                        hard_sl=decision.hard_sl,
                        brain_ids=decision.brain_ids,
                        brain_votes=getattr(decision, "brain_votes", None) or None,
                        confidence=getattr(decision, "confidence", None),
                        p_win=getattr(decision, "p_win", 0.0) or 0.0,
                        p_win_source=getattr(decision, "p_win_source", "unknown") or "unknown",
                        p_win_degraded=bool(getattr(decision, "p_win_degraded", False)),
                        kelly_mult=getattr(decision, "kelly_mult", 1.0) or 1.0,
                    )
                    _dispatched = True
                    break
                except UnattributedOrderRejected as _sentinel_exc:  # DQAF-20260622-059 / P2
                    # ── FAIL-FAST: sentinel magic 90401 detected on OPEN order ──
                    # This is a NON-RECOVERABLE configuration error — the
                    # strategy→magic resolution pipeline is broken upstream.
                    # Retrying will produce the same error.  Block the strategy
                    # permanently (until restart) to prevent infinite reject loops.
                    self._unattributed_blocked.add(queued.strategy_name)
                    _last_error = str(_sentinel_exc)
                    logger.critical(
                        "UNATTRIBUTED_ORDER_REJECTED: strategy=%s magic=%s intent_id=%s — "
                        "strategy PERMANENTLY BLOCKED until system restart. "
                        "Upstream strategy→magic resolution pipeline is broken.",
                        queued.strategy_name,
                        _sentinel_exc.magic,
                        _sentinel_exc.intent_id,
                    )
                    break  # do NOT retry — fail-fast
                except Exception as exc:  # BLE001:FOG (logged, Phase 3b)
                    with fail_open_guard("execution_queue:_flush_unsafe"):
                        _last_error = str(exc)
                        if _attempt < _max_attempts - 1:
                            _time.sleep(1.5)
            if _dispatched:
                _close_pnl: float | None = None
                if _close_result is not None:
                    _close_pnl = _close_result.get("pnl")
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=True,
                        direction=decision.direction,
                        reason="ok",
                        journal_entry=_journal_entry,
                        net_out_ticket_update=_net_out_ticket_update,
                        pnl=_close_pnl,
                        volume=risk.adjusted_volume
                        if risk.adjusted_volume > 0
                        else decision.volume,
                    )
                )
            else:
                # ── Blind Spot 3: clear entry-in-flight on definitive failure ──
                self._clear_pending_open(queued.strategy_name)
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=False,
                        direction=decision.direction,
                        reason=_last_error,
                        net_out_ticket_update=_net_out_ticket_update,
                    )
                )

            self._last_dispatch_time = _time.monotonic()

        # Clear queue
        self._queue.clear()

        return results

    @property
    def queue_size(self) -> int:
        return len(self._queue)

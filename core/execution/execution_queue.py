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

import time as _time
from dataclasses import dataclass
from typing import Any

# Default priority: Micro > Barrier > StatArb (shortest hold time first)
DEFAULT_PRIORITY = {
    "micro_3bar": 0,
    "barrier_12bar": 1,
    "statarb_dynamic": 2,
}


@dataclass
class QueuedDecision:
    strategy_name: str
    priority: int
    decision: Any  # StrategyDecision
    risk_result: Any  # RiskResult


@dataclass
class DispatchResult:
    strategy_name: str
    magic: int
    dispatched: bool
    reason: str = ""
    journal_entry: dict[str, Any] | None = None


class ExecutionQueue:
    """Serializes multi-strategy dispatch with stagger delay."""

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
    ) -> list[DispatchResult]:
        """Process all queued decisions in priority order with stagger delay.

        Args:
            dispatch_fn: Callable that takes (decision, mt5_terminal_path, ...)
                         and dispatches to MT5.  Signature should match
                         ``dispatch_live_open_order``.
            broker: Optional BrokerAdapter for pre-dispatch price validation.
                    When provided, SL/TP are validated against current mid price
                    before dispatch (price guard).  This avoids the MT5 re-init
                    inside dispatch_live_open_order.

        Returns:
            List of DispatchResult, one per queued decision.
        """
        if not self._queue:
            return []

        # Sort by priority (lowest first)
        self._queue.sort(key=lambda q: q.priority)
        results: list[DispatchResult] = []

        # Pre-fetch current price for validation if broker is available
        _current_price: float | None = None
        if broker is not None:
            try:
                _mid, _bid, _ask = broker.fetch_prices(symbol)
                _current_price = _mid
            except Exception:
                _current_price = None

        for i, queued in enumerate(self._queue):
            decision = queued.decision
            risk = queued.risk_result

            if risk.verdict.value == "rejected":
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=False,
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
                                    reason=f"price_guard_failed: tp={decision.tp} < price={_current_price} < sl={decision.sl}",
                                )
                            )
                            self._last_dispatch_time = _time.monotonic()
                            continue
                except Exception:
                    pass  # if price guard validation itself fails, let the order through

            # If NET_OUT or REDUCED, handle opposing position first
            if risk.verdict.value in ("net_out", "reduced") and risk.net_out_ticket:
                # Close/reduce existing opposing position
                _close_confirmed = False
                try:
                    from pathlib import Path as _Path

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
                    }
                    # Carry brain_ids from the position being closed (or aggressor as fallback)
                    _aggressor_brain_ids = getattr(queued.decision, "brain_ids", None)
                    _net_out_brain_ids = getattr(risk, "net_out_brain_ids", None)
                    if _net_out_brain_ids:
                        _close_payload["brain_ids"] = _net_out_brain_ids
                    elif _aggressor_brain_ids:
                        _close_payload["brain_ids"] = _aggressor_brain_ids
                    _close_result = dispatch_live_order(
                        base_dir=base_dir,
                        broker=None,
                        symbol=symbol,
                        execution_payload=_close_payload,
                        skip_price_guard=True,
                        ignore_protection_flag=ignore_protection_flag,
                        protection_flag_path=protection_flag_path,
                        adapter_name="mt5",
                        extensions={"mt5_terminal_path": mt5_terminal_path},
                    )
                    # ── Close confirmation poll (timeout 30 s, max 120 iterations) ──
                    if isinstance(_close_result, dict):
                        _intent_id = _close_result.get("intent_id")
                        if _intent_id:
                            import json as _json

                            _today = _time.strftime("%Y-%m-%d")
                            _receipt_dir = _Path(base_dir) / "receipts" / _today / "exec_bridge"
                            _receipt_path = _receipt_dir / f"{_intent_id}.ack.json"
                            if _receipt_dir.exists():
                                _deadline = _time.monotonic() + 30.0
                                _poll_iters = 0
                                while _time.monotonic() < _deadline and _poll_iters < 120:
                                    if _receipt_path.exists():
                                        try:
                                            _ack = _json.loads(
                                                _receipt_path.read_text(encoding="utf-8")
                                            )
                                            if _ack.get("ack_status") == "accepted":
                                                _close_confirmed = True
                                                break
                                        except Exception:
                                            pass
                                    _time.sleep(0.5)
                                    _poll_iters += 1
                        else:
                            _close_confirmed = True  # no intent_id: backward-compat / test mock
                except Exception:
                    pass

                if not _close_confirmed:
                    results.append(
                        DispatchResult(
                            strategy_name=queued.strategy_name,
                            magic=decision.magic,
                            dispatched=False,
                            reason="net_out_close_not_confirmed",
                        )
                    )
                    self._last_dispatch_time = _time.monotonic()
                    continue

            # Dispatch the actual order (price guard already validated above)
            # Retry once on transient MT5 failures (1.5 s delay)
            _max_attempts = 2
            _dispatched = False
            _last_error = ""
            _journal_entry = None

            for _attempt in range(_max_attempts):
                try:
                    _journal_entry = dispatch_fn(
                        base_dir=base_dir,
                        mt5_terminal_path=mt5_terminal_path,
                        symbol=symbol,
                        side=decision.direction,
                        stop_loss=decision.sl,
                        take_profit=decision.tp,
                        skip_price_guard=True,
                        ignore_protection_flag=ignore_protection_flag,
                        protection_flag_path=protection_flag_path,
                        volume=risk.adjusted_volume
                        if risk.adjusted_volume > 0
                        else decision.volume,
                        magic=decision.magic,
                        hard_sl=decision.hard_sl,
                        brain_ids=decision.brain_ids,
                        entry_context=decision.entry_context if decision.entry_context else None,
                    )
                    _dispatched = True
                    break
                except Exception as exc:
                    _last_error = str(exc)
                    if _attempt < _max_attempts - 1:
                        _time.sleep(1.5)

            if _dispatched:
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=True,
                        reason="ok",
                        journal_entry=_journal_entry,
                    )
                )
            else:
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=False,
                        reason=_last_error,
                    )
                )

            self._last_dispatch_time = _time.monotonic()

        # Clear queue
        self._queue.clear()

        return results

    @property
    def queue_size(self) -> int:
        return len(self._queue)

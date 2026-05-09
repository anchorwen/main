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
        stagger_seconds: float = 20.0,
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
    ) -> list[DispatchResult]:
        """Process all queued decisions in priority order with stagger delay.

        Args:
            dispatch_fn: Callable that takes (decision, mt5_terminal_path, ...)
                         and dispatches to MT5.  Signature should match
                         ``dispatch_live_open_order``.

        Returns:
            List of DispatchResult, one per queued decision.
        """
        if not self._queue:
            return []

        # Sort by priority (lowest first)
        self._queue.sort(key=lambda q: q.priority)
        results: list[DispatchResult] = []

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

            # If NET_OUT or REDUCED, handle opposing position first
            if risk.verdict.value in ("net_out", "reduced") and risk.net_out_ticket:
                # Close/reduce existing opposing position
                try:
                    from scripts.send_live_order import dispatch_live_order

                    dispatch_live_order(
                        base_dir=base_dir,
                        broker=None,
                        symbol=symbol,
                        execution_payload={
                            "action": "modify_sltp" if risk.verdict.value == "reduced" else "close",
                            "position_ticket": risk.net_out_ticket,
                            "volume": risk.adjusted_volume if risk.adjusted_volume > 0 else 0.01,
                            "comment": f"net_out:{queued.strategy_name}",
                        },
                        skip_price_guard=True,
                        ignore_protection_flag=ignore_protection_flag,
                        protection_flag_path=protection_flag_path,
                        adapter_name="mt5",
                        extensions={"mt5_terminal_path": mt5_terminal_path},
                    )
                except Exception:
                    pass

            # Dispatch the actual order
            try:
                journal_entry = dispatch_fn(
                    base_dir=base_dir,
                    mt5_terminal_path=mt5_terminal_path,
                    symbol=symbol,
                    side=decision.direction,
                    stop_loss=decision.sl,
                    take_profit=decision.tp,
                    skip_price_guard=True,
                    ignore_protection_flag=ignore_protection_flag,
                    protection_flag_path=protection_flag_path,
                    volume=risk.adjusted_volume if risk.adjusted_volume > 0 else decision.volume,
                    magic=decision.magic,
                    brain_ids=decision.brain_ids,
                )

                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=True,
                        reason="ok",
                        journal_entry=journal_entry,
                    )
                )
            except Exception as exc:
                results.append(
                    DispatchResult(
                        strategy_name=queued.strategy_name,
                        magic=decision.magic,
                        dispatched=False,
                        reason=str(exc),
                    )
                )

            self._last_dispatch_time = _time.monotonic()

        # Clear queue
        self._queue.clear()

        return results

    @property
    def queue_size(self) -> int:
        return len(self._queue)

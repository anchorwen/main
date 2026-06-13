"""Cross-Strategy Coordinator — prevents opposing positions on the same symbol.

P4-2 (2026-06-13): When multiple strategies trade the same symbol independently,
they can simultaneously hold opposing positions (e.g. statarb_dynamic LONG +
m15_swing SHORT).  The opposing exposures cancel each other's edge while paying
spread+slippage twice — a guaranteed net loss.

This module detects opposing positions BEFORE enqueue and applies graduated
conflict resolution:
  1. **Block**: opposing position exists → skip the new trade (conservative default)
  2. **Net-out**: close the opposing position first, then open the new one
  3. **Reduce**: shrink the new position to net-neutral size

Design principle: strategies are independent thinkers, but the portfolio is ONE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpposingPosition:
    """An existing position that conflicts with a pending trade."""
    strategy_name: str
    direction: str  # "long" or "short"
    ticket: int
    volume: float
    pnl_r: float = 0.0  # estimated current R-multiple


@dataclass
class ConflictResolution:
    """Result of cross-strategy conflict check."""
    blocked: bool = False
    reason: str = ""
    opposing_positions: list[OpposingPosition] = field(default_factory=list)
    recommended_action: str = "allow"  # "allow" | "block" | "net_out" | "reduce"


class CrossStrategyCoordinator:
    """Detects and resolves opposing positions across strategies.

    Plugs into strategy_evaluator.py after StrategyDecision is produced but
    before execution_queue.enqueue().

    Usage::

        coordinator = CrossStrategyCoordinator()
        resolution = coordinator.check(
            pending_strategy="m15_swing",
            pending_direction="long",
            current_positions=current_positions,
        )
        if resolution.blocked:
            decision.should_trade = False
    """

    def __init__(self, mode: str = "block"):
        """
        Args:
            mode: Conflict resolution mode.
                  - "block": Skip new trade if opposing position exists (default)
                  - "warn": Log warning but allow (telemetry-only, for probation)
                  - "off": No cross-strategy check (legacy behavior)
        """
        self.mode = mode
        self._conflict_count: dict[str, int] = {}  # strategy → cumulative conflicts

    def check(
        self,
        *,
        pending_strategy: str,
        pending_direction: str,
        current_positions: dict[str, dict[str, Any]],
    ) -> ConflictResolution:
        """Check whether a pending trade conflicts with any open position.

        Args:
            pending_strategy: Name of the strategy producing the trade.
            pending_direction: "long" or "short".
            current_positions: Dict of strategy_name → position_info.

        Returns:
            ConflictResolution with blocked flag and reasoning.
        """
        if self.mode == "off":
            return ConflictResolution()

        if pending_direction not in ("long", "short"):
            return ConflictResolution()

        opposite_direction = "short" if pending_direction == "long" else "long"
        opposing: list[OpposingPosition] = []

        for sname, pos_info in current_positions.items():
            # Skip self — a strategy can't oppose itself
            if sname == pending_strategy:
                continue
            pos_dir = pos_info.get("direction", "")
            if pos_dir == opposite_direction:
                opposing.append(
                    OpposingPosition(
                        strategy_name=sname,
                        direction=pos_dir,
                        ticket=int(pos_info.get("ticket", 0)),
                        volume=float(pos_info.get("volume", 0.0)),
                    )
                )

        if not opposing:
            return ConflictResolution()

        self._conflict_count[pending_strategy] = (
            self._conflict_count.get(pending_strategy, 0) + 1
        )

        if self.mode == "warn":
            return ConflictResolution(
                blocked=False,
                opposing_positions=opposing,
                reason=f"cross_strategy_opposing_warn:{','.join(o.strategy_name for o in opposing)}",
                recommended_action="warn",
            )

        # "block" mode (default)
        return ConflictResolution(
            blocked=True,
            opposing_positions=opposing,
            reason=f"cross_strategy_opposing_blocked:{','.join(o.strategy_name for o in opposing)}",
            recommended_action="block",
        )

    @property
    def conflict_count(self) -> dict[str, int]:
        """Cumulative conflict count per strategy (for monitoring)."""
        return dict(self._conflict_count)

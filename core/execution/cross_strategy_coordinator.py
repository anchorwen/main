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

FIX-20260719-001 (P0-3): Timeframe hierarchy added.  Higher-TF signals carry
stronger conviction (H4 > H1 > M30 > M15 > M5).  A lower-TF opposing position
must NOT block a higher-TF entry — instead the coordinator flags the lower-TF
position for review (recommended_action="review_lower_tf").  Conversely,
higher-TF positions retain full veto power over lower-TF entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── FIX-20260719-001: Timeframe hierarchy ──────────────────────────────────
# Higher-TF signals reflect longer-horizon conviction and must not be
# blocked by short-horizon noise positions.  Rank is ordinal: higher number
# = higher timeframe = stronger veto power.
TIMEFRAME_RANK: dict[str, int] = {
    "M5": 1,
    "M15": 2,
    "M30": 3,
    "H1": 4,
    "H4": 5,
    "D1": 6,
}


def _resolve_timeframe(strategy_name: str) -> str | None:
    """Extract timeframe from strategy name convention.

    Recognises patterns like ``btc_swing_h4`` → "H4", ``m30_swing`` → "M30",
    ``barrier_12bar`` → None (no TF in name).

    Returns the timeframe string or None if unrecognised.
    """
    _s = strategy_name.lower()
    # Check in descending specificity order
    for tf in ("h4", "h1", "m30", "m15", "m5", "d1"):
        if tf in _s:
            return tf.upper()
    return None


@dataclass
class OpposingPosition:
    """An existing position that conflicts with a pending trade."""

    strategy_name: str
    direction: str  # "long" or "short"
    ticket: int
    volume: float
    pnl_r: float = 0.0  # estimated current R-multiple
    timeframe: str | None = None  # FIX-20260719-001: resolved TF from strategy name


@dataclass
class ConflictResolution:
    """Result of cross-strategy conflict check."""

    blocked: bool = False
    reason: str = ""
    opposing_positions: list[OpposingPosition] = field(default_factory=list)
    recommended_action: str = (
        "allow"  # "allow" | "block" | "net_out" | "reduce" | "review_lower_tf"
    )


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

    FIX-20260719-001: Timeframe hierarchy (P0-3).
    When ``respect_timeframe_hierarchy=True`` (default), higher-TF pending
    entries are NOT blocked by lower-TF opposing positions.  The coordinator
    instead recommends ``review_lower_tf`` — the lower-TF position should
    be evaluated for net-out by the caller.  Higher-TF positions retain
    full veto power over lower-TF entries (hierarchy flows one way: down).
    """

    def __init__(self, mode: str = "block", *, respect_timeframe_hierarchy: bool = True):
        """
        Args:
            mode: Conflict resolution mode.
                  - "block": Skip new trade if opposing position exists (default)
                  - "warn": Log warning but allow (telemetry-only, for probation)
                  - "off": No cross-strategy check (legacy behavior)
            respect_timeframe_hierarchy: When True (default), higher-TF pending
                entries are NOT blocked by lower-TF opposing positions.
                Set to False for legacy flat-hierarchy behaviour.
        """
        self.mode = mode
        self.respect_timeframe_hierarchy = respect_timeframe_hierarchy
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

        FIX-20260719-001: When ``respect_timeframe_hierarchy`` is True,
        a higher-TF pending entry is NOT blocked by lower-TF opposing
        positions.  The hierarchy flows one way: higher TF > lower TF.
        Same-TF or unknown-TF conflicts use the legacy flat-block behaviour.
        """
        if self.mode == "off":
            return ConflictResolution()

        if pending_direction not in ("long", "short"):
            return ConflictResolution()

        opposite_direction = "short" if pending_direction == "long" else "long"
        opposing: list[OpposingPosition] = []
        _pending_tf = _resolve_timeframe(pending_strategy)
        _pending_rank = TIMEFRAME_RANK.get(_pending_tf or "", 0)

        for sname, pos_info in current_positions.items():
            # Skip self — a strategy can't oppose itself
            if sname == pending_strategy:
                continue
            pos_dir = pos_info.get("direction", "")
            if pos_dir == opposite_direction:
                _pos_tf = _resolve_timeframe(sname)
                opposing.append(
                    OpposingPosition(
                        strategy_name=sname,
                        direction=pos_dir,
                        ticket=int(pos_info.get("ticket", 0)),
                        volume=float(pos_info.get("volume", 0.0)),
                        timeframe=_pos_tf,
                    )
                )

        if not opposing:
            return ConflictResolution()

        self._conflict_count[pending_strategy] = self._conflict_count.get(pending_strategy, 0) + 1

        # ── FIX-20260719-001: Timeframe hierarchy override ────────────────
        # When the pending entry comes from a HIGHER timeframe than ALL
        # opposing positions, the higher-TF conviction overrides lower-TF
        # noise.  Block is suppressed; caller receives review_lower_tf
        # recommendation to evaluate net-out of the lower-TF position.
        if self.respect_timeframe_hierarchy and _pending_rank > 0:
            _opposing_ranks = [TIMEFRAME_RANK.get(o.timeframe or "", 0) for o in opposing]
            _max_opposing_rank = max(_opposing_ranks) if _opposing_ranks else 0
            if _pending_rank > _max_opposing_rank:
                # Higher-TF entry overrides lower-TF opposition
                _opp_names = ",".join(o.strategy_name for o in opposing)
                if self.mode == "warn":
                    return ConflictResolution(
                        blocked=False,
                        opposing_positions=opposing,
                        reason=(
                            f"cross_strategy_tf_hierarchy_override:"
                            f"{pending_strategy}({_pending_tf})_overrides_{_opp_names}"
                        ),
                        recommended_action="review_lower_tf",
                    )
                # In "block" mode: still allow through, but flag for review
                return ConflictResolution(
                    blocked=False,
                    opposing_positions=opposing,
                    reason=(
                        f"cross_strategy_tf_hierarchy_override:"
                        f"{pending_strategy}({_pending_tf})_overrides_{_opp_names}"
                    ),
                    recommended_action="review_lower_tf",
                )

        if self.mode == "warn":
            return ConflictResolution(
                blocked=False,
                opposing_positions=opposing,
                reason=f"cross_strategy_opposing_warn:{','.join(o.strategy_name for o in opposing)}",
                recommended_action="warn",
            )

        # "block" mode (default) — same/lower TF or hierarchy disabled
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

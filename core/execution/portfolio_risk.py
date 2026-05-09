"""Portfolio-level risk controller.

Operates ABOVE the three strategy lines.  When strategies produce independent
trade decisions, this controller checks aggregate risk limits:

  1. Gross exposure (total absolute notional) must not exceed limit.
  2. Net exposure (long - short) must not exceed limit.
  3. No more than N strategies may hold the same direction simultaneously.
  4. Opposite-direction positions can be netted out (default) or coexist.

This is the "portfolio manager" layer — it doesn't tell strategies what to
think, it only controls how much risk the combined book can take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskVerdict(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REDUCED = "reduced"
    NET_OUT = "net_out"  # internal hedge: reduce existing opposing position


@dataclass
class RiskResult:
    verdict: RiskVerdict
    reason: str = ""
    adjusted_volume: float = 0.0
    net_out_ticket: int | None = None  # ticket to reduce


@dataclass
class PortfolioState:
    """Snapshot of current portfolio for risk checking."""

    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # position dicts: {"strategy": str, "direction": str, "volume": float, "ticket": int}


class PortfolioRiskController:
    """Cross-strategy risk limits."""

    def __init__(
        self,
        *,
        max_gross_exposure: float = 0.10,
        max_net_exposure: float = 0.05,
        max_same_direction: int = 2,
        netting_mode: str = "net_out",
    ):
        self.max_gross = max_gross_exposure
        self.max_net = max_net_exposure
        self.max_same_dir = max_same_direction
        self.netting_mode = netting_mode  # "net_out" | "allow_coexist"

    def check(
        self,
        new_decision: Any,  # StrategyDecision
        current_positions: dict[str, dict[str, Any]],
    ) -> RiskResult:
        """Check a new trade decision against portfolio limits.

        Args:
            new_decision: StrategyDecision from a strategy line.
            current_positions: dict keyed by strategy_name → position dict.
                Each position: {"strategy": str, "direction": str, "volume": float, "ticket": int}

        Returns:
            RiskResult with verdict and possibly adjusted volume.
        """
        direction = new_decision.direction
        volume = new_decision.volume
        strategy = new_decision.strategy_name

        # ── 1. Gross exposure check ──
        current_gross = sum(p["volume"] for p in current_positions.values())
        new_gross = current_gross + volume
        if new_gross > self.max_gross:
            return RiskResult(
                RiskVerdict.REJECTED,
                reason=f"gross_exposure_{new_gross:.3f}_gt_{self.max_gross}",
            )

        # ── 2. Net exposure check ──
        net_long = sum(p["volume"] for p in current_positions.values() if p["direction"] == "long")
        net_short = sum(
            p["volume"] for p in current_positions.values() if p["direction"] == "short"
        )
        current_net = net_long - net_short
        if direction == "long":
            new_net = current_net + volume
        else:
            new_net = current_net - volume
        if abs(new_net) > self.max_net:
            return RiskResult(
                RiskVerdict.REJECTED,
                reason=f"net_exposure_{abs(new_net):.3f}_gt_{self.max_net}",
            )

        # ── 3. Same-direction concentration check ──
        same_dir_count = sum(
            1
            for p in current_positions.values()
            if p["direction"] == direction and p["strategy"] != strategy
        )
        if same_dir_count >= self.max_same_dir:
            return RiskResult(
                RiskVerdict.REJECTED,
                reason=f"direction_concentration_{same_dir_count}_max_{self.max_same_dir}",
            )

        # ── 4. Opposite-direction netting ──
        opposite_dir = "short" if direction == "long" else "long"
        opposite_positions = [
            (name, pos)
            for name, pos in current_positions.items()
            if pos["direction"] == opposite_dir
        ]
        if opposite_positions and self.netting_mode == "net_out":
            # Reduce the largest opposing position instead of adding new
            largest_name, largest_pos = max(opposite_positions, key=lambda x: x[1]["volume"])
            if largest_pos["volume"] <= volume:
                # New trade fully offsets existing → close existing, open reduced new
                return RiskResult(
                    RiskVerdict.NET_OUT,
                    reason=f"net_out_against_{largest_name}",
                    adjusted_volume=round(volume - largest_pos["volume"], 2),
                    net_out_ticket=largest_pos["ticket"],
                )
            else:
                # Existing position larger → just reduce it, don't open new
                return RiskResult(
                    RiskVerdict.REDUCED,
                    reason=f"reduce_existing_{largest_name}",
                    adjusted_volume=largest_pos["volume"] - volume,
                    net_out_ticket=largest_pos["ticket"],
                )

        return RiskResult(RiskVerdict.APPROVED, adjusted_volume=volume)

    def get_portfolio_summary(self, current_positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Return a summary dict of current portfolio state."""
        net_long = sum(p["volume"] for p in current_positions.values() if p["direction"] == "long")
        net_short = sum(
            p["volume"] for p in current_positions.values() if p["direction"] == "short"
        )
        gross = net_long + net_short
        return {
            "gross_exposure": round(gross, 4),
            "net_exposure": round(net_long - net_short, 4),
            "long_exposure": round(net_long, 4),
            "short_exposure": round(net_short, 4),
            "position_count": len(current_positions),
            "strategies_active": list(current_positions.keys()),
        }

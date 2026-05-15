"""Portfolio-level risk controller.

Operates ABOVE the three strategy lines.  When strategies produce independent
trade decisions, this controller checks aggregate risk limits:

  1. Gross exposure (total absolute notional) must not exceed limit.
  2. Net exposure (long - short) must not exceed limit.
  3. No more than N strategies may hold the same direction simultaneously.
  4. Opposite-direction positions can coexist (default) or be netted out.
  5. VaR / CVaR from rolling historical strategy returns.
  6. Correlation penalty — when strategies are highly correlated, reduce
     the combined position size to avoid concentration risk.

This is the "portfolio manager" layer — it doesn't tell strategies what to
think, it only controls how much risk the combined book can take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


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
    net_out_ticket: int | None = None  # ticket to reduce/close
    net_out_close_volume: float = 0.0  # volume to close from opposing position
    net_out_brain_ids: list[str] = field(default_factory=list)
    # ── P2.1: VaR / correlation diagnostics (non-blocking) ──
    var_warning: bool = False
    var_value: float = 0.0
    cvar_value: float = 0.0
    correlation_warning: bool = False
    correlation_penalty_applied: float = 1.0


@dataclass
class PortfolioState:
    """Snapshot of current portfolio for risk checking."""

    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # position dicts: {"strategy": str, "direction": str, "volume": float, "ticket": int}


class PortfolioRiskController:
    """Cross-strategy risk limits with VaR / CVaR and correlation penalty."""

    def __init__(
        self,
        *,
        max_gross_exposure: float = 0.10,
        max_net_exposure: float = 0.05,
        max_same_direction: int = 2,
        netting_mode: str = "allow_coexist",
        min_hold_cycles: int = 6,  # minimum cycles before net_out can close a position
        # ── Notional conversion ──
        symbol_contract_size: float = 100.0,  # 100 oz per standard lot (XAUUSD)
        # ── VaR / CVaR ──
        var_confidence: float = 0.95,
        var_lookback: int = 100,
        var_max_pct: float = 0.03,  # 3% of equity — warn above this
        # ── Correlation penalty ──
        max_correlation: float = 0.70,
        correlation_penalty: float = 0.50,  # multiply volume by this when corr > max
        correlation_min_samples: int = 20,
    ):
        self.max_gross = max_gross_exposure
        self.max_net = max_net_exposure
        self.max_same_dir = max_same_direction
        self.netting_mode = netting_mode  # "net_out" | "allow_coexist"
        self.min_hold_cycles = min_hold_cycles
        self.contract_size = symbol_contract_size

        # VaR / CVaR
        self.var_confidence = var_confidence
        self.var_lookback = var_lookback
        self.var_max_pct = var_max_pct

        # Correlation
        self.max_correlation = max_correlation
        self.correlation_penalty = correlation_penalty
        self.correlation_min_samples = correlation_min_samples

        # Rolling returns buffer: strategy_name → list of P&L values (in account currency)
        self._returns_buffer: dict[str, list[float]] = {}

    # ── Data feed ────────────────────────────────────────────────────────

    def update_returns(self, strategy_name: str, pnl: float) -> None:
        """Record a realised trade P&L for VaR / correlation tracking."""
        buf = self._returns_buffer.setdefault(strategy_name, [])
        buf.append(pnl)
        if len(buf) > self.var_lookback:
            buf.pop(0)

    def load_returns_history(self, history: dict[str, list[float]]) -> None:
        """Bulk-load pre-existing returns (e.g. from journal replay)."""
        for sname, pnls in history.items():
            buf = self._returns_buffer.setdefault(sname, [])
            for v in pnls[-self.var_lookback :]:
                buf.append(v)

    # ── VaR / CVaR ───────────────────────────────────────────────────────

    def compute_var(self, strategy_name: str) -> float:
        """Historical VaR at configured confidence level.

        Returns a positive number representing "worst-case loss at
        (1-confidence) tail" in account-currency units.
        """
        returns = self._returns_buffer.get(strategy_name, [])
        if len(returns) < self.correlation_min_samples:
            return 0.0
        sorted_returns = sorted(returns)
        idx = int((1.0 - self.var_confidence) * len(sorted_returns))
        idx = max(0, min(idx, len(sorted_returns) - 1))
        var = -sorted_returns[idx]
        return var if var > 0 else 0.0

    def compute_cvar(self, strategy_name: str) -> float:
        """CVaR (Expected Shortfall) — mean loss beyond VaR."""
        returns = self._returns_buffer.get(strategy_name, [])
        if len(returns) < self.correlation_min_samples:
            return 0.0
        var = self.compute_var(strategy_name)
        if var <= 0:
            return 0.0
        tail = [-r for r in returns if -r > var]
        if not tail:
            return var
        return sum(tail) / len(tail)

    def compute_correlation(self, s1: str, s2: str) -> float:
        """Pearson correlation between two strategy return streams."""
        r1 = self._returns_buffer.get(s1, [])
        r2 = self._returns_buffer.get(s2, [])
        n = min(len(r1), len(r2))
        if n < self.correlation_min_samples:
            return 0.0
        r1 = r1[-n:]
        r2 = r2[-n:]
        try:
            corr = float(np.corrcoef(r1, r2)[0, 1])
            return corr if not np.isnan(corr) else 0.0
        except Exception:
            return 0.0

    def compute_correlation_matrix(self) -> dict[str, dict[str, float]]:
        """Pairwise correlation matrix across all tracked strategies."""
        names = sorted(self._returns_buffer.keys())
        matrix: dict[str, dict[str, float]] = {}
        for s1 in names:
            matrix[s1] = {}
            for s2 in names:
                matrix[s1][s2] = self.compute_correlation(s1, s2) if s1 != s2 else 1.0
        return matrix

    def compute_optimal_allocation(self, *, method: str = "risk_parity") -> dict[str, float]:
        """Bridge to portfolio_optimizer — compute optimal capital weights from live P&L.

        Uses the rolling returns buffer (fed by update_returns() after each trade
        settlement) to compute risk-parity / min-variance / max-Sharpe weights.
        """
        from core.execution.capital_allocator import compute_optimal_group_weights

        return compute_optimal_group_weights(dict(self._returns_buffer), method=method)

    # ── Main check ───────────────────────────────────────────────────────

    def _to_notional(self, volume_lots: float, current_price: float) -> float:
        """Convert lot volume to notional (USD) exposure."""
        return volume_lots * self.contract_size * current_price

    def check(
        self,
        new_decision: Any,  # StrategyDecision
        current_positions: dict[str, dict[str, Any]],
        current_price: float | None = None,
        account_equity: float | None = None,
        current_cycle: int = 0,
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

        # ── 0. Per-strategy duplicate check ──
        if strategy in current_positions:
            return RiskResult(
                RiskVerdict.REJECTED,
                reason=f"duplicate_strategy_{strategy}_already_holding",
            )

        # ── 1. Gross exposure check ──
        if current_price is not None and current_price > 0:
            current_gross_n = sum(
                self._to_notional(p["volume"], current_price) for p in current_positions.values()
            )
            new_gross_n = current_gross_n + self._to_notional(volume, current_price)
            max_gross_n = self._to_notional(self.max_gross, current_price)
            if new_gross_n > max_gross_n:
                return RiskResult(
                    RiskVerdict.REJECTED,
                    reason=f"gross_exposure_{new_gross_n:.0f}_gt_{max_gross_n:.0f}",
                )
        else:
            current_gross = sum(p["volume"] for p in current_positions.values())
            new_gross = current_gross + volume
            if new_gross > self.max_gross:
                return RiskResult(
                    RiskVerdict.REJECTED,
                    reason=f"gross_exposure_{new_gross:.3f}_gt_{self.max_gross}",
                )

        # ── 2. Net exposure check ──
        if current_price is not None and current_price > 0:
            net_long_n = sum(
                self._to_notional(p["volume"], current_price)
                for p in current_positions.values()
                if p["direction"] == "long"
            )
            net_short_n = sum(
                self._to_notional(p["volume"], current_price)
                for p in current_positions.values()
                if p["direction"] == "short"
            )
            current_net_n = net_long_n - net_short_n
            vol_n = self._to_notional(volume, current_price)
            new_net_n = current_net_n + vol_n if direction == "long" else current_net_n - vol_n
            max_net_n = self._to_notional(self.max_net, current_price)
            if abs(new_net_n) > max_net_n:
                return RiskResult(
                    RiskVerdict.REJECTED,
                    reason=f"net_exposure_{abs(new_net_n):.0f}_gt_{max_net_n:.0f}",
                )
        else:
            net_long = sum(
                p["volume"] for p in current_positions.values() if p["direction"] == "long"
            )
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
        # Filter out positions that haven't met min_hold_cycles
        if current_cycle > 0 and self.min_hold_cycles > 0:
            opposite_positions = [
                (name, pos)
                for name, pos in opposite_positions
                if (pos.get("entry_cycle", 0) == 0)
                or (current_cycle - pos["entry_cycle"] >= self.min_hold_cycles)
            ]
        if opposite_positions and self.netting_mode == "net_out":
            # Reduce the largest opposing position instead of adding new
            largest_name, largest_pos = max(opposite_positions, key=lambda x: x[1]["volume"])
            _opp_brain_ids = largest_pos.get("brain_ids", [])
            if largest_pos["volume"] <= volume:
                # New trade fully offsets existing → close existing, open reduced new
                return RiskResult(
                    RiskVerdict.NET_OUT,
                    reason=f"net_out_against_{largest_name}",
                    adjusted_volume=round(volume - largest_pos["volume"], 2),
                    net_out_ticket=largest_pos["ticket"],
                    net_out_close_volume=largest_pos["volume"],  # full close of opposing
                    net_out_brain_ids=_opp_brain_ids,
                )
            else:
                # Existing position larger → just reduce it, don't open new
                return RiskResult(
                    RiskVerdict.REDUCED,
                    reason=f"reduce_existing_{largest_name}",
                    adjusted_volume=largest_pos["volume"] - volume,
                    net_out_ticket=largest_pos["ticket"],
                    net_out_close_volume=volume,  # close new trade's volume from opposing
                    net_out_brain_ids=_opp_brain_ids,
                )

        # ── 5. VaR / CVaR diagnostic (non-blocking warning) ──
        var_warning = False
        var_value = 0.0
        cvar_value = 0.0
        try:
            var_value = round(self.compute_var(strategy), 6)
            cvar_value = round(self.compute_cvar(strategy), 6)
            _equity = (
                account_equity if (account_equity is not None and account_equity > 0) else 10_000
            )
            if cvar_value > self.var_max_pct * _equity:
                var_warning = True
        except Exception:
            pass

        # ── 6. Correlation penalty (continuous gradient) ──
        # Penalty scales linearly from 1.0 (at max_correlation) to
        # correlation_penalty (at corr=1.0).  The worst (lowest) penalty
        # across all correlated same-direction pairs is applied.
        correlation_warning = False
        correlation_penalty_applied = 1.0
        adjusted_volume = volume

        _worst_penalty = 1.0
        for sname, pos in current_positions.items():
            if sname == strategy:
                continue
            if pos["direction"] != direction:
                continue
            corr = self.compute_correlation(strategy, sname)
            if corr > self.max_correlation:
                correlation_warning = True
                # Linear gradient: 1.0 at max_correlation → correlation_penalty at corr=1.0
                excess = corr - self.max_correlation
                max_excess = 1.0 - self.max_correlation
                gradient = 1.0 - (excess / max_excess) * (1.0 - self.correlation_penalty)
                gradient = max(self.correlation_penalty, min(1.0, gradient))
                if gradient < _worst_penalty:
                    _worst_penalty = gradient

        if correlation_warning:
            correlation_penalty_applied = _worst_penalty
            adjusted_volume = round(volume * _worst_penalty, 4)

        return RiskResult(
            RiskVerdict.APPROVED,
            reason="ok",
            adjusted_volume=adjusted_volume,
            var_warning=var_warning,
            var_value=var_value,
            cvar_value=cvar_value,
            correlation_warning=correlation_warning,
            correlation_penalty_applied=correlation_penalty_applied,
        )

    def get_portfolio_summary(self, current_positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Return a summary dict of current portfolio state including VaR/correlation."""
        net_long = sum(p["volume"] for p in current_positions.values() if p["direction"] == "long")
        net_short = sum(
            p["volume"] for p in current_positions.values() if p["direction"] == "short"
        )
        gross = net_long + net_short

        # Per-strategy VaR
        var_by_strategy: dict[str, dict[str, float]] = {}
        for sname in self._returns_buffer:
            var_by_strategy[sname] = {
                "var": round(self.compute_var(sname), 6),
                "cvar": round(self.compute_cvar(sname), 6),
                "samples": len(self._returns_buffer[sname]),
            }

        return {
            "gross_exposure": round(gross, 4),
            "net_exposure": round(net_long - net_short, 4),
            "long_exposure": round(net_long, 4),
            "short_exposure": round(net_short, 4),
            "position_count": len(current_positions),
            "strategies_active": list(current_positions.keys()),
            "var_by_strategy": var_by_strategy,
            "correlation_matrix": self.compute_correlation_matrix(),
        }

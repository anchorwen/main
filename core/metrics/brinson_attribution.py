"""Brinson performance attribution — sector/strategy decomposition.

Decomposes active return (portfolio − benchmark) into:
- **Allocation effect**: overweighting sectors that outperform the benchmark
- **Selection effect**: picking better instruments within sectors
- **Interaction effect**: cross-term between allocation and selection

Usage:
    from core.metrics.brinson_attribution import brinson_decompose

    result = brinson_decompose(
        sectors=["trend", "mean_rev", "carry"],
        port_weights=[0.5, 0.3, 0.2],
        bench_weights=[0.4, 0.4, 0.2],
        port_returns=[0.02, 0.01, -0.005],
        bench_returns=[0.015, 0.015, 0.0],
    )
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BrinsonResult:
    """Decomposed active return for one period."""

    sectors: list[str]
    port_weights: np.ndarray
    bench_weights: np.ndarray
    port_returns: np.ndarray
    bench_returns: np.ndarray
    allocation_effects: np.ndarray
    selection_effects: np.ndarray
    interaction_effects: np.ndarray
    total_allocation: float
    total_selection: float
    total_interaction: float
    active_return: float

    def to_dict(self) -> dict:
        return {
            "sectors": self.sectors,
            "port_weights": [round(float(w), 6) for w in self.port_weights],
            "bench_weights": [round(float(w), 6) for w in self.bench_weights],
            "port_returns": [round(float(r), 6) for r in self.port_returns],
            "bench_returns": [round(float(r), 6) for r in self.bench_returns],
            "allocation_effects": [round(float(e), 6) for e in self.allocation_effects],
            "selection_effects": [round(float(e), 6) for e in self.selection_effects],
            "interaction_effects": [round(float(e), 6) for e in self.interaction_effects],
            "total_allocation": round(float(self.total_allocation), 6),
            "total_selection": round(float(self.total_selection), 6),
            "total_interaction": round(float(self.total_interaction), 6),
            "active_return": round(float(self.active_return), 6),
        }


@dataclass(frozen=True)
class BrinsonMultiPeriod:
    """Brinson attribution aggregated over multiple periods.

    Supports arithmetic (sum) and geometric (compounded) linking.
    """

    periods: list[BrinsonResult]
    linking_method: str = "arithmetic"

    @property
    def cumulative_active_return(self) -> float:
        if self.linking_method == "geometric":
            ret = 1.0
            for p in self.periods:
                ret *= 1.0 + p.active_return
            return ret - 1.0
        return sum(p.active_return for p in self.periods)

    @property
    def avg_allocation_effect(self) -> float:
        vals = [p.total_allocation for p in self.periods]
        return float(np.mean(vals)) if vals else 0.0

    @property
    def avg_selection_effect(self) -> float:
        vals = [p.total_selection for p in self.periods]
        return float(np.mean(vals)) if vals else 0.0

    @property
    def avg_interaction_effect(self) -> float:
        vals = [p.total_interaction for p in self.periods]
        return float(np.mean(vals)) if vals else 0.0

    def to_dict(self) -> dict:
        return {
            "period_count": len(self.periods),
            "linking_method": self.linking_method,
            "cumulative_active_return": round(self.cumulative_active_return, 6),
            "average_allocation_effect": round(self.avg_allocation_effect, 6),
            "average_selection_effect": round(self.avg_selection_effect, 6),
            "average_interaction_effect": round(self.avg_interaction_effect, 6),
            "periods": [p.to_dict() for p in self.periods],
        }


def brinson_decompose(
    sectors: list[str],
    port_weights: list[float] | np.ndarray,
    bench_weights: list[float] | np.ndarray,
    port_returns: list[float] | np.ndarray,
    bench_returns: list[float] | np.ndarray,
) -> BrinsonResult:
    """Single-period Brinson decomposition.

    Args:
        sectors: Sector/strategy names.
        port_weights: Portfolio weight per sector (must sum ≈ 1).
        bench_weights: Benchmark weight per sector (must sum ≈ 1).
        port_returns: Portfolio return per sector (e.g. 0.02 = 2%).
        bench_returns: Benchmark return per sector.

    Returns:
        BrinsonResult with per-sector and total effects.
    """
    n = len(sectors)

    pw = np.asarray(port_weights, dtype=np.float64).ravel()
    bw = np.asarray(bench_weights, dtype=np.float64).ravel()
    pr = np.asarray(port_returns, dtype=np.float64).ravel()
    br = np.asarray(bench_returns, dtype=np.float64).ravel()

    if len(pw) != n or len(bw) != n or len(pr) != n or len(br) != n:
        raise ValueError(
            f"All arrays must have length {n} (sectors count), got "
            f"pw={len(pw)}, bw={len(bw)}, pr={len(pr)}, br={len(br)}"
        )

    # Brinson decomposition formulas:
    # Allocation:  (w_p - w_b) * R_b
    # Selection:    w_b * (R_p - R_b)
    # Interaction: (w_p - w_b) * (R_p - R_b)
    allocation = (pw - bw) * br
    selection = bw * (pr - br)
    interaction = (pw - bw) * (pr - br)

    portfolio_return = float(pw @ pr)
    benchmark_return = float(bw @ br)
    active_return = portfolio_return - benchmark_return

    return BrinsonResult(
        sectors=sectors,
        port_weights=pw,
        bench_weights=bw,
        port_returns=pr,
        bench_returns=br,
        allocation_effects=allocation,
        selection_effects=selection,
        interaction_effects=interaction,
        total_allocation=float(allocation.sum()),
        total_selection=float(selection.sum()),
        total_interaction=float(interaction.sum()),
        active_return=active_return,
    )


def brinson_multi_period(
    sectors: list[str],
    period_port_weights: np.ndarray,  # (T, N)
    period_bench_weights: np.ndarray,  # (T, N)
    period_port_returns: np.ndarray,  # (T, N)
    period_bench_returns: np.ndarray,  # (T, N)
    *,
    linking_method: str = "arithmetic",
) -> BrinsonMultiPeriod:
    """Multi-period Brinson attribution.

    Args:
        sectors: Sector/strategy names.
        period_port_weights: Shape (T, N) — portfolio weights over time.
        period_bench_weights: Shape (T, N) — benchmark weights over time.
        period_port_returns: Shape (T, N) — portfolio sector returns.
        period_bench_returns: Shape (T, N) — benchmark sector returns.
        linking_method: ``"arithmetic"`` (sum) or ``"geometric"`` (compound).

    Returns:
        BrinsonMultiPeriod with per-period results and aggregate stats.
    """
    T, N = period_port_weights.shape
    periods: list[BrinsonResult] = []

    for t in range(T):
        r = brinson_decompose(
            sectors=sectors,
            port_weights=period_port_weights[t],
            bench_weights=period_bench_weights[t],
            port_returns=period_port_returns[t],
            bench_returns=period_bench_returns[t],
        )
        periods.append(r)

    return BrinsonMultiPeriod(periods=periods, linking_method=linking_method)

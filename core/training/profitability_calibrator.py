"""Profitability surface calibrator for barrier label contracts.

Given historical OHLC data, computes the expected-value surface across a grid
of (SL, TP) barrier pairs and selects configurations with positive expectancy.

This replaces the previous hardcoded SL/TP multipliers (2.0/3.5) that produced
mathematically unprofitable labels (10% TP hit rate → EV = -0.21R/trade).

Algorithm:
  1. Walk forward through every bar, simulating a "blind entry"
  2. For each (sl_atr_mult, tp_atr_mult) in the search grid, determine
     which barrier hits first (or timeout)
  3. Aggregate hit rates across all entry bars
  4. Compute expected PnL: EV = P(TP) * tp_mult - P(SL) * sl_mult
  5. Select configurations with EV > min_profitability
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class BarrierConfig:
    """A single (SL, TP) barrier configuration."""

    sl_atr_mult: float
    tp_atr_mult: float
    horizon_bars: int
    atr_period: int = 14


@dataclass
class ProfitabilityPoint:
    """Expected-value metrics for one (SL, TP) configuration."""

    sl_atr_mult: float
    tp_atr_mult: float
    horizon_bars: int
    tp_hit_rate: float  # P(TP hits first)
    sl_hit_rate: float  # P(SL hits first)
    timeout_rate: float  # P(neither within horizon)
    expected_pnl_r: float  # EV in R-units: tp_rate*tp - sl_rate*sl
    sharpe_estimate: float  # Approx Sharpe based on binary outcome distribution
    breakeven_tp: float  # TP multiplier needed for breakeven at current hit rates

    @property
    def is_profitable(self) -> bool:
        return self.expected_pnl_r > 0.0

    @property
    def reward_risk_ratio(self) -> float:
        if self.sl_atr_mult <= 0:
            return float("inf")
        return self.tp_atr_mult / self.sl_atr_mult


@dataclass
class ProfitabilitySurface:
    """Complete profitability surface over (SL, TP) grid."""

    symbol: str
    timeframe: str
    total_bars: int
    entries_simulated: int
    horizon_bars: int
    atr_period: int
    mean_atr: float
    sl_range: list[float] = field(default_factory=list)
    tp_range: list[float] = field(default_factory=list)
    points: list[ProfitabilityPoint] = field(default_factory=list)

    def best_config(self) -> ProfitabilityPoint | None:
        """Return the configuration with highest expected PnL."""
        if not self.points:
            return None
        return max(self.points, key=lambda p: p.expected_pnl_r)

    def profitable_configs(self) -> list[ProfitabilityPoint]:
        """Return all configurations with positive expected PnL, sorted best first."""
        return sorted(
            [p for p in self.points if p.is_profitable],
            key=lambda p: p.expected_pnl_r,
            reverse=True,
        )

    def config_with_rr(self, min_rr: float = 2.0) -> ProfitabilityPoint | None:
        """Return the best config with at least min_rr reward:risk ratio."""
        candidates = [p for p in self.points if p.is_profitable and p.reward_risk_ratio >= min_rr]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.expected_pnl_r)


# ── Core calibration algorithm ──────────────────────────────────────────────


def compute_profitability_surface(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    *,
    horizon_bars: int = 12,
    atr_period: int = 14,
    sl_range: list[float] | None = None,
    tp_range: list[float] | None = None,
    entry_stride: int = 1,
    warmup_bars: int = 100,
    side: str = "long",
    symbol: str = "unknown",
    timeframe: str = "M5",
    min_profitability: float = 0.0,
    spread_pips: float = 0.3,
    slippage_pips: float = 0.5,
    pip_value: float = 0.01,
) -> ProfitabilitySurface:
    """Compute the expected-value surface for a grid of (SL, TP) configurations.

    Walks forward through every bar (after warmup), simulates a hypothetical
    entry, and records which barrier would have been hit for each configuration.

    Args:
        highs, lows, closes: OHLC arrays.
        horizon_bars: Max bars to look forward for barrier hit.
        atr_period: ATR lookback period.
        sl_range: SL multipliers to test. Default: [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        tp_range: TP multipliers to test. Default: [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
        entry_stride: Test every Nth bar as entry (1 = every bar).
        warmup_bars: Skip first N bars (need ATR history).
        side: Entry direction ("long", "short", "both").
        symbol, timeframe: Metadata labels.
        min_profitability: Filter threshold for reporting.

    Returns:
        ProfitabilitySurface with all computed grid points.
    """
    if sl_range is None:
        sl_range = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    if tp_range is None:
        tp_range = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]

    n_bars = len(closes)
    max_horizon_idx = n_bars - horizon_bars - 1
    entries: list[int] = list(range(warmup_bars, max_horizon_idx, entry_stride))

    if len(entries) < 50:
        raise ValueError(f"Not enough bars: need >{warmup_bars + horizon_bars}, got {n_bars}")

    sides = ["long", "short"] if side == "both" else [side]

    # Accumulators: config_idx → (tp_count, sl_count, timeout_count)
    grid: dict[tuple[float, float], tuple[int, int, int]] = {}
    for sl in sl_range:
        for tp in tp_range:
            grid[(sl, tp)] = (0, 0, 0)

    # Mean ATR tracker
    atr_sum = 0.0
    atr_count = 0

    spread_cost = spread_pips * pip_value
    slippage_cost = slippage_pips * pip_value

    for entry_idx in entries:
        for entry_side in sides:
            # Compute ATR at entry once (same for all configs at this entry)
            from core.contracts.training.label_contract import _compute_atr

            atr_val = _compute_atr(
                highs[: entry_idx + 1],
                lows[: entry_idx + 1],
                closes[: entry_idx + 1],
                period=atr_period,
            )
            if atr_val <= 0:
                continue
            atr_sum += atr_val
            atr_count += 1

            entry_price = float(closes[entry_idx])
            end_idx = min(entry_idx + horizon_bars + 1, n_bars)

            # Walk forward once per entry, checking all barriers
            hit_info: dict[tuple[float, float], str] = {}
            for sl in sl_range:
                for tp in tp_range:
                    hit_info[(sl, tp)] = "timeout"

            # Single forward pass: check each bar against all un-hit configs
            for i in range(entry_idx + 1, end_idx):
                bar_high = float(highs[i])
                bar_low = float(lows[i])

                pending = [(k, v) for k, v in hit_info.items() if v == "timeout"]
                if not pending:
                    break

                for (sl, tp), _ in pending:
                    sl_price = (
                        entry_price - sl * atr_val
                        if entry_side == "long"
                        else entry_price + sl * atr_val
                    )
                    tp_price = (
                        entry_price + tp * atr_val
                        if entry_side == "long"
                        else entry_price - tp * atr_val
                    )

                    if entry_side == "long":
                        # With costs: SL hits earlier (slippage), TP must exceed spread
                        effective_sl = sl_price - slippage_cost
                        effective_tp = tp_price - spread_cost
                        if bar_low <= effective_sl:
                            hit_info[(sl, tp)] = "sl_hit_first"
                        elif bar_high >= effective_tp:
                            hit_info[(sl, tp)] = "tp_hit_first"
                    else:
                        effective_sl = sl_price + slippage_cost
                        effective_tp = tp_price + spread_cost
                        if bar_high >= effective_sl:
                            hit_info[(sl, tp)] = "sl_hit_first"
                        elif bar_low <= effective_tp:
                            hit_info[(sl, tp)] = "tp_hit_first"

            # Aggregate results
            for (sl, tp), result in hit_info.items():
                tp_c, sl_c, to_c = grid[(sl, tp)]
                if result == "tp_hit_first":
                    grid[(sl, tp)] = (tp_c + 1, sl_c, to_c)
                elif result == "sl_hit_first":
                    grid[(sl, tp)] = (tp_c, sl_c + 1, to_c)
                else:
                    grid[(sl, tp)] = (tp_c, sl_c, to_c + 1)

    total_entries = len(entries) * len(sides)
    mean_atr = atr_sum / max(atr_count, 1)

    # Build ProfitabilityPoint for each grid cell
    points: list[ProfitabilityPoint] = []
    for (sl, tp), (tp_c, sl_c, to_c) in sorted(grid.items()):
        total = tp_c + sl_c + to_c
        if total < 30:
            continue  # statistically unreliable

        tp_rate = tp_c / total
        sl_rate = sl_c / total
        to_rate = to_c / total
        ev = tp_rate * tp - sl_rate * sl

        # Sharpe estimate: binary outcome with timeout=0
        outcomes = np.array([tp] * tp_c + [-sl] * sl_c + [0.0] * to_c)
        mu = float(np.mean(outcomes))
        sigma = float(np.std(outcomes))
        sharpe = (mu / sigma * np.sqrt(252)) if sigma > 1e-12 else 0.0

        breakeven_tp = (sl_rate * sl) / tp_rate if tp_rate > 0 else float("inf")

        points.append(
            ProfitabilityPoint(
                sl_atr_mult=sl,
                tp_atr_mult=tp,
                horizon_bars=horizon_bars,
                tp_hit_rate=round(tp_rate, 6),
                sl_hit_rate=round(sl_rate, 6),
                timeout_rate=round(to_rate, 6),
                expected_pnl_r=round(ev, 6),
                sharpe_estimate=round(sharpe, 4),
                breakeven_tp=round(breakeven_tp, 4),
            )
        )

    return ProfitabilitySurface(
        symbol=symbol,
        timeframe=timeframe,
        total_bars=n_bars,
        entries_simulated=total_entries,
        horizon_bars=horizon_bars,
        atr_period=atr_period,
        mean_atr=round(mean_atr, 6),
        sl_range=sl_range,
        tp_range=tp_range,
        points=points,
    )


# ── Optimal contract builder ────────────────────────────────────────────────


def recommend_label_contract(
    surface: ProfitabilitySurface,
    *,
    min_expected_pnl: float = 0.05,
    min_reward_risk: float = 1.5,
    prefer_higher_tp: bool = True,
) -> dict[str, Any] | None:
    """Recommend a label contract configuration from the profitability surface.

    Selection criteria (in order):
      1. Expected PnL >= min_expected_pnl (in R-units)
      2. Reward:risk ratio >= min_reward_risk
      3. If prefer_higher_tp: among qualifying configs, pick highest TP
         (higher TP = more selective labels = higher quality training data)
    """
    candidates = [
        p
        for p in surface.points
        if p.expected_pnl_r >= min_expected_pnl and p.reward_risk_ratio >= min_reward_risk
    ]
    if not candidates:
        return None

    if prefer_higher_tp:
        best = max(candidates, key=lambda p: (p.tp_atr_mult, p.expected_pnl_r))
    else:
        best = max(candidates, key=lambda p: p.expected_pnl_r)

    return {
        "sl_atr_mult": best.sl_atr_mult,
        "tp_atr_mult": best.tp_atr_mult,
        "horizon_bars": best.horizon_bars,
        "expected_pnl_r": best.expected_pnl_r,
        "tp_hit_rate": best.tp_hit_rate,
        "sl_hit_rate": best.sl_hit_rate,
        "timeout_rate": best.timeout_rate,
        "reward_risk_ratio": best.reward_risk_ratio,
        "sharpe_estimate": best.sharpe_estimate,
    }


# ── Report generation ───────────────────────────────────────────────────────


def surface_to_report(surface: ProfitabilitySurface) -> dict[str, Any]:
    """Export profitability surface as a structured report."""
    return {
        "symbol": surface.symbol,
        "timeframe": surface.timeframe,
        "total_bars": surface.total_bars,
        "entries_simulated": surface.entries_simulated,
        "horizon_bars": surface.horizon_bars,
        "mean_atr": surface.mean_atr,
        "sl_range": surface.sl_range,
        "tp_range": surface.tp_range,
        "profitable_configs": [
            {
                "sl_atr_mult": p.sl_atr_mult,
                "tp_atr_mult": p.tp_atr_mult,
                "rr_ratio": round(p.reward_risk_ratio, 2),
                "tp_hit_rate": round(p.tp_hit_rate, 4),
                "sl_hit_rate": round(p.sl_hit_rate, 4),
                "timeout_rate": round(p.timeout_rate, 4),
                "expected_pnl_r": round(p.expected_pnl_r, 4),
                "sharpe_estimate": p.sharpe_estimate,
            }
            for p in surface.profitable_configs()
        ],
        "best_config": (
            {
                "sl_atr_mult": best.sl_atr_mult,
                "tp_atr_mult": best.tp_atr_mult,
                "expected_pnl_r": best.expected_pnl_r,
                "tp_hit_rate": best.tp_hit_rate,
                "sl_hit_rate": best.sl_hit_rate,
                "timeout_rate": best.timeout_rate,
                "sharpe_estimate": best.sharpe_estimate,
            }
            if (best := surface.best_config())
            else None
        ),
    }

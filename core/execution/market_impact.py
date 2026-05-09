"""Market impact estimation — Almgren-Chriss inspired model.

Decomposes execution cost into permanent impact (information leakage) and
temporary impact (liquidity pressure), parameterised for XAU/USD and forex.

Usage:
    from core.execution.market_impact import estimate_market_impact, MarketImpactEstimate

    est = estimate_market_impact(
        order_volume=0.05,          # lots
        daily_volume=5000,          # lots/day
        volatility=0.0012,          # daily return std dev
        time_fraction=0.25,         # execute over 1/4 day (6h)
        spread_bps=1.5,
    )
    print(f"Total impact: {est.total_impact_bps:.2f} bps")
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ── Default calibration (XAU/USD typical) ───────────────────────────────────

DEFAULT_ALPHA = 0.80  # permanent impact coefficient
DEFAULT_BETA = 1.20  # temporary impact coefficient
DEFAULT_GAMMA = 0.60  # volume fraction exponent (Almgren-Chriss ~0.6)
DEFAULT_ETA = 0.15  # spread-related fixed cost multiplier


# ── Public API ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarketImpactEstimate:
    """Decomposed market impact for a single order."""

    symbol: str
    side: str
    order_volume: float
    daily_volume: float
    volatility: float
    time_fraction: float
    spread_bps: float
    permanent_impact_bps: float
    temporary_impact_bps: float
    fixed_cost_bps: float
    total_impact_bps: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "order_volume": self.order_volume,
            "daily_volume": self.daily_volume,
            "volatility": self.volatility,
            "time_fraction": self.time_fraction,
            "spread_bps": self.spread_bps,
            "permanent_impact_bps": self.permanent_impact_bps,
            "temporary_impact_bps": self.temporary_impact_bps,
            "fixed_cost_bps": self.fixed_cost_bps,
            "total_impact_bps": self.total_impact_bps,
        }


def estimate_market_impact(
    *,
    symbol: str = "XAUUSDc",
    side: str = "long",
    order_volume: float,
    daily_volume: float,
    volatility: float,
    time_fraction: float = 0.25,
    spread_bps: float = 0.0,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
    gamma: float = DEFAULT_GAMMA,
    eta: float = DEFAULT_ETA,
) -> MarketImpactEstimate:
    """Estimate market impact for an order using an Almgren-Chriss style model.

    Args:
        symbol: Trading instrument.
        side: ``"long"`` or ``"short"`` (sign is inferred).
        order_volume: Position size in lots (absolute value).
        daily_volume: Average daily volume in same units as order_volume.
        volatility: Daily return standard deviation (e.g. 0.0012 for 12 bps).
        time_fraction: Fraction of trading day to execute (0–1).
        spread_bps: Bid-ask spread in basis points.
        alpha: Permanent impact scaling.
        beta: Temporary impact scaling.
        gamma: Volume-fraction exponent.
        eta: Spread cost multiplier.

    Returns:
        MarketImpactEstimate with decomposed impact components in bps.
    """
    vol = abs(order_volume)
    if vol <= 0 or daily_volume <= 0:
        return MarketImpactEstimate(
            symbol=symbol,
            side=side,
            order_volume=order_volume,
            daily_volume=daily_volume,
            volatility=volatility,
            time_fraction=time_fraction,
            spread_bps=spread_bps,
            permanent_impact_bps=0.0,
            temporary_impact_bps=0.0,
            fixed_cost_bps=0.0,
            total_impact_bps=0.0,
        )

    participation_rate = vol / (daily_volume * max(time_fraction, 1e-6))

    # Volatility in bps
    sigma_bps = volatility * 10000

    # Permanent impact (bps) — price moves against you permanently
    permanent_bps = alpha * sigma_bps * np.sqrt(vol / max(daily_volume, 1))

    # Temporary impact (bps) — transient pressure, decays after execution
    temporary_bps = beta * sigma_bps * (participation_rate**gamma)

    # Fixed cost — bid-ask spread crossed once
    fixed_bps = eta * spread_bps

    total = round(permanent_bps + temporary_bps + fixed_bps, 6)

    direction_mult = 1.0 if side.lower() in ("long", "buy") else -1.0

    return MarketImpactEstimate(
        symbol=symbol,
        side=side,
        order_volume=order_volume,
        daily_volume=daily_volume,
        volatility=volatility,
        time_fraction=time_fraction,
        spread_bps=spread_bps,
        permanent_impact_bps=round(permanent_bps * direction_mult, 6),
        temporary_impact_bps=round(temporary_bps, 6),
        fixed_cost_bps=round(fixed_bps, 6),
        total_impact_bps=round(total * direction_mult, 6),
    )


def impact_volatility_adjustment(
    order_volume: float,
    daily_volume: float,
    current_atr: float,
    mid_price: float,
) -> float:
    """Estimate volatility adjustment from market impact in bps.

    Convenience wrapper that derives volatility from ATR/price ratio.
    Returns total impact in bps.
    """
    if mid_price <= 0 or daily_volume <= 0:
        return 0.0
    vol = current_atr / mid_price
    est = estimate_market_impact(
        order_volume=abs(order_volume),
        daily_volume=daily_volume,
        volatility=vol,
    )
    return est.total_impact_bps

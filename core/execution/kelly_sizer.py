"""Kelly Sizer — Edge-targeted position scaling with EV veto.

Tier 2 of the three-tier institutional sizing architecture.
Scales base (vol-targeted) position size by the meta-model's P(TP|signal).

When the expected value (EV) is negative, issues a hard veto — not a soft floor.
This is the "Negative Kelly Ghost" trap: a trade with negative EV should never
be entered, regardless of how conservative the floor is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class KellyResult:
    """Output of the Kelly sizing computation."""

    p_win: float
    rr_ratio: float
    kelly_fraction: float  # raw kf = p - (1-p)/R
    fractional_mult: float  # 0.0 = hard veto; (0.5, 1.5] = multiplier
    sizing_label: str  # "negative_ev_veto" | "offensive" | "normal" | "defensive"


def compute_kelly_mult(
    p_win: float,
    rr_ratio: float,
    fractional_k: float = 0.5,
    floor: float = 0.5,
    cap: float = 1.5,
    *,
    epsilon: float = 0.02,
) -> KellyResult:
    """Compute the fractional Kelly multiplier for position sizing.

    Args:
        p_win: Calibrated P(TP|signal) — from MetaFilter or rolling PnL win rate.
        rr_ratio: Reward-to-risk ratio = TP_distance / SL_distance.
        fractional_k: Fraction of full Kelly to use (0.5 = half-Kelly, conservative).
        floor: Minimum multiplier when EV > 0 but kf is very small.
        cap: Maximum multiplier — prevents unbounded position amplification.
        epsilon: kf below this threshold is treated as zero-EV (defensive floor).

    Returns:
        KellyResult with fractional_mult:
          - 0.0  → negative EV veto (should_trade must be set to False)
          - 0.5  → defensive (low edge)
          - 1.0  → neutral (no amplification or dampening)
          - >1.0 → offensive (high edge)

    Trap 1 fix: When kf <= 0, the trade has negative expected value.
    The multiplier is 0.0 (hard veto), not a soft floor.  No amount of
    conservative scaling can make a negative-EV trade profitable.
    """
    if rr_ratio <= 0:
        rr_ratio = 0.01  # prevent division by zero; results in very negative kf

    kf = p_win - (1.0 - p_win) / rr_ratio

    if kf <= 0:
        return KellyResult(
            p_win=round(p_win, 4),
            rr_ratio=round(rr_ratio, 4),
            kelly_fraction=round(kf, 4),
            fractional_mult=0.0,
            sizing_label="negative_ev_veto",
        )

    # Near-zero EV: clamp to floor to avoid treating noise as real edge
    if kf < epsilon:
        return KellyResult(
            p_win=round(p_win, 4),
            rr_ratio=round(rr_ratio, 4),
            kelly_fraction=round(kf, 4),
            fractional_mult=floor,
            sizing_label="near_zero_ev_defensive",
        )

    mult = 1.0 + fractional_k * kf
    mult = max(floor, min(cap, mult))

    if mult > 1.2:
        label = "offensive"
    elif mult < 0.8:
        label = "defensive"
    else:
        label = "normal"

    return KellyResult(
        p_win=round(p_win, 4),
        rr_ratio=round(rr_ratio, 4),
        kelly_fraction=round(kf, 4),
        fractional_mult=round(mult, 4),
        sizing_label=label,
    )


def resolve_p_win_from_brains(
    brains: list[Any],
    pnl_store: Any | None,
    direction: str = "long",
) -> float:
    """Resolve dynamic p_win for a strategy that does NOT use MetaFilter.

    Delegates to :func:`core.execution.pwin_chain.resolve_p_win_from_brains`
    (S3 — Functional Core extraction).
    """
    from core.execution.pwin_chain import resolve_p_win_from_brains as _impl

    return _impl(brains, pnl_store, direction)

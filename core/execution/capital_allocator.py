"""Capital allocator — cross-group conflict resolution and position sizing.

Replaces the old approach of blindly averaging all brain proposals
into a single consensus.  Instead:

1. Accepts one GroupSignal per contract group.
2. Resolves cross-group conflicts via a rules matrix.
3. Computes dynamic position size from agreement level, regime, and
   historical group correlation.

Principles:
  - When groups agree: trade with full confidence → full size.
  - When groups are neutral or absent: trade with reduced size.
  - When groups conflict (long vs short): NO trade.
  - Size is modulated by volatility regime to normalize risk contribution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class AllocationDecision:
    """Output of the capital allocator for one cycle."""

    should_trade: bool
    direction: str  # "long", "short", or "neutral"
    confidence: float  # allocation-level confidence [0, 1]
    volume: float  # final lot size
    agreement_level: str  # "full" | "reduced" | "minimal" | "none"
    active_groups: list[str] = field(default_factory=list)
    dissenting_groups: list[str] = field(default_factory=list)
    reason: str = ""


# ── Conflict resolution matrix ────────────────────────────────────────────


def _count_directions(signals: dict[str, Any]) -> dict[str, list[str]]:
    """Count groups by their direction.

    Returns: {"long": [group_names], "short": [...], "neutral": [...]}
    """
    result: dict[str, list[str]] = {"long": [], "short": [], "neutral": []}
    for name, sig in signals.items():
        if sig is None:
            continue
        d = sig.direction
        if d in result:
            result[d].append(name)
        else:
            result["neutral"].append(name)
    return result


def resolve_conflicts(
    signals: dict[str, Any],
    *,
    require_unanimous: bool = False,
) -> AllocationDecision:
    """Apply conflict matrix to group signals and produce an allocation decision.

    Conflict matrix:
        - 3 groups agree (all long or all short)        → full, 100% confidence
        - 2 agree + 1 neutral                           → reduced, 85% confidence
        - 2 agree + 1 absent (group empty)              → reduced, 80% confidence
        - 2 agree + 1 oppose                            → minimal, 60% confidence
        - 1 agrees + others neutral                     → reduced, 65% confidence
        - 1 agrees + 1 opposes + 1 neutral              → no trade
        - groups disagree (long vs short)               → no trade
        - all neutral                                    → no trade

    Args:
        signals: dict group_name → GroupSignal | None
        require_unanimous: if True, only trade when ALL present groups agree.

    Returns:
        AllocationDecision with direction, confidence, and size multiplier.
    """
    direction_map = _count_directions(signals)
    longs = direction_map["long"]
    shorts = direction_map["short"]
    neutrals = direction_map["neutral"]

    n_long = len(longs)
    n_short = len(shorts)
    n_neutral = len(neutrals)
    total_present = n_long + n_short + n_neutral

    if total_present == 0:
        return AllocationDecision(
            should_trade=False,
            direction="neutral",
            confidence=0.0,
            volume=0.0,
            agreement_level="none",
            reason="no_active_groups",
        )

    # ── Conflict: long vs short exists ──
    if n_long > 0 and n_short > 0:
        return AllocationDecision(
            should_trade=False,
            direction="neutral",
            confidence=0.0,
            volume=0.0,
            agreement_level="none",
            dissenting_groups=list(set(longs + shorts)),
            reason=f"cross_group_conflict_long_{n_long}_short_{n_short}",
        )

    # ── No conflict: pick the non-neutral direction ──
    if n_long > 0:
        direction = "long"
        supporting = longs
        opposing = shorts  # empty
    elif n_short > 0:
        direction = "short"
        supporting = shorts
        opposing = longs  # empty
    else:
        return AllocationDecision(
            should_trade=False,
            direction="neutral",
            confidence=0.0,
            volume=0.0,
            agreement_level="none",
            reason="all_groups_neutral",
        )

    # ── Determine agreement level and confidence multiplier ──
    if require_unanimous and n_neutral > 0:
        return AllocationDecision(
            should_trade=False,
            direction=direction,
            confidence=0.0,
            volume=0.0,
            agreement_level="none",
            reason=f"require_unanimous_blocked_by_{n_neutral}_neutral",
        )

    # Compute raw confidence from supporting groups (average their confidences)
    conf_values: list[float] = []
    for gname in supporting:
        sig = signals.get(gname)
        if sig is not None:
            conf_values.append(sig.confidence)
    raw_conf = sum(conf_values) / max(len(conf_values), 1)

    if n_long + n_short == total_present:
        # All present groups agree on direction
        if total_present >= 3:
            agreement = "full"
            conf_mult = 1.0
        elif total_present == 2:
            agreement = "reduced"
            conf_mult = 0.85
        else:
            agreement = "reduced"
            conf_mult = 0.65
    elif n_neutral == 1 and n_long + n_short == 2:
        # 2 agree, 1 neutral
        agreement = "reduced"
        conf_mult = 0.85
    elif n_neutral >= 2 or n_long + n_short == 1:
        # Only 1 group agrees, rest neutral
        agreement = "reduced"
        conf_mult = 0.65
    else:
        agreement = "reduced"
        conf_mult = 0.70

    confidence = round(raw_conf * conf_mult, 4)

    return AllocationDecision(
        should_trade=True,
        direction=direction,
        confidence=confidence,
        volume=0.0,  # computed separately
        agreement_level=agreement,
        active_groups=supporting,
        dissenting_groups=opposing,
        reason=f"{agreement}_{direction}_{'+'.join(supporting)}",
    )


# ── Dynamic position sizing ───────────────────────────────────────────────


def compute_volume(
    base_volume: float,
    decision: AllocationDecision,
    *,
    regime: str = "normal",
    vol_atr: float | None = None,
    vol_reference: float = 5.0,
    min_volume: float = 0.01,
    max_volume: float = 0.10,
) -> float:
    """Compute dynamic position size.

    volume = base_volume × agreement_factor × regime_factor × vol_factor
    """
    # Agreement factor
    agreement_map = {"full": 1.0, "reduced": 0.70, "minimal": 0.45}
    agreement_factor = agreement_map.get(decision.agreement_level, 0.50)

    # Regime factor: normalize risk across volatility regimes
    regime_map = {"low": 1.20, "normal": 1.00, "high": 0.70}
    regime_factor = regime_map.get(regime, 1.0)

    # Volatility factor: use ATR to normalize risk contribution
    if vol_atr is not None and vol_atr > 0:
        vol_factor = min(1.5, max(0.5, vol_reference / vol_atr))
    else:
        vol_factor = 1.0

    size = base_volume * agreement_factor * regime_factor * vol_factor
    # Round to lot step (0.01 for XAUUSDc); MT5 rejects non-multiple volumes
    return round(max(min_volume, min(size, max_volume)), 2)


# ── Group correlation tracker ─────────────────────────────────────────────


class GroupCorrelationTracker:
    """Track directional agreement between contract groups.

    Maintains an EMA of pairwise co-direction so that the capital
    allocator can apply a penalty when historically-correlated groups
    suddenly diverge — a signal that the regime may be shifting and
    sizing should be conservative.
    """

    def __init__(self, ema_alpha: float = 0.05) -> None:
        self.ema_alpha = ema_alpha
        # pairwise (g1, g2) → EMA of agreement (1.0=always same dir, 0.0=always opposite)
        self._pairwise_ema: dict[tuple[str, str], float] = {}
        self._group_names: list[str] = ["barrier_12bar", "micro_3bar", "statarb_dynamic"]
        self._update_count: int = 0

    def update(self, group_signals: dict[str, Any]) -> None:
        """Ingest one cycle of group signals and update EMA."""
        self._update_count += 1
        names = self._group_names

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                g1, g2 = names[i], names[j]
                sig1 = group_signals.get(g1)
                sig2 = group_signals.get(g2)

                # Only count pairs where both groups are active (non-None)
                if sig1 is None or sig2 is None:
                    continue

                d1 = sig1.direction
                d2 = sig2.direction

                # Both neutral → skip (no directional info)
                if d1 == "neutral" and d2 == "neutral":
                    continue

                # Agreement: 1.0 if same direction, 0.0 if opposite
                if d1 == d2:
                    agree = 1.0
                elif d1 == "neutral" or d2 == "neutral":
                    agree = 0.5  # one neutral = partial agreement
                else:
                    agree = 0.0  # long vs short = complete disagreement

                key = (g1, g2)
                prev = self._pairwise_ema.get(key, 0.5)  # prior = 0.5 (unknown)
                self._pairwise_ema[key] = prev + self.ema_alpha * (agree - prev)

    def get_correlation_penalty(self, group_signals: dict[str, Any]) -> float:
        """Compute a correlation-based volume penalty.

        Returns a multiplier in [0.7, 1.0]:
          1.0 = no same-direction concentration risk
          0.7 = maximum penalty (highly-correlated groups all pointing same way)

        Penalty triggers when two groups that USUALLY co-move both signal the
        same direction — indicating redundant, concentrated exposure.
        Opposite-direction signals are treated as a natural hedge (no penalty).
        """
        if self._update_count < 3:
            return 1.0  # not enough data

        penalties: list[float] = []
        names = self._group_names

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                g1, g2 = names[i], names[j]
                sig1 = group_signals.get(g1)
                sig2 = group_signals.get(g2)

                if sig1 is None or sig2 is None:
                    continue

                d1 = sig1.direction
                d2 = sig2.direction

                # Neutral signals → skip
                if d1 == "neutral" or d2 == "neutral":
                    continue

                # Opposite directions → natural hedge, no penalty
                if d1 != d2:
                    continue

                # Same direction: concentration risk when historically correlated
                # If groups usually co-move (high hist_corr) and both signal same
                # direction, reduce volume to avoid over-concentration
                key = (g1, g2)
                hist_corr = self._pairwise_ema.get(key, 0.5)

                if hist_corr > 0.5:
                    # corr_pen ∈ [0.7, 1.0]: corr=1.0 → 0.70x, corr=0.5 → 1.0x
                    corr_pen = 1.0 - 0.3 * max(0.0, (hist_corr - 0.5) / 0.5)
                    penalties.append(corr_pen)

        if not penalties:
            return 1.0

        return round(min(penalties), 4)  # most conservative penalty wins


# ── Portfolio-optimizer bridge ──────────────────────────────────────────────


def compute_optimal_group_weights(
    group_returns: dict[str, list[float]],
    *,
    method: str = "risk_parity",
    lookback: int = 50,
    shrinkage: float = 0.2,
) -> dict[str, float]:
    """Bridge to ``core.metrics.portfolio_optimizer`` for cross-group allocation.

    Uses historical per-group P&L snapshots to compute optimal capital weights
    via Markowitz, Risk Parity, or Equal Weight — then normalises to [0, 1].

    Args:
        group_returns: dict group_name → list of periodic returns (e.g. trade P&L %).
        method: "risk_parity" (default), "min_variance", "max_sharpe", or "equal".
        lookback: max number of recent returns to use per group.
        shrinkage: Ledoit-Wolf shrinkage delta for covariance.

    Returns:
        dict group_name → weight (float, sum ≈ 1.0).
    """
    from core.metrics.portfolio_optimizer import (
        equal_weights,
        max_sharpe_weights,
        min_variance_weights,
        risk_parity_weights,
        shrunk_covariance,
    )

    groups = sorted(group_returns.keys())
    if len(groups) < 2:
        return {g: 1.0 for g in groups} if groups else {}

    # Build aligned returns matrix (T x N)
    min_len = min(len(group_returns[g][-lookback:]) if group_returns[g] else 0 for g in groups)
    if min_len < 5:
        return {g: 1.0 / len(groups) for g in groups}

    returns_matrix = np.zeros((min_len, len(groups)), dtype=np.float64)
    for j, g in enumerate(groups):
        series = group_returns[g][-min_len:]
        returns_matrix[:, j] = np.asarray(series, dtype=np.float64)

    cov = shrunk_covariance(returns_matrix, delta=shrinkage)

    if method == "min_variance":
        weights = min_variance_weights(cov)
    elif method == "max_sharpe":
        weights = max_sharpe_weights(cov)
    elif method == "risk_parity":
        weights = risk_parity_weights(cov)
    else:
        weights = equal_weights(len(groups))

    return {g: round(float(w), 6) for g, w in zip(groups, weights, strict=False)}


def blend_group_weight(
    portfolio_weight: float,
    agreement_mult: float,
    *,
    portfolio_blend: float = 0.3,
) -> float:
    """Blend portfolio-optimizer weight with agreement-based volume multiplier.

    ``portfolio_blend`` controls how much the portfolio-optimizer weight
    influences the final size.  At 0.0 the optimiser is ignored; at 1.0
    agreement multipliers are ignored.
    """
    return round(
        portfolio_blend * portfolio_weight + (1.0 - portfolio_blend) * agreement_mult,
        4,
    )

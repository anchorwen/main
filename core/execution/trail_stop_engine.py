"""Trail Stop Engine — Risk Exit subsystem (Phase C: physical isolation).

The Chandelier trailing stop is a pure risk-exit mechanism.  It responds ONLY to:
  - Entry price and price extremes (highest_high, lowest_low)
  - Current ATR (market volatility)
  - TrailPolicy (per-strategy immutable configuration)
  - Brain live Sharpe (widening only — reward for proven performance, never a penalty)

It is completely agnostic to:
  - Strategy type (statarb, barrier, swing — irrelevant)
  - Model confidence or consensus
  - Brain identity beyond live Sharpe for trail widening

Model Exit signals (confidence decay, consensus flip, Sharpe decay for exit)
flow through evaluate_brain_exit() in ActivePositionManager — never through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from core.execution.position_manager import ActivePosition


# ── Trail Policy (Phase B: Risk Exit subsystem isolation) ──────────────────


@dataclass(frozen=True)
class TrailPolicy:
    """Per-strategy Chandelier trailing stop configuration — Risk Exit ONLY.

    Immutable declaration of how the trail behaves for a specific strategy.
    Does NOT reference brain performance, confidence, or model quality.
    Those belong to Model Exit (evaluate_brain_exit).

    This is the physical boundary between subsystem 3A (Risk Exit) and
    subsystem 3B (Model Exit).  TrailPolicy encodes market-microstructure
    decisions (ATR multiples, breakeven thresholds, lock-in caps).
    Model quality decisions (Sharpe, confidence decay, consensus flip)
    flow through evaluate_brain_exit() exclusively.
    """

    trail_atr_mult: float = 2.0
    trail_atr_mult_low: float = 1.5
    trail_atr_mult_high: float = 3.0
    breakeven_threshold_atr: float = 1.0
    min_step: float = 0.15  # minimum SL change to fire modify (price units)
    # FIX-20260603-064: activation watermark — trail only starts after
    # unrealized profit exceeds this many ATRs.  Prevents $3 bounce from
    # stopping out a position that never had room to breathe.
    trail_activation_atr: float = 1.0  # 0 = immediate (old behavior)
    min_trail_mult: float = 1.2  # absolute floor on effective trail multiplier
    max_lock_atr: float = 4.0  # max R to lock in via trailing
    graduated_lock_enabled: bool = True
    graduated_lock_levels: tuple[tuple[float, float], ...] = (
        (3.0, 1.5),  # at +3R peak, SL floor >= +1.5R
        (5.0, 3.5),  # at +5R peak, SL floor >= +3.5R
    )
    # DQAF-20260609-001: Non-linear dynamic decay — profit-aware trail tightening.
    # Multiplier decays from base to min_trail_mult as R-multiple grows from
    # decay_start_r to decay_full_r.  Prevents the "breakeven floor deadlock"
    # where Chandelier trail can never exceed entry_price because trail_mult
    # is too wide for the profit earned.  Also prevents premature stops right
    # after breakeven by keeping the trail relaxed at low R.
    decay_start_r: float = 0.5  # R-multiple where decay begins
    decay_full_r: float = 2.0  # R-multiple where decay completes (trail hits min_mult)
    decay_enabled: bool = True  # set False to restore pre-009 behavior


# ── Trail Stop Engine ──────────────────────────────────────────────────────


class TrailStopEngine:
    """Risk Exit subsystem — Chandelier trailing stop calculator.

    Physically isolated from Model Exit.  Operates on an ActivePosition and
    current ATR using the position's TrailPolicy (or the engine default).
    """

    def __init__(
        self,
        default_policy: TrailPolicy | None = None,
        pnl_store: Any = None,
    ):
        self.default_policy = default_policy or TrailPolicy()
        self.pnl_store = pnl_store

    # ── Public API ─────────────────────────────────────────────────────────

    @staticmethod
    def _compute_decayed_mult(pos: ActivePosition, tp: TrailPolicy) -> float:
        """Profit-aware nonlinear decay of trail multiplier.

        DQAF-20260609-001: As a position accumulates profit, the trail
        multiplier smoothly decays from the regime-given base to min_trail_mult.
        This prevents the "breakeven floor deadlock" where Chandelier trail
        can never exceed entry_price because the multiplier is too wide for
        the profit earned.

        Decay is linear in R-space between decay_start_r and decay_full_r:
          - R < decay_start_r:  keep base multiplier (room to breathe)
          - R > decay_full_r:   use min_trail_mult  (lock in profits)
          - Between:            linear interpolation

        The effective_mult is then capped by max(min_trail_mult, decayed_value).
        """
        base_mult = max(tp.min_trail_mult, pos.trail_multiplier)
        if not tp.decay_enabled or pos.entry_atr <= 0:
            return base_mult

        # R_max from highest_high or lowest_low
        if pos.side == "long":
            r_max = (pos.highest_high - pos.entry_price) / pos.entry_atr
        else:
            r_max = (pos.entry_price - pos.lowest_low) / pos.entry_atr

        if r_max < tp.decay_start_r:
            return base_mult
        if r_max > tp.decay_full_r:
            return tp.min_trail_mult

        # Linear interpolation
        ratio = (r_max - tp.decay_start_r) / (tp.decay_full_r - tp.decay_start_r)
        decayed = base_mult - ratio * (base_mult - tp.min_trail_mult)
        return max(tp.min_trail_mult, decayed)

    def compute_trail_stop(self, pos: ActivePosition, current_atr: float) -> float | None:
        """Return new SL if the trail has advanced, else None.

        Long:  max(current_sl, highest_high - trail_mult × atr)
        Short: min(current_sl, lowest_low + trail_mult × atr)

        The trail never exceeds original_SL + max_lock_atr × entry_atr
        (respects the model training contract).  The original TP remains
        the hard ceiling — never cancelled.

        FIX-20260603-064: Activation watermark — trail stays at initial SL
        until unrealized profit exceeds trail_activation_atr × entry_atr.
        Prevents $3 micro-bounces from stopping out positions that never
        had breathing room.
        """
        tp = self._resolve_policy(pos)

        # ── Activation watermark check ──
        if tp.trail_activation_atr > 0 and pos.entry_atr > 0:
            _activation_price = tp.trail_activation_atr * pos.entry_atr
            if pos.side == "long":
                _unrealized_r = (pos.highest_high - pos.entry_price) / pos.entry_atr
            else:
                _unrealized_r = (pos.entry_price - pos.lowest_low) / pos.entry_atr
            if _unrealized_r < tp.trail_activation_atr:
                return None  # not enough profit yet — keep initial SL

        effective_mult = self._compute_decayed_mult(pos, tp)

        if pos.side == "long":
            candidate = pos.highest_high - effective_mult * current_atr
            if pos.breakeven_triggered:
                candidate = max(candidate, pos.entry_price)
            candidate = max(candidate, pos.initial_sl)
            if tp.graduated_lock_enabled and pos.entry_atr > 0:
                current_r = (pos.highest_high - pos.entry_price) / pos.entry_atr
                for r_threshold, lock_r in tp.graduated_lock_levels:
                    if current_r >= r_threshold:
                        candidate = max(candidate, pos.entry_price + lock_r * pos.entry_atr)
            max_lock = pos.entry_price + tp.max_lock_atr * pos.entry_atr
            candidate = min(candidate, max_lock)
            if candidate <= pos.current_sl + tp.min_step:
                return None
            return round(candidate, 3)
        else:
            candidate = pos.lowest_low + effective_mult * current_atr
            if pos.breakeven_triggered:
                candidate = min(candidate, pos.entry_price)
            candidate = min(candidate, pos.initial_sl)
            if tp.graduated_lock_enabled and pos.entry_atr > 0:
                current_r = (pos.entry_price - pos.lowest_low) / pos.entry_atr
                for r_threshold, lock_r in tp.graduated_lock_levels:
                    if current_r >= r_threshold:
                        candidate = min(candidate, pos.entry_price - lock_r * pos.entry_atr)
            max_lock = pos.entry_price - tp.max_lock_atr * pos.entry_atr
            candidate = max(candidate, max_lock)
            if candidate >= pos.current_sl - tp.min_step:
                return None
            return round(candidate, 3)

    def should_breakeven(self, pos: ActivePosition, current_atr: float) -> bool:
        """Return True when the favorable move exceeds the breakeven threshold.

        FIX-20260603-064: activation watermark — breakeven is suppressed until
        unrealized profit exceeds trail_activation_atr × entry_atr.  Prevents
        premature breakeven lock on positions that never had breathing room.
        """
        if pos.breakeven_triggered:
            return False
        tp = self._resolve_policy(pos)

        # ── Activation watermark check ──
        if tp.trail_activation_atr > 0 and pos.entry_atr > 0:
            if pos.side == "long":
                _unrealized_r = (pos.highest_high - pos.entry_price) / pos.entry_atr
            else:
                _unrealized_r = (pos.entry_price - pos.lowest_low) / pos.entry_atr
            if _unrealized_r < tp.trail_activation_atr:
                return False  # not enough profit yet — keep breakeven suppressed

        threshold = tp.breakeven_threshold_atr * current_atr
        if pos.side == "long":
            return (pos.highest_high - pos.entry_price) >= threshold
        else:
            return (pos.entry_price - pos.lowest_low) >= threshold

    def adjust_trail_for_regime(
        self,
        pos: ActivePosition,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
    ) -> None:
        """Dynamically set pos.trail_multiplier from volatility regime + brain P&L.

        Called every cycle from update_prices().  The multiplier feeds into
        compute_trail_stop() on the same cycle.
        """
        tp = self._resolve_policy(pos)
        regime = (regime_info or {}).get("regime", "normal")
        if regime == "low":
            base = tp.trail_atr_mult_low
        elif regime == "high":
            base = tp.trail_atr_mult_high
        else:
            base = tp.trail_atr_mult

        base = self._compute_adaptive_trail_k(current_atr, pos, base)
        base *= self._compute_brain_specific_trail_scale(pos)
        pos.trail_multiplier = round(base, 3)

    # ── Internal ───────────────────────────────────────────────────────────

    def _resolve_policy(self, pos: ActivePosition) -> TrailPolicy:
        """Return the position's TrailPolicy, or the engine default."""
        tp = getattr(pos, "trail_policy", None)
        return tp if tp is not None else self.default_policy

    def _compute_adaptive_trail_k(
        self, current_atr: float, pos: ActivePosition, base_k: float
    ) -> float:
        """Adjust trail multiplier based on realised volatility only.

        Vol expanding (trend accelerating) → widen trail, give room.
        Vol contracting (trend exhausting) → tighten trail.

        Returns adjusted K in [1.0, 4.0].

        Confidence adjustments deliberately REMOVED (Phase A).  Confidence
        collapse is a Model Exit signal (evaluate_brain_exit), not a
        Risk Exit trail-width input.
        """
        if pos.entry_atr <= 0:
            return base_k

        vol_ratio = current_atr / pos.entry_atr
        # FIX-20260603-071: quadratic explosion fix.
        # Previously vol_ratio > 1.5 → vol_adj = +0.8, which multiplied k
        # AND the already-doubled ATR → SL distance exploded (200→560 pts).
        # At extreme vol, ATR expansion alone provides enough room — tighten k.
        # Architect directive: extreme vol = trend climax, not room to breathe.
        if vol_ratio > 1.5:
            vol_adj = -0.5  # was +0.8
        elif vol_ratio > 1.2:
            vol_adj = -0.2  # was +0.4
        elif vol_ratio < 0.7:
            vol_adj = 0.5  # was -0.3
        else:
            vol_adj = 0.0

        k = base_k + vol_adj
        return max(0.8, min(3.0, k))

    def _compute_brain_specific_trail_scale(self, pos: ActivePosition) -> float:
        """Widen trail for brains with high live Sharpe ratios.  [1.0, 1.5].

        This is the ONLY remaining cross-touch between brain performance and
        trail width.  It only WIDENS (never tightens) — a reward for proven
        performance, not a penalty.  Losing brains should trigger model exit
        via evaluate_brain_exit(), not trail compression.

        Phase A: floor raised from 0.6→1.0 (death spiral severed).
        """
        if not pos.supporting_brain_ids:
            return 1.0
        if self.pnl_store is None:
            return 1.0

        try:
            sharpe_values: list[float] = []
            for bid in pos.supporting_brain_ids:
                metrics = self.pnl_store.get_metrics(bid)
                if metrics and metrics.sample_count >= 5:
                    s = float(metrics.sharpe_ratio)
                    sharpe_values.append(s)

            if not sharpe_values:
                return 1.0

            avg_sharpe = float(np.mean(sharpe_values))
            scale = 1.0 + 0.25 * float(np.tanh(avg_sharpe * 1.2))
            return float(np.clip(scale, 1.0, 1.5))
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return 1.0

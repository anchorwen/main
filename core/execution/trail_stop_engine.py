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
    max_lock_atr: float = (
        2.0  # max R(bracket) to lock in via trailing  [PER_TF: 4.0→2.0, bracket_atr units]
    )
    graduated_lock_enabled: bool = True
    graduated_lock_levels: tuple[tuple[float, float], ...] = (
        (
            1.0,
            0.5,
        ),  # at +1.0R(bracket) peak, SL floor >= +0.5R(bracket)  [PER_TF: folded from (3.0,1.5)]
        (
            2.0,
            1.0,
        ),  # at +2.0R(bracket) peak, SL floor >= +1.0R(bracket)  [PER_TF: folded from (5.0,3.5)]
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

    # ── FIX-20260708-004: Profit Ratchet Floor — broker-bound positive lock ──
    # DQAF-20260708-004 give-back root cause: the trailing SL never locked a
    # POSITIVE floor at the broker (MODE_B 87-89% of give-backs), so a model
    # exit (signal_close) realised ~$0 at breakeven (MODE_C 80-84%).  The
    # Chandelier trail alone leaves three structural holes: it returns None
    # (no floor at all) when the raw candidate did not advance; its candidate
    # uses current_atr which balloons the goalpost in volatile moves; and it
    # only floors via graduated_lock at +3R, leaving the dominant +1R..+3R
    # band unprotected.  The ratchet floor closes all three: once the peak
    # reaches ratchet_arm_r (measured in ENTRY_ATR — a stable goalpost), the
    # SL is forced to lock at least ratchet_breakeven_floor_r and to give back
    # no more than ratchet_giveback_r from the peak, INDEPENDENT of the
    # breakeven_triggered intent latch, and applied even when the Chandelier
    # trail itself did not advance.  It is monotonic (peak R only grows), so
    # the min_step guard suppresses NO_CHANGES (retcode 10025) resends, and it
    # is combined with the Chandelier candidate via max()/min() so a running
    # winner (tighter Chandelier) is unaffected — the floor only binds when the
    # Chandelier failed to protect.
    ratchet_enabled: bool = True
    ratchet_arm_r: float = 1.0  # peak R (entry_atr units) at which the floor arms
    ratchet_giveback_r: float = 1.0  # max R surrendered from the peak once armed
    ratchet_breakeven_floor_r: float = 0.1  # min positive lock (covers spread+commission)

    # ── FIX-20260713-008: TP trailing structural parity ──
    # TP trailing was price-blind (anchored to entry_price, ATR-only trigger)
    # while SL trailing was price-aware (Chandelier highest_high/lowest_low).
    # These three fields give the TP trail the same structural protection the
    # SL trail already has.  Active by default (全盘激活 2026-07-13).
    # Set to 0.0 to disable individual protections (opt-out per strategy).
    tp_proximity_ratio: float = 0.7  # 0=disabled; 0.7=suppress TP tighten when price has covered ≥70% of the entry→TP journey
    tp_min_distance_atr: float = 1.5  # 0=disabled; 1.5=TP floor in bracket_atr units (TP must stay ≥ this × bracket_atr from anchor)
    tp_min_step: float = 0.15  # 0=disabled (caller's exit_min_step applies); >0=override min step for TP-only changes

    # ── TECH_DEBT-019: RR 耦合修复 (RR floor / symmetric tightening / elastic expansion) ──
    # 0.0=disabled (structural/legacy zero-change); >0 (e.g. 0.85) arms the
    # three-mechanism RR contract for the trailing bracket:
    #   ① trailing TP distance stays >= this × current SL distance (RR hard floor)
    #   ② ATR contraction also tightens the SL (SL_Volatility_Trail)
    #   ③ ATR recovery elastically re-expands TP up to initial_tp
    tp_min_rr_ratio: float = 0.0


def compute_rr_floor_price(
    side: str,
    entry_price: float,
    current_sl: float,
    min_rr_ratio: float,
) -> float | None:
    """TECH_DEBT-019 §1: minimum TP price (entry reference) preserving RR >= min_rr.

    RR convention matches ``check_minimum_rr()`` and the audit scripts: both TP
    distance and SL distance are measured from entry.
      - LONG:  tp >= entry + min_rr × (entry − current_sl), requires entry > SL
      - SHORT: tp <= entry − min_rr × (current_sl − entry), requires SL > entry
    Returns None when the RR contract is disabled (min_rr <= 0) or the risk leg
    has closed (post-breakeven SL crossed entry → sl_dist <= 0 → zero constraint).
    Single convergence point for RR distance math (compute_trail_tp + trail_dispatch).
    """
    if min_rr_ratio <= 0 or current_sl <= 0:
        return None
    if side == "long":
        sl_dist = entry_price - current_sl
        return None if sl_dist <= 0 else entry_price + min_rr_ratio * sl_dist
    sl_dist = current_sl - entry_price
    return None if sl_dist <= 0 else entry_price - min_rr_ratio * sl_dist


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
        _geom_atr = TrailStopEngine._resolve_geometry_atr(pos)
        if not tp.decay_enabled or _geom_atr <= 0:
            return base_mult

        # R_max from highest_high or lowest_low — bracket_atr units (PER_TF geometry)
        if pos.side == "long":
            r_max = (pos.highest_high - pos.entry_price) / _geom_atr
        else:
            r_max = (pos.entry_price - pos.lowest_low) / _geom_atr

        if r_max < tp.decay_start_r:
            return base_mult
        if r_max > tp.decay_full_r:
            return tp.min_trail_mult

        # Linear interpolation
        ratio = (r_max - tp.decay_start_r) / (tp.decay_full_r - tp.decay_start_r)
        decayed = base_mult - ratio * (base_mult - tp.min_trail_mult)
        return max(tp.min_trail_mult, decayed)

    @staticmethod
    def _ratchet_lock_r(pos: ActivePosition, tp: TrailPolicy) -> float | None:
        """Return the profit-ratchet floor lock in ENTRY_ATR R-units, or None.

        FIX-20260708-004.  Once the position's peak favorable excursion reaches
        ``ratchet_arm_r`` (measured against entry_atr — a stable goalpost that
        does not balloon with current_atr), guarantee a positive SL floor that
        gives back at most ``ratchet_giveback_r`` from the peak, never less than
        ``ratchet_breakeven_floor_r``, and never more than ``max_lock_atr``.

        The lock is a pure function of the monotonic peak (highest_high /
        lowest_low), so it only ratchets up — combined with the caller's
        min_step guard this cannot re-send an unchanged SL (retcode 10025).
        Returns None when the ratchet is disabled, entry_atr is unusable, or
        the peak has not yet reached the arming threshold.
        """
        if not tp.ratchet_enabled or pos.entry_atr <= 0:
            return None
        if pos.side == "long":
            r_max = (pos.highest_high - pos.entry_price) / pos.entry_atr
        else:
            r_max = (pos.entry_price - pos.lowest_low) / pos.entry_atr
        if r_max < tp.ratchet_arm_r:
            return None
        lock_r = max(tp.ratchet_breakeven_floor_r, r_max - tp.ratchet_giveback_r)
        return min(lock_r, tp.max_lock_atr)

    def compute_trail_stop(
        self,
        pos: ActivePosition,
        current_atr: float,
        pre_close_atr_mult_override: float | None = None,
    ) -> float | None:  # current_atr vestigial post PER_TF migration
        """Return new SL if the trail has advanced, else None.

        Long:  max(current_sl, highest_high - trail_mult × atr)
        Short: min(current_sl, lowest_low + trail_mult × atr)

        The trail never exceeds original_SL + max_lock_atr × entry_atr
        (respects the model training contract).

        FIX-20260707-009: TP yields to Trailing SL upon bracket crossover.
        When the Chandelier trail advances the SL past the TP zone — or
        compute_trail_tp() detects that a tightened TP would fall inside
        the current SL — the TP is released (set to 0.0, i.e. no
        take-profit).  The position is then fully managed by the trailing
        stop, bounded by max_lock_atr and graduated_lock_levels.

        FIX-20260603-064: Activation watermark — trail stays at initial SL
        until unrealized profit exceeds trail_activation_atr × entry_atr.
        Prevents $3 micro-bounces from stopping out positions that never
        had breathing room.
        """
        tp = self._resolve_policy(pos)

        # ── Activation watermark check ──
        # DQAF-20260722-003: Use entry_atr for the activation threshold, NOT
        # _geom_atr (bracket_atr).  The activation watermark must measure
        # profit in the same R-units as the ratchet floor and position
        # snapshots (entry_atr).  When bracket_atr >> entry_atr (H1/H4
        # strategies), using bracket_atr makes trail_activation_atr=1.0
        # effectively unreachable — the Chandelier trail and ratchet floor
        # are never computed, even at extreme entry_atr R-multiples.
        # Ticket 4207155654 (h4_swing SHORT): +6.03R entry_atr → only
        # 0.54R in bracket_atr (<1.0) → trail blocked → SL never trailed
        # from 4110.62 → gave back all $345 unrealized profit.
        _geom_atr = self._resolve_geometry_atr(pos)
        _activation_atr = pos.entry_atr if pos.entry_atr > 0 else _geom_atr
        if tp.trail_activation_atr > 0 and _activation_atr > 0:
            if pos.side == "long":
                _unrealized_r = (pos.highest_high - pos.entry_price) / _activation_atr
            else:
                _unrealized_r = (pos.entry_price - pos.lowest_low) / _activation_atr
            if _unrealized_r < tp.trail_activation_atr:
                return None  # not enough profit yet — keep initial SL

        effective_mult = self._compute_decayed_mult(pos, tp)

        # ── Pre-close tightening (Institutional Risk Override) ──
        # min() guarantees pre_close can only TIGHTEN the trail, never loosen.
        # Normal trading: pre_close_atr_mult_override=None → no effect.
        if pre_close_atr_mult_override is not None:
            effective_mult = min(effective_mult, pre_close_atr_mult_override)

        if pos.side == "long":
            candidate = (
                pos.highest_high - effective_mult * _geom_atr
            )  # PER_TF: bracket_atr trail distance
            if pos.breakeven_triggered:
                candidate = max(candidate, pos.entry_price)
            candidate = max(candidate, pos.initial_sl)
            if tp.graduated_lock_enabled and _geom_atr > 0:
                current_r = (
                    pos.highest_high - pos.entry_price
                ) / _geom_atr  # PER_TF: bracket_atr units
                for r_threshold, lock_r in tp.graduated_lock_levels:
                    if current_r >= r_threshold:
                        candidate = max(candidate, pos.entry_price + lock_r * _geom_atr)
            # ── FIX-20260708-004: Profit Ratchet Floor (long) ──
            # Enforce a positive lock even if the Chandelier candidate above did
            # not advance — this is the safety net that stops the +1R..+3R
            # give-back cohort from unwinding to breakeven.
            _ratchet_r = self._ratchet_lock_r(pos, tp)
            if _ratchet_r is not None:
                pos.ratchet_floor_r = _ratchet_r
                candidate = max(candidate, pos.entry_price + _ratchet_r * pos.entry_atr)
            max_lock = pos.entry_price + tp.max_lock_atr * _geom_atr  # PER_TF: bracket_atr units
            candidate = min(candidate, max_lock)
            if candidate <= pos.current_sl + tp.min_step:
                return None
            return round(candidate, 3)
        else:
            candidate = (
                pos.lowest_low + effective_mult * _geom_atr
            )  # PER_TF: bracket_atr trail distance
            if pos.breakeven_triggered:
                candidate = min(candidate, pos.entry_price)
            candidate = min(candidate, pos.initial_sl)
            if tp.graduated_lock_enabled and _geom_atr > 0:
                current_r = (
                    pos.entry_price - pos.lowest_low
                ) / _geom_atr  # PER_TF: bracket_atr units
                for r_threshold, lock_r in tp.graduated_lock_levels:
                    if current_r >= r_threshold:
                        candidate = min(candidate, pos.entry_price - lock_r * _geom_atr)
            # ── FIX-20260708-004: Profit Ratchet Floor (short) ──
            # Mirror of the long branch: force the SL down to lock at least
            # _ratchet_r in entry_atr units so a retracement cannot reach
            # breakeven before the broker-side stop catches it.
            _ratchet_r = self._ratchet_lock_r(pos, tp)
            if _ratchet_r is not None:
                pos.ratchet_floor_r = _ratchet_r
                candidate = min(candidate, pos.entry_price - _ratchet_r * pos.entry_atr)
            max_lock = pos.entry_price - tp.max_lock_atr * _geom_atr  # PER_TF: bracket_atr units
            candidate = max(candidate, max_lock)
            if candidate >= pos.current_sl - tp.min_step:
                return None
            return round(candidate, 3)

    def compute_volatility_trail_sl(
        self,
        pos: ActivePosition,
        atr_ratio: float,
    ) -> float | None:
        """TECH_DEBT-019 §2: Symmetric Volatility Tightening (SL_Volatility_Trail).

        When M5 realized volatility contracts (atr_ratio <= 0.80) and the TP
        trail tightens the profit target, the risk leg (SL) is tightened by the
        SAME ratio so the reduced absolute bracket space keeps the open-time RR
        expectation (>= 1.0 when min_rr_ratio ~ 1).  The Chandelier geometry
        (bracket_atr, FIX-20260709-006) is untouched — this is an additive,
        bounded override on top of the normal SL trail:
          - LONG:  sl_dist = entry − current_sl; target = entry − sl_dist × atr_ratio
          - SHORT: sl_dist = current_sl − entry; target = entry + sl_dist × atr_ratio
        Protected by the profit ratchet floor (FIX-20260708-004, defensive —
        the ratchet is applied upstream in compute_trail_stop so this only binds
        in inconsistent state), max_lock_atr ceiling (FIX-20260709-006), and
        min_step debounce (retcode 10025).
        Gated off when min_rr_ratio == 0 (structural/legacy zero-change) and
        post-breakeven (SL already past entry — risk already locked).
        """
        tp = self._resolve_policy(pos)
        # Gate must mirror compute_trail_tp's tightening trigger (atr_ratio <= 0.80)
        # exactly — at the boundary the TP tightens, so the SL must too, or the
        # symmetric coupling (TECH_DEBT-019 §2) silently breaks at atr_ratio == 0.80.
        if pos.entry_atr <= 0 or not (0 < atr_ratio <= 0.80):
            return None
        if getattr(tp, "tp_min_rr_ratio", 0.0) <= 0:
            return None
        _geom_atr = self._resolve_geometry_atr(pos)

        if pos.side == "long":
            if pos.current_sl >= pos.entry_price:
                return None  # post-breakeven — risk already locked
            sl_dist = pos.entry_price - pos.current_sl
            target = pos.entry_price - sl_dist * atr_ratio
            _ratchet_r = self._ratchet_lock_r(pos, tp)
            if _ratchet_r is not None:
                pos.ratchet_floor_r = _ratchet_r
                target = max(target, pos.entry_price + _ratchet_r * pos.entry_atr)
            if tp.max_lock_atr > 0 and _geom_atr > 0:
                target = min(target, pos.entry_price + tp.max_lock_atr * _geom_atr)
            if target <= pos.current_sl + tp.min_step:
                return None
            return round(target, 3)
        elif pos.side == "short":
            if pos.current_sl <= pos.entry_price:
                return None  # post-breakeven — risk already locked
            sl_dist = pos.current_sl - pos.entry_price
            target = pos.entry_price + sl_dist * atr_ratio
            _ratchet_r = self._ratchet_lock_r(pos, tp)
            if _ratchet_r is not None:
                pos.ratchet_floor_r = _ratchet_r
                target = min(target, pos.entry_price - _ratchet_r * pos.entry_atr)
            if tp.max_lock_atr > 0 and _geom_atr > 0:
                target = max(target, pos.entry_price - tp.max_lock_atr * _geom_atr)
            if target >= pos.current_sl - tp.min_step:
                return None
            return round(target, 3)
        return None

    def should_breakeven(
        self,
        pos: ActivePosition,
        current_atr: float,
        breakeven_threshold_mult_override: float | None = None,
    ) -> bool:  # current_atr vestigial post PER_TF migration
        """Return True when the favorable move exceeds the breakeven threshold.

        breakeven_threshold_mult_override: pre-close tightening factor (< 1.0).
        Multiplied into the threshold to trigger breakeven earlier as the
        market close approaches.  None → no override.

        FIX-20260603-064: activation watermark — breakeven is suppressed until
        unrealized profit exceeds trail_activation_atr × entry_atr.  Prevents
        premature breakeven lock on positions that never had breathing room.
        """
        if pos.breakeven_triggered:
            return False
        tp = self._resolve_policy(pos)
        _geom_atr = self._resolve_geometry_atr(pos)

        # ── Activation watermark check ──
        # DQAF-20260722-003: Use entry_atr, consistent with trail activation.
        _activation_atr = pos.entry_atr if pos.entry_atr > 0 else _geom_atr
        if tp.trail_activation_atr > 0 and _activation_atr > 0:
            if pos.side == "long":
                _unrealized_r = (pos.highest_high - pos.entry_price) / _activation_atr
            else:
                _unrealized_r = (pos.entry_price - pos.lowest_low) / _activation_atr
            if _unrealized_r < tp.trail_activation_atr:
                return False  # not enough profit yet — keep breakeven suppressed

        threshold = tp.breakeven_threshold_atr * _geom_atr  # PER_TF: bracket_atr units
        # ── Pre-close tightening: lower threshold → earlier BE trigger ──
        if breakeven_threshold_mult_override is not None:
            threshold *= breakeven_threshold_mult_override
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

    @staticmethod
    def _resolve_geometry_atr(pos: ActivePosition) -> float:
        """Return the ATR that governs exit geometry (trail/breakeven/lock).

        PER_TF_ATR_HALF_MIGRATION: geometry uses ``bracket_atr`` (per-TF
        bracket-sizing ATR, FIX-20260709-004) instead of ``entry_atr``
        (M5-scale).  Falls back to entry_atr for positions created before
        bracket_atr was introduced so the engine never divides by zero.
        """
        batr = getattr(pos, "bracket_atr", 0.0) or 0.0
        return batr if batr > 0 else pos.entry_atr

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

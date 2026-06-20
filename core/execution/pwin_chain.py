"""p_win computation chain — pure functions extracted under Golden Master safety net.

S3 — Functional Core路线: 将开单闸门的核心计算逻辑提取为纯函数。
纯函数 = 无I/O、无全局状态、相同输入永远产生相同输出。

Extracted functions:
  - resolve_p_win_from_brains(): 从大脑PnL历史计算动态p_win
  - adjust_p_win_for_regime(): 根据趋势方向调整OU策略的p_win

Both were previously scattered across kelly_sizer.py and strategy_line.py.
Consolidated here for:
  - Property-based testing (Hypothesis) on boundary conditions
  - Golden Master replay verification
  - Single point of change for p_win logic

Related FIXes: 018-032, 026-030, 026-031, 026-032, 026-035
KI-004 RESOLVED (2026-06-12): All 3 fallback paths now emit structured
  warning logs. BLE001 at get_metrics() replaced with fail_open_guard.
  Callers should propagate p_win_degraded=True to the journal.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Any

from core.runtime.fault_handler import fail_open_guard

logger = logging.getLogger(__name__)

# ── resolve_p_win_from_brains ──────────────────────────────────────────────


def resolve_p_win_from_brains(
    brains: list[Any],
    pnl_store: Any | None,
    direction: str = "long",
) -> float:
    """Resolve dynamic p_win for a strategy that does NOT use MetaFilter.

    Uses rolling 100-trade win rate from BrainPnLStore (FIX-20260526-032:
    window=100 explicitly passed to avoid all-time aggregation bias).
    Requires at least 10 settled trades before trusting the win rate.

    Falls back to 0.40 (Fail-Closed — FIX-20260526-031) when data is
    insufficient.  With min_p_win=0.45 (statarb) or 0.50 (barrier),
    0.40 < both → trades rejected when system lacks evidence.

    Trap 3 fix: Static historical win rate is a fixed multiplier in disguise.
    Rolling PnL win rate is dynamic and reflects current model performance.
    Alpha decays, regimes shift — all-time WR drags stale history into today.

    Args:
        brains: List of brain info dicts with "brain_id" key.
        pnl_store: BrainPnLStore instance or None.
        direction: "long" or "short" — for directional win rate lookup.
    """
    if pnl_store is None:
        logger.warning(
            "[pwin_chain:FALLBACK_PATH_1] pnl_store is None — "
            "returning fail-closed 0.40. Brain PnL metrics unavailable; "
            "rolling win rate cannot be computed. p_win_source will be 'rolling_wr' "
            "with p_win_degraded=True."
        )
        return 0.40

    valid_rates: list[float] = []
    for b in brains:
        brain_id = b.get("brain_id") if isinstance(b, dict) else getattr(b, "brain_id", None)
        if not brain_id:
            continue
        try:
            m = pnl_store.get_metrics(str(brain_id), window=100)
        except Exception:  # BLE001:FOG
            with fail_open_guard("pwin_chain:resolve_p_win_from_brains"):
                with fail_open_guard("PWinMetricsResolver"):
                    raise
                continue
        if m is None:
            continue
        sc = getattr(m, "sample_count", 0)
        if sc < 10:
            continue
        wr = getattr(m, "win_rate", 0.0)
        if wr > 0:
            valid_rates.append(float(wr))

    if valid_rates:
        _median = float(statistics.median(valid_rates))
        logger.debug(
            "[pwin_chain:OK] median win rate=%.4f from %d brains",
            _median,
            len(valid_rates),
        )
        return _median

    # ── FALLBACK PATH 3: no brain produced a valid win rate ──
    # Distinguish: (a) no brains registered vs (b) all brains below threshold
    _brain_count = len(brains)
    if _brain_count == 0:
        logger.warning(
            "[pwin_chain:FALLBACK_PATH_3a] No brains provided — "
            "returning fail-closed 0.40. p_win_degraded=True."
        )
    else:
        logger.warning(
            "[pwin_chain:FALLBACK_PATH_3b] %d brain(s) checked, none produced "
            "a valid win rate (sample_count < 10, win_rate <= 0, or "
            "brain_id not found in PnL store). Returning fail-closed 0.40. "
            "p_win_degraded=True.",
            _brain_count,
        )
    return 0.40


# ── _adjust_p_win_for_regime ────────────────────────────────────────────────


def adjust_p_win_for_regime(
    p_win: float,
    strategy_name: str,
    regime_info: dict[str, object] | None,
    entry_z_score: float | None,
    trade_direction: str = "neutral",
) -> float:
    """Dynamically adjust p_win for OU strategies based on trend strength.

    FIX-20260526-030: Historical p_win (rolling 100-trade WR ~0.49) is a static
    average applied uniformly to all trades.  In trending regimes, high |z_score|
    means momentum ignition (price is trending away from mean), NOT a mean-reversion
    setup.  The model's "confidence" (z_score depth) is actually ANTI-informative
    in trends — the more confident the brain, the worse the outcome.

    FIX-20260526-035: Direction-aware asymmetric penalty.  With-trend pullbacks
    ("千金难买牛回头") are the highest-quality OU setups — the trend is your
    friend, not a risk factor.  Counter-trend signals receive the full penalty.

    This function inversely maps z_score → p_win discount when ADX indicates
    trending conditions.  Hard floor at 65% of original p_win prevents the
    adjustment from ever being the sole veto (that's the p_win gate's job).

    Non-OU strategies and non-trending regimes pass through unchanged.
    """
    if "statarb" not in strategy_name or not regime_info or entry_z_score is None:
        return p_win

    _rg: dict[str, Any] = {}
    if isinstance(regime_info, dict):
        _maybe_rg = regime_info.get("regime_gate")
        if isinstance(_maybe_rg, dict):
            _rg = _maybe_rg

    _h1_adx = float(_rg.get("h1_adx") or 0.0)

    if _h1_adx < 15.0:
        return p_win

    # ── Direction-aware bypass: with-trend pullbacks are Alpha, not risk ──
    _primary_dir = str(_rg.get("primary_trend") or "neutral")
    _h1_dir = str(_rg.get("h1_trend_direction") or "neutral")
    _ref_dir = _primary_dir if _primary_dir != "neutral" else _h1_dir

    if trade_direction != "neutral" and _ref_dir != "neutral":
        if trade_direction == _ref_dir:
            return p_win  # 千金难买牛回头 — no penalty for with-trend pullbacks

    abs_z = abs(entry_z_score)
    if abs_z < 0.8:
        return p_win

    # ── Penalty: higher |z_score| → stronger penalty ──
    _penalty = min((abs_z - 0.8) * 0.35, 0.40)
    _adjusted = p_win * (1.0 - _penalty)
    return max(_adjusted, p_win * 0.65)


# ── adjust_p_win_for_z_strength ─────────────────────────────────────────────


def adjust_p_win_for_z_strength(
    p_win: float,
    strategy_name: str,
    entry_z_score: float,
    *,
    z_threshold: float = 1.0,
    penalty_max: float = 0.15,
    steepness: float = 8.0,
) -> float:
    """Weak-Z penalty for mean-reversion strategies.

    DQAF-20260608-003 sub-finding: meta_filter can produce high p_win (>0.60)
    even when |z| < 1.0 — the neutral zone where OU reversion force is absent.
    The existing pwin_chain trend penalty only addresses HIGH-|z| risk (momentum
    ignition in trending regimes) but leaves LOW-|z| signals unprotected.

    This function applies a continuous multiplicative penalty on p_win when |z|
    falls below the threshold.  The penalty uses a sigmoid for smooth transition
    — no binary cliff.  MetaFilter retains final authority: sufficiently
    confident signals (raw p_win > 0.55 / (1 - penalty)) can still pass.

    Calibration (z_threshold=1.0, penalty_max=0.15, steepness=8.0):
      |z| = 0.00 → ~15.0% penalty   (p_win × 0.85)
      |z| = 0.50 → ~14.7% penalty   (strong penalty — no reversion force)
      |z| = 0.81 → ~12.4% penalty   (Trade 1: 0.6229 → 0.546 → REJECTED)
      |z| = 0.95 →  ~8.9% penalty   (marginal — approaching neutral boundary)
      |z| = 1.00 →  ~7.5% penalty   (transition midpoint)
      |z| = 1.05 →  ~6.0% penalty   (rapidly decaying above threshold)
      |z| = 1.50 →  ~1.4% penalty   (essentially no penalty)

    Non-statarb strategies pass through unchanged.
    """
    if "statarb" not in strategy_name:
        return p_win

    abs_z = abs(entry_z_score)
    # Sigmoid: high penalty at low |z|, decays to zero above z_threshold
    # penalty_factor ∈ (0, penalty_max]
    penalty_factor = penalty_max / (1.0 + math.exp(steepness * (abs_z - z_threshold)))
    return p_win * (1.0 - penalty_factor)


# ── PWinResolution — p_win resolution chain result ────────────────────────────
# FIX-20260620-017: Extracted from strategy_line.evaluate() lines 1067-1181.


@dataclass
class PWinResolution:
    """Result of the 7-step p_win resolution chain.

    Encapsulates all state mutations produced by the p_win backoff chain:
    cold_explore → meta_filter → rolling_wr → brain_confidence →
    meta_filter_absent → ucb_elastic_floor → regime/z adjustments →
    degraded detection.
    """

    p_win: float
    p_win_source: str
    p_win_degraded: bool
    meta_filter_absent: bool
    meta_absent_floor: float


def resolve_p_win(
    *,
    is_cold_explore: bool,
    meta_p_win: float | None,
    pnl_store: Any | None,
    brains: list[Any],
    direction: str,
    confidence: float,
    strategy_name: str,
    meta_filter: Any | None,
    min_p_win: float,
    regime_info: dict[str, Any] | None,
    entry_z_score: float,
) -> PWinResolution:
    """Run the 7-step p_win resolution chain for Tier 2 Kelly sizing.

    Resolution order (first available source wins):
      1. Cold explore → 0.50 (bounded-volume data collection)
      2. MetaFilter → Platt-calibrated P(TP|signal)
      3. Rolling WR → median win rate from PnL store (≥10 settled trades)
      4. Brain confidence → monotonic fallback (0.40 + confidence × 0.20)
      5. MetaFilter absent → elevated floor (max(min_p_win, 0.50)), disables UCB
      6. UCB elastic floor → confidence-based lift for rolling_wr
      7. Regime + Z-strength adjustments → trend/weak-Z penalties

    Returns a :class:`PWinResolution` with the final p_win, its source,
    degradation flag, and MetaFilter-absent state for downstream gate logic.

    Args:
        is_cold_explore: True when MetaFilter hasn't produced p_win yet and
            confidence ≥ 0.35.  Forces p_win=0.50 for bounded exploration.
        meta_p_win: Platt-calibrated P(TP|signal) from MetaFilter (or None).
        pnl_store: BrainPnLStore for rolling WR resolution (or None).
        brains: List of brain info dicts with ``brain_id``.
        direction: ``"long"`` / ``"short"`` / ``"neutral"``.
        confidence: Consensus confidence [0.0, 1.0].
        strategy_name: Strategy line name (e.g. ``"statarb_dynamic"``).
        meta_filter: MetaSignalFilter instance (or None).
        min_p_win: ``config.min_p_win`` — hard floor for position sizing.
        regime_info: Regime gate output dict (or None).
        entry_z_score: OU Z-score at entry (or 0.0).

    Returns:
        PWinResolution with all resolved fields.
    """
    p_win: float = 0.5
    p_win_source: str = "neutral_default"
    meta_filter_absent: bool = False
    meta_absent_floor: float = 0.50

    # ── Step 1: Cold explore → forced neutral for data collection ──
    if is_cold_explore:
        # FIX-20260610-007-C: Cold explore bypasses all p_win resolution.
        # Force p_win=0.50 (Kelly mult=1.0) for bounded-volume data collection.
        p_win = 0.50
        p_win_source = "cold_explore_neutral"
    elif meta_p_win is not None:
        # ── Step 2: MetaFilter — Platt-calibrated P(TP|signal) ──
        p_win = meta_p_win
        p_win_source = "meta_filter"
    elif pnl_store is not None:
        # ── Step 3: Rolling WR from PnL ledger ──
        p_win = resolve_p_win_from_brains(brains, pnl_store, direction)
        p_win_source = "rolling_wr"

    # ── Step 4: Brain confidence → p_win monotonic fallback ──
    # FIX-20260531-015: Tier-3 fallback when MetaFilter unavailable AND
    # PnLStore has < 10 trades → resolve_p_win_from_brains returns 0.40.
    # Override with brain confidence mapping to break chicken-and-egg deadlock.
    # confidence ∈ [0.35, 1.0] → p_win ∈ [0.47, 0.60] — bounded.
    _is_fail_closed = p_win_source == "neutral_default" or (
        p_win_source == "rolling_wr" and p_win <= 0.40
    )
    if _is_fail_closed:
        _conf = max(0.0, min(1.0, confidence))
        p_win = 0.40 + _conf * 0.20
        p_win_source = "brain_confidence"

    # ── Step 5: MetaFilter absent — hard floor defense ──
    # FIX-20260609-002-UPDATE: When MetaFilter is unavailable (model file
    # missing, cross-symbol blind spot), p_win falls back to rolling_wr —
    # an uncalibrated historical average.  Without Platt calibration, the
    # elastic UCB floor and COLD exploration are untrustworthy.
    # Defense: enforce elevated floor (coin-flip minimum) + disable elastic UCB.
    if meta_filter is None and p_win_source in ("rolling_wr", "neutral_default"):
        meta_filter_absent = True
        # Log warning — silent degradation is the real enemy
        _lg = logging.getLogger(__name__)
        _lg.warning(
            "[STRATEGY_DEGRADE] %s running WITHOUT MetaFilter! "
            "p_win=%.3f from %s. MetaFilter model file may be missing or "
            "strategy not routed. Falling back to hard floor defense.",
            strategy_name,
            p_win,
            p_win_source,
        )
        meta_absent_floor = max(min_p_win, 0.50)
        p_win_source = "rolling_wr_no_metafilter"

    # ── Step 6: UCB elastic floor ──
    # FIX-20260606-139: Only active when MetaFilter is available.
    # When rolling_wr is between 0.40 and min_p_win, lift it with a
    # confidence-based bonus to avoid unnecessarily blocking trades.
    if not meta_filter_absent:
        _elastic_trigger = (
            p_win_source == "rolling_wr" and 0.40 < p_win < min_p_win
        )
        if _elastic_trigger:
            _conf = max(0.0, min(1.0, confidence))
            _elastic_p_win = min_p_win - 0.05 + _conf * 0.10
            p_win = max(p_win, _elastic_p_win)
            p_win_source = "ucb_elastic_floor"

    # ── Step 7a: Regime adjustment for OU strategies ──
    # FIX-20260526-030: In trending regimes, high |z_score| is momentum
    # ignition, not mean reversion.  Discount p_win to prevent Kelly from
    # sizing into anti-informative high-confidence OU signals.
    p_win = adjust_p_win_for_regime(
        p_win, strategy_name, regime_info, entry_z_score, direction
    )

    # ── Step 7b: Weak-Z penalty for OU strategies ──
    # DQAF-20260608-003: When |z| < 1.0, the OU reversion force is absent.
    # Continuous sigmoid penalty — no binary cliff.
    p_win = adjust_p_win_for_z_strength(p_win, strategy_name, entry_z_score)

    # ── Degraded detection ──
    # DQAF-20260612-004: Track whether p_win is backed by real statistical
    # evidence or is a fallback estimate.
    # REAL: meta_filter, cold_explore_neutral (intentional), rolling_wr > 0.40
    # DEGRADED: neutral_default, brain_confidence, rolling_wr ≤ 0.40
    p_win_degraded = p_win_source in (
        "neutral_default",
        "brain_confidence",
    ) or (
        p_win_source in ("rolling_wr", "rolling_wr_no_metafilter") and p_win <= 0.40
    )

    return PWinResolution(
        p_win=round(p_win, 4),
        p_win_source=p_win_source,
        p_win_degraded=p_win_degraded,
        meta_filter_absent=meta_filter_absent,
        meta_absent_floor=meta_absent_floor,
    )

"""Strategy line — independent trading logic for one contract group.

Each strategy line represents a self-contained trading approach with its own:
  - Set of brains (trained on the same contract)
  - Group consensus computation (contract-homogeneous voting)
  - Dynamic SL/TP parameters
  - Exit management rules
  - Risk budget

Strategy lines operate INDEPENDENTLY — they do not cross-check or block each
other.  That responsibility belongs to the PortfolioRiskController.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

# ── Extracted sub-modules (Strangler Fig #11-18) ──
# Circular imports RESOLVED: meta_filter_routing + trend_isolation_gates
# now import StrategyDecision from strategy_decision.py (leaf module).
from core.execution.brain_gates import check_min_valid_brains, extract_entry_z_score
from core.execution.conformal_ou_gate import apply_conformal_ou_gate
from core.execution.dynamic_sl_tp import compute_dynamic_sl_tp, compute_sl_tp_levels
from core.execution.meta_filter_routing import apply_meta_filter_gate
from core.execution.ofi_gate import apply_ofi_toxicity_gate
from core.execution.pwin_chain import resolve_p_win
from core.execution.strategy_decision import StrategyDecision
from core.execution.trend_isolation_gates import apply_trend_isolation_gates
from core.execution.trend_volume_guard import check_minimum_rr, compute_counter_trend_volume_mult
from core.runtime.fault_handler import FaultLevel, FaultTolerantContext, fail_open_guard
from core.runtime.shadow_recorder import record_brain_votes

logger = logging.getLogger(__name__)

# ── Bandit sizing constants (v3.1) ──

SIGMOID_Z_MID = 1.75  # Z-score midpoint for sigmoid
SIGMOID_K = 4.0  # sigmoid steepness
MVS_THRESHOLD = 0.20  # minimum viable size — effective_mult below this → 0.0


def sigmoid_exhaustion(
    abs_z_score: float,
    z_mid: float = SIGMOID_Z_MID,
    k: float = SIGMOID_K,
) -> float:
    """Sigmoid convex mapping: |Z| → exhaustion factor [0, 1].

    Small probe at |Z| ~ 1.0 (5%), full position at |Z| ~ 2.5 (95%).
    Convexity toward tail ensures capital is deployed where edge is strongest.
    """
    return 1.0 / (1.0 + math.exp(-k * (abs_z_score - z_mid)))


def apply_mvs(effective_mult: float, threshold: float = MVS_THRESHOLD) -> float:
    """Minimum Viable Size: kill micro-positions where fixed costs eat EV.

    effective_mult below threshold → 0.0 (don't trade fly-leg sizes).
    """
    return 0.0 if effective_mult < threshold else effective_mult


def z_depth_penalty(
    abs_z: float,
    z_entry: float = 1.5,
    strength: float = 0.3,
) -> float:
    """v3.2: Dynamic decay for deep Z excursions — volatility parity.

    Deeper |Z| at entry → higher risk of secondary reversion failure.
    Penalty scales position size automatically for extreme excursions,
    removing the need for hard-coded session boundaries.

    |Z| = 1.5 → 1.00x    (baseline, no penalty)
    |Z| = 2.5 → 0.77x    (deep, moderate penalty)
    |Z| = 3.5 → 0.62x    (extreme, significant penalty)
    """
    if abs_z <= z_entry:
        return 1.0
    return 1.0 / (1.0 + strength * (abs_z - z_entry))


def trend_maturity_discount(
    *,
    hurst: float | None = None,
    trend_strength: float = 0.5,
    strategy_family: str = "trend_following",
) -> float:
    """Trend maturity discount: reduce size when persistence is fading.

    FIX-20260607-007: Wire Kalman velocity decay + Hurst persistence loss
    into position sizing.  Only applies to trend-following / swing strategies
    — mean-reversion and statarb have their own sizing (sigmoid + OU regime).

    Two independent signals:
      Hurst → 0.5 (random walk): trend structure deteriorating
      Kalman strength < 0.5: velocity losing conviction vs noise

    H=0.60 → 1.00x    (strong persistence)
    H=0.55 → 0.85x    (weakening)
    H=0.50 → 0.55x    (random walk — trend may be over)
    H=0.45 → 0.40x    (anti-persistent — floor)

    Kalman strength < 0.5 → proportional discount (strength / 0.5)

    Combined: multiplicative, floor 0.40.
    """
    if strategy_family not in ("trend_following", "swing"):
        return 1.0

    discount = 1.0

    # ── Hurst persistence decay ──
    if hurst is not None and hurst > 0:
        # Hurst in [0.45, 0.60] → discount in [0.40, 1.00]
        _h_clipped = max(0.40, min(0.65, hurst))
        hurst_discount = max(0.40, 1.0 - max(0, (0.60 - _h_clipped) * 3.0))
        discount *= hurst_discount

    # ── Kalman strength decay ──
    if trend_strength < 0.50:
        # Below 0.50 → proportional discount (strength=0.30 → 0.60x)
        strength_discount = max(0.50, trend_strength / 0.50)
        discount *= strength_discount

    return max(0.40, min(1.0, discount))


def check_z_inflection(
    current_z: float,
    prev_z: float | None,
    direction: str,
    z_entry: float = 1.5,
) -> tuple[bool, str]:
    """v3.2: Z-score inflection gate — avoid catching falling knives.

    For long (oversold, z < -z_entry): require z increasing (z > prev_z).
    For short (overbought, z > z_entry): require z decreasing (z < prev_z).

    If prev_z is None (first cycle), passes by default.

    Returns (should_allow, reason).
    """
    if prev_z is None:
        return True, "first_cycle_no_prev_z"

    if direction == "long":
        if current_z >= prev_z:
            return True, "inflection_long_turning"
        else:
            return False, f"inflection_blocked_long_z_still_falling_{current_z:.3f}_lt_{prev_z:.3f}"
    elif direction == "short":
        if current_z <= prev_z:
            return True, "inflection_short_turning"
        else:
            return False, f"inflection_blocked_short_z_still_rising_{current_z:.3f}_gt_{prev_z:.3f}"
    else:
        return True, "neutral_no_check"


# StrategyDecision extracted to core/execution/strategy_decision.py (Strangler Fig #18)


@dataclass
class StrategyLineConfig:
    """Immutable configuration for one strategy line."""

    name: str
    magic: int
    brain_types: set[str]
    base_dir: str  # FIX-20260615-006/C1: required — no default, caller MUST provide
    symbol: str = "XAUUSDc"  # FIX-20260531-008: config-driven, not hardcoded
    strategy_family: str = ""  # Phase 4: "mean_reversion" | "trend_following" | "" (auto-infer)
    base_volume: float = 0.01
    max_volume: float = 0.05

    # Dynamic SL/TP
    base_sl_atr_mult: float = 2.0
    base_tp_atr_mult: float = 3.5
    hard_sl_ratio: float = 1.5
    ref_atr: float = 5.0

    # Confidence
    confidence_threshold: float = 0.40

    # Hard p_win floor — intercepts signals without statistical advantage before Kelly sizing.
    # Mean-reversion (statarb/OU) strategies need ≥0.50; trend strategies can use 0.48.
    # Set per-strategy via live.yaml entry.* block, or 0.0 to disable.
    min_p_win: float = 0.50

    # Volume regime factors
    regime_vol_mult_low: float = 1.20
    regime_vol_mult_normal: float = 1.00
    regime_vol_mult_high: float = 0.70

    # Direction balance — counteracts systemic LONG bias in brain training data
    # 0.0 = no adjustment, 0.05 = mild (5% LONG penalty), 0.10 = moderate
    long_bias_discount: float = 0.0

    # Per-strategy exit overrides (wired from live.yaml exit.* block)
    exit_flip_enabled: bool = True
    exit_time_cycles: int | None = None  # None → use brain JSON training_horizon / max_hold_cycles
    exit_hesitation_cycles: int = 0  # M5-bar scaled — breakeven-not-reached timeout
    exit_zscore_enabled: bool = False  # OU mean-reversion exit gate
    exit_min_r: float = 0.3  # minimum R to hold during time-decay phases

    # Absolute SL/TP floors (in price units, e.g. 0.80 = 8 pips on XAUUSD)
    min_sl_distance: float = 0.0  # 0.0 = disabled
    min_rr_ratio: float = 0.0  # 0.0 = disabled; e.g. 1.5 maintains min 1.5:1 RR

    # Spread cost alignment with training labels (FIX-20260529-030)
    # Default 0.0 preserves backward compat. Set to 30 (XAUUSDc typical)
    # once label_contract.py price basis (mid vs bid/ask) is confirmed.
    spread_points: float = 0.0
    tick_size: float = 0.01  # MT5 SYMBOL_TRADE_TICK_SIZE

    # Per-strategy max allowed spread before trade is blocked (FIX-20260529-038)
    # 0.0 = disabled. e.g. 50 = block when current spread >= 50 points.
    # Physical cost gate — replaces hardcoded time-of-day / day-of-week filters.
    # H22 rollover spread spike → naturally blocked. H12 lunch dead-zone → naturally blocked.
    max_spread_points: float = 0.0

    # Timeframe for auto-scaling (M5/M15/M30/H1/H4/D1)
    timeframe: str = "M5"

    _TIMEFRAME_TO_M5: dict[str, int] = field(
        default_factory=lambda: {
            "M5": 1,
            "M15": 3,
            "M30": 6,
            "H1": 12,
            "H4": 48,
            "D1": 288,
        }
    )

    @property
    def timeframe_mult(self) -> int:
        """M5-bar multiplier derived from timeframe label (e.g. H1→12)."""
        return self._TIMEFRAME_TO_M5.get(self.timeframe, 1)

    # Lot granularity
    lot_step: float = 0.01

    # Minimum number of brains that must produce valid (non-neutral) proposals
    # before the strategy line can generate an entry signal.  Prevents
    # single-brain decision-making on multi-brain strategy lines.
    # Default 1 (least restrictive); deployment config in live.yaml sets
    # higher values per strategy line (e.g. 3 for barrier_12bar).
    min_valid_brains: int = 1

    # Meta-probe specs — regression brains serving as directional probes for
    # the Meta Pipeline (Track 2).  Declared per-strategy in live.yaml or
    # auto-discovered from brain JSON ``"roles": ["meta_probe"]``.
    meta_probe_specs: list[Any] = field(default_factory=list)

    # Budget
    daily_loss_limit_pct: float = -0.03
    max_consecutive_losses: int = 5

    # FIX-20260531-008: contract_size from ASSET_REGISTRY (Defense 1)
    contract_size: float = 100.0  # overridden per symbol via builder

    def __post_init__(self) -> None:
        """Architectural Defense 2: fail-fast on unregistered or mismatched assets."""
        from core.config.asset_registry import ASSET_REGISTRY

        if self.symbol and self.symbol in ASSET_REGISTRY:
            expected_cs = ASSET_REGISTRY[self.symbol].contract_size
            if self.contract_size != expected_cs:
                raise ValueError(
                    f"StrategyLineConfig [{self.name}]: contract_size mismatch "
                    f"for '{self.symbol}' (got {self.contract_size}, expected {expected_cs})"
                )


# ── Strategy line base class ────────────────────────────────────────────


class StrategyLine:
    """Base class for contract-group strategy lines.

    Subclasses implement ``_run_inference()`` to produce a list of proposals
    (BrainDecisionProposal objects) from their specific brains and features.
    """

    def __init__(
        self,
        config: StrategyLineConfig,
        brains: list[dict[str, Any]],
        *,
        budget: Any = None,
    ):
        self.config = config
        self.brains = brains  # brain registry entries for this strategy
        self.budget = budget
        self._last_entry_z: float | None = None  # v3.2: previous cycle z-score for inflection gate

        # ── FIX-20260601-039: TF close buffer for OU/Hurst computation ──
        # Shared by SwingStrategy and BarrierStrategy (and any future strategy
        # that uses swing_enhanced_* feature schemas).  Extracted from
        # SwingStrategy to avoid copy-paste in BarrierStrategy.
        from collections import deque

        self._TF_CLOSE_BUFFER_SIZE: int = 60
        self._tf_close_buffer: deque[float] = deque(maxlen=self._TF_CLOSE_BUFFER_SIZE)

    # ── TF OU/Hurst computation (shared by swing + barrier strategies) ──

    def _compute_tf_ou_theta(self, lookback: int = 20) -> float:
        """Estimate OU mean-reversion speed (theta) from recent closes."""
        buf = list(self._tf_close_buffer)
        if len(buf) < max(5, lookback):
            return 0.0
        try:
            series = np.array(buf[-lookback:], dtype=np.float64)
            log_p = np.log(series)
            dx = np.diff(log_p)
            if len(dx) < 2:
                return 0.0
            mu = np.mean(dx)
            x_prev = log_p[:-1] - np.mean(log_p[:-1])
            if np.sum(x_prev**2) < 1e-12:
                return 0.0
            theta_hat: float = float(-np.sum(x_prev * (dx - mu)) / np.sum(x_prev**2))
            if not math.isfinite(theta_hat):
                return 0.0
            return max(0.0, min(10.0, float(theta_hat)))
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:_compute_tf_ou_theta"):
                return 0.0

    def _compute_tf_hurst(self, max_lag: int = 20) -> float:
        """Estimate Hurst exponent from recent close buffer (R/S method)."""
        buf = list(self._tf_close_buffer)
        if len(buf) < max(8, max_lag):
            return 0.5
        try:
            series = np.array(buf[-max_lag:], dtype=np.float64)
            log_p = np.log(series)
            rets = np.diff(log_p)
            n = len(rets)
            if n < 8:
                return 0.5
            lags = [max(2, int(n / 2**k)) for k in range(4) if int(n / 2**k) >= 4]
            if len(lags) < 2:
                return 0.5
            rs_vals: list[float] = []
            for lag in lags:
                segments = n // lag
                if segments < 1:
                    continue
                rs_list = []
                for s in range(segments):
                    chunk = rets[s * lag : (s + 1) * lag]
                    mean_r = np.mean(chunk)
                    cum_dev = np.cumsum(chunk - mean_r)
                    r_val = max(cum_dev) - min(cum_dev)
                    s_val = np.std(chunk, ddof=1)
                    if s_val > 1e-12:
                        rs_list.append(r_val / s_val)
                if rs_list:
                    rs_vals.append(float(np.mean(rs_list)))
            if len(rs_vals) < 2:
                return 0.5
            log_lags = np.log(lags[: len(rs_vals)])
            log_rs = np.log(rs_vals)
            slope = np.polyfit(log_lags, log_rs, 1)[0]
            if not math.isfinite(slope):
                return 0.5
            return max(0.1, min(1.0, float(slope)))
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:_compute_tf_hurst"):
                return 0.5

    # ── Subclass overrides ──────────────────────────────────────────────

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
    ) -> list[Any]:
        """Run brain inference for this strategy's brains.

        Subclasses override this to route the correct feature vector to each
        brain type.  Returns a list of BrainDecisionProposal objects.

        Args:
            micro_sequences: optional dict mapping TF → (32,9) ndarray for
                             HMRE brains that need per-resolution sequences.
            daily_feature_vector: optional (24,) ndarray for D1 swing brains.
        """
        raise NotImplementedError

    # Decision factory (FIX-20260620-014)
    # _make_decision() replaces 19 verbose StrategyDecision(...) calls.
    # All parameters are explicit (no **kwargs) for mypy type safety.

    def _make_decision(
        self,
        *,
        should_trade: bool = False,
        direction: str = "neutral",
        confidence: float = 0.0,
        volume: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        hard_sl: float = 0.0,
        reason: str = "",
        brain_ids: list[str] | None = None,
        brain_votes: list[dict[str, Any]] | None = None,
        supporting_count: int = 0,
        total_count: int = 0,
        regime_mode: str = "full",
        venue: str = "live",
        entry_z_score: float = 0.0,
        entry_half_life: float = 0.0,
        entry_context: dict[str, Any] | None = None,
        p_win: float = 0.5,
        p_win_source: str = "unknown",
        p_win_degraded: bool = False,
        kelly_mult: float = 1.0,
        cold_explore: bool = False,
        gate_diag: dict[str, Any] | None = None,
    ) -> StrategyDecision:
        """Create a StrategyDecision with config-derived name/magic defaults."""
        return StrategyDecision(
            strategy_name=self.config.name,
            magic=self.config.magic,
            should_trade=should_trade,
            direction=direction,
            confidence=confidence,
            volume=volume,
            sl=sl,
            tp=tp,
            hard_sl=hard_sl,
            reason=reason,
            brain_ids=brain_ids if brain_ids is not None else [],
            brain_votes=brain_votes if brain_votes is not None else [],
            supporting_count=supporting_count,
            total_count=total_count,
            regime_mode=regime_mode,
            venue=venue,
            entry_z_score=entry_z_score,
            entry_half_life=entry_half_life,
            entry_context=entry_context if entry_context is not None else {},
            p_win=p_win,
            p_win_source=p_win_source,
            p_win_degraded=p_win_degraded,
            kelly_mult=kelly_mult,
            cold_explore=cold_explore,
            gate_diag=gate_diag if gate_diag is not None else {},
        )

    # ── Main evaluation ─────────────────────────────────────────────────
    #
    # STRANGLER FIG TRIGGER (FIX-079): This function is 1293 lines with 8 logical phases:
    #   1. Regime gate  2. Spread gate  3. Budget  4. Brain inference (3a-3c)
    #   5. Group consensus + gates (4-4e)  6. Trend gates (4aa-4e)
    #   7. Dynamic SL/TP  8. Volume + Kelly
    #
    # WHEN ANY PHASE IS NEXT MODIFIED, extract that phase as a private method:
    #   Phase 3a-3c → _run_brain_inference()
    #   Phase 4-4e  → _apply_entry_gates()
    #   Phase 7     → _compute_sl_tp_levels()
    #   Phase 8     → _compute_position_size()
    #
    # Each extraction should be ~100 lines, tested by the new feature being added.
    # DO NOT extract all at once — one phase per modification, feature-driven.

    def evaluate(
        self,
        *,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        bid: float | None = None,
        ask: float | None = None,
        current_atr: float = 5.0,
        regime_info: dict[str, Any] | None = None,
        regime_gate_mode: str = "full",
        trend_direction: str = "neutral",
        trend_strength: float = 0.0,
        h4_trend_strength: float = 0.0,
        hurst: float | None = None,  # FIX-20260607-007: M5 Hurst for trend maturity
        kalman_velocity_bps: float | None = None,  # FIX-20260607-007: H1 Kalman velocity (bps)
        macro_regime: str = "mixed",
        risk_budget_usd: float = 0.0,
        tracker: Any = None,
        pnl_ledger: Any = None,
        pnl_store: Any = None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        meta_filter: Any = None,
        meta_filter_gate: Any = None,
        conformal_ou_gate: Any = None,
        micro_feature_dict: dict[str, float] | None = None,
        btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
        governance_state: dict[str, Any] | None = None,  # DQAF-20260622-059: LIVE-brain filter
    ) -> StrategyDecision:
        """Run the full strategy evaluation for one cycle.

        Args:
            trend_direction: Primary trend from multi-timeframe analysis
                             ("long"/"short"/"neutral").  Counter-trend trades
                             are blocked or penalised depending on strength.
            trend_strength: [0, 1] H1 trend strength.
            h4_trend_strength: [0, 1] H4 trend strength (gates barrier).
            macro_regime: "risk_on" | "risk_off" | "mixed" (from D1×H4).
            risk_budget_usd: Per-trade risk budget for vol-targeted sizing.
                             0 = use fixed base_volume.
            micro_sequences: optional dict TF → (32,9) ndarray for HMRE brains.
            meta_filter: Optional :class:`MetaSignalFilter` for Gate 4d ML check.

        Returns a StrategyDecision — may have should_trade=False.
        """
        name = self.config.name
        _meta_p_win: float | None = None  # P(TP|signal) — resolved by MetaFilter or downstream

        # ── DQAF-20260622-059 (P0-1): Build LIVE-brain filter from governance state ──
        # Only brains with status=="live" in governance_state are allowed to
        # contribute to the p_win calculation.  Retired/frozen/archived brains
        # carry stale or negative-alpha PnL data that contaminates the estimate.
        _live_brain_ids: set[str] | None = None
        if governance_state is not None:
            # ── DQAF-20260623-069: Iterate brain_states, NOT top-level ──
            # governance_state = {"brain_states": {bid: {status, ...}}, ...}
            # The old code iterated governance_state.items() (top-level keys
            # like "brain_states", "schema_version"), producing an empty set
            # every time.  This silently disabled DQAF-059 (governance gate)
            # AND DQAF-066 (governance cold-start fallback) since 2026-06-22.
            _gov_brains = (
                governance_state.get("brain_states", {})
                if isinstance(governance_state, dict)
                else {}
            )
            _live_brain_ids = {
                str(bid)
                for bid, b_info in _gov_brains.items()
                if isinstance(b_info, dict) and b_info.get("status") == "live"
            }
            if _live_brain_ids:
                logger.debug(
                    "[DQAF-059] %s: %d LIVE brain(s) from governance: %s",
                    name,
                    len(_live_brain_ids),
                    sorted(_live_brain_ids),
                )
            else:
                logger.warning(
                    "[DQAF-059] %s: governance_state loaded but ZERO LIVE brains found. "
                    "All p_win resolution will fall back to fail-closed 0.40.",
                    name,
                )

        # ── 1. Regime gate ──
        if regime_gate_mode == "off":
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                regime_mode="off",
                venue="live",
                reason="regime_gate_off",
            )

        # ── 1b. OU high-volatility gate (FIX-20260604-082) ──
        # Mean-reversion strategies are crushed in high-vol trending regimes.
        # When volatility is HIGH, OU signals are physically blocked regardless
        # of signal quality.  This gate prevents the "catching a falling knife"
        # death spiral that caused the previous WR decline.
        if "statarb" in name and regime_info:
            _rg = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
            _detected_regime = str(regime_info.get("regime", ""))
            if _detected_regime == "high":
                return self._make_decision(
                    should_trade=False,
                    direction="neutral",
                    confidence=0.0,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    regime_mode="high_vol_blocked",
                    venue="live",
                    reason=f"ou_high_vol_blocked:{_detected_regime}",
                )

        # ── 1c. Spread gate (FIX-20260529-038) ──
        # Physical cost gate: block when current spread exceeds per-strategy threshold.
        # Replaces hardcoded time-of-day / day-of-week filters.
        # H22 rollover spread spike → naturally blocked.  H12 lunch dead-zone → naturally blocked.
        if self.config.max_spread_points > 0 and bid is not None and ask is not None and ask > bid:
            _tick = self.config.tick_size if self.config.tick_size > 0 else 0.01
            _current_spread = (ask - bid) / _tick
            if _current_spread > self.config.max_spread_points:
                return self._make_decision(
                    should_trade=False,
                    direction="neutral",
                    confidence=0.0,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    regime_mode="spread_gate_blocked",
                    venue="live",
                    reason=f"spread_gate:{_current_spread:.1f}pts > {self.config.max_spread_points:.1f}pts",
                )

        # ── 2. Budget check ──
        if self.budget is not None and self.budget.check_pause():
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                regime_mode=regime_gate_mode,
                reason="budget_paused",
            )

        # ── 3. Run brain inference ──
        try:
            proposals = self._run_inference(
                feature_vector,
                micro_feature_vector,
                mid_price,
                micro_sequences,
                daily_feature_vector,
                btc_augment,  # FIX-20260613-052: resolved placeholder
            )
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:evaluate"):
                return self._make_decision(
                    should_trade=False,
                    direction="neutral",
                    confidence=0.0,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    regime_mode=regime_gate_mode,
                    reason="inference_error",
                )
        if not proposals:
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                regime_mode=regime_gate_mode,
                reason="no_proposals",
            )

        # ── 3a1. Huber BPS trapline (BEFORE any gate or consensus) ──
        # Log every raw BPS prediction from regression-probe brains for
        # distribution analysis.  This is the primary observability surface
        # for shadow-mode barrier_12bar — without it, threshold calibration
        # (0.75) is flying blind.
        for p in proposals:
            with fail_open_guard("StrategyLine:BrainIdResolve"):
                _brain_id = str(getattr(p, "brain_id", ""))
                # Match regression brains by training_contract in brain config
                _b_entry = next((b for b in self.brains if b.get("brain_id") == _brain_id), None)
                _contract = str(_b_entry.get("training_contract", "")) if _b_entry else ""
                _is_regression = _contract.startswith("barrier_12bar_regression")
                if _is_regression:
                    _raw = getattr(p, "raw_score", None)
                    if _raw is not None:
                        import json as _json

                        print(
                            _json.dumps(
                                {
                                    "event": "huber_bps_trapline",
                                    "time": datetime.now(UTC).isoformat().replace("+00:00", "Z")
                                    + "Z",
                                    "brain_id": _brain_id,
                                    "raw_bps": round(float(_raw), 6),
                                    "price": round(float(mid_price or 0), 2),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                pass  # BLE001 — migrated from blind pass

        # ── 3a2. Record counterfactual signals (BEFORE approval gates) ──
        # Counterfactual P&L must be recorded every cycle for every brain,
        # independent of whether the trade is later approved.  Per-proposal
        # try/except prevents one misbehaving brain from silencing others.
        if pnl_ledger is not None and mid_price is not None and mid_price > 0:
            _entry_spread = (
                float(ask - bid) if (bid is not None and ask is not None and ask > bid) else 0.0
            )
            for p in proposals:
                try:
                    pnl_ledger.record_signal(
                        brain_id=getattr(p, "brain_id", "unknown"),
                        symbol=self.config.symbol,
                        direction=p.direction
                        if hasattr(p, "direction")
                        else p.prediction.get("direction_bias", "neutral"),
                        entry_price=mid_price,
                        confidence=p.confidence
                        if hasattr(p, "confidence")
                        else p.prediction.get("confidence", 0.5),
                        entry_spread=_entry_spread,
                        entry_slippage=0.10,
                    )
                except Exception as _rec_exc:  # BLE001:FOG
                    with fail_open_guard("strategy_line:evaluate"):
                        import logging as _lg

                        _lg.getLogger(__name__).debug(
                            "PnL record_signal failed for brain=%s: %s",
                            getattr(p, "brain_id", "?"),
                            _rec_exc,
                            exc_info=True,
                        )
        # ── 3a3. Capture entry_z_score + entry_half_life from OU-style brains ──
        # Strangler Fig #17: uses extract_entry_z_score from brain_gates.py
        entry_z_score, entry_half_life = extract_entry_z_score(proposals)

        # ── 3b. Apply dynamic brain weights from real P&L metrics ──
        if tracker is not None:
            from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

            weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
            for b_info in self.brains:
                brain_id = str(b_info.get("brain_id", ""))
                if brain_id:
                    weighter.set_brain_metadata(
                        brain_id,
                        {
                            "contract_group": b_info.get("contract_group", ""),
                            "feature_schema": b_info.get("feature_schema", ""),
                        },
                    )
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="DynamicBrainWeighter:apply_weights",
            ):
                weighter.apply_weights(proposals)

        # ── 3c. Minimum valid brains gate ──
        # Strangler Fig #17: uses check_min_valid_brains from brain_gates.py
        _valid_voters = check_min_valid_brains(proposals, self.config.min_valid_brains)
        if _valid_voters > 0:
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                regime_mode=regime_gate_mode,
                reason=f"insufficient_voters_{_valid_voters}_lt_{self.config.min_valid_brains}",
            )

        # ── 4. Group consensus ──
        direction, confidence, brain_ids, support_count, total_count = self._compute_consensus(
            proposals
        )

        # ── 4a. Record per-brain votes with REAL consensus confidence ──
        # Recorded AFTER consensus so reported confidence matches what the
        # gate sees.  Runs for every cycle so individual brain behaviour
        # can be tracked regardless of whether the consensus passes gates.
        try:
            _status_map: dict[str, str] = {
                str(b.get("brain_id", "")): str(b.get("status", "unknown")) for b in self.brains
            }
            record_brain_votes(
                proposals=proposals,
                strategy_name=name,
                consensus_direction=direction,
                consensus_confidence=confidence,
                symbol=getattr(self.config, "symbol", "XAUUSDc"),
                base_dir=self.config.base_dir,
                brain_status_map=_status_map,
            )
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:evaluate"):
                logger.warning(
                    "Brain vote recording failed strategy=%s — audit trail incomplete",
                    name,
                )
        parliament_passed = (
            direction != "neutral" and confidence >= self.config.confidence_threshold
        )

        # ── Track 2: Meta Pipeline (Executive Veto) ──
        # ALWAYS runs for barrier_12bar regardless of parliament consensus.
        # When long-biased brains create a spurious LONG majority, the
        # Meta_Stage1_Huber_V1 probe (the only short-biased brain) must have
        # first-refusal to override parliament via the Stage 2 filter chain.
        # The veto is not unconditional — Huber must clear |raw_score|>0.30,
        # Stage 2 LGB+MLP+Platt+Conformal approval, RR check, and Kelly EV>0.
        #   Track 1 (Parliament) — group consensus with 0.45 threshold
        #   Track 2 (Meta Pipeline) — Huber probe → Stage 2 filter, executive veto
        meta_decision = None
        # max_volume > 0 gate: strategies with zero capital allocation
        # (shadow mode, base_volume=0) must not generate real trades through
        # ANY path — Parliament, Meta Pipeline, or otherwise.
        if meta_filter is not None and name == "barrier_12bar" and self.config.max_volume > 0:
            meta_decision = self._try_meta_pipeline(
                proposals=proposals,
                feature_vector=feature_vector,
                micro_feature_vector=micro_feature_vector,
                meta_filter=meta_filter,
                current_atr=current_atr,
                mid_price=mid_price,
                entry_z_score=entry_z_score,
                pnl_store=pnl_store,
                trend_direction=trend_direction,
                trend_strength=trend_strength,
                h4_trend_strength=h4_trend_strength,
                macro_regime=macro_regime,
                risk_budget_usd=risk_budget_usd,
                regime_info=regime_info,
                regime_gate_mode=regime_gate_mode,
                brain_ids=brain_ids,
                support_count=support_count,
                total_count=total_count,
            )
            if meta_decision is not None:
                return meta_decision

        # ── Track 3c: OFI Toxicity Gate ──
        # FIX-20260620-018: Extracted to ofi_gate.apply_ofi_toxicity_gate()
        _ofi_blocked = apply_ofi_toxicity_gate(
            strategy_name=name,
            micro_feature_dict=micro_feature_dict,
            direction=direction,
            confidence=confidence,
            brain_ids=brain_ids,
            support_count=support_count,
            total_count=total_count,
            regime_gate_mode=regime_gate_mode,
            make_decision=self._make_decision,
        )
        if _ofi_blocked is not None:
            return _ofi_blocked

        # ── Track 3d: Conformal OU Gate (OU physics-based signal quality) ──
        # FIX-20260620-016: Extracted to conformal_ou_gate.apply_conformal_ou_gate()
        # For OU strategies (statarb_dynamic M5, statarb_m15 M15), the
        # ConformalOUGate replaces the generic 47-dim LightGBM MetaFilterGate
        # with OU-specific physics features.
        # Falls back to MetaFilterGate if ConformalOUGate is not available.
        if name in ("statarb_dynamic", "statarb_m15"):
            _ou_blocked, _ou_result = apply_conformal_ou_gate(
                strategy_name=name,
                conformal_ou_gate=conformal_ou_gate,
                meta_filter_gate=meta_filter_gate,
                proposals=proposals,
                trend_strength=trend_strength,
                feature_vector=feature_vector,
                micro_feature_dict=micro_feature_dict,
                direction=direction,
                confidence=confidence,
                brain_ids=brain_ids,
                support_count=support_count,
                total_count=total_count,
                regime_gate_mode=regime_gate_mode,
                make_decision=self._make_decision,
            )
            self._last_ou_result = _ou_result
            if _ou_blocked is not None:
                return _ou_blocked
        # ── 4ab. MetaFilter gate (Strangler Fig #12: meta_filter_routing.py) ──

        # FIX-20260610-007: Direction-specific MetaFilter routing.
        # Per-direction models stored on config (set by live_intent_loop).
        _mf_long = getattr(self.config, "meta_filter_long", None)
        _mf_short = getattr(self.config, "meta_filter_short", None)
        # Strangler Fig #18: now top-level import (circle broken via strategy_decision)
        _meta_p_win, _meta_reject = apply_meta_filter_gate(
            name=name,
            direction=direction,
            confidence=confidence,
            entry_z_score=entry_z_score,
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            meta_filter=meta_filter,
            proposals=proposals,
            config=self.config,
            brain_ids=brain_ids,
            support_count=support_count,
            total_count=total_count,
            regime_gate_mode=regime_gate_mode,
            _meta_p_win=_meta_p_win,
            _last_ou_result=getattr(self, "_last_ou_result", None),
            meta_filter_long=_mf_long,
            meta_filter_short=_mf_short,
        )
        # ── FIX-20260621-044-bis (DQAF-044-bis): Temporal State Decoupling ──
        # _is_cold_explore is DERIVED from MetaFilter's actual return values,
        # never pre-set.  Only (None, None) — MetaFilter vacuum (cold bypass,
        # passthrough, or not-routed) — triggers bounded exploration.
        #
        # This eliminates the "set-then-clear" race window that made DQAF-044's
        # original fix incomplete: the old teardown only cleared the flag when
        # _meta_p_win was not None, missing the _meta_reject case.  Now both
        # PASS (p_win, None) and REJECT (None, rejection) naturally suppress
        # cold_explore because at least one return value is non-None.
        #
        # ReB Pattern: TEMPORAL_STATE_DECOUPLING
        # State MUST be derived from execution results, never pre-set before
        # the I/O boundary and cleared after.  This instance of BooleanStateDesync
        # (Ω Phase 2 audit family) is now structurally eliminated.
        _is_cold_explore: bool = (
            _meta_p_win is None
            and _meta_reject is None
            and confidence >= 0.35
            and ("statarb" in name or "ou" in name.lower() or "swing" in name or "btc" in name)
        )
        # ── DQAF-20260623-066 (P0-3): Cold explore entry gate ──
        # Require at least 2 LIVE brains with governance win_rate > 0 before
        # allowing cold_explore exploration.  Without this gate, swing strategies
        # (post DQAF-065 MetaFilter excision) always enter cold_explore with
        # blind p_win=0.50, producing -34.84R in 36h with 10% WR.
        # Governance performance_metrics survive restarts — they provide the
        # "is this brain known to have edge?" signal that the cold PnL store cannot.
        if _is_cold_explore:
            _gov_qualified = 0
            if governance_state is not None and _live_brain_ids:
                _gov_brain_states = governance_state.get("brain_states", {})
                for _g_bid in _live_brain_ids:
                    _bs = _gov_brain_states.get(str(_g_bid), {})
                    if isinstance(_bs, dict):
                        _pm = _bs.get("performance_metrics", {})
                        if isinstance(_pm, dict):
                            _wr = _pm.get("win_rate", 0) or 0
                            _trades = _pm.get("total_trades", 0) or 0
                            if _wr > 0 and _trades >= 3:
                                _gov_qualified += 1
            # ── DQAF-066 gate: < 2 qualified LIVE brains → block cold explore ──
            # The number 2 is calibrated: with 1 brain, a single outlier WR
            # (e.g. 1 trade, lucky win → WR=1.0) would unlock full exploration.
            # With 2+, we have cross-brain confirmation that the strategy family
            # has alpha.  This gate auto-relaxes as more brains accumulate live
            # labels — the "3" in the DQAF report recommendation is aspirational;
            # 2 is the minimum viable cross-section.
            if _gov_qualified < 2:
                logger.info(
                    "[DQAF-066] Cold explore blocked for %s: only %d LIVE brain(s) "
                    "with governance win_rate>0 (need ≥2). Waiting for more "
                    "live_labels data to accumulate.",
                    name,
                    _gov_qualified,
                )
                _is_cold_explore = False
        if _is_cold_explore:
            # p_win resolved in resolve_p_win() Step 1 — governance fallback
            # replaces the old hardcoded 0.50 (DQAF-20260623-066 P0-2).
            _p_win = 0.50  # ultimate floor; resolve_p_win() may override
            _p_win_source = "cold_explore_neutral"
        if _meta_reject is not None and not _is_cold_explore:
            # ── FIX-20260611-001: Swing MetaFilter routing excision ──
            # Swing strategies routed through Conformal OU gate get a garbage
            # s1_prediction (brain raw_score * 12.5 → pseudo-z-score up to 12.5)
            # → MetaFilter p_win ~0.24 → all trades killed.
            # Fall back to rolling_wr from PnL ledger — the brain's actual
            # historical win rate is more informative than a broken z-score proxy.
            _is_swing = name in ("m15_swing", "m30_swing", "h1_swing", "h4_swing", "btc_swing")
            if _is_swing and pnl_store is not None:
                from core.execution.kelly_sizer import resolve_p_win_from_brains

                _rolling = resolve_p_win_from_brains(
                    self.brains,
                    pnl_store,
                    direction,
                    live_brain_ids=_live_brain_ids,
                    governance_state=governance_state,
                )
                # DQAF-063: Cold-start Pathfinder Exemption.
                # Two triggers:
                # 1. Newly-live brains: sample_count < 10 → no PnL history yet.
                # 2. Zero-win-rate brains: sample_count ≥ 10 but win_rate == 0.0
                #    → labels missing from PnL ledger (data quality gap).  A real
                #    brain cannot have 0% WR over 800+ trades — this is a sentinel
                #    for "labels not backfilled".  Once labels are present and
                #    win_rate > 0, amnesty auto-expires.
                # DQAF-20260622-059 (P0-1): amnesty check ONLY consults LIVE brains.
                # Zombie/retired brains with stale PnL data must not block the
                # cold-start amnesty for a newly-live strategy.
                _amnesty_applied = False
                if _rolling < 0.50:
                    _all_need_amnesty = True
                    for _b in self.brains:
                        _bid: str | None = (
                            _b.get("brain_id")
                            if isinstance(_b, dict)
                            else getattr(_b, "brain_id", None)
                        )
                        if not _bid:
                            continue
                        # DQAF-059: skip non-LIVE brains in amnesty assessment
                        if _live_brain_ids is not None and _bid not in _live_brain_ids:
                            continue
                        if pnl_store is not None:
                            try:
                                _m = pnl_store.get_metrics(str(_bid), window=100)
                            except Exception:
                                _m = None
                            if _m is not None:
                                _sc = getattr(_m, "sample_count", 0)
                                _wr = getattr(_m, "win_rate", 0.0)
                                # Brain has reliable data: ≥10 labelled trades with non-zero WR
                                if _sc >= 10 and _wr > 0:
                                    _all_need_amnesty = False
                                    break
                    _live_count = sum(
                        1
                        for _b in self.brains
                        if _live_brain_ids is None
                        or (
                            _b.get("brain_id")
                            if isinstance(_b, dict)
                            else getattr(_b, "brain_id", None)
                        )
                        in _live_brain_ids
                    )
                    if _all_need_amnesty:
                        _rolling = 0.51
                        _amnesty_applied = True
                        logger.info(
                            "[DQAF-063] Cold-start amnesty granted for %s: "
                            "%d LIVE brain(s), rolling WR fallback overridden → 0.51",
                            name,
                            _live_count,
                        )
                # Soft-bypass: if rolling WR >= 0.50, allow with p_win=0.55
                # and let the V9 brains decide.  Below 0.50 → reject.
                if _rolling >= 0.50:
                    _meta_p_win = 0.55
                    _p_win_source = (
                        "cold_start_amnesty" if _amnesty_applied else "rolling_wr_soft_bypass"
                    )
                    _meta_reject = None  # clear rejection
                else:
                    _meta_reject.reason = (
                        f"rolling_wr_fallback_rejected:p_win={_rolling:.3f}_below_0.50"
                    )
                    return _meta_reject
            else:
                return _meta_reject
        if _is_cold_explore:
            _meta_p_win = 0.50  # force neutral for cold explore

        # ── 4aa-4d: Trend isolation gates (Strangler Fig #13 → now top-level via #18) ──
        _ct_vol_mult = 1.0  # default, may be overridden by counter-trend penalise

        _trend_reject = apply_trend_isolation_gates(
            name=name,
            direction=direction,
            confidence=confidence,
            entry_z_score=entry_z_score,
            regime_info=regime_info,
            config=self.config,
            brain_ids=brain_ids,
            support_count=support_count,
            total_count=total_count,
            regime_gate_mode=regime_gate_mode,
            last_entry_z=self._last_entry_z,
        )
        if _trend_reject is not None and not _is_cold_explore:
            return _trend_reject
        if _is_cold_explore:
            _ct_vol_mult = 0.5  # half volume for counter-trend explore
        if "statarb" in name and entry_z_score != 0.0:
            self._last_entry_z = entry_z_score

        # ── Counter-trend volume penalty ──
        # Strangler Fig #16: extracted to core/execution/trend_volume_guard.py

        _ct_vol_mult = compute_counter_trend_volume_mult(
            strategy_name=name,
            direction=direction,
            regime_info=regime_info,
            default_mult=1.0,
            penalised_mult=0.70,
        )

        # ── 5. Dynamic SL/TP ──

        # Dynamic ref ATR: use live EWMA atr_mean when available (Phase 4)
        _dynamic_ref_atr = self.config.ref_atr
        if regime_info and regime_info.get("atr_mean", 0) > 0:
            _dynamic_ref_atr = regime_info["atr_mean"]

        dsl = compute_dynamic_sl_tp(
            base_sl_mult=self.config.base_sl_atr_mult,
            base_tp_mult=self.config.base_tp_atr_mult,
            current_atr=current_atr,
            ref_atr=_dynamic_ref_atr,
            hard_sl_ratio=self.config.hard_sl_ratio,
            timeframe_mult=self.config.timeframe_mult,
            min_sl_distance=self.config.min_sl_distance,
            min_rr_ratio=self.config.min_rr_ratio,
            strategy_family=self.config.strategy_family,
        )

        entry_price = mid_price or 0.0
        if entry_price <= 0:
            return self._make_decision(
                should_trade=False,
                direction=direction,
                confidence=confidence,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                reason="invalid_entry_price",
            )
        levels = compute_sl_tp_levels(
            direction,
            entry_price,
            dsl,
            spread_points=self.config.spread_points,
            tick_size=self.config.tick_size,
        )

        # ── 5b. Minimum RR guard (skip for shadow — virtual tracking) ──
        # Strangler Fig #16: check_minimum_rr extracted to trend_volume_guard.py

        tp_dist = abs(levels["take_profit"] - entry_price)
        sl_dist = abs(levels["stop_loss"] - entry_price)
        _min_rr = self.config.min_rr_ratio if self.config.min_rr_ratio > 0 else 1.2
        if regime_gate_mode != "shadow" and not check_minimum_rr(
            entry_price, levels["stop_loss"], levels["take_profit"], min_rr_ratio=_min_rr
        ):
            return self._make_decision(
                should_trade=False,
                direction=direction,
                confidence=confidence,
                volume=0.0,
                sl=levels["stop_loss"],
                tp=levels["take_profit"],
                hard_sl=levels["hard_sl"],
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                venue="live",
                reason="rr_below_minimum",
            )

        # ── 6. Volume ──
        # FIX-20260620-017: p_win resolution chain extracted to pwin_chain.resolve_p_win()
        _p_res = resolve_p_win(
            is_cold_explore=_is_cold_explore,
            meta_p_win=_meta_p_win,
            pnl_store=pnl_store,
            brains=self.brains,
            direction=direction,
            confidence=confidence,
            strategy_name=name,
            meta_filter=meta_filter,
            min_p_win=self.config.min_p_win,
            regime_info=regime_info,
            entry_z_score=entry_z_score,
            live_brain_ids=_live_brain_ids,
            governance_state=governance_state,
        )
        _p_win = _p_res.p_win
        _p_win_source = _p_res.p_win_source
        _p_win_degraded = _p_res.p_win_degraded
        _meta_filter_absent = _p_res.meta_filter_absent
        if _meta_filter_absent:
            # MetaFilter absent disables cold exploration (uncalibrated without it)
            _is_cold_explore = False
            _meta_absent_floor = _p_res.meta_absent_floor

        # ── 5g. Hard p_win gate — physical isolation of Entry Conditions from Position Sizing ──
        # Mean-reversion strategies with p_win < 0.50 have lost statistical advantage.
        # Kelly formula alone produces "expected value illusion": theoretical RR is rarely
        # fully captured in mean-reversion trades, but stop-loss is always real.
        # This gate executes BEFORE Kelly sizing — if the model edge is gone, we don't size.
        #
        # COLD exploration bypass: when ConformalOUGate is accumulating its first 50
        # calibration samples, p_win is overridden to 0.50 and this gate is skipped.
        # Risk is bounded by the 0.01 lot volume cap enforced below.
        #
        # Phase C Fix 2: friction-adjusted dynamic breakeven floor.
        # The net SL/TP distances already include spread_points.  Compute the true
        # breakeven win rate and add a 2% safety margin so the model must provably
        # beat the spread cost before taking a position.
        _effective_min_p_win = self.config.min_p_win
        # ── FIX-20260609-002-UPDATE: MetaFilter-absent elevated floor ──
        # When MetaFilter is missing, the floor is elevated to at least 0.50
        # (coin-flip) because rolling_wr is uncalibrated historical noise.
        if _meta_filter_absent:
            _effective_min_p_win = max(_effective_min_p_win, _meta_absent_floor)
        if sl_dist > 0 and tp_dist > 0 and tp_dist >= sl_dist:
            # Dynamic breakeven floor: only for RR >= 1.0 strategies where
            # every loss = full SL and every win = full TP.
            # When SL > TP (RR < 1.0), the surface scan validates EV
            # with proper timeout/trail modeling — the simple breakeven
            # formula overestimates required p_win (FIX-20260604-084).
            _breakeven_p_win = sl_dist / (tp_dist + sl_dist)
            _effective_min_p_win = max(self.config.min_p_win, _breakeven_p_win)
        if _effective_min_p_win > 0 and _p_win < _effective_min_p_win and not _is_cold_explore:
            return self._make_decision(
                should_trade=False,
                direction=direction,
                confidence=round(confidence, 4),
                volume=0.0,
                sl=levels["stop_loss"],
                tp=levels["take_profit"],
                hard_sl=levels["hard_sl"],
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                venue="live",
                reason=f"p_win_below_dynamic_floor_{_p_win:.3f}_lt_{_effective_min_p_win:.3f}",
                entry_z_score=entry_z_score,
                entry_half_life=entry_half_life,
                p_win=_p_win,
                kelly_mult=0.0,
            )

        # RR ratio from SL/TP levels (already computed in step 5)
        _rr_ratio: float = 1.0
        sl_dist = abs(levels["stop_loss"] - entry_price)
        tp_dist = abs(levels["take_profit"] - entry_price)
        if sl_dist > 0:
            _rr_ratio = tp_dist / sl_dist

        # ── 6b. Tier 2 Kelly/Edge sizing (before _compute_volume, so applied pre-rounding) ──
        from core.execution.kelly_sizer import compute_kelly_mult

        kelly_result = compute_kelly_mult(_p_win, _rr_ratio)
        # FIX-20260604-086: For RR < 1.0 strategies, skip Kelly veto.
        # Kelly assumes binary outcomes (full SL or full TP), but low-RR
        # strategies rely on timeout exits and trail stops for partial
        # outcomes. The surface scan (with proper timeout modeling) already
        # validates EV > 0. Same principle as the dynamic floor RR<1.0 skip
        # in Phase C Fix 2.
        #
        # ── FIX-20260609-002: fail-safe floor for low-RR strategies ──
        # Even with timeout exits, p_win below the RR-implied breakeven is
        # unconditionally negative EV.  RR < 1.0 means you lose MORE on SL
        # than you gain on TP, so p_win must be proportionally HIGHER to
        # compensate.  p_win=0.41 with RR=0.65 → breakeven=0.606 → clearly
        # negative EV.  DQAF-20260609-002 diagnosed.
        _is_low_rr = _rr_ratio > 0 and _rr_ratio < 1.0
        _rr_breakeven = 1.0 / (1.0 + _rr_ratio) if _rr_ratio > 0 else 0.5
        if kelly_result.fractional_mult == 0.0 and not _is_low_rr:
            # Hard EV veto — negative expected value trade
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=round(confidence, 4),
                volume=0.0,
                sl=levels["stop_loss"],
                tp=levels["take_profit"],
                hard_sl=levels["hard_sl"],
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                venue="live",
                reason=f"negative_kelly_ev:p_win={_p_win:.3f}_rr={_rr_ratio:.3f}_kf={kelly_result.kelly_fraction:.3f}",
                entry_z_score=entry_z_score,
                entry_half_life=entry_half_life,
                p_win=_p_win,
                kelly_mult=0.0,
            )
        # ── FIX-20260609-002b: low-RR fail-safe floor ──
        # For low-RR strategies where Kelly veto is normally skipped
        # (FIX-086), add an absolute floor: if p_win is below the
        # RR-implied breakeven, the trade is unconditionally negative EV
        # regardless of timeout/trailing exit mechanics.
        # FIX-20260615-009h: cold-explore bypass — allow minimum-volume
        # exploration trades through even when RR < 1.0, matching the
        # hard p_win gate pattern at line 1395.  Without this, MetaFilter-
        # absent strategies with low RR are permanently blocked.
        if _is_low_rr and _p_win < _rr_breakeven and not _is_cold_explore:
            return self._make_decision(
                should_trade=False,
                direction="neutral",
                confidence=round(confidence, 4),
                volume=0.0,
                sl=levels["stop_loss"],
                tp=levels["take_profit"],
                hard_sl=levels["hard_sl"],
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                venue="live",
                reason=f"negative_ev_low_rr:p_win={_p_win:.3f}_lt_breakeven={_rr_breakeven:.3f}_rr={_rr_ratio:.3f}",
                entry_z_score=entry_z_score,
                entry_half_life=entry_half_life,
                p_win=_p_win,
                kelly_mult=0.0,
            )
        _kelly_mult = kelly_result.fractional_mult

        # v3.1: compute OU bandit factors (exhaustion + regime) for statarb strategies
        _ou_regime_factor = 1.0
        _exhaustion_factor = 1.0
        if "statarb" in name or "ou" in name.lower():
            if regime_info is not None:
                _ou_regime_factor = float(regime_info.get("ou_regime_factor", 1.0))
            _exhaustion_factor = sigmoid_exhaustion(abs(entry_z_score))

        # Kelly applied inside _compute_volume BEFORE lot_step rounding (single rounding at end)
        volume = self._compute_volume(
            confidence,
            current_atr,
            regime_info,
            regime_gate_mode,
            macro_regime,
            risk_budget_usd,
            exhaustion_factor=_exhaustion_factor,
            ou_regime_factor=_ou_regime_factor,
            depth_penalty=z_depth_penalty(abs(entry_z_score)),
            kelly_mult=_kelly_mult,
        )
        # Apply counter-trend volume penalty (post-rounding — this is a discrete gate)
        volume *= _ct_vol_mult
        # FIX-20260607-007: Trend maturity discount — reduce size when persistence fading
        _maturity_mult = trend_maturity_discount(
            hurst=hurst,
            trend_strength=trend_strength,
            strategy_family=self.config.strategy_family,
        )
        volume *= _maturity_mult
        _ticks2 = math.floor(volume / self.config.lot_step + 0.5)
        volume = max(self.config.lot_step, round(_ticks2 * self.config.lot_step, 2))

        # ── Volume finalization (COLD safety + Kelly diagnostic) ──
        volume = self._finalize_volume(
            volume=volume,
            strategy_name=name,
            p_win=_p_win,
            p_win_source=_p_win_source,
            rr_ratio=_rr_ratio,
            kelly_mult=_kelly_mult,
            sizing_label=kelly_result.sizing_label,
            is_cold_explore=_is_cold_explore,
        )

        # ── Build entry context for journal ──
        _brain_preds: list[dict[str, Any]] = []
        for p in proposals:
            _dir = getattr(p, "direction", None)
            _conf = float(getattr(p, "confidence", 0.5))
            if _dir is None:
                pred = getattr(p, "prediction", None) or {}
                _dir = pred.get("direction_bias", "neutral")
                _conf = float(pred.get("confidence", 0.5))
            if _dir == "long":
                _up, _down = _conf, round(1.0 - _conf, 4)
            elif _dir == "short":
                _up, _down = round(1.0 - _conf, 4), _conf
            else:
                _up, _down = 0.5, 0.5
            _brain_preds.append(
                {
                    "brain_id": getattr(p, "brain_id", "unknown"),
                    "up_prob": _up,
                    "down_prob": _down,
                    "confidence": round(_conf, 4),
                    "direction_bias": _dir,
                }
            )
        # ── Build entry_features snapshot (Phase 1: 40-dim V9 vector) ──
        # Guardrail 1: schema_version for future V10 compatibility
        # Guardrail 2: tuple() deep-copy for immutability
        # Guardrail 3: np.nan_to_num for JSON serialization safety
        _entry_features: dict[str, Any] | None = None
        if feature_vector is not None:
            import numpy as np

            _fv_arr = np.asarray(feature_vector, dtype=np.float64).ravel()
            _entry_features = {
                "schema_version": "v9_institutional",
                "vector": tuple(np.nan_to_num(_fv_arr).tolist()),
            }
        entry_context = {
            "atr": round(current_atr, 4),
            "regime": regime_info.get("regime", "normal") if regime_info else "normal",
            "vol_regime": (regime_info.get("regime", "normal") if regime_info else "normal"),
            "trend_direction": trend_direction,
            "macro_regime": macro_regime,
            "z_score": entry_z_score,
            "half_life": entry_half_life,
            "brain_predictions": _brain_preds,
            "entry_features": _entry_features,
            "entry_spread": float(ask - bid)
            if (bid is not None and ask is not None and ask > bid)
            else 0.0,
            # ── Phase 0: observable degradation injection ──
            "p_win_source": _p_win_source,
            "p_win_degraded": _p_win_degraded,
        }

        # ── Determine venue ──
        _venue = "shadow" if regime_gate_mode == "shadow" else "live"
        _volume = 0.0 if regime_gate_mode == "shadow" else volume
        _should_trade = regime_gate_mode != "shadow"  # shadow: full eval, no real order

        return self._make_decision(
            should_trade=_should_trade,
            direction=direction,
            confidence=round(confidence, 4),
            volume=_volume,
            sl=levels["stop_loss"],
            tp=levels["take_profit"],
            hard_sl=levels["hard_sl"],
            brain_ids=brain_ids,
            supporting_count=support_count,
            total_count=total_count,
            regime_mode=regime_gate_mode,
            venue=_venue,
            reason="approved",
            entry_z_score=entry_z_score,
            entry_half_life=entry_half_life,
            entry_context=entry_context,
            p_win=_p_win,
            p_win_source=_p_win_source,
            p_win_degraded=_p_win_degraded,
            kelly_mult=kelly_result.fractional_mult,
            cold_explore=_is_cold_explore,
        )

    # ── Consensus computation ───────────────────────────────────────────

    def _try_meta_pipeline(
        self,
        *,
        proposals: list[Any],
        feature_vector: Any,
        micro_feature_vector: Any,
        meta_filter: Any,
        current_atr: float,
        mid_price: float | None,
        entry_z_score: float,
        pnl_store: Any,
        trend_direction: str,
        trend_strength: float,
        h4_trend_strength: float,
        macro_regime: str,
        risk_budget_usd: float,
        regime_info: dict[str, Any] | None,
        regime_gate_mode: str,
        brain_ids: list[str],
        support_count: int,
        total_count: int,
    ) -> StrategyDecision | None:
        """Track 2: Meta Pipeline — config-driven probe → Stage-N filter.

        Delegates to :class:`MetaPipeline` which auto-discovers probe brains
        from ``StrategyLineConfig.meta_probe_specs`` or brain JSON roles.
        """
        if not self.config.meta_probe_specs:
            return None

        from core.execution.meta_pipeline import MetaPipeline

        pipeline = MetaPipeline(
            specs=self.config.meta_probe_specs,
            filter_registry={"stage2": meta_filter} if meta_filter is not None else {},
        )
        return pipeline.evaluate(
            proposals=proposals,
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            parliament_direction="neutral",  # Meta Pipeline is direction-agnostic
            current_atr=current_atr,
            mid_price=mid_price,
            entry_z_score=entry_z_score,
            pnl_store=pnl_store,
            risk_budget_usd=risk_budget_usd,
            regime_info=regime_info,
            regime_gate_mode=regime_gate_mode,
            brain_ids=brain_ids,
            support_count=support_count,
            total_count=total_count,
            config=self.config,
        )

    def _compute_consensus(self, proposals: list[Any]) -> tuple[str, float, list[str], int, int]:
        """Within-group consensus — delegates to ContractGroupConsensus.

        Routes to union or weighted-average voting based on the group
        definition in contract_groups.py.

        Returns: (direction, confidence, brain_ids, support_count, total_count)
        """
        if not proposals:
            return "neutral", 0.0, [], 0, 0

        # Resolve ContractGroupConsensus by strategy name (= contract group name)
        from core.parliament.contract_groups import (
            ContractGroupConsensus,
            get_group_for_contract_group,
        )

        group_def = get_group_for_contract_group(self.config.name)

        if group_def is not None:
            # Delegate to ContractGroupConsensus (handles union/weighted routing)
            cc = ContractGroupConsensus(group_def)
            signal = cc.compute(proposals)
            if signal is not None:
                direction = signal.direction
                confidence = signal.confidence
                # Direction balance — counteract systemic LONG bias
                if direction == "long" and self.config.long_bias_discount > 0:
                    confidence = round(confidence * (1.0 - self.config.long_bias_discount), 4)
                return (
                    direction,
                    confidence,
                    signal.brain_ids,
                    signal.supporting_count,
                    signal.total_count,
                )

        # Defensive fallback: weighted-average for strategies without a registered
        # contract group (tests, custom setups, misconfigured strategies).
        # Rarely/never hit in production — all active strategies have contract groups.
        # KEEP as safety net — removing would turn a graceful fallback into a crash.
        return self._compute_weighted_fallback(proposals)

    def _compute_weighted_fallback(
        self, proposals: list[Any]
    ) -> tuple[str, float, list[str], int, int]:
        """Direction-count consensus — used when no contract group matches.

        Each BrainSignal votes its *direction* weighted by *confidence*.
        The up_prob/down_prob comparison from the old BrainDecisionProposal
        is intentionally removed — it was the root cause of FIX-20260522-013
        (sign-flip bug).  BrainSignal carries only the decided direction.
        """
        brain_ids: list[str] = []
        long_weight: float = 0.0
        short_weight: float = 0.0
        long_brains: list[str] = []
        short_brains: list[str] = []
        total = len(proposals)

        for p in proposals:
            bid = getattr(p, "brain_id", "unknown")
            brain_ids.append(bid)

            direction = getattr(p, "direction", None)
            if direction is None:
                direction = getattr(p, "prediction", {}).get("direction_bias", "neutral")

            confidence = getattr(p, "confidence", None)
            if confidence is None:
                confidence = float(getattr(p, "prediction", {}).get("confidence", 0.5))

            fallback = getattr(p, "fallback", None)
            if fallback is None:
                health = getattr(p, "health", None) or {}
                fallback = health.get("fallback_used", False)

            weight = float(confidence) * (0.5 if fallback else 1.0)

            if direction == "long":
                long_weight += weight
                long_brains.append(bid)
            elif direction == "short":
                short_weight += weight
                short_brains.append(bid)
            # neutral: weight is discarded

        long_count = len(long_brains)
        short_count = len(short_brains)
        total_weights = long_weight + short_weight
        if total_weights < 1e-9:
            return "neutral", 0.0, brain_ids, 0, len(proposals)

        if long_weight > short_weight:
            direction = "long"
            supporting = long_brains
        elif short_weight > long_weight:
            direction = "short"
            supporting = short_brains
        else:
            return "neutral", 0.0, brain_ids, 0, len(proposals)

        majority_ratio = max(long_weight, short_weight) / total_weights
        confidence = round(
            majority_ratio * 0.65 + (long_weight + short_weight) / max(len(proposals), 1) * 0.35, 4
        )

        if direction == "long" and self.config.long_bias_discount > 0:
            confidence *= 1.0 - self.config.long_bias_discount

        support_count = max(long_count, short_count) if direction != "neutral" else 0
        return direction, round(float(confidence), 4), brain_ids, support_count, total

    # ── Volume computation ──────────────────────────────────────────────

    def _compute_volume(
        self,
        confidence: float,
        current_atr: float,
        regime_info: dict[str, Any] | None,
        regime_gate_mode: str,
        macro_regime: str = "mixed",
        risk_budget_usd: float = 0.0,
        *,
        exhaustion_factor: float = 1.0,
        ou_regime_factor: float = 1.0,
        depth_penalty: float = 1.0,
        kelly_mult: float = 1.0,
    ) -> float:
        """Compute dynamic volume with bandit sizing (v3.1 + v3.2 depth decay).

        Core formula mirrors _compute_meta_volume in meta_pipeline.py —
        keep these two implementations in sync.

        When risk_budget_usd > 0, uses vol-targeted sizing:
          base = risk_budget / (ATR × SL_mult × contract_size)
        Otherwise falls back to fixed base_volume.

        v3.2 bandit formula:
          M = base_lot × agreement × gate × vol × macro
              × exhaustion (sigmoid) × ou_regime × depth_penalty
          → apply_mvs(M) → kelly_mult → round_to_lot_step

        Kelly (Tier 2) is applied BEFORE the final lot_step rounding so
        the effect is not destroyed by premature discretization.
        """
        if current_atr <= 0:
            current_atr = self.config.ref_atr

        # Base volume: vol-targeted if risk budget is set, else fixed
        if risk_budget_usd > 0:
            from core.execution.pre_trade_guards import compute_position_size

            base_volume = compute_position_size(
                risk_budget_usd=risk_budget_usd,
                atr=current_atr,
                sl_atr_mult=self.config.base_sl_atr_mult,
                contract_size=self.config.contract_size,
                min_lot=0.01,
                max_lot=self.config.max_volume,
                lot_step=0.01,
                symbol=self.config.symbol,
            )
        else:
            base_volume = self.config.base_volume

        # Agreement factor: confidence directly scales volume
        agreement_factor = 0.45 + confidence * 0.55  # maps [0,1] to [0.45, 1.0]

        # Regime gate factor
        gate_factors = {"full": 1.0, "reduced": 0.65, "shadow": 0.0, "off": 0.0}
        gate_factor = gate_factors.get(regime_gate_mode, 1.0)

        # Volatility regime factor (from RegimeDetector)
        vol_regime = regime_info.get("regime", "normal") if regime_info else "normal"
        vol_factors = {
            "low": self.config.regime_vol_mult_low,
            "normal": self.config.regime_vol_mult_normal,
            "high": self.config.regime_vol_mult_high,
        }
        vol_factor = vol_factors.get(vol_regime, 1.0)

        # Macro regime factor: risk_off cuts barrier volume by 0.7
        macro_factor = 1.0
        if macro_regime == "risk_off":
            if self.config.name == "barrier_12bar":
                macro_factor = 0.70

        # v3.2: Bandit sizing — OU regime × sigmoid exhaustion × Z depth decay
        bandit_factor = ou_regime_factor * exhaustion_factor * depth_penalty

        effective_mult = agreement_factor * gate_factor * vol_factor * macro_factor * bandit_factor

        size = base_volume * effective_mult

        # ── Graduated streak reduction ──
        streak_mult = 1.0
        if self.budget is not None:
            with fail_open_guard("StrategyLine:StreakMultiplier"):
                streak_mult = self.budget.get_streak_multiplier()
        size *= streak_mult

        # Save pre-Kelly raw size for diagnostic logging
        self._last_pre_kelly_size = size

        # ── Tier 2 Kelly/Edge sizing (before rounding) ──
        size *= kelly_mult

        # v3.1: MVS cut-off AFTER Kelly — kills micro-positions where final
        # multiplier (including Kelly) is too low.  Previously ran before Kelly,
        # which prevented Kelly amplification from saving marginal signals.
        # Uses config.base_volume (not risk-budget-computed base_volume) so the
        # cutoff is stable regardless of risk_budget_usd.
        _mvs_cutoff = self.config.base_volume * MVS_THRESHOLD
        if size > 0 and size < _mvs_cutoff:
            size = 0.0

        # Round to lot_step using floor-round (consistent with compute_position_size).
        # Python's built-in round() uses banker's rounding which truncates 0.015→0.01
        # due to float representation, causing volumes in the 0.011-0.014 range to
        # always die at 0.01.
        _lot_step = self.config.lot_step
        _ticks = math.floor(size / _lot_step + 0.5)
        return max(_lot_step, min(self.config.max_volume, round(_ticks * _lot_step, 2)))

    def _finalize_volume(
        self,
        *,
        volume: float,
        strategy_name: str,
        p_win: float,
        p_win_source: str,
        rr_ratio: float,
        kelly_mult: float,
        sizing_label: str,
        is_cold_explore: bool = False,
    ) -> float:
        """Apply COLD phase safety cap and emit Kelly sizing diagnostic.

        FIX-20260620-018: Extracted from evaluate() volume post-processing block.

        Returns the finalized volume after COLD override and diagnostic output.
        """
        # ── Layer 3 COLD phase exploration safety ──
        # When the ConformalOUGate is in COLD phase (calibrator samples < 50),
        # force-caps volume at min lot (0.01) regardless of Kelly/position sizer
        # output.  This bounds exploration risk while the calibrator accumulates
        # (p_win, label) samples to break the chicken-and-egg deadlock.
        _ou_gate = getattr(self, "_last_ou_result", None)
        if _ou_gate is not None and _ou_gate.get("force_min_volume"):
            _pre_override = volume
            volume = 0.01
            import logging as _logging

            _logging.getLogger(__name__).info(
                "COLD phase volume override: %s → 0.01 (samples=%s, phase=%s)",
                _pre_override,
                _ou_gate.get("warmup_phase", "?"),
            )

        # Diagnostic: three-way volume distinction (raw vs stepped)
        _pre_kelly_raw = getattr(self, "_last_pre_kelly_size", volume)
        _raw_target = _pre_kelly_raw * kelly_mult
        import json as _json

        print(
            _json.dumps(
                {
                    "event": "kelly_sizing",
                    "time": datetime.now(UTC).isoformat().replace("+00:00", "Z") + "Z",
                    "strategy": strategy_name,
                    "p_win": round(p_win, 4),
                    "p_win_source": p_win_source,
                    "rr_ratio": round(rr_ratio, 4),
                    "kelly_mult": round(kelly_mult, 4),
                    "sizing_label": sizing_label,
                    "base_volume": round(_pre_kelly_raw, 4),
                    "raw_target_volume": round(_raw_target, 4),
                    "final_stepped_volume": volume,
                    "cold_explore": is_cold_explore,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        return volume

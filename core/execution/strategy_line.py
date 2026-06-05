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
from typing import Any

import numpy as np

from core.runtime.fault_handler import FaultLevel, FaultTolerantContext

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


def _adjust_p_win_for_regime(
    p_win: float,
    name: str,
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
    friend, not a risk factor.  Counter-trend signals (catching a falling knife
    in a downtrend or shorting into an uptrend) receive the full penalty.

    This function inversely maps z_score → p_win discount when ADX indicates
    trending conditions.  Hard floor at 65% of original p_win prevents the
    adjustment from ever being the sole veto (that's the p_win gate's job).

    Non-OU strategies and non-trending regimes pass through unchanged.
    """
    if "statarb" not in name or not regime_info or entry_z_score is None:
        return p_win

    _rg: dict[str, Any] = {}
    if isinstance(regime_info, dict):
        _maybe_rg = regime_info.get("regime_gate")
        if isinstance(_maybe_rg, dict):
            _rg = _maybe_rg

    _h1_adx = float(_rg.get("h1_adx") or 0.0)

    # Only adjust when trend is significant (FIX-20260526-031: lowered from 20→15
    # to match actual ADX distribution and prevent false negatives)
    if _h1_adx < 15.0:
        return p_win

    # ── Direction-aware bypass: with-trend pullbacks are Alpha, not risk ──
    _primary_dir = str(_rg.get("primary_trend") or "neutral")
    _h1_dir = str(_rg.get("h1_trend_direction") or "neutral")
    _ref_dir = _primary_dir if _primary_dir != "neutral" else _h1_dir

    if trade_direction != "neutral" and _ref_dir != "neutral":
        if trade_direction == _ref_dir:
            return p_win  # 千金难买牛回头 — no penalty for with-trend pullbacks
        # Counter-trend: continue to penalty logic below

    abs_z = abs(entry_z_score)
    # FIX-20260526-031: lowered from 1.5→0.8 — the old threshold was physically
    # unreachable (actual OU |z| range 0.1-0.3).  Fix 3 (z_depth veto) filters
    # |z|<0.325, so signals reaching here have |z|≥0.325.  The 0.8 threshold
    # starts modest penalties for moderate deviations in trending markets.
    if abs_z < 0.8:
        return p_win

    # Trending: inverse z_score effect on p_win
    # At ADX=25, |z|=0.8: discount ≈ 0.97 (mild penalty at boundary)
    # At ADX=30, |z|=1.5: discount ≈ 0.90 (moderate penalty)
    # At ADX=40, |z|=2.5: discount ≈ 0.79 (strong penalty)
    trend_penalty = min(0.90, (_h1_adx - 15.0) / 100.0)
    z_amplification = min(1.0, (abs_z - 0.5) / 3.0)  # FIX-20260526-031: baseline 0.5→0.8→3.5 ramp
    discount = 1.0 - trend_penalty * z_amplification

    adjusted = p_win * max(discount, 0.65)  # floor at 65% of original
    return round(adjusted, 4)


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


# ── Strategy decision dataclass ──────────────────────────────────────────


@dataclass
class StrategyDecision:
    """Output of one strategy line evaluation for one cycle."""

    strategy_name: str
    magic: int
    should_trade: bool
    direction: str  # "long", "short", or "neutral"
    confidence: float
    volume: float
    sl: float
    tp: float
    hard_sl: float
    brain_ids: list[str] = field(default_factory=list)
    brain_votes: list[dict[str, Any]] = field(default_factory=list)
    supporting_count: int = 0
    total_count: int = 0
    regime_mode: str = "full"  # "full" | "reduced" | "shadow"
    venue: str = "live"  # "live" | "shadow"
    reason: str = ""
    entry_z_score: float = 0.0  # OU z-score at entry (0 = not an OU strategy or unknown)
    entry_half_life: float = 0.0  # OU half-life at entry (0 = unknown / not OU)
    entry_context: dict[str, Any] = field(default_factory=dict)
    p_win: float = 0.5  # P(TP|signal) from MetaFilter or rolling PnL win rate
    kelly_mult: float = 1.0  # fractional Kelly multiplier (0.0 = EV veto)
    cold_explore: bool = (
        False  # forced exploration budget — bypass trailing, collect uncensored labels
    )
    gate_diag: dict[str, Any] = field(default_factory=dict)  # gate audit diagnostics
    # entry_context carries passthrough data for the journal:
    #   {"atr": float, "regime": str, "vol_regime": str, "trend_direction": str,
    #    "macro_regime": str, "brain_predictions": [dict, ...],
    #    "feature_vector_summary": dict}


@dataclass
class StrategyLineConfig:
    """Immutable configuration for one strategy line."""

    name: str
    magic: int
    brain_types: set[str]
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
        except Exception:
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
        except Exception:
            return 0.5

    # ── Subclass overrides ──────────────────────────────────────────────

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
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

        # ── 1. Regime gate ──
        if regime_gate_mode == "off":
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
            )
        except Exception:
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
            try:
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
                                    "time": __import__("datetime").datetime.utcnow().isoformat()
                                    + "Z",
                                    "brain_id": _brain_id,
                                    "raw_bps": round(float(_raw), 6),
                                    "price": round(float(mid_price or 0), 2),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
            except Exception:
                pass

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
                except Exception as _rec_exc:
                    import logging as _lg

                    _lg.getLogger(__name__).debug(
                        "PnL record_signal failed for brain=%s: %s",
                        getattr(p, "brain_id", "?"),
                        _rec_exc,
                        exc_info=True,
                    )

        # ── 3a3. Capture entry_z_score + entry_half_life from OU-style brains ──
        entry_z_score = 0.0
        entry_half_life = 0.0
        for p in proposals:
            try:
                z = getattr(p, "raw_score", 0.0)
                if z is not None and float(z) != 0.0:
                    entry_z_score = float(z)
                diag = getattr(p, "diagnostics", {}) or {}
                hl = diag.get("half_life")
                if hl is not None and isinstance(hl, int | float) and 0 < float(hl) < float("inf"):
                    entry_half_life = float(hl)
                    break
            except (TypeError, ValueError, AttributeError):
                pass

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
        # Count brains that produced a non-neutral directional signal AND
        # have a positive vote_weight.  Brains with vote_weight=0.0 are
        # contract-muted or governance-silenced — they cannot influence
        # consensus, so counting them as "valid voters" creates a deadlock
        # where (muted_brain_count > 0) < min_valid_brains but the muted
        # brain can never actually vote.
        # All-neutral proposals pass through to consensus computation
        # (which will naturally return neutral).
        _valid_voters = 0
        for p in proposals:
            _vw = float(getattr(p, "vote_weight", 1.0) or 1.0)
            if _vw <= 0.0:
                continue  # muted brain — cannot vote, don't count
            # BrainSignal attribute first, fall back to legacy dict
            _dir = getattr(p, "direction", None)
            if _dir is None:
                _pred = getattr(p, "prediction", None) or {}
                _dir = (
                    _pred.get("direction_bias", "neutral") if isinstance(_pred, dict) else "neutral"
                )
            if _dir != "neutral":
                _valid_voters += 1
        if _valid_voters > 0 and _valid_voters < self.config.min_valid_brains:
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
            from core.runtime.shadow_recorder import record_brain_votes

            _status_map: dict[str, str] = {
                str(b.get("brain_id", "")): str(b.get("status", "unknown")) for b in self.brains
            }
            record_brain_votes(
                proposals=proposals,
                strategy_name=name,
                consensus_direction=direction,
                consensus_confidence=confidence,
                symbol=getattr(self.config, "symbol", "XAUUSDc"),
                base_dir="data",
                brain_status_map=_status_map,
            )
        except Exception:
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
        # Hard physical gate: when Order Flow Imbalance is extremely one-sided,
        # physically block counter-trend mean-reversion signals. Mean-reversion
        # against toxic order flow must surrender — the liquidity vacuum crushes
        # any reversal attempt.
        # OFI is NOT an ML feature — it's a standalone risk signal computed in
        # MicrostructureFeatureComputer._compute_tick_features().
        if name in ("statarb_dynamic", "statarb_m15") and micro_feature_dict:
            _ofi_z = micro_feature_dict.get("OFI", 0.0)
            if direction == "short" and _ofi_z > 2.0:
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
                    reason=f"ofi_toxicity_blocked_short:OFI_Z={_ofi_z:.2f}_gt_2.0",
                )
            if direction == "long" and _ofi_z < -2.0:
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
                    reason=f"ofi_toxicity_blocked_long:OFI_Z={_ofi_z:.2f}_lt_-2.0",
                )

        # ── Track 3d: Conformal OU Gate (OU physics-based signal quality) ──
        # For OU strategies (statarb_dynamic M5, statarb_m15 M15), the
        # ConformalOUGate replaces the generic 47-dim LightGBM MetaFilterGate
        # with OU-specific physics features: Z-Depth, Z-Velocity, Half-life
        # quality, Theta strength, and ADX trend penalty.
        # Falls back to MetaFilterGate if ConformalOUGate is not available.
        # barrier_12bar is EXEMPT — Track 4d MetaSignalFilter handles it.
        if name in ("statarb_dynamic", "statarb_m15"):
            if conformal_ou_gate is not None and conformal_ou_gate.is_loaded:
                try:
                    adx_approx = 15.0 + trend_strength * 40.0
                    ou_result = conformal_ou_gate.filter(
                        strategy_name=name,
                        proposals=proposals,
                        adx_value=adx_approx,
                    )
                    # Capture for downstream volume override (COLD phase exploration safety)
                    self._last_ou_result = ou_result
                    if not ou_result["passed"] and not ou_result.get("force_min_volume"):
                        # FIX-20260527-006: COLD phase exploration bypass.
                        # When force_min_volume=True (ConformalOU calibrator < 50 samples),
                        # the gate rejection is overridden — fall through to downstream
                        # COLD exploration logic (p_win=0.50, 0.01 lot cap).
                        # Only reject when the gate says no AND there is no exploration mandate.
                        _gd: dict[str, Any] = {}
                        _feat = ou_result.get("features", {})
                        if _feat:
                            _gd = {
                                "gate": "conformal_ou",
                                "composite_score": ou_result.get("score"),
                                "threshold": ou_result.get("threshold"),
                                "z_score": _feat.get("z_score"),
                                "z_entry": _feat.get("z_entry"),
                                "z_depth_q": _feat.get("z_depth_q"),
                                "half_life": _feat.get("half_life"),
                                "hl_q": _feat.get("hl_q"),
                                "theta": _feat.get("theta"),
                                "theta_q": _feat.get("theta_q"),
                                "adx": _feat.get("adx"),
                                "adx_q": _feat.get("adx_q"),
                                "vel_q": _feat.get("vel_q"),
                            }
                        return StrategyDecision(
                            strategy_name=name,
                            magic=self.config.magic,
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
                            reason=ou_result["reason"],
                            gate_diag=_gd,
                        )
                except Exception:
                    import logging

                    _sl_logger = logging.getLogger(__name__)
                    _sl_logger.warning(
                        "OU gate evaluation failed for strategy=%s — BLOCKING trade",
                        name,
                        exc_info=True,
                    )
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason="ou_gate_exception_blocked",
                    )
            elif (
                meta_filter_gate is not None
                and meta_filter_gate.is_loaded
                and feature_vector is not None
            ):
                try:
                    mf_result = meta_filter_gate.filter(
                        feature_vector=feature_vector,
                        micro_features=micro_feature_dict or {},
                    )
                    if not mf_result["passed"]:
                        return StrategyDecision(
                            strategy_name=name,
                            magic=self.config.magic,
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
                            reason=mf_result["reason"],
                        )
                except Exception:
                    import logging

                    _sl_logger = logging.getLogger(__name__)
                    _sl_logger.warning(
                        "Meta-filter gate evaluation failed for strategy=%s — BLOCKING trade",
                        name,
                        exc_info=True,
                    )
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason="meta_filter_gate_exception_blocked",
                    )

        # ── 4ab. MetaFilter experimental routing for statarb (FIX-20260526-041) ──
        # EXPERIMENTAL: MetaFilter (48-dim LGB + Platt, Forward Sharpe 1.30) was
        # trained on barrier_12bar trend/breakout logic.  Applying it to statarb
        # mean-reversion signals is a domain-shift transfer-learning hack.
        # entry_z_score * 12.5 serves as a proxy s1_prediction (|z|≤4 → |proxy|≤50,
        # within the BPS training distribution).  The Platt calibrator + 47-dim
        # context features provide partial domain-shift buffering.
        #
        # GUARDRAILS:
        #   - Every 50 settled trades, evaluate corr(meta_p_win, realized_pnl).
        #   - If corr < 0.05 for TWO consecutive periods → DISABLE this route
        #     (revert to Fix 1C confidence mapping fallback).
        #   - Long-term: collect 200+ OU settled trades → train OU-specific MetaFilter.
        # FIX-20260604-083: extend MetaFilter to swing strategies
        if (
            meta_filter is not None
            and ("statarb" in name or name in ("m15_swing", "m30_swing"))
            and _meta_p_win is None
        ):
            try:
                _z_proxy = entry_z_score * 12.5
                _result = meta_filter.filter_arrays(
                    direction=direction,
                    s1_prediction=_z_proxy,
                    v9_array=feature_vector,
                    micro_array=micro_feature_vector,
                )
                if not _result.passed:
                    # FIX-20260527-006: COLD phase exploration bypass.
                    # When the ConformalOU calibrator is in COLD phase
                    # (force_min_volume=True), skip MetaFilter gate and
                    # fall through to downstream COLD exploration logic.
                    _ou_gate = getattr(self, "_last_ou_result", None)
                    if _ou_gate and _ou_gate.get("force_min_volume"):
                        _meta_p_win = None  # don't use MetaFilter p_win for cold explore
                    else:
                        import json as _json

                        print(
                            _json.dumps(
                                {
                                    "event": "kelly_diag",
                                    "time": __import__("datetime").datetime.utcnow().isoformat()
                                    + "Z",
                                    "strategy": name,
                                    "stage": "meta_filter_rejected_statarb",
                                    "z_score": round(entry_z_score, 4),
                                    "z_proxy": round(_z_proxy, 4),
                                    "result_p_win": round(float(getattr(_result, "p_win", 0)), 4),
                                    "passed": False,
                                    "reason": getattr(_result, "reason", None) or "threshold",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        return StrategyDecision(
                            strategy_name=name,
                            magic=self.config.magic,
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
                            reason=f"meta_filter_rejected_statarb:{getattr(_result, 'reason', 'threshold')}",
                        )
                _meta_p_win = float(_result.p_win)
                import json as _json

                print(
                    _json.dumps(
                        {
                            "event": "kelly_diag",
                            "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                            "strategy": name,
                            "stage": "meta_filter_p_win_statarb",
                            "z_score": round(entry_z_score, 4),
                            "z_proxy": round(_z_proxy, 4),
                            "result_p_win": round(_meta_p_win, 4),
                            "passed": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "MetaFilter statarb routing failed for %s: fallthrough to p_win resolution",
                    name,
                    exc_info=True,
                )

        # ── 4aa. Direction-aware trend isolation gate (OU/statarb only) ──
        # FIX-20260526-033: Replace symmetric ADX>25 block with direction-aware
        # counter-trend gating.  Kalman fusion trend detection (no ADX lag) +
        # primary_trend direction check via H4>H1>M5 priority chain.
        #
        # Physics: mean-reversion WITH the trend (pullback in uptrend, bounce
        # in downtrend) has trend tailwind — the trend pulls price back toward
        # the mean.  Counter-trend MR (fading the trend) is catching a falling
        # knife — blocked.  This explains the LONG +44.2 vs SHORT -100.8 PnL
        # asymmetry in OU_Params_V6_Sniper (1284 trades).
        #
        # Two-stage gating:
        #   1. Trend detection: Kalman strength > 25 OR multi-TF consensus
        #   2. Direction check: counter-trend → BLOCK; with-trend → ALLOW
        if "statarb" in name and regime_info:
            _rg = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
            _trend_strength = float(_rg.get("h1_adx") or 0.0)  # Kalman fusion ×100
            _h4_ts = float(_rg.get("h4_trend_strength") or 0.0)
            _m5_ts = float(_rg.get("m5_trend_strength") or 0.0)
            _h1_dir = str(_rg.get("h1_trend_direction") or "neutral")
            _primary_dir = str(_rg.get("primary_trend") or "neutral")
            _primary_source = str(_rg.get("primary_trend_source") or "h1")

            _is_strong_trend = _trend_strength > 25.0
            _mtf_consensus = _trend_strength > 20.0 and _h4_ts > 0.5 and _m5_ts > 0.5

            if _is_strong_trend or _mtf_consensus:
                # Direction-aware: ref_dir uses longest available timeframe
                _ref_dir = _primary_dir if _primary_dir != "neutral" else _h1_dir
                _is_counter_trend = (
                    direction != "neutral" and _ref_dir != "neutral" and direction != _ref_dir
                )

                if _is_counter_trend:
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason=f"counter_trend_blocked:{direction}_vs_{_ref_dir}({_primary_source})_ts={_trend_strength:.1f}",
                    )
                # With-trend MR → allowed (trend tailwind)

        if not parliament_passed:
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
                reason=(
                    f"low_confidence_{confidence:.4f}_lt_{self.config.confidence_threshold}"
                    if direction != "neutral"
                    else "neutral_consensus"
                ),
                gate_diag={
                    "gate": "parliament",
                    "confidence": confidence,
                    "threshold": self.config.confidence_threshold,
                    "direction": direction,
                    "supporting": support_count,
                    "total": total_count,
                    "brain_diag": [
                        {
                            "brain_id": getattr(p, "brain_id", "?"),
                            "z_score": getattr(p, "raw_score", None),
                            "half_life": getattr(p, "diagnostics", {}).get("half_life"),
                            "buffer_len": getattr(p, "diagnostics", {}).get("buffer_len"),
                            "theta": getattr(p, "diagnostics", {}).get("theta"),
                        }
                        for p in (proposals or [])
                    ],
                },
            )

        # ── 4b. Hard multi-TF trend filter (Phase C Fix 1) ──
        # When H4 and H1 agree on direction, swing strategies are physically
        # blocked from counter-trend entries.  This is stricter than the
        # strength-based counter-trend gate below — alignment alone is enough.
        # barrier_12bar and statarb families are EXEMPT.
        _h4_dir = "neutral"
        if regime_info is not None:
            _rg = regime_info.get("regime_gate", {})
            if isinstance(_rg, dict):
                _h4_dir = str(_rg.get("h4_trend_direction", "neutral"))
        _h1_dir = trend_direction  # primary_trend: H4 > H1 > M5 hierarchy
        _is_swing = name in ("m30_swing", "m15_swing", "h1_swing", "h4_swing")
        if (
            _is_swing
            and direction is not None
            and _h1_dir != "neutral"
            and _h4_dir != "neutral"
            and _h1_dir == _h4_dir
            and direction != _h1_dir
        ):
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
                reason=f"hard_trend_filter_{direction}_vs_h1h4_{_h1_dir}",
                gate_diag={
                    "gate": "hard_trend_filter",
                    "signal_direction": direction,
                    "h1_trend": _h1_dir,
                    "h4_trend": _h4_dir,
                },
            )

        # ── 4c. Counter-trend gate ──
        # Block trades that oppose the higher-timeframe trend.
        # barrier_12bar is EXEMPT — Dictator Protocol: the Huber BPS probe IS
        # the trend signal; a counter-trend block would silence the only voter
        # (FIX-20260522-013).
        # statarb (mean-reversion) family is EXEMPT — mean-reversion is
        # inherently counter-trend.  Blocking a statarb SHORT during a BULL
        # trend is a category error: the strategy is *supposed* to fade the
        # trend at extremes (FIX-20260526-028).
        _ct_vol_mult = 1.0
        if (
            name != "barrier_12bar"
            and "statarb" not in name
            and trend_direction != "neutral"
            and direction != trend_direction
        ):
            ct_block = _counter_trend_action(name, trend_strength, h4_trend_strength)
            if ct_block["action"] == "block":
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
                    reason=f"counter_trend_blocked_{direction}_vs_{trend_direction}",
                    gate_diag={
                        "gate": "counter_trend",
                        "signal_direction": direction,
                        "trend_direction": trend_direction,
                        "trend_strength": trend_strength,
                        "h4_trend_strength": h4_trend_strength,
                    },
                )
            elif ct_block["action"] == "penalise":
                confidence *= ct_block["confidence_mult"]
                _ct_vol_mult = float(ct_block.get("vol_mult", 1.0))
                if confidence < self.config.confidence_threshold:
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason=f"counter_trend_penalised_{direction}_vs_{trend_direction}",
                    )

        # ── 4d. Z-score inflection gate (v3.2) ──
        # For OU/statarb strategies: require z-score turning back toward mean.
        # Prevents catching falling knives when z is still accelerating away.
        # Knife 1: z_entry raised to 2.0 — only trade extreme reversions where
        # the edge is strongest and mean-drift risk is lowest.
        if "statarb" in name or "ou" in name.lower():
            if entry_z_score != 0.0:
                _z_entry = 1.3 if "statarb" in name else 1.5
                _inf_allow, _inf_reason = check_z_inflection(
                    entry_z_score,
                    self._last_entry_z,
                    direction,
                    z_entry=_z_entry,
                )
                self._last_entry_z = entry_z_score
                if not _inf_allow:
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason=_inf_reason,
                    )

        # ── 4e. Meta-Labeling ML Gate (Stage 2) ──
        # Filters barrier_12bar signals through the LGB+MLP ensemble model.
        # Extracts Stage 1 raw prediction from the Huber brain, assembles the
        # 48-dim named feature dict from the V9 + micro ndarrays, and applies
        # Platt calibration + conformal thresholding.  Other strategies pass
        # through unchanged (scope isolation).
        if meta_filter is not None and name == "barrier_12bar":
            from core.execution.meta_pipeline import extract_probe_score

            _s1_prediction: float | None = None
            for spec in self.config.meta_probe_specs:
                _s1 = extract_probe_score(proposals, spec.brain_id)
                if _s1 is not None:
                    _s1_prediction = _s1
                    break
            # Fallback: if no meta_probe_specs configured, scan all proposals
            if _s1_prediction is None:
                for p in proposals:
                    raw = getattr(p, "raw_score", None)
                    if raw is not None:
                        _s1_prediction = float(raw)
                        break
                    # Legacy fallback
                    ext = getattr(p, "extensions", None)
                    if ext and isinstance(ext, dict):
                        ro = ext.get("raw_outputs", {})
                        if isinstance(ro, dict):
                            raw = ro.get("raw_score")
                            if raw is not None:
                                _s1_prediction = float(raw)
                                break

            if _s1_prediction is not None:
                result = meta_filter.filter_arrays(
                    direction=direction,
                    s1_prediction=_s1_prediction,
                    v9_array=feature_vector,
                    micro_array=micro_feature_vector,
                )
                if not result.passed:
                    import json as _json

                    _diag_p_win = getattr(result, "p_win", None)
                    print(
                        _json.dumps(
                            {
                                "event": "kelly_diag",
                                "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                                "strategy": name,
                                "stage": "meta_filter_rejected",
                                "s1_prediction": round(_s1_prediction, 6)
                                if _s1_prediction
                                else None,
                                "result_p_win": round(float(_diag_p_win), 4)
                                if _diag_p_win is not None
                                else None,
                                "passed": False,
                                "reason": result.reason if result.reason else "threshold",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    return StrategyDecision(
                        strategy_name=name,
                        magic=self.config.magic,
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
                        reason=f"meta_filter_rejected:{result.reason}"
                        if result.reason
                        else "meta_filter_rejected",
                    )
                _meta_p_win = float(result.p_win)
                import json as _json

                print(
                    _json.dumps(
                        {
                            "event": "kelly_diag",
                            "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                            "strategy": name,
                            "stage": "meta_filter_p_win",
                            "s1_prediction": round(_s1_prediction, 6),
                            "result_p_win": round(_meta_p_win, 4),
                            "passed": result.passed,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── 5. Dynamic SL/TP ──
        from core.execution.dynamic_sl_tp import compute_dynamic_sl_tp, compute_sl_tp_levels

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
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
        tp_dist = abs(levels["take_profit"] - entry_price)
        sl_dist = abs(levels["stop_loss"] - entry_price)
        if regime_gate_mode != "shadow":
            _min_rr = self.config.min_rr_ratio if self.config.min_rr_ratio > 0 else 1.2
            if sl_dist > 0 and tp_dist / sl_dist < _min_rr:
                return StrategyDecision(
                    strategy_name=name,
                    magic=self.config.magic,
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
        # Resolve p_win for Tier 2 Kelly sizing
        _p_win: float = 0.5
        _p_win_source: str = "neutral_default"
        if _meta_p_win is not None:
            _p_win = _meta_p_win  # Platt-calibrated P(TP|signal) from MetaFilter
            _p_win_source = "meta_filter"
        elif pnl_store is not None:
            from core.execution.kelly_sizer import resolve_p_win_from_brains

            _p_win = resolve_p_win_from_brains(self.brains, pnl_store, direction)
            _p_win_source = "rolling_wr"

        # ── 6a. COLD phase exploration budget (FIX-20260526-041) ──
        # When ConformalOUGate is in COLD phase (calibrator samples < 50),
        # the p_win gate creates a chicken-and-egg deadlock: trading requires
        # calibration, but calibration requires trades.  Forced Exploration
        # Budget: override p_win=0.50 (Kelly mult=1.0, no amplification) and
        # bypass the hard min_p_win gate.  Risk is bounded by the COLD volume
        # cap at 0.01 lot enforced below.  Total exploration budget: ~$7.50-15.
        _is_cold_explore: bool = False
        if "statarb" in name or "ou" in name.lower():
            _ou_gate = getattr(self, "_last_ou_result", None)
            if _ou_gate is not None and _ou_gate.get("force_min_volume"):
                _is_cold_explore = True
                _p_win = 0.50
                _p_win_source = "cold_explore_neutral"

        # ── 6b. Brain confidence → p_win monotonic fallback (FIX-20260531-015) ──
        # Tier-3 fallback: when MetaFilter is unavailable AND PnLStore has
        # insufficient history (< 10 trades), resolve_p_win_from_brains returns
        # the hardcoded fail-closed 0.40.  For new assets this creates a
        # chicken-and-egg deadlock.  Override with brain confidence mapping.
        # confidence ∈ [0.35, 1.0] → p_win ∈ [0.47, 0.60] — bounded.
        _is_fail_closed = _p_win_source == "neutral_default" or (
            _p_win_source == "rolling_wr" and _p_win <= 0.40
        )
        if _is_fail_closed:
            _conf = max(0.0, min(1.0, confidence))
            _p_win = 0.40 + _conf * 0.20
            _p_win_source = "brain_confidence"
        # else: neutral 0.5 → Kelly mult = 1.0 (no amplification or dampening)

        # ── 5f. Dynamic p_win adjustment for OU strategies (FIX-20260526-030) ──
        # In trending regimes, high |z_score| is momentum ignition, not mean
        # reversion.  Inversely discount p_win to prevent Kelly from sizing
        # into anti-informative high-confidence OU signals.
        _p_win = _adjust_p_win_for_regime(_p_win, name, regime_info, entry_z_score, direction)

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
        if sl_dist > 0 and tp_dist > 0 and tp_dist >= sl_dist:
            # Dynamic breakeven floor: only for RR >= 1.0 strategies where
            # every loss = full SL and every win = full TP.
            # When SL > TP (RR < 1.0), the surface scan validates EV
            # with proper timeout/trail modeling — the simple breakeven
            # formula overestimates required p_win (FIX-20260604-084).
            _breakeven_p_win = sl_dist / (tp_dist + sl_dist)
            _effective_min_p_win = max(self.config.min_p_win, _breakeven_p_win)
        if _effective_min_p_win > 0 and _p_win < _effective_min_p_win and not _is_cold_explore:
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
        _is_low_rr = _rr_ratio > 0 and _rr_ratio < 1.0
        if kelly_result.fractional_mult == 0.0 and not _is_low_rr:
            # Hard EV veto — negative expected value trade
            return StrategyDecision(
                strategy_name=name,
                magic=self.config.magic,
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
        _ticks2 = math.floor(volume / self.config.lot_step + 0.5)
        volume = max(self.config.lot_step, round(_ticks2 * self.config.lot_step, 2))

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
        _raw_target = _pre_kelly_raw * _kelly_mult
        import json as _json

        print(
            _json.dumps(
                {
                    "event": "kelly_sizing",
                    "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    "strategy": name,
                    "p_win": round(_p_win, 4),
                    "p_win_source": _p_win_source,
                    "rr_ratio": round(_rr_ratio, 4),
                    "kelly_mult": round(_kelly_mult, 4),
                    "sizing_label": kelly_result.sizing_label,
                    "base_volume": round(_pre_kelly_raw, 4),
                    "raw_target_volume": round(_raw_target, 4),
                    "final_stepped_volume": volume,
                    "cold_explore": _is_cold_explore,
                },
                ensure_ascii=False,
            ),
            flush=True,
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
        }

        # ── Determine venue ──
        _venue = "shadow" if regime_gate_mode == "shadow" else "live"
        _volume = 0.0 if regime_gate_mode == "shadow" else volume
        _should_trade = regime_gate_mode != "shadow"  # shadow: full eval, no real order

        return StrategyDecision(
            strategy_name=name,
            magic=self.config.magic,
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
            try:  # noqa: SIM105
                streak_mult = self.budget.get_streak_multiplier()
            except Exception:
                pass
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


# ── Counter-trend gate helpers ─────────────────────────────────────────


def _counter_trend_action(
    strategy_name: str,
    trend_strength: float,
    h4_trend_strength: float = 0.0,
) -> dict[str, Any]:
    """Determine how a strategy reacts to counter-trend signals.

    Per-strategy rules:
      - barrier_12bar: block at H1 >= 0.30 or H4 >= 0.25,
                       penalise at H1 >= 0.15 or H4 >= 0.15
                       (strict: barrier strategy needs trend alignment)
      - micro_*:       block at H1 >= 0.50, penalise at H1 >= 0.25
                       (moderate: short-horizon microstructure can trade
                       counter-trend, but with reduced conviction + volume)
      - statarb_dynamic: block at H1 >= 0.55 or H4 >= 0.35,
                        penalise at H1 >= 0.30 or H4 >= 0.20
                        (permissive: mean-reversion is counter-trend by design,
                        but strong trends crush OU mean-reversion, especially shorts)
      - statarb_m15:    same as statarb_dynamic — M15 mean-reversion uses
                        identical permissive thresholds

    Penalise now applies BOTH a confidence reduction AND a volume multiplier
    (vol_mult), making the penalty meaningful.  Previously only confidence was
    reduced, and a 0.90→0.72 signal still easily cleared the 0.40 threshold.

    H4 takes priority — higher-TF block fires before H1 thresholds.

    Returns dict with keys: action ("block"|"penalise"|"allow"),
                            confidence_mult, vol_mult (for penalise).
    """
    thresholds: dict[str, dict[str, Any]] = {
        "barrier_12bar": {
            "block": 0.30,
            "penalise": 0.15,
            "conf_mult": 0.60,
            "vol_mult": 0.65,
            "h4_block": 0.25,
            "h4_penalise": 0.15,
            "h4_conf_mult": 0.50,
            "h4_vol_mult": 0.50,
        },
        "micro_3bar": {
            "block": 0.50,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "micro_m15": {
            "block": 0.50,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "micro_h1": {
            "block": 0.45,
            "penalise": 0.22,
            "conf_mult": 0.60,
            "vol_mult": 0.65,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "statarb_dynamic": {
            "block": 0.55,
            "penalise": 0.30,
            "conf_mult": 0.70,
            "vol_mult": 0.75,
            "h4_block": 0.35,
            "h4_penalise": 0.20,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "statarb_m15": {
            "block": 0.55,
            "penalise": 0.30,
            "conf_mult": 0.70,
            "vol_mult": 0.75,
            "h4_block": 0.35,
            "h4_penalise": 0.20,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        # FIX-20260529-039: swing strategies were falling through to default
        # (block=0.40, penalise=0.20).  Swing enters on pullbacks within a
        # trend — counter-trend by design on short TF (M15/M30).
        # H1 block=0.70: only block when H1 trend is dominant (>0.70);
        # H1_TS 0.25-0.70 → penalise path (conf×0.65, vol×0.75).
        # H4 block=0.60: multi-TF consensus required for hard block.
        # Gate-audit evidence (2026-05-29): H1_TS=0.6, H4_TS=0.057 —
        # single-TF trend, not MTF consensus.  Blocking M30 swing shorts
        # on H1-alone trend is over-blocking for short-horizon strategies.
        "m15_swing": {
            "block": 0.70,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.60,
            "h4_penalise": 0.30,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "m30_swing": {
            "block": 0.70,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.60,
            "h4_penalise": 0.30,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        # FIX-20260604-086: h1/h4 swing counter-trend thresholds.
        # Long-horizon strategies need HIGHER block thresholds than short-horizon
        # (m15/m30=0.70).  At H4=0.19 (macro funds idle / wide range-bound),
        # H1=0.54 means price is at the box edge — this is where counter-trend
        # signals capture the most alpha.  Default block=0.40 was silently
        # blocking all non-trivial counter-trend signals on h1/h4.
        "h1_swing": {
            "block": 0.75,
            "penalise": 0.55,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.70,
            "h4_penalise": 0.50,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "h4_swing": {
            "block": 0.80,
            "penalise": 0.60,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.75,
            "h4_penalise": 0.55,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
    }
    t = thresholds.get(
        strategy_name,
        {
            "block": 0.40,
            "penalise": 0.20,
            "conf_mult": 0.60,
            "vol_mult": 0.65,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
    )

    # H4 gate checked first — higher TF takes priority
    if h4_trend_strength >= t["h4_block"]:
        return {
            "action": "block",
            "confidence_mult": t["h4_conf_mult"],
            "vol_mult": t["h4_vol_mult"],
        }
    if h4_trend_strength >= t["h4_penalise"]:
        return {
            "action": "penalise",
            "confidence_mult": t["h4_conf_mult"],
            "vol_mult": t["h4_vol_mult"],
        }

    # H1 thresholds
    if trend_strength >= t["block"]:
        return {"action": "block", "confidence_mult": t["conf_mult"], "vol_mult": t["vol_mult"]}
    if trend_strength >= t["penalise"]:
        return {"action": "penalise", "confidence_mult": t["conf_mult"], "vol_mult": t["vol_mult"]}
    return {"action": "allow", "confidence_mult": 1.0, "vol_mult": 1.0}

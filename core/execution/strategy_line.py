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

import math
from dataclasses import dataclass, field
from typing import Any

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
    entry_context: dict[str, Any] = field(default_factory=dict)
    p_win: float = 0.5  # P(TP|signal) from MetaFilter or rolling PnL win rate
    kelly_mult: float = 1.0  # fractional Kelly multiplier (0.0 = EV veto)
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
    base_volume: float = 0.01
    max_volume: float = 0.05

    # Dynamic SL/TP
    base_sl_atr_mult: float = 2.0
    base_tp_atr_mult: float = 3.5
    hard_sl_ratio: float = 1.5
    ref_atr: float = 5.0

    # Confidence
    confidence_threshold: float = 0.40

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

    # Budget
    daily_loss_limit_pct: float = -0.03
    max_consecutive_losses: int = 5


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

        # ── 1. Regime gate ──
        # "off" is deprecated (v3.0) — legacy guard, should never be reached
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
                _is_regression = _brain_id == "Meta_Stage1_Huber_V1" or _contract.startswith(
                    "barrier_12bar_regression"
                )
                if _is_regression:
                    _raw = getattr(p, "extensions", {}).get("raw_outputs", {}).get("raw_score")
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
            for p in proposals:
                try:
                    pnl_ledger.record_signal(
                        brain_id=getattr(p, "brain_id", "unknown"),
                        symbol="XAUUSDc",
                        direction=p.prediction.get("direction_bias", "neutral"),
                        entry_price=mid_price,
                        confidence=p.prediction.get("confidence", 0.5),
                    )
                except Exception:
                    pass

        # ── 3a3. Capture entry_z_score from OU-style brains ──
        entry_z_score = 0.0
        for p in proposals:
            try:
                raw_outputs = getattr(p, "extensions", {}).get("raw_outputs", {})
                z = raw_outputs.get("z_score")
                if z is not None and float(z) != 0.0:
                    entry_z_score = float(z)
                    break
            except (TypeError, ValueError, AttributeError):
                pass

        # ── 3b. Apply dynamic brain weights from real P&L metrics ──
        if tracker is not None:
            from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

            try:
                weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
                for b_info in self.brains:
                    bid = b_info.get("brain_id", "")
                    if bid:
                        weighter.set_brain_metadata(
                            bid,
                            {
                                "contract_group": b_info.get("contract_group", ""),
                                "feature_schema": b_info.get("feature_schema", ""),
                            },
                        )
                weighter.apply_weights(proposals)
            except Exception:
                pass  # fallback to default weights (1.0)

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
            pred = getattr(p, "prediction", None) or {}
            if isinstance(pred, dict):
                if pred.get("direction_bias", "neutral") != "neutral":
                    _valid_voters += 1
            elif getattr(pred, "direction_bias", "neutral") != "neutral":
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
            pass

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

        # ── Track 3: LightGBM Meta-Filter Gate (OU signal quality) ──
        # For OU-based strategies (statarb_dynamic, barrier_12bar), the
        # 47-dim LightGBM meta-filter predicts P(breakeven | signal_fired).
        # This is a Stage 2 precision filter trained on meta-labeling data —
        # ML amplifies the weak alpha in the rule engine, not creates it.
        if meta_filter_gate is not None and name in ("statarb_dynamic", "barrier_12bar"):
            if meta_filter_gate.is_loaded and feature_vector is not None:
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
                    pass  # Meta-filter failure is non-blocking

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
            )

        # ── 4b. Counter-trend gate ──
        # Block trades that oppose the higher-timeframe trend.
        # Threshold varies by strategy: barrier is strict (needs trend alignment),
        # micro is moderate, statarb ignores trend (mean-reversion logic).
        _ct_vol_mult = 1.0
        if trend_direction != "neutral" and direction != trend_direction:
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

        # ── 4c. Z-score inflection gate (v3.2) ──
        # For OU/statarb strategies: require z-score turning back toward mean.
        # Prevents catching falling knives when z is still accelerating away.
        # Knife 1: z_entry raised to 2.0 — only trade extreme reversions where
        # the edge is strongest and mean-drift risk is lowest.
        if "statarb" in name or "ou" in name.lower():
            if entry_z_score != 0.0:
                _z_entry = 2.0 if "statarb" in name else 1.5
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

        # ── 4d. Meta-Labeling ML Gate (Stage 2) ──
        # Filters barrier_12bar signals through the LGB+MLP ensemble model.
        # Extracts Stage 1 raw prediction from the Huber brain, assembles the
        # 49-dim named feature dict from the V9 + micro ndarrays, and applies
        # Platt calibration + conformal thresholding.  Other strategies pass
        # through unchanged (scope isolation).
        _meta_p_win: float | None = None  # P(TP|signal) for Kelly sizing
        if meta_filter is not None and name == "barrier_12bar":
            _s1_prediction: float | None = None
            for p in proposals:
                try:
                    raw_outputs = getattr(p, "extensions", {}).get("raw_outputs", {})
                    # Meta_Stage1_Huber_V1 uses raw_score (regression bps)
                    _s1 = raw_outputs.get("raw_score")
                    if _s1 is not None:
                        _s1_prediction = float(_s1)
                        break
                except (TypeError, ValueError, AttributeError):
                    pass

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

        dsl = compute_dynamic_sl_tp(
            base_sl_mult=self.config.base_sl_atr_mult,
            base_tp_mult=self.config.base_tp_atr_mult,
            current_atr=current_atr,
            ref_atr=self.config.ref_atr,
            hard_sl_ratio=self.config.hard_sl_ratio,
            timeframe_mult=self.config.timeframe_mult,
            min_sl_distance=self.config.min_sl_distance,
            min_rr_ratio=self.config.min_rr_ratio,
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
        levels = compute_sl_tp_levels(direction, entry_price, dsl)

        # ── 5b. Minimum RR guard (skip for shadow — virtual tracking) ──
        if regime_gate_mode != "shadow":
            tp_dist = abs(levels["take_profit"] - entry_price)
            sl_dist = abs(levels["stop_loss"] - entry_price)
            if sl_dist > 0 and tp_dist / sl_dist < 1.2:
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
        if _meta_p_win is not None:
            _p_win = _meta_p_win  # Platt-calibrated P(TP|signal) from MetaFilter
        elif pnl_store is not None:
            from core.execution.kelly_sizer import resolve_p_win_from_brains

            _p_win = resolve_p_win_from_brains(self.brains, pnl_store, direction)
        # else: neutral 0.5 → Kelly mult = 1.0 (no amplification or dampening)

        # RR ratio from SL/TP levels (already computed in step 5)
        _rr_ratio: float = 1.0
        sl_dist = abs(levels["stop_loss"] - entry_price)
        tp_dist = abs(levels["take_profit"] - entry_price)
        if sl_dist > 0:
            _rr_ratio = tp_dist / sl_dist

        # ── 6b. Tier 2 Kelly/Edge sizing (before _compute_volume, so applied pre-rounding) ──
        from core.execution.kelly_sizer import compute_kelly_mult

        kelly_result = compute_kelly_mult(_p_win, _rr_ratio)
        if kelly_result.fractional_mult == 0.0:
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
                    "rr_ratio": round(_rr_ratio, 4),
                    "kelly_mult": round(_kelly_mult, 4),
                    "sizing_label": kelly_result.sizing_label,
                    "base_volume": round(_pre_kelly_raw, 4),
                    "raw_target_volume": round(_raw_target, 4),
                    "final_stepped_volume": volume,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # ── Build entry context for journal ──
        _brain_preds: list[dict[str, Any]] = []
        for p in proposals:
            pred = getattr(p, "prediction", None) or {}
            _brain_preds.append(
                {
                    "brain_id": getattr(p, "brain_id", "unknown"),
                    "up_prob": round(float(pred.get("up_probability", 0.5)), 4),
                    "down_prob": round(float(pred.get("down_probability", 0.5)), 4),
                    "confidence": round(float(pred.get("confidence", 0.5)), 4),
                    "direction_bias": pred.get("direction_bias", "neutral"),
                }
            )
        entry_context = {
            "atr": round(current_atr, 4),
            "regime": regime_info.get("regime", "normal") if regime_info else "normal",
            "vol_regime": (regime_info.get("regime", "normal") if regime_info else "normal"),
            "trend_direction": trend_direction,
            "macro_regime": macro_regime,
            "brain_predictions": _brain_preds,
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
            entry_context=entry_context,
            p_win=_p_win,
            kelly_mult=kelly_result.fractional_mult,
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
        """Track 2: Meta Pipeline — Huber probe → Stage 2 filter, bypasses Parliament.

        Extracts Meta_Stage1_Huber_V1 raw_score from proposals, maps it to a
        directional signal (|raw_score| > 0.30), and runs the full Stage 2
        LGB+MLP+Platt+Conformal filter chain.  Returns a StrategyDecision if
        the filter approves, None otherwise.
        """
        import json as _json

        # Defense-in-depth: max_volume=0 means shadow-only, no real capital.
        # The call-site gate in evaluate() should catch this, but repeat here
        # in case _try_meta_pipeline is ever called from a different path.
        if self.config.max_volume <= 0:
            return None

        # 1. Extract Huber raw_score from proposals
        s1_prediction: float | None = None
        for p in proposals:
            try:
                raw_outputs = getattr(p, "extensions", {}).get("raw_outputs", {})
                _s1 = raw_outputs.get("raw_score")
                if _s1 is not None:
                    s1_prediction = float(_s1)
                    break
            except (TypeError, ValueError, AttributeError):
                pass

        if s1_prediction is None:
            return None

        # 2. Map raw_score to direction (threshold ±0.30)
        # Score range [-1, 1] maps bps regression output to directional intent.
        if s1_prediction < -0.30:
            meta_dir = "short"
        elif s1_prediction > 0.30:
            meta_dir = "long"
        else:
            return None  # No strong directional signal from Huber probe

        # 3. Run Stage 2 filter (LGB + MLP + Platt + Conformal)
        result = meta_filter.filter_arrays(
            direction=meta_dir,
            s1_prediction=s1_prediction,
            v9_array=feature_vector,
            micro_array=micro_feature_vector,
        )
        if not result.passed:
            print(
                _json.dumps(
                    {
                        "event": "kelly_diag",
                        "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                        "strategy": self.config.name,
                        "stage": "meta_pipeline_rejected",
                        "source": "track2_meta_pipeline",
                        "s1_prediction": round(s1_prediction, 6),
                        "meta_dir": meta_dir,
                        "result_p_win": round(float(getattr(result, "p_win", 0)), 4),
                        "passed": False,
                        "reason": result.reason if result.reason else "threshold",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return None

        _meta_p_win = float(result.p_win)
        print(
            _json.dumps(
                {
                    "event": "kelly_diag",
                    "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    "strategy": self.config.name,
                    "stage": "meta_pipeline_approved",
                    "source": "track2_meta_pipeline",
                    "s1_prediction": round(s1_prediction, 6),
                    "meta_dir": meta_dir,
                    "result_p_win": round(_meta_p_win, 4),
                    "passed": True,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # 4. Compute SL/TP (shared plumbing with Track 1)
        from core.execution.dynamic_sl_tp import compute_dynamic_sl_tp, compute_sl_tp_levels

        dsl = compute_dynamic_sl_tp(
            base_sl_mult=self.config.base_sl_atr_mult,
            base_tp_mult=self.config.base_tp_atr_mult,
            current_atr=current_atr,
            ref_atr=self.config.ref_atr,
            hard_sl_ratio=self.config.hard_sl_ratio,
            timeframe_mult=self.config.timeframe_mult,
            min_sl_distance=self.config.min_sl_distance,
            min_rr_ratio=self.config.min_rr_ratio,
        )

        entry_price = mid_price or 0.0
        if entry_price <= 0:
            return None
        levels = compute_sl_tp_levels(meta_dir, entry_price, dsl)

        # 5. RR check
        entry_price = mid_price or 0.0
        sl_dist = abs(levels["stop_loss"] - entry_price)
        tp_dist = abs(levels["take_profit"] - entry_price)
        _rr_ratio: float = 1.0
        if sl_dist > 0:
            _rr_ratio = tp_dist / sl_dist
        if _rr_ratio < self.config.min_rr_ratio:
            return None

        # 6. Kelly sizing with Platt-calibrated P(TP|signal)
        from core.execution.kelly_sizer import compute_kelly_mult

        kelly_result = compute_kelly_mult(_meta_p_win, _rr_ratio)
        if kelly_result.fractional_mult == 0.0:
            return None  # Negative EV — hard veto
        _kelly_mult = kelly_result.fractional_mult

        # 7. Volume computation (barrier_12bar: no OU exhaustion/debt)
        volume = self._compute_volume(
            _meta_p_win,  # use meta_p_win as confidence proxy
            current_atr,
            regime_info,
            regime_gate_mode,
            macro_regime,
            risk_budget_usd,
            exhaustion_factor=1.0,  # barrier_12bar not OU
            ou_regime_factor=1.0,
            depth_penalty=z_depth_penalty(abs(entry_z_score)),
            kelly_mult=_kelly_mult,
        )
        _ticks2 = math.floor(volume / self.config.lot_step + 0.5)
        volume = max(self.config.lot_step, round(_ticks2 * self.config.lot_step, 2))

        # 8. Diagnostic log
        _pre_kelly_raw = getattr(self, "_last_pre_kelly_size", volume)
        _raw_target = _pre_kelly_raw * _kelly_mult
        print(
            _json.dumps(
                {
                    "event": "kelly_sizing",
                    "time": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    "strategy": self.config.name,
                    "source": "track2_meta_pipeline",
                    "p_win": round(_meta_p_win, 4),
                    "rr_ratio": round(_rr_ratio, 4),
                    "kelly_mult": round(_kelly_mult, 4),
                    "sizing_label": kelly_result.sizing_label,
                    "base_volume": round(_pre_kelly_raw, 4),
                    "raw_target_volume": round(_raw_target, 4),
                    "final_stepped_volume": volume,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # 9. Build entry context
        _brain_preds: list[dict[str, Any]] = []
        for p in proposals:
            pred = getattr(p, "prediction", None) or {}
            _brain_preds.append(
                {
                    "brain_id": getattr(p, "brain_id", "unknown"),
                    "up_prob": round(float(pred.get("up_probability", 0.5)), 4),
                    "down_prob": round(float(pred.get("down_probability", 0.5)), 4),
                    "confidence": round(float(pred.get("confidence", 0.5)), 4),
                    "direction_bias": pred.get("direction_bias", "neutral"),
                }
            )
        entry_context: dict[str, Any] = {
            "atr": round(current_atr, 4),
            "regime": regime_info.get("regime", "normal") if regime_info else "normal",
            "vol_regime": regime_info.get("regime", "normal") if regime_info else "normal",
            "trend_direction": trend_direction,
            "macro_regime": macro_regime,
            "brain_predictions": _brain_preds,
            "meta_pipeline_source": "track2",
            "meta_p_win": round(_meta_p_win, 4),
            "s1_prediction": round(s1_prediction, 6),
        }

        _venue = "shadow" if regime_gate_mode == "shadow" else "live"
        _volume = 0.0 if regime_gate_mode == "shadow" else volume
        _should_trade = regime_gate_mode != "shadow"

        return StrategyDecision(
            strategy_name=self.config.name,
            magic=self.config.magic,
            should_trade=_should_trade,
            direction=meta_dir,
            confidence=round(_meta_p_win, 4),
            volume=_volume,
            sl=levels["stop_loss"],
            tp=levels["take_profit"],
            hard_sl=levels["hard_sl"],
            brain_ids=brain_ids,
            supporting_count=support_count,
            total_count=total_count,
            regime_mode=regime_gate_mode,
            venue=_venue,
            reason=f"meta_pipeline_{meta_dir}",
            entry_z_score=entry_z_score,
            entry_context=entry_context,
            p_win=round(_meta_p_win, 4),
            kelly_mult=round(_kelly_mult, 4),
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

        # Fallback: weighted-average for unknown contract groups (tests, custom setups)
        return self._compute_weighted_fallback(proposals)

    def _compute_weighted_fallback(
        self, proposals: list[Any]
    ) -> tuple[str, float, list[str], int, int]:
        """Original weighted-average consensus — used when no contract group matches."""
        up_scores: list[float] = []
        down_scores: list[float] = []
        weights: list[float] = []
        directions: list[str] = []
        brain_ids: list[str] = []

        for p in proposals:
            bid = getattr(p, "brain_id", "unknown")
            brain_ids.append(bid)

            pred = getattr(p, "prediction", None) or {}
            health = getattr(p, "health", None) or {}

            up = float(pred.get("up_probability", 0.5))
            down = float(pred.get("down_probability", 0.5))
            conf = float(pred.get("confidence", 0.5))
            runtime_ok = not health.get("fallback_used", False)

            vote_weight = float(getattr(p, "vote_weight", 1.0) or 1.0)
            weight = vote_weight * conf * (1.0 if runtime_ok else 0.5)
            up_scores.append(up * weight)
            down_scores.append(down * weight)
            weights.append(weight)

            bias = pred.get("direction_bias", "neutral")
            directions.append(bias if bias in ("long", "short") else "neutral")

        total_weight = sum(weights)
        if total_weight < 1e-9:
            return "neutral", 0.0, brain_ids, 0, len(proposals)

        weighted_up = sum(up_scores) / total_weight
        weighted_down = sum(down_scores) / total_weight

        if weighted_up > weighted_down:
            direction = "long"
            raw_score = weighted_up
        elif weighted_down > weighted_up:
            direction = "short"
            raw_score = weighted_down
        else:
            return "neutral", 0.0, brain_ids, 0, len(proposals)

        neutral_count = directions.count("neutral")
        total = len(proposals)
        if neutral_count > 0:
            raw_score *= max(0.50, 1.0 - (neutral_count / total) * 0.30)

        long_count = directions.count("long")
        short_count = directions.count("short")
        majority_ratio = max(long_count, short_count) / max(total, 1)
        confidence = raw_score * 0.65 + majority_ratio * 0.35

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
                min_lot=0.01,
                max_lot=self.config.max_volume,
                lot_step=0.01,
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
            try:
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
        if size > 0 and size < base_volume * MVS_THRESHOLD:
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

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
    supporting_count: int = 0
    total_count: int = 0
    regime_mode: str = "full"  # "full" | "reduced" | "shadow"
    venue: str = "live"  # "live" | "shadow"
    reason: str = ""
    entry_z_score: float = 0.0  # OU z-score at entry (0 = not an OU strategy or unknown)
    entry_context: dict[str, Any] = field(default_factory=dict)
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
    exit_zscore_enabled: bool = False  # OU mean-reversion exit gate
    exit_min_r: float = 0.3  # minimum R to hold during time-decay phases

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
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        meta_filter: Any = None,
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
        # Count brains that produced a non-neutral directional signal.
        # If too few brains vote (but at least one did), the strategy line
        # cannot form a valid consensus — prevents single-brain decision on
        # multi-brain lines.  All-neutral proposals pass through to
        # consensus computation (which will naturally return neutral).
        _valid_voters = 0
        for p in proposals:
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

            record_brain_votes(
                proposals=proposals,
                strategy_name=name,
                consensus_direction=direction,
                consensus_confidence=confidence,
                symbol=getattr(self.config, "symbol", "XAUUSDc"),
                base_dir="data",
            )
        except Exception:
            pass

        if direction == "neutral" or confidence < self.config.confidence_threshold:
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
                reason="low_confidence" if direction != "neutral" else "neutral_consensus",
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

        # ── 5. Dynamic SL/TP ──
        from core.execution.dynamic_sl_tp import compute_dynamic_sl_tp, compute_sl_tp_levels

        dsl = compute_dynamic_sl_tp(
            base_sl_mult=self.config.base_sl_atr_mult,
            base_tp_mult=self.config.base_tp_atr_mult,
            current_atr=current_atr,
            ref_atr=self.config.ref_atr,
            hard_sl_ratio=self.config.hard_sl_ratio,
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
        # v3.1: compute OU bandit factors (exhaustion + regime) for statarb strategies
        _ou_regime_factor = 1.0
        _exhaustion_factor = 1.0
        if "statarb" in name or "ou" in name.lower():
            if regime_info is not None:
                _ou_regime_factor = float(regime_info.get("ou_regime_factor", 1.0))
            _exhaustion_factor = sigmoid_exhaustion(abs(entry_z_score))
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
        )
        # Apply counter-trend volume penalty
        volume *= _ct_vol_mult

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
        )

    # ── Consensus computation ───────────────────────────────────────────

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
    ) -> float:
        """Compute dynamic volume with bandit sizing (v3.1 + v3.2 depth decay).

        When risk_budget_usd > 0, uses vol-targeted sizing:
          base = risk_budget / (ATR × SL_mult × contract_size)
        Otherwise falls back to fixed base_volume.

        v3.2 bandit formula:
          M = base_lot × agreement × gate × vol × macro
              × exhaustion (sigmoid) × ou_regime × depth_penalty
          → apply_mvs(M) → round_to_lot_step
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

        # v3.1: MVS cut-off — kill micro-positions where multiplier too low
        effective_mult = apply_mvs(effective_mult, threshold=MVS_THRESHOLD)

        size = base_volume * effective_mult

        # ── Graduated streak reduction ──
        streak_mult = 1.0
        if self.budget is not None:
            try:
                streak_mult = self.budget.get_streak_multiplier()
            except Exception:
                pass
        size *= streak_mult

        # Round to 0.01 lot step (MT5 requirement)
        return max(0.01, min(self.config.max_volume, round(size, 2)))


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
      - statarb_dynamic: never block (mean-reversion is counter-trend by design)

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
            "block": 0.99,
            "penalise": 0.99,
            "conf_mult": 1.0,
            "vol_mult": 1.0,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
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

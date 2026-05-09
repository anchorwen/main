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

from dataclasses import dataclass, field
from typing import Any

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
    regime_mode: str = "full"  # "full" | "reduced" | "off"
    reason: str = ""


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

    # ── Subclass overrides ──────────────────────────────────────────────

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
    ) -> list[Any]:
        """Run brain inference for this strategy's brains.

        Subclasses override this to route the correct feature vector to each
        brain type.  Returns a list of BrainDecisionProposal objects.
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
        tracker: Any = None,
        pnl_ledger: Any = None,
    ) -> StrategyDecision:
        """Run the full strategy evaluation for one cycle.

        Args:
            trend_direction: Primary trend from multi-timeframe analysis
                             ("long"/"short"/"neutral").  Counter-trend trades
                             are blocked or penalised depending on strength.
            trend_strength: [0, 1] strength of the primary trend.

        Returns a StrategyDecision — may have should_trade=False.
        """
        name = self.config.name

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
            proposals = self._run_inference(feature_vector, micro_feature_vector, mid_price)
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

        # ── 4. Group consensus ──
        direction, confidence, brain_ids, support_count, total_count = self._compute_consensus(
            proposals
        )

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
        if trend_direction != "neutral" and direction != trend_direction:
            ct_block = _counter_trend_action(name, trend_strength)
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
        levels = compute_sl_tp_levels(direction, entry_price, dsl)

        # ── 5b. Minimum RR guard ──
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
                reason="rr_below_minimum",
            )

        # ── 6. Volume ──
        volume = self._compute_volume(confidence, current_atr, regime_info, regime_gate_mode)

        # ── 7. Record counterfactual signals ──
        if pnl_ledger is not None and mid_price is not None and mid_price > 0:
            try:
                for p in proposals:
                    pnl_ledger.record_signal(
                        brain_id=getattr(p, "brain_id", "unknown"),
                        symbol="XAUUSDc",
                        direction=p.prediction.get("direction_bias", "neutral"),
                        entry_price=mid_price,
                        confidence=p.prediction.get("confidence", 0.5),
                    )
            except Exception:
                pass

        return StrategyDecision(
            strategy_name=name,
            magic=self.config.magic,
            should_trade=True,
            direction=direction,
            confidence=round(confidence, 4),
            volume=volume,
            sl=levels["stop_loss"],
            tp=levels["take_profit"],
            hard_sl=levels["hard_sl"],
            brain_ids=brain_ids,
            supporting_count=support_count,
            total_count=total_count,
            regime_mode=regime_gate_mode,
            reason="approved",
        )

    # ── Consensus computation ───────────────────────────────────────────

    def _compute_consensus(self, proposals: list[Any]) -> tuple[str, float, list[str], int, int]:
        """Weighted within-group consensus for homogeneous proposals.

        All proposals in this list should be from brains trained on the
        SAME contract, so their up/down probabilities are commensurate.

        Returns: (direction, confidence, brain_ids, support_count, total_count)
        """
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

        if weighted_up >= weighted_down:
            direction = "long"
            raw_score = weighted_up
        else:
            direction = "short"
            raw_score = weighted_down

        # Neutral penalty
        neutral_count = directions.count("neutral")
        total = len(proposals)
        if neutral_count > 0:
            raw_score *= max(0.50, 1.0 - (neutral_count / total) * 0.30)

        # Majority agreement boost
        long_count = directions.count("long")
        short_count = directions.count("short")
        majority_ratio = max(long_count, short_count) / max(total, 1)
        confidence = raw_score * 0.65 + majority_ratio * 0.35

        # Direction balance — counteract systemic LONG bias
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
    ) -> float:
        """Compute dynamic volume for this trade."""

        if current_atr <= 0:
            current_atr = self.config.ref_atr

        # Agreement factor: confidence directly scales volume
        agreement_factor = 0.45 + confidence * 0.55  # maps [0,1] to [0.45, 1.0]

        # Regime gate factor
        gate_factors = {"full": 1.0, "reduced": 0.65, "off": 0.0}
        gate_factor = gate_factors.get(regime_gate_mode, 1.0)

        # Volatility regime factor (from RegimeDetector)
        vol_regime = regime_info.get("regime", "normal") if regime_info else "normal"
        vol_factors = {
            "low": self.config.regime_vol_mult_low,
            "normal": self.config.regime_vol_mult_normal,
            "high": self.config.regime_vol_mult_high,
        }
        vol_factor = vol_factors.get(vol_regime, 1.0)

        size = self.config.base_volume * agreement_factor * gate_factor * vol_factor

        # Round to 0.01 lot step (MT5 requirement)
        return max(0.01, min(self.config.max_volume, round(size, 2)))


# ── Counter-trend gate helpers ─────────────────────────────────────────


def _counter_trend_action(strategy_name: str, trend_strength: float) -> dict[str, Any]:
    """Determine how a strategy reacts to counter-trend signals.

    Per-strategy rules:
      - barrier_12bar: block counter-trend at strength >= 0.30,
                       penalise at strength >= 0.15
      - micro_3bar:    block at strength >= 0.50,
                       penalise at strength >= 0.25
      - statarb_dynamic: never block (mean-reversion logic is counter-trend
                        by design)

    Returns dict with keys: action ("block"|"penalise"|"allow"),
                            confidence_mult (for penalise).
    """
    thresholds = {
        "barrier_12bar": {"block": 0.30, "penalise": 0.15, "conf_mult": 0.75},
        "micro_3bar": {"block": 0.50, "penalise": 0.25, "conf_mult": 0.82},
        "statarb_dynamic": {"block": 0.99, "penalise": 0.99, "conf_mult": 1.0},
    }
    t = thresholds.get(strategy_name, {"block": 0.40, "penalise": 0.20, "conf_mult": 0.80})

    if trend_strength >= t["block"]:
        return {"action": "block", "confidence_mult": t["conf_mult"]}
    if trend_strength >= t["penalise"]:
        return {"action": "penalise", "confidence_mult": t["conf_mult"]}
    return {"action": "allow", "confidence_mult": 1.0}

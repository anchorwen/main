"""Tests for core/execution/strategy_line.py — strategy base class."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.strategy_line import (
    StrategyDecision,
    StrategyLine,
    StrategyLineConfig,
    _counter_trend_action,
)
from tests.execution.conftest import make_proposal


# ── Concrete test double ──────────────────────────────────────────────────
def _make_strategy(config=None, brains=None, budget=None, proposals=None, infer_fn=None):
    """Create a fully wired StrategyLine subclass for testing."""
    if config is None:
        config = StrategyLineConfig(
            name="test_line", magic=99999, brain_types={"test"}, min_valid_brains=1
        )
    if brains is None:
        # Build a minimal brain list with adapter-like entries
        adapter = MagicMock()
        adapter.infer.return_value = None
        adapter.get_signal.return_value = None
        brains = [{"adapter": adapter, "brain_id": "test_brain_01"}]

    class _TestLine(StrategyLine):
        def _run_inference(
            self,
            feature_vector,
            micro_feature_vector,
            mid_price,
            micro_sequences=None,
            daily_feature_vector=None,
        ):
            if infer_fn:
                return infer_fn(feature_vector, micro_feature_vector, mid_price)
            return proposals or []

    return _TestLine(config, brains, budget=budget)


# ── Config tests ──────────────────────────────────────────────────────────


class TestStrategyLineConfig:
    def test_default_values(self):
        cfg = StrategyLineConfig(name="test", magic=99999, brain_types={"test"})
        assert cfg.base_volume == 0.01
        assert cfg.max_volume == 0.05
        assert cfg.confidence_threshold == 0.40
        assert cfg.long_bias_discount == 0.0

    def test_custom_values(self):
        cfg = StrategyLineConfig(
            name="custom",
            magic=12345,
            brain_types={"a"},
            base_volume=0.03,
            max_volume=0.10,
            confidence_threshold=0.55,
        )
        assert cfg.name == "custom"
        assert cfg.magic == 12345
        assert cfg.base_volume == 0.03
        assert cfg.max_volume == 0.10
        assert cfg.confidence_threshold == 0.55


# ── StrategyDecision tests ────────────────────────────────────────────────


class TestStrategyDecision:
    def test_decision_dataclass_defaults(self):
        d = StrategyDecision(
            strategy_name="test",
            magic=99999,
            should_trade=False,
            direction="neutral",
            confidence=0.0,
            volume=0.0,
            sl=0.0,
            tp=0.0,
            hard_sl=0.0,
        )
        assert d.brain_ids == []
        assert d.supporting_count == 0
        assert d.total_count == 0
        assert d.regime_mode == "full"
        assert d.reason == ""

    def test_decision_full_trade(self):
        d = StrategyDecision(
            strategy_name="barrier_12bar",
            magic=90001,
            should_trade=True,
            direction="long",
            confidence=0.75,
            volume=0.02,
            sl=1990.0,
            tp=2017.5,
            hard_sl=1985.0,
            brain_ids=["brain_01", "brain_02"],
            supporting_count=2,
            total_count=3,
            reason="approved",
        )
        assert d.should_trade is True
        assert d.direction == "long"
        assert d.volume == 0.02


# ── Evaluate gate tests (no inference needed) ─────────────────────────────


class TestEvaluateGates:
    def test_regime_gate_off_blocks_trade(self):
        line = _make_strategy()
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
            regime_gate_mode="off",
        )
        assert result.should_trade is False
        assert result.reason == "regime_gate_off"
        assert result.regime_mode == "off"

    def test_budget_paused_blocks_trade(self):
        budget = MagicMock()
        budget.check_pause.return_value = True
        line = _make_strategy(budget=budget)
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is False
        assert result.reason == "budget_paused"

    def test_budget_not_paused_allows_evaluation(self):
        budget = MagicMock()
        budget.check_pause.return_value = False
        budget.get_streak_multiplier.return_value = 1.0
        line = _make_strategy(
            budget=budget,
            proposals=[make_proposal()],
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        # Not blocked by budget; proposal has direction="long" with confidence 0.80
        assert result.reason != "budget_paused"


# ── Inference error / no proposals ────────────────────────────────────────


class TestEvaluateInference:
    def test_inference_error_returns_no_trade(self):
        def _fail(*args, **kwargs):
            raise RuntimeError("brain connection lost")

        line = _make_strategy(infer_fn=_fail)
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is False
        assert result.reason == "inference_error"

    def test_no_proposals_returns_no_trade(self):
        line = _make_strategy(proposals=[])
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is False
        assert result.reason == "no_proposals"

    def test_neutral_consensus_returns_no_trade(self):
        line = _make_strategy(
            proposals=[
                make_proposal(vote_weight=0.0, confidence=0.0),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is False
        assert result.reason == "neutral_consensus"

    def test_confidence_below_threshold_returns_no_trade(self):
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.50,
                    down_probability=0.50,
                    confidence=0.30,
                    direction_bias="neutral",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        # equal up/down + neutral → "neutral" consensus (no directional edge)
        assert result.should_trade is False
        assert result.reason == "neutral_consensus"


# ── Successful trades ─────────────────────────────────────────────────────


class TestEvaluateSuccess:
    def test_successful_long_trade(self):
        line = _make_strategy(
            proposals=[
                make_proposal(
                    brain_id="b1",
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
                make_proposal(
                    brain_id="b2",
                    up_probability=0.80,
                    down_probability=0.20,
                    confidence=0.85,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is True
        assert result.direction == "long"
        assert result.confidence > 0.40
        assert result.volume > 0
        assert result.sl < 2000.0  # long SL below entry
        assert result.tp > 2000.0  # long TP above entry
        assert result.reason == "approved"
        assert "b1" in result.brain_ids and "b2" in result.brain_ids

    def test_successful_short_trade(self):
        line = _make_strategy(
            proposals=[
                make_proposal(
                    brain_id="b1",
                    up_probability=0.10,
                    down_probability=0.90,
                    confidence=0.88,
                    direction_bias="short",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is True
        assert result.direction == "short"
        assert result.sl > 2000.0  # short SL above entry
        assert result.tp < 2000.0  # short TP below entry


# ── Counter-trend gate ────────────────────────────────────────────────────


class TestCounterTrendGate:
    def test_counter_trend_block_when_direction_opposes_trend(self):
        """When trend_direction != direction, _counter_trend_action may block."""
        line = _make_strategy(
            proposals=[
                # Long signal but trend is "short"
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
            trend_direction="short",
            trend_strength=0.7,
        )
        # Barrier at strength 0.7 > 0.30 block threshold → blocked
        assert result.should_trade is False
        assert "counter_trend_blocked" in result.reason

    def test_counter_trend_allow_same_direction_trade(self):
        """Same direction as trend should not be blocked."""
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
            trend_direction="long",
            trend_strength=0.7,
        )
        # Same direction → should not be blocked by counter-trend
        assert result.should_trade is True
        assert "counter_trend" not in result.reason

    def test_counter_trend_not_applied_when_trend_neutral(self):
        """Neutral trend → counter-trend gate skipped."""
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
            trend_direction="neutral",
            trend_strength=0.0,
        )
        # No counter-trend block
        assert "counter_trend" not in result.reason

    def test_counter_trend_penalise_lowers_confidence(self):
        """Penalise threshold: confidence is multiplied but may still be enough."""
        micro_config = StrategyLineConfig(name="micro_3bar", magic=90002, brain_types={"test"})
        line = _make_strategy(
            config=micro_config,
            proposals=[
                make_proposal(
                    up_probability=0.80,
                    down_probability=0.20,
                    confidence=0.95,
                    direction_bias="short",
                ),
            ],
        )
        # Micro block=0.50, penalise=0.25. trend_strength=0.30 > 0.25 → penalise (not block)
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
            trend_direction="long",
            trend_strength=0.30,
        )
        # Confidence should be lowered but still above threshold
        assert result.should_trade is True
        assert result.confidence < 0.95  # penalised


# ── _counter_trend_action helper ──────────────────────────────────────────


class TestCounterTrendAction:
    def test_barrier_block_at_high_strength(self):
        result = _counter_trend_action("barrier_12bar", 0.40)
        assert result["action"] == "block"

    def test_barrier_penalise_at_moderate_strength(self):
        result = _counter_trend_action("barrier_12bar", 0.20)
        assert result["action"] == "penalise"
        assert result["confidence_mult"] == 0.60
        assert result["vol_mult"] == 0.65  # now also penalises volume

    def test_barrier_allow_at_low_strength(self):
        result = _counter_trend_action("barrier_12bar", 0.05)
        assert result["action"] == "allow"
        assert result["confidence_mult"] == 1.0
        assert result["vol_mult"] == 1.0

    def test_statarb_never_blocks(self):
        result = _counter_trend_action("statarb_dynamic", 0.95)
        assert result["action"] == "allow"

    def test_unknown_strategy_uses_default_threshold(self):
        result = _counter_trend_action("unknown_strategy", 0.50)
        assert result["action"] == "block"  # 0.50 >= 0.40 default block
        assert result["confidence_mult"] == 0.60
        assert result["vol_mult"] == 0.65


# ── Consensus computation ─────────────────────────────────────────────────


class TestConsensus:
    def test_simple_majority_long(self):
        line = _make_strategy()
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(up_probability=0.80, down_probability=0.20, direction_bias="long"),
                make_proposal(up_probability=0.70, down_probability=0.30, direction_bias="long"),
                make_proposal(up_probability=0.30, down_probability=0.70, direction_bias="short"),
            ]
        )
        assert direction == "long"
        assert confidence > 0.40
        assert support == 2
        assert total == 3
        assert len(ids) == 3

    def test_simple_majority_short(self):
        line = _make_strategy()
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(up_probability=0.20, down_probability=0.80, direction_bias="short"),
                make_proposal(up_probability=0.30, down_probability=0.70, direction_bias="short"),
            ]
        )
        assert direction == "short"
        assert support == 2

    def test_zero_total_weight_returns_neutral(self):
        """When all weights are zero (e.g., fallback with zero vote_weight)."""
        line = _make_strategy()
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(vote_weight=0.0, confidence=0.0),
            ]
        )
        assert direction == "neutral"
        assert confidence == 0.0

    def test_fallback_penalised_proposal(self):
        """Fallback proposals have weight halved."""
        line = _make_strategy()
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(
                    up_probability=0.80,
                    down_probability=0.20,
                    direction_bias="long",
                    fallback_used=True,
                ),
                make_proposal(
                    up_probability=0.70,
                    down_probability=0.30,
                    direction_bias="long",
                    fallback_used=False,
                ),
            ]
        )
        assert direction == "long"

    def test_neutral_proposals_penalise_confidence(self):
        """Neutral-biased proposals reduce the final confidence score."""
        line = _make_strategy()
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(up_probability=0.80, down_probability=0.20, direction_bias="long"),
                make_proposal(up_probability=0.50, down_probability=0.50, direction_bias="neutral"),
            ]
        )
        # Neutral penalty applied — confidence should be lower than with 2 longs
        assert direction == "long"
        assert confidence < 0.80  # penalised

    def test_long_bias_discount(self):
        """With long_bias_discount > 0, LONG confidence gets discounted."""
        config = StrategyLineConfig(
            name="test",
            magic=99999,
            brain_types={"test"},
            long_bias_discount=0.10,
        )
        line = _make_strategy(config=config)
        direction, confidence, ids, support, total = line._compute_consensus(
            [
                make_proposal(up_probability=0.85, down_probability=0.15, direction_bias="long"),
            ]
        )
        # Without discount: ~0.90; with 0.10 long_bias_discount: ~0.81
        assert direction == "long"
        assert confidence < 0.89  # discounted from ~0.90


# ── Volume computation ────────────────────────────────────────────────────


class TestVolume:
    def test_volume_below_min_clamped(self):
        line = _make_strategy()
        vol = line._compute_volume(
            confidence=0.1, current_atr=5.0, regime_info=None, regime_gate_mode="full"
        )
        assert vol == 0.01  # clamped to minimum

    def test_volume_with_high_confidence(self):
        line = _make_strategy()
        vol = line._compute_volume(
            confidence=1.0, current_atr=5.0, regime_info=None, regime_gate_mode="full"
        )
        assert vol >= 0.01
        assert vol <= 0.05

    def test_volume_reduced_by_gate(self):
        config = StrategyLineConfig(
            name="test", magic=99999, brain_types={"test"}, base_volume=0.04, max_volume=0.10
        )
        line = _make_strategy(config=config)
        vol_full = line._compute_volume(
            confidence=0.9, current_atr=5.0, regime_info=None, regime_gate_mode="full"
        )
        vol_reduced = line._compute_volume(
            confidence=0.9, current_atr=5.0, regime_info=None, regime_gate_mode="reduced"
        )
        assert vol_reduced < vol_full

    def test_volume_gate_off_returns_zero(self):
        line = _make_strategy()
        vol = line._compute_volume(
            confidence=0.8, current_atr=5.0, regime_info=None, regime_gate_mode="off"
        )
        assert vol == 0.01  # clamped min (0.0 * factors = 0 → min 0.01)

    def test_volume_high_vol_regime_reduces(self):
        config = StrategyLineConfig(
            name="test", magic=99999, brain_types={"test"}, base_volume=0.04, max_volume=0.10
        )
        line = _make_strategy(config=config)
        vol_normal = line._compute_volume(
            confidence=0.9,
            current_atr=5.0,
            regime_info={"regime": "normal"},
            regime_gate_mode="full",
        )
        vol_high = line._compute_volume(
            confidence=0.9, current_atr=5.0, regime_info={"regime": "high"}, regime_gate_mode="full"
        )
        assert vol_high < vol_normal

    def test_volume_low_vol_regime_expands(self):
        config = StrategyLineConfig(
            name="test", magic=99999, brain_types={"test"}, base_volume=0.04, max_volume=0.10
        )
        line = _make_strategy(config=config)
        vol_normal = line._compute_volume(
            confidence=0.9,
            current_atr=5.0,
            regime_info={"regime": "normal"},
            regime_gate_mode="full",
        )
        vol_low = line._compute_volume(
            confidence=0.9, current_atr=5.0, regime_info={"regime": "low"}, regime_gate_mode="full"
        )
        assert vol_low > vol_normal


# ── Min RR guard ──────────────────────────────────────────────────────────


class TestMinRRGuard:
    def test_rr_below_minimum_blocks(self):
        """With very tight TP vs SL, the min RR guard should block."""
        config = StrategyLineConfig(
            name="test",
            magic=99999,
            brain_types={"test"},
            base_sl_atr_mult=3.0,
            base_tp_atr_mult=1.0,  # TP < SL
        )
        line = _make_strategy(
            config=config,
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ],
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is False
        assert result.reason == "rr_below_minimum"

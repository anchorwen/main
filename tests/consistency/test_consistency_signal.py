"""Consistency tests — deterministic behaviour verification."""

from __future__ import annotations

from core.execution.strategy_line import StrategyLineConfig
from tests.execution.conftest import make_proposal
from tests.execution.test_strategy_line import _make_strategy


class TestSignalDeterminism:
    def test_same_input_same_decision(self):
        """Identical proposals should produce identical decisions."""
        config = StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"test"})
        proposals = [
            make_proposal(
                up_probability=0.85, down_probability=0.15, confidence=0.90, direction_bias="long"
            ),
            make_proposal(
                up_probability=0.80, down_probability=0.20, confidence=0.85, direction_bias="long"
            ),
        ]

        decisions = []
        for _ in range(3):
            line = _make_strategy(config=config, proposals=proposals)
            result = line.evaluate(
                feature_vector=None,
                micro_feature_vector=None,
                mid_price=2000.0,
            )
            decisions.append(result)

        # All runs should be identical
        for attr in ("should_trade", "direction", "confidence", "volume", "sl", "tp"):
            values = [getattr(d, attr) for d in decisions]
            assert all(v == values[0] for v in values), f"{attr} differs: {values}"

    def test_same_input_same_sl_tp(self):
        """With same ATR and mid_price, SL/TP should be identical."""
        config = StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"test"})
        proposals = [
            make_proposal(
                up_probability=0.85, down_probability=0.15, confidence=0.90, direction_bias="long"
            )
        ]

        sl_values = []
        tp_values = []
        for _ in range(5):
            line = _make_strategy(config=config, proposals=proposals)
            result = line.evaluate(
                feature_vector=None,
                micro_feature_vector=None,
                mid_price=2000.0,
                current_atr=5.0,
            )
            sl_values.append(result.sl)
            tp_values.append(result.tp)

        assert len(set(sl_values)) == 1, f"SL not deterministic: {sl_values}"
        assert len(set(tp_values)) == 1, f"TP not deterministic: {tp_values}"

    def test_consensus_deterministic(self):
        """Consensus algorithm should be deterministic for same inputs."""
        config = StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"test"})
        proposals = [
            make_proposal(up_probability=0.75, down_probability=0.25, direction_bias="long"),
            make_proposal(up_probability=0.70, down_probability=0.30, direction_bias="long"),
            make_proposal(up_probability=0.20, down_probability=0.80, direction_bias="short"),
        ]

        results = []
        for _ in range(3):
            line = _make_strategy(config=config)
            result = line._compute_consensus(proposals)
            results.append(result)

        for i in range(len(results[0])):
            values = [r[i] for r in results]
            if isinstance(values[0], list):
                assert all(v == values[0] for v in values)
            else:
                assert all(v == values[0] for v in values), f"Index {i}: {values}"

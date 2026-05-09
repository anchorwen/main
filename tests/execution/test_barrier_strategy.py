"""Tests for core/execution/barrier_strategy.py — 60-min barrier predictor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.execution.barrier_strategy import BarrierStrategy
from core.execution.strategy_line import StrategyLineConfig
from tests.execution.conftest import make_proposal


@pytest.fixture
def barrier_brain():
    """Create a minimal brain entry for barrier strategy."""
    adapter = MagicMock()
    adapter.infer.return_value = {"raw_output": [0.85, 0.15]}
    adapter.get_signal.return_value = make_proposal(
        brain_id="onnx_v9_01",
        up_probability=0.85,
        down_probability=0.15,
        confidence=0.80,
        direction_bias="long",
    )
    return {"adapter": adapter, "brain_id": "onnx_v9_01"}


class TestBarrierStrategyInference:
    def test_passes_feature_vector_to_adapter(self, barrier_brain):
        strat = BarrierStrategy(
            config=StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"onnx_v9"}),
            brains=[barrier_brain],
        )
        proposals = strat._run_inference(
            feature_vector=[1.0] * 40,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert len(proposals) == 1
        barrier_brain["adapter"].infer.assert_called_once_with([1.0] * 40)

    def test_brain_id_stamped_on_proposal(self, barrier_brain):
        strat = BarrierStrategy(
            config=StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"onnx_v9"}),
            brains=[barrier_brain],
        )
        proposals = strat._run_inference([], None, 2000.0)
        assert proposals[0].brain_id == "onnx_v9_01"

    def test_adapter_exception_skipped(self, barrier_brain):
        fail_adapter = MagicMock()
        fail_adapter.infer.side_effect = RuntimeError("crash")
        brains = [
            {"adapter": fail_adapter, "brain_id": "crash_brain"},
            barrier_brain,
        ]
        strat = BarrierStrategy(
            config=StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"onnx_v9"}),
            brains=brains,
        )
        proposals = strat._run_inference([], None, 2000.0)
        # Only the non-crashing brain produces a proposal
        assert len(proposals) == 1
        assert proposals[0].brain_id == "onnx_v9_01"

    def test_empty_brains_returns_empty(self):
        strat = BarrierStrategy(
            config=StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"onnx_v9"}),
            brains=[],
        )
        proposals = strat._run_inference([], None, 2000.0)
        assert proposals == []

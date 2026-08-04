"""Tests for core/execution/micro_strategy.py — 3-bar tick microstructure."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.execution.micro_strategy import MicroStrategy
from core.execution.strategy_line import StrategyLineConfig
from tests.execution.conftest import make_proposal
from tests.mock_kit.config_factory import TEST_BASE_DIR


@pytest.fixture
def micro_brain():
    """Create a minimal brain entry for micro strategy."""
    adapter = MagicMock()
    adapter.inference.return_value = make_proposal(
        brain_id="xgb_v4.5_01",
        up_probability=0.75,
        down_probability=0.25,
        confidence=0.82,
        direction_bias="long",
    )
    return {"adapter": adapter, "brain_id": "xgb_v4.5_01"}


class TestMicroStrategyInference:
    def test_passes_micro_feature_vector_to_adapter(self, micro_brain):
        strat = MicroStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR, name="micro_3bar", magic=90002, brain_types={"xgboost_v4.5"}
            ),
            brains=[micro_brain],
        )
        micro_fv = [0.5] * 9
        proposals = strat._run_inference(
            feature_vector=[1.0] * 40,
            micro_feature_vector=micro_fv,
            mid_price=2000.0,
        )
        assert len(proposals) == 1
        # Micro strategy passes micro_feature_vector, not feature_vector
        micro_brain["adapter"].inference.assert_called_once_with(micro_fv)

    def test_brain_id_stamped(self, micro_brain):
        strat = MicroStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR, name="micro_3bar", magic=90002, brain_types={"xgboost_v4.5"}
            ),
            brains=[micro_brain],
        )
        proposals = strat._run_inference([], [], 2000.0)
        assert proposals[0].brain_id == "xgb_v4.5_01"

    def test_adapter_exception_skipped(self, micro_brain):
        fail_adapter = MagicMock()
        fail_adapter.inference.side_effect = RuntimeError("timeout")
        brains = [
            {"adapter": fail_adapter, "brain_id": "fail_brain"},
            micro_brain,
        ]
        strat = MicroStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR, name="micro_3bar", magic=90002, brain_types={"xgboost_v4.5"}
            ),
            brains=brains,
        )
        proposals = strat._run_inference([], [], 2000.0)
        assert len(proposals) == 1

    def test_empty_brains_returns_empty(self):
        strat = MicroStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR, name="micro_3bar", magic=90002, brain_types={"xgboost_v4.5"}
            ),
            brains=[],
        )
        proposals = strat._run_inference([], [], 2000.0)
        assert proposals == []

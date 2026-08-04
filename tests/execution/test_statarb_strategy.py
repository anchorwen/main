"""Tests for core/execution/statarb_strategy.py — OU mean-reversion."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.execution.statarb_strategy import StatArbStrategy
from core.execution.strategy_line import StrategyLineConfig
from tests.execution.conftest import make_proposal
from tests.mock_kit.config_factory import TEST_BASE_DIR


@pytest.fixture
def statarb_brain():
    """Create a minimal brain entry for statarb strategy."""
    adapter = MagicMock()
    adapter.inference.return_value = make_proposal(
        brain_id="ou_params_v6_01",
        up_probability=0.88,
        down_probability=0.12,
        confidence=0.85,
        direction_bias="long",
    )
    return {"adapter": adapter, "brain_id": "ou_params_v6_01"}


class TestStatArbStrategyInference:
    def test_passes_mid_price_to_adapter(self, statarb_brain):
        strat = StatArbStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR,
                name="statarb_dynamic",
                magic=90003,
                brain_types={"ou_params_v6"},
            ),
            brains=[statarb_brain],
        )
        proposals = strat._run_inference(
            feature_vector=[1.0] * 40,
            micro_feature_vector=[0.5] * 9,
            mid_price=2015.75,
        )
        assert len(proposals) == 1
        call_arg = statarb_brain["adapter"].inference.call_args[0][0]
        assert isinstance(call_arg, np.ndarray)
        assert call_arg.dtype == np.float32
        assert float(call_arg[0]) == pytest.approx(2015.75)

    def test_brain_id_stamped(self, statarb_brain):
        strat = StatArbStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR,
                name="statarb_dynamic",
                magic=90003,
                brain_types={"ou_params_v6"},
            ),
            brains=[statarb_brain],
        )
        proposals = strat._run_inference([], [], 2015.0)
        assert proposals[0].brain_id == "ou_params_v6_01"

    def test_adapter_exception_skipped(self, statarb_brain):
        fail_adapter = MagicMock()
        fail_adapter.inference.side_effect = RuntimeError("ou divergence")
        brains = [
            {"adapter": fail_adapter, "brain_id": "crash_brain"},
            statarb_brain,
        ]
        strat = StatArbStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR,
                name="statarb_dynamic",
                magic=90003,
                brain_types={"ou_params_v6"},
            ),
            brains=brains,
        )
        proposals = strat._run_inference([], [], 2000.0)
        assert len(proposals) == 1

    def test_none_mid_price_uses_zero(self, statarb_brain):
        """When mid_price is None, 0.0 is used as fallback."""
        strat = StatArbStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR,
                name="statarb_dynamic",
                magic=90003,
                brain_types={"ou_params_v6"},
            ),
            brains=[statarb_brain],
        )
        _ = strat._run_inference([], [], None)
        call_arg = statarb_brain["adapter"].inference.call_args[0][0]
        assert float(call_arg[0]) == 0.0

    def test_empty_brains_returns_empty(self):
        strat = StatArbStrategy(
            config=StrategyLineConfig(
                base_dir=TEST_BASE_DIR,
                name="statarb_dynamic",
                magic=90003,
                brain_types={"ou_params_v6"},
            ),
            brains=[],
        )
        proposals = strat._run_inference([], [], 2000.0)
        assert proposals == []

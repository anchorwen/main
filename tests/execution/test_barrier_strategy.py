"""Tests for core/execution/barrier_strategy.py — 60-min barrier predictor."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.execution.barrier_strategy import BarrierStrategy
from core.execution.strategy_line import StrategyLineConfig
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from tests.execution.conftest import make_proposal


@pytest.fixture
def barrier_brain():
    """Create a minimal brain entry for barrier strategy."""
    adapter = MagicMock()
    adapter.inference.return_value = make_proposal(
        brain_id="xgboost_v9_01",
        up_probability=0.85,
        down_probability=0.15,
        confidence=0.80,
        direction_bias="long",
    )
    return {"adapter": adapter, "brain_id": "xgboost_v9_01"}


class TestBarrierStrategyInference:
    def test_passes_feature_vector_to_adapter(self, barrier_brain):
        strat = BarrierStrategy(
            config=StrategyLineConfig(
                name="barrier_12bar", magic=90001, brain_types={"xgboost_v9"}
            ),
            brains=[barrier_brain],
        )
        proposals = strat._run_inference(
            feature_vector=[1.0] * 40,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert len(proposals) == 1
        called_arg = barrier_brain["adapter"].inference.call_args[0][0]
        assert isinstance(called_arg, np.ndarray)
        assert called_arg.shape == (40,)
        np.testing.assert_allclose(called_arg, np.ones(40, dtype=np.float32))

    def test_brain_id_stamped_on_proposal(self, barrier_brain):
        strat = BarrierStrategy(
            config=StrategyLineConfig(
                name="barrier_12bar", magic=90001, brain_types={"xgboost_v9"}
            ),
            brains=[barrier_brain],
        )
        proposals = strat._run_inference([], None, 2000.0)
        assert proposals[0].brain_id == "xgboost_v9_01"

    def test_adapter_exception_skipped(self, barrier_brain):
        fail_adapter = MagicMock()
        fail_adapter.inference.side_effect = RuntimeError("crash")
        brains = [
            {"adapter": fail_adapter, "brain_id": "crash_brain"},
            barrier_brain,
        ]
        strat = BarrierStrategy(
            config=StrategyLineConfig(
                name="barrier_12bar", magic=90001, brain_types={"xgboost_v9"}
            ),
            brains=brains,
        )
        proposals = strat._run_inference([], None, 2000.0)
        # Only the non-crashing brain produces a proposal
        assert len(proposals) == 1
        assert proposals[0].brain_id == "xgboost_v9_01"

    def test_empty_brains_returns_empty(self):
        strat = BarrierStrategy(
            config=StrategyLineConfig(
                name="barrier_12bar", magic=90001, brain_types={"xgboost_v9"}
            ),
            brains=[],
        )
        proposals = strat._run_inference([], None, 2000.0)
        assert proposals == []


class TestFeatureReordering:
    """FIX-20260526-028: feature vectors are reordered from V9 canonical order
    to each brain's training order before inference."""

    def test_passthrough_when_no_features_list(self):
        from core.execution.barrier_strategy import _reorder_for_brain

        v9_vec = np.arange(40, dtype=np.float32)
        result = _reorder_for_brain(v9_vec, None)
        np.testing.assert_array_equal(result, v9_vec)

    def test_passthrough_when_wrong_length(self):
        from core.execution.barrier_strategy import _reorder_for_brain

        v9_vec = np.arange(40, dtype=np.float32)
        result = _reorder_for_brain(v9_vec, ["M5_Ret_1"] * 10)  # too short
        np.testing.assert_array_equal(result, v9_vec)

    def test_reorders_to_training_order(self):
        from core.execution.barrier_strategy import _reorder_for_brain

        # Build vector with known values at V9 positions
        v9_vec = np.zeros(40, dtype=np.float32)
        # V9 position 0 = M5_Ret_1 → set to 0.0
        # V9 position 24 = H1_Ret_1 → set to 24.0
        v9_vec[24] = 24.0  # H1_Ret_1 at V9 index 24
        v9_vec[25] = 25.0  # H1_Body_Ratio at V9 index 25

        # Training order: H1-first
        training_order = ["H1_Ret_1", "H1_Body_Ratio"] + ["M5_Ret_1"] * 38
        result = _reorder_for_brain(v9_vec, training_order)
        # H1_Ret_1 (V9 idx 24, value 24.0) → result[0]
        assert result[0] == 24.0
        # H1_Body_Ratio (V9 idx 25, value 25.0) → result[1]
        assert result[1] == 25.0
        # M5_Ret_1 (V9 idx 0, value 0.0) → result[2]
        assert result[2] == 0.0

    def test_missing_feature_defaults_to_zero(self):
        from core.execution.barrier_strategy import _reorder_for_brain

        v9_vec = np.zeros(40, dtype=np.float32)
        training_order = ["NonExistent_Feature"] * 40
        result = _reorder_for_brain(v9_vec, training_order)
        np.testing.assert_allclose(result, np.zeros(40, dtype=np.float32))

    def test_binary_cls_v1_full_reorder(self):
        """End-to-end: V9 schema order → training order for Binary_Cls_V1."""
        from core.execution.barrier_strategy import _reorder_for_brain

        # Build a V9-ordered vector with distinct values per position
        v9_vec = np.arange(40, dtype=np.float32)

        # Binary_Cls_V1 training order (from model meta.json)
        training_order = [
            "H1_ATR_14",
            "H1_Body_Ratio",
            "H1_Hurst",
            "H1_MACD",
            "H1_Macro1_Corr",
            "H1_OU_Theta",
            "H1_Price_ZScore",
            "H1_RSI_14",
            "H1_Ret_1",
            "H1_Vol_ZScore",
            "M15_ATR_14",
            "M15_Body_Ratio",
            "M15_Hurst",
            "M15_MACD",
            "M15_Macro1_Corr",
            "M15_OU_Theta",
            "M15_Price_ZScore",
            "M15_RSI_14",
            "M15_Ret_1",
            "M15_Vol_ZScore",
            "M30_ATR_14",
            "M30_Body_Ratio",
            "M30_Hurst",
            "M30_MACD",
            "M30_Macro1_Corr",
            "M30_OU_Theta",
            "M30_Price_ZScore",
            "M30_RSI_14",
            "M30_Ret_1",
            "M30_Vol_ZScore",
            "M5_ATR_14",
            "M5_Body_Ratio",
            "M5_Hurst",
            "M5_MACD",
            "M5_Macro1_Corr",
            "M5_OU_Theta",
            "M5_Price_ZScore",
            "M5_RSI_14",
            "M5_Ret_1",
            "M5_Vol_ZScore",
        ]

        result = _reorder_for_brain(v9_vec, training_order)

        # Verify H1_ATR_14 (V9 idx 26) → result[0]
        assert result[0] == pytest.approx(
            float(list(V9_INSTITUTIONAL_40_FEATURES).index("H1_ATR_14"))
        )
        # Verify M5_Vol_ZScore (V9 idx 7) → result[39]
        assert result[39] == pytest.approx(
            float(list(V9_INSTITUTIONAL_40_FEATURES).index("M5_Vol_ZScore"))
        )
        # All 40 values should be present (no duplicates, no gaps)
        assert len(set(result)) == 40

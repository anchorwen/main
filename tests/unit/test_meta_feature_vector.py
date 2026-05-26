"""Tests for _build_meta_feature_vector() — FIX-20260525-026 train-serve skew fix.

Validates that the 43-dim meta-labeling feature vector is assembled in the
exact training order from the brain config, NOT in V9 schema order.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter
from core.runtime.live_cycle import _build_meta_feature_vector

# ── Training feature order from Meta_Stage1_MetaLabel_Binary_V1 ──
META_FEATURE_NAMES_TRAINING = [
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
    "ou_z_score",
    "ou_half_life",
    "ou_theta",
]

# V9 schema order (the WRONG order that was being used)
V9_SCHEMA_ORDER = [
    "M5_Ret_1",
    "M5_Body_Ratio",
    "M5_ATR_14",
    "M5_RSI_14",
    "M5_MACD",
    "M5_Vol_ZScore",
    "M5_Macro1_Corr",
    "M5_Price_ZScore",
    "M15_Ret_1",
    "M15_Body_Ratio",
    "M15_ATR_14",
    "M15_RSI_14",
    "M15_MACD",
    "M15_Vol_ZScore",
    "M15_Macro1_Corr",
    "M15_Price_ZScore",
    "M30_Ret_1",
    "M30_Body_Ratio",
    "M30_ATR_14",
    "M30_RSI_14",
    "M30_MACD",
    "M30_Vol_ZScore",
    "M30_Macro1_Corr",
    "M30_Price_ZScore",
    "H1_Ret_1",
    "H1_Body_Ratio",
    "H1_ATR_14",
    "H1_RSI_14",
    "H1_MACD",
    "H1_Vol_ZScore",
    "H1_Macro1_Corr",
    "H1_Price_ZScore",
    "M5_OU_Theta",
    "M15_OU_Theta",
    "M30_OU_Theta",
    "H1_OU_Theta",
    "M5_Hurst",
    "M15_Hurst",
    "M30_Hurst",
    "H1_Hurst",
]


class _MockParamsAdapter(ParamsBrainAdapter):
    """Minimal ParamsBrainAdapter that returns controlled OU params."""

    def __init__(self, infer_result: dict[str, float]):
        super().__init__({"brain_id": "mock_ou"})
        self._infer_result = infer_result

    def load(self) -> None:
        self._backend = "mock"

    def infer(self, feature_vector: np.ndarray) -> dict:
        return dict(self._infer_result)


def _make_ou_adapter(
    z_score: float = 2.0,
    half_life: float = 15.0,
    theta: float = 0.05,
) -> _MockParamsAdapter:
    return _MockParamsAdapter(
        {
            "z_score": z_score,
            "half_life": half_life,
            "theta": theta,
            "raw_score": z_score,
            "runtime_ms": 1.0,
            "fallback": False,
        }
    )


def _make_meta_brain_entry(features: list[str] | None = None):
    """Mock brain entry dict for MetaLabel brain."""
    entry: dict = {
        "brain_id": "Meta_Stage1_MetaLabel_Binary_V1",
        "contract_group": "barrier_12bar_meta",
    }
    if features is not None:
        entry["features"] = features
    return entry


def _make_v9_record(values_dict: dict[str, float] | None = None):
    """Mock feature store record with V9 40-dim values."""
    record = MagicMock()
    record.values = {}
    if values_dict:
        record.values = dict(values_dict)
    return record


class TestMetaFeatureVectorOrder:
    """Validate training-order feature assembly (FIX-20260525-026)."""

    def test_feature_order_matches_brain_config(self):
        """Feature vector positions match brain config features list."""
        # Create V9 values where each feature stores its V9 index as value
        # so we can identify which value landed in which position
        v9_values: dict[str, float] = {}
        for i, name in enumerate(V9_SCHEMA_ORDER):
            v9_values[name] = float(i + 1)

        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=2.0, half_life=15.0, theta=0.05)
        meta_brain = _make_meta_brain_entry(features=META_FEATURE_NAMES_TRAINING)
        meta_brain["adapter"] = ou_adapter

        brains = [
            meta_brain,
            {"brain_id": "Some_Other_Brain", "adapter": MagicMock()},
        ]

        vec, ou_params = _build_meta_feature_vector(
            brains=brains,
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        assert vec is not None
        assert ou_params is not None
        assert vec.shape == (1, 43)

        arr = vec[0]

        # Position [0] should be H1_ATR_14 (V9 idx 26, val 27)
        h1_atr_idx = V9_SCHEMA_ORDER.index("H1_ATR_14")
        assert arr[0] == pytest.approx(
            float(h1_atr_idx + 1)
        ), f"Position [0] expected H1_ATR_14 (V9 idx {h1_atr_idx}), got {arr[0]:.1f}"

        # Position [10] should be M15_ATR_14 (V9 idx 10, val 11)
        m15_atr_idx = V9_SCHEMA_ORDER.index("M15_ATR_14")
        assert arr[10] == pytest.approx(
            float(m15_atr_idx + 1)
        ), f"Position [10] expected M15_ATR_14 (V9 idx {m15_atr_idx}), got {arr[10]:.1f}"

        # OU features at positions 40-42
        assert arr[40] == pytest.approx(2.0)
        assert arr[41] == pytest.approx(15.0)
        assert arr[42] == pytest.approx(0.05)

    def test_feature_order_not_v9_schema(self):
        """First feature position must NOT be M5_Ret_1 (V9 schema order)."""
        v9_values = {name: float(i) for i, name in enumerate(V9_SCHEMA_ORDER)}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter()
        meta_brain = _make_meta_brain_entry(features=META_FEATURE_NAMES_TRAINING)
        meta_brain["adapter"] = ou_adapter

        vec, _ = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # In training order, position [0] is H1_ATR_14 (V9 idx 26, val 26.0)
        # NOT M5_Ret_1 (V9 idx 0, val 0.0)
        assert abs(vec[0, 0] - 26.0) < 0.01, (
            f"Position [0] expected H1_ATR_14=26.0, got {vec[0, 0]:.1f}. "
            f"TRAIN-SERVE SKEW: features still in V9 schema order!"
        )

    def test_ou_params_returned_correctly(self):
        """OU parameter dict contains correct values."""
        v9_values = {name: 0.0 for name in V9_SCHEMA_ORDER}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=1.8, half_life=22.0, theta=0.03)
        meta_brain = _make_meta_brain_entry(features=META_FEATURE_NAMES_TRAINING)
        meta_brain["adapter"] = ou_adapter

        _, ou_params = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        assert ou_params is not None
        assert ou_params["z_score"] == 1.8
        assert ou_params["half_life"] == 22.0
        assert ou_params["theta"] == 0.03

    def test_z_score_clipping(self):
        """z_score is clipped to [1.3, 2.5] in the feature vector."""
        v9_values = {name: 0.0 for name in V9_SCHEMA_ORDER}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=3.0, half_life=10.0, theta=0.04)
        meta_brain = _make_meta_brain_entry(features=META_FEATURE_NAMES_TRAINING)
        meta_brain["adapter"] = ou_adapter

        vec, ou_params = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # ou_params retains raw un-clipped value
        assert ou_params["z_score"] == 3.0
        # feature vector has clipped value
        assert vec[0, 40] == pytest.approx(2.5)

    def test_no_ou_adapter_returns_none(self):
        """When no ParamsBrainAdapter is found, returns (None, None)."""
        v9_values = {name: 0.0 for name in V9_SCHEMA_ORDER}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        non_ou_brain = {
            "brain_id": "NoOp",
            "contract_group": "barrier_12bar_meta",
            "adapter": MagicMock(),  # not a ParamsBrainAdapter
        }

        vec, ou_params = _build_meta_feature_vector(
            brains=[non_ou_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        assert vec is None
        assert ou_params is None

    def test_missing_features_default_to_zero(self):
        """Features not in V9 store default to 0.0 (not NaN)."""
        record = _make_v9_record({})
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=1.5, half_life=12.0, theta=0.02)
        meta_brain = _make_meta_brain_entry(features=META_FEATURE_NAMES_TRAINING)
        meta_brain["adapter"] = ou_adapter

        vec, _ = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # V9 features should all be 0.0
        for i in range(40):
            assert vec[0, i] == 0.0, f"Position [{i}] expected 0.0, got {vec[0, i]}"
        # OU features should have real values
        assert vec[0, 40] == pytest.approx(1.5)
        assert vec[0, 41] == pytest.approx(12.0)
        assert vec[0, 42] == pytest.approx(0.02)

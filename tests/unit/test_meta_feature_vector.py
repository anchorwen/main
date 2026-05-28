"""Tests for _build_meta_feature_vector().

Validates that the 40-dim meta-labeling feature vector is assembled in the
exact training order from the brain config, NOT in V9 schema order, and that
OU params are returned for diagnostic logging but NOT concatenated into the vector.

FIX-20260528-016: 43→40 dim (OU features removed from vector).
FIX-20260528-017: hardcoded 40 → schema registry lookup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter
from core.runtime.live_cycle import _build_meta_feature_vector

# Canonical V9 institutional 40 feature names (M5-first)
V9_FEATURE_NAMES = [
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

# Training feature order from the retrained Meta_Stage1_MetaLabel_Binary_V1
# model (M5-first — same as canonical for this model, but the test uses
# H1-first to validate order detection).
META_40_TRAINING_ORDER = [
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
        "feature_schema_id": "v9_institutional_40",
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
    """Validate training-order feature assembly (40-dim V9-only)."""

    def test_feature_order_matches_brain_config(self):
        """Feature vector positions match brain config features list (40-dim)."""
        # Create V9 values where each feature stores its canonical index + 1
        v9_values: dict[str, float] = {}
        for i, name in enumerate(V9_FEATURE_NAMES):
            v9_values[name] = float(i + 1)

        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=2.0, half_life=15.0, theta=0.05)
        meta_brain = _make_meta_brain_entry(features=META_40_TRAINING_ORDER)
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
        assert vec.shape == (1, 40), f"Expected (1, 40), got {vec.shape}"

        arr = vec[0]

        # Position [0] should be H1_ATR_14 (canonical idx 26, val 27)
        h1_atr_idx = V9_FEATURE_NAMES.index("H1_ATR_14")
        assert arr[0] == pytest.approx(
            float(h1_atr_idx + 1)
        ), f"Position [0] expected H1_ATR_14 (V9 idx {h1_atr_idx}), got {arr[0]:.1f}"

        # Position [10] should be M15_ATR_14 (canonical idx 10, val 11)
        m15_atr_idx = V9_FEATURE_NAMES.index("M15_ATR_14")
        assert arr[10] == pytest.approx(
            float(m15_atr_idx + 1)
        ), f"Position [10] expected M15_ATR_14 (V9 idx {m15_atr_idx}), got {arr[10]:.1f}"

        # OU params should NOT be in the feature vector — they are
        # returned separately in the ou_params dict for diagnostics only.
        assert ou_params["z_score"] == 2.0
        assert ou_params["half_life"] == 15.0
        assert ou_params["theta"] == 0.05

    def test_feature_order_not_v9_schema(self):
        """When config uses H1-first order, position [0] must NOT be M5_Ret_1."""
        v9_values = {name: float(i) for i, name in enumerate(V9_FEATURE_NAMES)}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter()
        meta_brain = _make_meta_brain_entry(features=META_40_TRAINING_ORDER)
        meta_brain["adapter"] = ou_adapter

        vec, _ = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # In H1-first training order, position [0] is H1_ATR_14 (canonical idx 26)
        # NOT M5_Ret_1 (canonical idx 0)
        assert abs(vec[0, 0] - 26.0) < 0.01, (
            f"Position [0] expected H1_ATR_14=26.0, got {vec[0, 0]:.1f}. "
            f"TRAIN-SERVE SKEW: features still in V9 schema order!"
        )

    def test_ou_params_returned_correctly(self):
        """OU parameter dict contains correct values for diagnostic logging."""
        v9_values = {name: 0.0 for name in V9_FEATURE_NAMES}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=1.8, half_life=22.0, theta=0.03)
        meta_brain = _make_meta_brain_entry(features=META_40_TRAINING_ORDER)
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

    def test_missing_ou_adapter_still_returns_vector(self):
        """When no ParamsBrainAdapter is found, still returns 40-dim vector (OU is diagnostic-only)."""
        v9_values = {name: float(i) for i, name in enumerate(V9_FEATURE_NAMES)}
        record = _make_v9_record(v9_values)
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        non_ou_brain = {
            "brain_id": "Meta_Stage1_MetaLabel_Binary_V1",
            "contract_group": "barrier_12bar_meta",
            "feature_schema_id": "v9_institutional_40",
            "features": META_40_TRAINING_ORDER,
            "adapter": MagicMock(),  # not a ParamsBrainAdapter
        }

        vec, ou_params = _build_meta_feature_vector(
            brains=[non_ou_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # OU params are None (no adapter to compute them), but vector is still built
        assert vec is not None
        assert vec.shape == (1, 40)
        assert ou_params is None

    def test_missing_features_default_to_zero(self):
        """Features not in V9 store default to 0.0 in vector (not NaN)."""
        record = _make_v9_record({})
        mock_store = MagicMock()
        mock_store.latest.return_value = record

        ou_adapter = _make_ou_adapter(z_score=1.5, half_life=12.0, theta=0.02)
        meta_brain = _make_meta_brain_entry(features=META_40_TRAINING_ORDER)
        meta_brain["adapter"] = ou_adapter

        vec, ou_params = _build_meta_feature_vector(
            brains=[meta_brain],
            feature_store=mock_store,
            mid_price=4560.0,
            symbol="XAUUSDc",
        )

        # All 40 V9 features should be 0.0
        for i in range(40):
            assert vec[0, i] == 0.0, f"Position [{i}] expected 0.0, got {vec[0, i]}"
        # OU params are still returned for diagnostics
        assert ou_params is not None
        assert ou_params["z_score"] == 1.5

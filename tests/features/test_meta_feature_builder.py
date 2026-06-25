"""Tests for core.features.meta_feature_builder — meta-labeling feature construction.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #7.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter
from core.features.meta_feature_builder import build_meta_feature_vector

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_feature_store(return_values: dict[str, float] | None = None) -> MagicMock:
    """Create a mock feature store with .latest() returning a FeatureRecord-like object."""
    store = MagicMock()
    record = MagicMock()
    record.values = return_values or {f"feat_{i}": float(i) for i in range(40)}
    store.latest.return_value = record
    return store


def _make_brains_list(
    brain_id: str = "Meta_Stage1_MetaLabel_Binary_V1",
    adapter: object | None = None,
    features: list[str] | None = None,
    contract_group: str = "barrier_12bar",
) -> list[dict]:
    """Build a minimal brains list for build_meta_feature_vector."""
    brain: dict = {
        "brain_id": brain_id,
        "adapter": adapter,
        "contract_group": contract_group,
    }
    if features is not None:
        brain["features"] = features
        brain["feature_schema_id"] = "v9_institutional_40"
    return [brain]


# ── No brains / None returns ───────────────────────────────────────────────


class TestBuildMetaFeatureVectorNoBrains:
    def test_empty_brains_list(self) -> None:
        store = _make_feature_store()
        result, ou_params = build_meta_feature_vector(
            brains=[],
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        # Empty brains list with valid feature store still produces a vector
        # (falls through to legacy V9 schema order)
        assert result is not None
        assert ou_params is None  # No adapter to extract OU params from

    def test_feature_store_returns_none_record(self) -> None:
        store = MagicMock()
        store.latest.return_value = None
        result, ou_params = build_meta_feature_vector(
            brains=_make_brains_list(),
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is None
        assert ou_params is None

    def test_feature_store_record_has_no_values(self) -> None:
        store = MagicMock()
        record = MagicMock()
        record.values = {}
        store.latest.return_value = record
        result, ou_params = build_meta_feature_vector(
            brains=_make_brains_list(),
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        # record.values is empty dict (falsy) → raw_features is still {}
        # Then raw_features is not None → proceeds to build vector
        assert result is not None


# ── OU Params Extraction ───────────────────────────────────────────────────


class TestBuildMetaFeatureVectorWithOU:
    def test_params_brain_adapter_extracts_ou(self) -> None:
        store = _make_feature_store()
        adapter = MagicMock(spec=ParamsBrainAdapter)
        adapter.infer.return_value = {
            "z_score": 1.5,
            "half_life": 15.0,
            "theta": 0.02,
        }
        brains = _make_brains_list(adapter=adapter)
        result, ou_params = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert ou_params is not None
        assert ou_params["z_score"] == 1.5
        assert ou_params["half_life"] == 15.0
        assert ou_params["theta"] == 0.02

    def test_adapter_infer_error_handled(self) -> None:
        store = _make_feature_store()
        adapter = MagicMock(spec=ParamsBrainAdapter)
        adapter.infer.side_effect = RuntimeError("infer failed")
        brains = _make_brains_list(adapter=adapter)
        result, ou_params = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        # OU params may or may not be extracted depending on adapter position
        # The function should not crash
        assert result is not None

    def test_zero_price_handling(self) -> None:
        store = _make_feature_store()
        adapter = MagicMock(spec=ParamsBrainAdapter)
        adapter.infer.return_value = {
            "z_score": 0.0,
            "half_life": float("inf"),
            "theta": 0.0,
        }
        brains = _make_brains_list(adapter=adapter)
        result, ou_params = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=0.0,
            symbol="XAUUSDc",
        )
        assert result is not None


# ── Feature Name Resolution ────────────────────────────────────────────────


class TestBuildMetaFeatureVectorFeatureNames:
    def test_from_brain_config_features_list(self) -> None:
        """Feature names are sourced from brain config's features field."""
        store = _make_feature_store()
        feature_names = [f"f{i:02d}" for i in range(40)]
        store.latest.return_value.values = {n: float(i) for i, n in enumerate(feature_names)}
        brains = _make_brains_list(features=feature_names)
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None
        assert result.shape == (1, 40)
        # Values should be in feature_names order
        for i, _name in enumerate(feature_names):
            assert result[0, i] == pytest.approx(float(i))

    def test_from_model_metadata_fallback(self, tmp_path: Path) -> None:
        """Fallback: read feature_names from normalization_config_path JSON."""
        store = _make_feature_store()
        feature_names = [f"m{i:02d}" for i in range(40)]
        store.latest.return_value.values = {n: float(i) for i, n in enumerate(feature_names)}

        meta_path = tmp_path / "norm_config.json"
        meta_path.write_text(json.dumps({"feature_names": feature_names}))

        brains = _make_brains_list(
            features=None,  # no features list — triggers JSON fallback
        )
        brains[0]["normalization_config_path"] = str(meta_path)

        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None
        assert result.shape == (1, 40)

    def test_metalabel_keyword_match_in_brain_id(self) -> None:
        store = _make_feature_store()
        feature_names = [f"k{i:02d}" for i in range(40)]
        store.latest.return_value.values = {n: float(i) for i, n in enumerate(feature_names)}
        brains = _make_brains_list(
            brain_id="SomeBrain_MetaLabel_v2",
            features=feature_names,
        )
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None
        assert result.shape == (1, 40)

    def test_metalabel_keyword_in_contract_group(self) -> None:
        store = _make_feature_store()
        feature_names = [f"g{i:02d}" for i in range(40)]
        store.latest.return_value.values = {n: float(i) for i, n in enumerate(feature_names)}
        brains = _make_brains_list(
            brain_id="SomeBrain_V1",
            contract_group="barrier_12bar_meta",
            features=feature_names,
        )
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None
        assert result.shape == (1, 40)

    def test_missing_features_zero_filled(self) -> None:
        """Missing feature values in store should be zero-filled."""
        store = _make_feature_store()
        feature_names = [f"f{i:02d}" for i in range(40)]
        # Only provide 20 features — the other 20 are missing → 0.0
        store.latest.return_value.values = {f"f{i:02d}": float(i) for i in range(20)}
        brains = _make_brains_list(features=feature_names)
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None
        assert result[0, 0] == 0.0  # f00
        assert result[0, 19] == 19.0  # f19
        assert result[0, 20] == 0.0  # f20 is missing → 0.0


# ── Edge Cases ──────────────────────────────────────────────────────────────


class TestBuildMetaFeatureVectorEdgeCases:
    def test_none_mid_price(self) -> None:
        store = _make_feature_store()
        adapter = MagicMock(spec=ParamsBrainAdapter)
        adapter.infer.return_value = {
            "z_score": 0.0,
            "half_life": float("inf"),
            "theta": 0.0,
        }
        brains = _make_brains_list(adapter=adapter)
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=None,
            symbol="XAUUSDc",
        )
        assert result is not None

    def test_feature_store_error_handled(self) -> None:
        store = MagicMock()
        store.latest.side_effect = RuntimeError("store error")
        # When feature_store.latest raises, raw_features stays None → return None
        result, ou_params = build_meta_feature_vector(
            brains=_make_brains_list(),
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is None
        assert ou_params is None

    def test_nonexistent_meta_json_handled(self) -> None:
        """If normalization_config_path points to nonexistent file, handled gracefully."""
        store = _make_feature_store()
        brains = _make_brains_list(features=None)
        brains[0]["normalization_config_path"] = "/nonexistent/path.json"
        # Falls through to legacy fallback — still returns a vector
        result, _ = build_meta_feature_vector(
            brains=brains,
            feature_store=store,
            mid_price=2000.0,
            symbol="XAUUSDc",
        )
        assert result is not None

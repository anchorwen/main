"""Tests for BrainConfigValidator — FIX-20260525-027 v9_40dim_ou3 schema.

Validates that the validator correctly accepts the augmented 43-dim schema
(40 V9 institutional + 3 OU physics features) and rejects invalid configs.
"""

from __future__ import annotations

import pytest

from core.deployment.brain_config_validator import (
    SCHEMA_DIMENSIONS,
    BrainConfigValidator,
    _get_schema_feature_names,
)

# ── 43 MetaLabel features (training order) ──
META_LABEL_43_FEATURES = [
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


def _make_brain_entry(
    brain_id: str = "Meta_Stage1_MetaLabel_Binary_V1",
    feature_schema_id: str = "v9_40dim_ou3",
    features: list[str] | None = None,
    brain_type: str = "lightgbm_v1",
    artifact_path: str = "data/models/institutional/test_model.txt",
    status: str = "probation",
) -> dict:
    entry: dict = {
        "brain_id": brain_id,
        "brain_type": brain_type,
        "feature_schema_id": feature_schema_id,
        "artifact_path": artifact_path,
        "status": status,
    }
    if features is not None:
        entry["features"] = features
    return entry


class TestSchemaRegistration:
    """Schema constant and feature name resolution."""

    def test_schema_dimensions_includes_v9_40dim_ou3(self):
        assert "v9_40dim_ou3" in SCHEMA_DIMENSIONS
        assert SCHEMA_DIMENSIONS["v9_40dim_ou3"] == 43

    def test_get_feature_names_returns_43_names(self):
        names = _get_schema_feature_names("v9_40dim_ou3")
        assert names is not None
        assert len(names) == 43
        # First 40 should be V9 institutional features
        assert names[0] == "M5_Ret_1"  # V9 schema starts with M5_Ret_1
        # Last 3 should be OU physics features
        assert names[40] == "ou_z_score"
        assert names[41] == "ou_half_life"
        assert names[42] == "ou_theta"

    def test_get_feature_names_v9_institutional_still_works(self):
        names = _get_schema_feature_names("v9_institutional_40")
        assert names is not None
        assert len(names) == 40
        assert "ou_z_score" not in names

    def test_unknown_schema_raises_keyerror(self):
        with pytest.raises(KeyError):
            _get_schema_feature_names("nonexistent_schema_999")


class TestValidatorAccepts43DimSchema:
    """Validator should accept valid 43-dim configs with v9_40dim_ou3."""

    def test_valid_43_features_accepted(self):
        entry = _make_brain_entry(features=META_LABEL_43_FEATURES)
        validator = BrainConfigValidator()
        result = validator.validate(entry)
        # Should have no errors related to feature schema/dimensions
        schema_errors = [
            e for e in result.errors if "schema" in e.lower() or "feature" in e.lower()
        ]
        assert len(schema_errors) == 0, f"Expected 0 schema/feature errors, got: {schema_errors}"

    def test_wrong_feature_name_in_43_rejected(self):
        features = list(META_LABEL_43_FEATURES)
        features[10] = "M15_Nonexistent_Feature"
        entry = _make_brain_entry(features=features)
        validator = BrainConfigValidator()
        result = validator.validate(entry)
        name_errors = [e for e in result.errors if "not in schema" in e]
        assert len(name_errors) == 1
        assert "M15_Nonexistent_Feature" in name_errors[0]

    def test_42_features_rejected_wrong_dim(self):
        entry = _make_brain_entry(features=META_LABEL_43_FEATURES[:42])
        validator = BrainConfigValidator()
        result = validator.validate(entry)
        dim_errors = [e for e in result.errors if "length" in e]
        assert len(dim_errors) >= 1
        assert "42" in dim_errors[0]

    def test_44_features_rejected_wrong_dim(self):
        features = list(META_LABEL_43_FEATURES) + ["extra_feature"]
        entry = _make_brain_entry(features=features)
        validator = BrainConfigValidator()
        result = validator.validate(entry)
        dim_errors = [e for e in result.errors if "length" in e]
        assert len(dim_errors) >= 1
        assert "44" in dim_errors[0]

    def test_v9_institutional_40_still_validates(self):
        """Existing v9_institutional_40 brains must not regress."""
        from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

        entry = _make_brain_entry(
            brain_id="Meta_Stage1_Binary_Cls_V1",
            feature_schema_id="v9_institutional_40",
            features=list(V9_INSTITUTIONAL_40_FEATURES),
        )
        validator = BrainConfigValidator()
        result = validator.validate(entry)
        schema_errors = [
            e for e in result.errors if "schema" in e.lower() or "feature" in e.lower()
        ]
        assert len(schema_errors) == 0


class TestModelDimensionValidation:
    """Post-load num_features check against schema dimension."""

    def test_43_features_matches_schema(self):
        entry = _make_brain_entry()
        validator = BrainConfigValidator()
        result = validator.validate_model_dimension(entry, num_features=43)
        assert result.ok is True

    def test_40_features_mismatches_v9_40dim_ou3(self):
        entry = _make_brain_entry()
        validator = BrainConfigValidator()
        result = validator.validate_model_dimension(entry, num_features=40)
        assert result.ok is False
        assert "40" in result.errors[0]

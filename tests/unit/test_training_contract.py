"""Unit tests for TrainingContract v2.1."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.contracts.training.training_contract import (
    SCHEMA_VERSION,
    ArchitectureSpec,
    DatasetSpec,
    LabelSpec,
    OutputSpec,
    QualityGateSpec,
    TrainingContract,
    ValidationSpec,
)


def _minimal_dict() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": "test_v1",
        "dataset": {
            "path": "data/training/test.npz",
            "feature_schema": "v9_institutional_40",
            "min_samples_per_class": 100,
            "sample_weighting": "none",
        },
        "label": {
            "contract_id": "label-survival-1.0",
            "sl_atr_mult": 2.0,
            "tp_atr_mult": 3.5,
            "horizon_bars": 12,
        },
        "architecture": {
            "type": "xgboost",
            "objective_function": "binary_logloss",
            "search_space": "default",
            "optuna_trials": 50,
            "n_seeds": 5,
        },
        "validation": {
            "method": "cpcv",
            "n_groups": 6,
            "n_test_groups": 2,
            "purge_bars": 12,
            "embargo_bars": 5,
        },
        "quality_gates": {
            "min_train_sharpe": 1.0,
            "min_train_win_rate": 0.50,
            "min_sortino_ratio": 1.0,
            "min_calmar_ratio": 0.5,
            "max_vol_scaled_dd_pct": 25.0,
            "min_forward_sharpe": 1.0,
            "min_forward_win_rate": 0.50,
            "max_overfit_gap": 0.30,
            "require_shap_stability": True,
            "model_type": "tree",
        },
        "output": {
            "brain_id_template": "{arch}_{contract}_{timestamp}",
            "model_dir": "data/models",
            "config_dir": "configs/brains",
            "registry_db": "data/training/registry.db",
            "auto_register": False,
            "initial_status": "shadow",
        },
    }


class TestTrainingContract:
    """Tests for TrainingContract deserialization, validation, and serialization."""

    def test_from_dict_minimal(self):
        contract = TrainingContract.from_dict(_minimal_dict())
        assert contract.contract_id == "test_v1"
        assert contract.schema_version == SCHEMA_VERSION
        assert contract.dataset.path == "data/training/test.npz"
        assert contract.architecture.type == "xgboost"
        assert contract.validation.method == "cpcv"

    def test_from_dict_with_date_range(self):
        d = _minimal_dict()
        d["dataset"]["date_range"] = ["2023-01-01", "2025-12-31"]
        contract = TrainingContract.from_dict(d)
        assert contract.dataset.date_range == ("2023-01-01", "2025-12-31")

    def test_from_dict_with_metadata(self):
        d = _minimal_dict()
        d["metadata"] = {"author": "test", "version": 1}
        contract = TrainingContract.from_dict(d)
        assert contract.metadata == {"author": "test", "version": 1}

    def test_from_dict_with_custom_params(self):
        d = _minimal_dict()
        d["architecture"]["custom_params"] = {"max_depth": 8, "subsample": 0.9}
        contract = TrainingContract.from_dict(d)
        assert contract.architecture.custom_params == {"max_depth": 8, "subsample": 0.9}

    def test_from_dict_defaults(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "contract_id": "minimal",
        }
        contract = TrainingContract.from_dict(d)
        assert contract.dataset.feature_schema == "v9_institutional_40"
        assert contract.dataset.sample_weighting == "none"
        assert contract.architecture.type == "xgboost"
        assert contract.validation.method == "cpcv"
        assert contract.validation.n_groups == 6

    def test_validate_empty_contract_id(self):
        d = _minimal_dict()
        d["contract_id"] = ""
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("contract_id" in i.lower() for i in issues)

    def test_validate_invalid_architecture(self):
        d = _minimal_dict()
        d["architecture"]["type"] = "invalid_arch"
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("invalid_arch" in i for i in issues)

    def test_validate_invalid_objective(self):
        d = _minimal_dict()
        d["architecture"]["objective_function"] = "bad_obj"
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("bad_obj" in i for i in issues)

    def test_validate_invalid_validation_method(self):
        d = _minimal_dict()
        d["validation"]["method"] = "kfold"
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("kfold" in i for i in issues)

    def test_validate_n_test_groups_out_of_range(self):
        d = _minimal_dict()
        d["validation"]["n_test_groups"] = 6  # must be < n_groups (6)
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("n_test_groups" in i for i in issues)

    def test_validate_negative_purge_bars(self):
        d = _minimal_dict()
        d["validation"]["purge_bars"] = -1
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("purge_bars" in i.lower() for i in issues)

    def test_validate_invalid_sample_weighting(self):
        d = _minimal_dict()
        d["dataset"]["sample_weighting"] = "bogus"
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("sample_weighting" in i.lower() for i in issues)

    def test_validate_invalid_initial_status(self):
        d = _minimal_dict()
        d["output"]["initial_status"] = "production"
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert any("initial_status" in i.lower() for i in issues)

    def test_validate_valid_contract(self):
        d = _minimal_dict()
        d["label"]["profitability_calibrated"] = True
        contract = TrainingContract.from_dict(d)
        issues = contract.validate()
        assert issues == []

    def test_to_dict_roundtrip(self):
        d = _minimal_dict()
        contract = TrainingContract.from_dict(d)
        output = contract.to_dict()
        # Roundtrip should be idempotent
        contract2 = TrainingContract.from_dict(output)
        assert contract2.contract_id == contract.contract_id
        assert contract2.architecture.type == contract.architecture.type
        assert contract2.validation.n_groups == contract.validation.n_groups

    def test_schema_version_mismatch(self):
        d = _minimal_dict()
        d["schema_version"] = "wrong_version"
        with pytest.raises(ValueError, match="Expected schema_version"):
            TrainingContract.from_dict(d)

    def test_from_yaml(self):
        import yaml

        d = _minimal_dict()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(d, f)
            yaml_path = f.name
        try:
            contract = TrainingContract.from_yaml(yaml_path)
            assert contract.contract_id == "test_v1"
        finally:
            Path(yaml_path).unlink()

    def test_from_json(self):
        d = _minimal_dict()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(d, f)
            json_path = f.name
        try:
            contract = TrainingContract.from_file(json_path)
            assert contract.contract_id == "test_v1"
        finally:
            Path(json_path).unlink()

    def test_from_file_unsupported_format(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("not a contract")
            txt_path = f.name
        try:
            with pytest.raises(ValueError, match="Unsupported format"):
                TrainingContract.from_file(txt_path)
        finally:
            Path(txt_path).unlink()

    def test_dataset_spec_validate_min_samples(self):
        spec = DatasetSpec(path="test.npz", min_samples_per_class=5)
        issues = spec.validate()
        assert any("min_samples_per_class" in i for i in issues)

    def test_label_spec_validate_negative_mult(self):
        spec = LabelSpec(sl_atr_mult=-1.0)
        issues = spec.validate()
        assert any("sl_atr_mult" in i for i in issues)

    def test_architecture_spec_validate_n_seeds(self):
        spec = ArchitectureSpec(n_seeds=0)
        issues = spec.validate()
        assert any("n_seeds" in i for i in issues)

    def test_validation_spec_validate_n_groups(self):
        spec = ValidationSpec(n_groups=1)
        issues = spec.validate()
        assert any("n_groups" in i for i in issues)

    def test_quality_gate_spec_validate_win_rate(self):
        spec = QualityGateSpec(min_train_win_rate=1.5)
        issues = spec.validate()
        assert any("min_train_win_rate" in i for i in issues)

    def test_output_spec_validate_status(self):
        spec = OutputSpec(initial_status="invalid")
        issues = spec.validate()
        assert any("initial_status" in i for i in issues)


class TestDatasetSpec:
    def test_validate_empty_path(self):
        spec = DatasetSpec(path="")
        issues = spec.validate()
        assert any("path" in i.lower() for i in issues)

    def test_validate_returns_list(self):
        spec = DatasetSpec(path="test.npz", sample_weighting="none", min_samples_per_class=100)
        issues = spec.validate()
        assert issues == []


class TestLabelSpec:
    def test_validate_valid(self):
        spec = LabelSpec(
            sl_atr_mult=2.0, tp_atr_mult=3.5, horizon_bars=12, profitability_calibrated=True
        )
        assert spec.validate() == []

    def test_validate_zero_horizon(self):
        spec = LabelSpec(horizon_bars=0)
        issues = spec.validate()
        assert any("horizon_bars" in i for i in issues)


class TestArchitectureSpec:
    def test_validate_negative_trials(self):
        spec = ArchitectureSpec(optuna_trials=-1)
        issues = spec.validate()
        assert any("optuna_trials" in i for i in issues)


class TestValidationSpec:
    def test_cpcv_defaults(self):
        spec = ValidationSpec()
        assert spec.method == "cpcv"
        assert spec.n_groups == 6
        assert spec.n_test_groups == 2
        assert spec.purge_bars == 12
        assert spec.embargo_bars == 5


class TestQualityGateSpec:
    def test_defaults(self):
        spec = QualityGateSpec()
        assert spec.min_train_sharpe == 1.0
        assert spec.require_shap_stability is True

    def test_validate_negative_overfit_gap(self):
        spec = QualityGateSpec(max_overfit_gap=-0.1)
        issues = spec.validate()
        assert any("max_overfit_gap" in i for i in issues)


class TestOutputSpec:
    def test_defaults(self):
        spec = OutputSpec()
        assert spec.initial_status == "shadow"
        assert spec.auto_register is False

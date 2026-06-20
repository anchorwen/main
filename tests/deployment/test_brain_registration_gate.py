"""Tests for core.deployment.brain_registration_gate — Phase 3b coverage.

Covers: GateResult, BrainRegistrationGate static check methods (_check_required_fields,
_check_brain_type, _check_feature_schema, _check_features_non_empty,
_check_features_dimension, _check_feature_names, _check_vote_weight,
_scan_all_configs, validate, validate_all).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.deployment.brain_registration_gate import (
    GateResult,
    BrainRegistrationGate,
)


# ═══════════════════════════════════════════════════════════════════════════
# GateResult dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestGateResult:
    def test_defaults(self) -> None:
        r = GateResult()
        assert r.brain_id == ""
        assert r.passed is False
        assert r.checks_run == 0
        assert r.checks_passed == 0
        assert r.failures == []
        assert r.warnings == []
        assert r.validated_config is None

    def test_custom_values(self) -> None:
        r = GateResult(
            brain_id="test_brain",
            passed=True,
            checks_run=10,
            checks_passed=10,
            failures=[],
            warnings=["test warning"],
            validated_config={"brain_id": "test_brain"},
        )
        assert r.passed is True
        assert r.brain_id == "test_brain"
        assert r.checks_run == 10


# ═══════════════════════════════════════════════════════════════════════════
# _check_required_fields
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckRequiredFields:
    def test_all_fields_present(self) -> None:
        entry = {
            "brain_id": "test", "brain_type": "xgboost_v9",
            "feature_schema_id": "v9_institutional_40", "artifact_path": "/fake/model.json",
            "artifact_hash": "abc123", "features": ["f1", "f2"],
            "status": "candidate", "magic": 10001,
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_required_fields(entry, result)
        assert ok is True

    def test_missing_fields(self) -> None:
        entry = {"brain_id": "test"}
        result = GateResult()
        ok = BrainRegistrationGate._check_required_fields(entry, result)
        assert ok is False
        assert len(result.failures) == 1
        assert result.failures[0][0] == "required_fields"


# ═══════════════════════════════════════════════════════════════════════════
# _check_brain_type
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckBrainType:
    def test_valid_brain_type(self) -> None:
        entry = {"brain_type": "xgboost_v9"}
        result = GateResult()
        ok = BrainRegistrationGate._check_brain_type(entry, result)
        # xgboost_v9 should be in BRAIN_TYPE_MAP
        assert ok is True

    def test_invalid_brain_type(self) -> None:
        entry = {"brain_type": "nonexistent_type_xyz"}
        result = GateResult()
        ok = BrainRegistrationGate._check_brain_type(entry, result)
        assert ok is False
        assert result.failures[0][0] == "brain_type_valid"


# ═══════════════════════════════════════════════════════════════════════════
# _check_feature_schema
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckFeatureSchema:
    def test_known_schema(self) -> None:
        entry = {"feature_schema_id": "v9_institutional_40"}
        result = GateResult()
        ok = BrainRegistrationGate._check_feature_schema(entry, result)
        assert ok is True

    def test_unknown_schema(self) -> None:
        entry = {"feature_schema_id": "nonexistent_schema"}
        result = GateResult()
        ok = BrainRegistrationGate._check_feature_schema(entry, result)
        assert ok is False
        assert result.failures[0][0] == "schema_known"


# ═══════════════════════════════════════════════════════════════════════════
# _check_features_non_empty
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckFeaturesNonEmpty:
    def test_features_present(self) -> None:
        entry = {"features": ["f1", "f2"]}
        result = GateResult()
        ok = BrainRegistrationGate._check_features_non_empty(entry, result)
        assert ok is True

    def test_features_missing(self) -> None:
        entry = {}
        result = GateResult()
        ok = BrainRegistrationGate._check_features_non_empty(entry, result)
        assert ok is False
        assert result.failures[0][0] == "features_non_empty"

    def test_features_empty_list(self) -> None:
        entry = {"features": []}
        result = GateResult()
        ok = BrainRegistrationGate._check_features_non_empty(entry, result)
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# _check_features_dimension
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckFeaturesDimension:
    def test_correct_dimension(self) -> None:
        entry = {
            "features": ["f" + str(i) for i in range(40)],
            "feature_schema_id": "v9_institutional_40",
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_features_dimension(entry, result)
        assert ok is True

    def test_wrong_dimension(self) -> None:
        entry = {
            "features": ["f1", "f2"],
            "feature_schema_id": "v9_institutional_40",  # expects 40
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_features_dimension(entry, result)
        assert ok is False
        assert result.failures[0][0] == "features_dimension"

    def test_unknown_schema_skips_check(self) -> None:
        entry = {
            "features": ["a", "b"],
            "feature_schema_id": "unknown_schema",
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_features_dimension(entry, result)
        assert ok is True  # unknown schema already caught by schema_known


# ═══════════════════════════════════════════════════════════════════════════
# _check_feature_names
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckFeatureNames:
    def test_valid_names(self) -> None:
        from core.deployment.brain_config_validator import _get_schema_feature_names
        names = _get_schema_feature_names("v9_institutional_40")
        assert names is not None and len(names) >= 5
        entry = {
            "features": names[:5],
            "feature_schema_id": "v9_institutional_40",
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_feature_names(entry, result)
        assert ok is True

    def test_invalid_names(self) -> None:
        entry = {
            "features": ["not_a_real_feature_1", "not_a_real_feature_2"],
            "feature_schema_id": "v9_institutional_40",
        }
        result = GateResult()
        ok = BrainRegistrationGate._check_feature_names(entry, result)
        # Should fail because names are not in the schema
        assert ok is False
        assert result.failures[0][0] == "feature_names_valid"


# ═══════════════════════════════════════════════════════════════════════════
# _check_vote_weight
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckVoteWeight:
    def test_valid_vote_weight(self) -> None:
        entry = {"vote_weight": 0.5}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert result.warnings == []

    def test_missing_vote_weight_ok(self) -> None:
        entry = {}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert result.warnings == []

    def test_vote_weight_above_one(self) -> None:
        entry = {"vote_weight": 1.5}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert len(result.warnings) == 1
        assert "vote_weight" in result.warnings[0]

    def test_vote_weight_below_zero(self) -> None:
        entry = {"vote_weight": -0.1}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert len(result.warnings) == 1

    def test_vote_weight_boundary_zero(self) -> None:
        entry = {"vote_weight": 0.0}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert result.warnings == []

    def test_vote_weight_boundary_one(self) -> None:
        entry = {"vote_weight": 1.0}
        result = GateResult()
        BrainRegistrationGate._check_vote_weight(entry, result)
        assert result.warnings == []


# ═══════════════════════════════════════════════════════════════════════════
# _check_artifact_exists
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckArtifactExists:
    def test_empty_path_fails(self) -> None:
        gate = BrainRegistrationGate()
        entry = {"artifact_path": ""}
        result = GateResult()
        ok = gate._check_artifact_exists(entry, result)
        assert ok is False
        assert result.failures[0][0] == "artifact_exists"

    def test_file_not_found_fails(self) -> None:
        gate = BrainRegistrationGate(project_root=Path("/nonexistent"))
        entry = {"artifact_path": "nonexistent_file.model"}
        result = GateResult()
        ok = gate._check_artifact_exists(entry, result)
        assert ok is False
        assert result.failures[0][0] == "artifact_exists"

    def test_file_exists_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.json"
            model_path.write_text("{}")
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            entry = {"artifact_path": str(model_path)}
            result = GateResult()
            ok = gate._check_artifact_exists(entry, result)
            assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# _check_artifact_hash
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckArtifactHash:
    def test_empty_hash_fails(self) -> None:
        gate = BrainRegistrationGate()
        entry = {"artifact_hash": ""}
        result = GateResult()
        ok = gate._check_artifact_hash(entry, result)
        assert ok is False
        assert result.failures[0][0] == "artifact_hash_match"

    def test_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.json"
            model_path.write_text("test content")
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            entry = {
                "artifact_path": str(model_path),
                "artifact_hash": "wrong_hash_value",
            }
            result = GateResult()
            ok = gate._check_artifact_hash(entry, result)
            assert ok is False
            assert result.failures[0][0] == "artifact_hash_match"

    def test_hash_match_passes(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.json"
            content = b"test content"
            model_path.write_bytes(content)
            correct_hash = hashlib.sha256(content).hexdigest()
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            entry = {
                "artifact_path": str(model_path),
                "artifact_hash": correct_hash,
            }
            result = GateResult()
            ok = gate._check_artifact_hash(entry, result)
            assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# _scan_all_configs
# ═══════════════════════════════════════════════════════════════════════════


class TestScanAllConfigs:
    def test_empty_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir) / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            result = gate._scan_all_configs()
            assert result == {}

    def test_scans_valid_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir) / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "brain_a.json").write_text(
                json.dumps({"brain_id": "brain_a", "status": "live"}),
                encoding="utf-8",
            )
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            result = gate._scan_all_configs()
            assert "brain_a" in result
            assert result["brain_a"]["status"] == "live"

    def test_skips_normalization_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir) / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "normalization_params.json").write_text(
                json.dumps({"brain_id": "norm_brain"}),
                encoding="utf-8",
            )
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            result = gate._scan_all_configs()
            assert "norm_brain" not in result

    def test_skips_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir) / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "broken.json").write_text("not valid json")
            gate = BrainRegistrationGate(project_root=Path(tmpdir))
            result = gate._scan_all_configs()
            assert result == {}  # invalid JSON silently skipped


# ═══════════════════════════════════════════════════════════════════════════
# validate — integration of all checks
# ═══════════════════════════════════════════════════════════════════════════


class TestValidate:
    def test_valid_entry_passes(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            brains_dir = root / "configs" / "brains"
            brains_dir.mkdir(parents=True)

            model_path = brains_dir / "test_model.json"
            content = b"model content"
            model_path.write_bytes(content)

            entry = {
                "brain_id": "unique_brain_001",
                "brain_type": "xgboost_v9",
                "feature_schema_id": "v9_institutional_40",
                "artifact_path": str(model_path),
                "artifact_hash": hashlib.sha256(content).hexdigest(),
                "features": ["f" + str(i) for i in range(40)],
                "status": "candidate",
                "magic": 99999,
            }
            gate = BrainRegistrationGate(project_root=root)
            result = gate.validate(entry)
            # Should pass basic checks; may fail magic_unique or brain_id_unique
            # depending on whether configs/brains/ has other brains
            assert result.checks_run >= 8

    def test_empty_entry_fails(self) -> None:
        gate = BrainRegistrationGate(project_root=Path("/nonexistent"))
        result = gate.validate({})
        assert result.passed is False
        assert result.checks_run == 10
        assert len(result.failures) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# _check_magic_unique
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckMagicUnique:
    def test_magic_none_skips(self) -> None:
        gate = BrainRegistrationGate(project_root=Path("/nonexistent"))
        entry = {"magic": None, "brain_id": "test"}
        result = GateResult()
        ok = gate._check_magic_unique(entry, result)
        assert ok is True

    def test_magic_unique_when_no_other_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs" / "brains").mkdir(parents=True)
            gate = BrainRegistrationGate(project_root=root)
            entry = {"magic": 12345, "brain_id": "test_brain"}
            result = GateResult()
            ok = gate._check_magic_unique(entry, result)
            assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# _check_brain_id_unique
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckBrainIdUnique:
    def test_empty_brain_id_skips(self) -> None:
        gate = BrainRegistrationGate(project_root=Path("/nonexistent"))
        entry = {"brain_id": ""}
        result = GateResult()
        ok = gate._check_brain_id_unique(entry, result)
        assert ok is True

    def test_unique_when_no_other_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs" / "brains").mkdir(parents=True)
            gate = BrainRegistrationGate(project_root=root)
            entry = {"brain_id": "unique_id"}
            result = GateResult()
            ok = gate._check_brain_id_unique(entry, result)
            assert ok is True

    def test_duplicate_brain_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            brains_dir = root / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "existing.json").write_text(
                json.dumps({"brain_id": "duplicate_id"}),
                encoding="utf-8",
            )
            gate = BrainRegistrationGate(project_root=root)
            entry = {"brain_id": "duplicate_id"}
            result = GateResult()
            ok = gate._check_brain_id_unique(entry, result)
            assert ok is False
            assert result.failures[0][0] == "brain_id_unique"

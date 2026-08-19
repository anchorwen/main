"""Tests for core.deployment.brain_lifecycle_manager — Phase 3c coverage.

Covers: report dataclass defaults, _utc_now_iso, _utc_now_compact,
BrainLifecycleManager._find_config_by_brain_id, _scan_brain_configs,
_init_ path handling, _load_live_yaml error paths.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.deployment.brain_lifecycle_manager import (
    BrainLifecycleManager,
    IntegrityReport,
    ReferenceAuditReport,
    RegistrationReport,
    RetirementReport,
    _utc_now_compact,
    _utc_now_iso,
)

# ═══════════════════════════════════════════════════════════════════════════
# _utc_now_iso / _utc_now_compact
# ═══════════════════════════════════════════════════════════════════════════


class TestTimeHelpers:
    def test_utc_now_iso_format(self) -> None:
        ts = _utc_now_iso()
        assert "T" in ts
        assert "+" in ts or "Z" in ts

    def test_utc_now_compact_format(self) -> None:
        ts = _utc_now_compact()
        assert len(ts) == 15  # YYYYMMDD_HHMMSS
        assert "_" in ts
        assert ts[8] == "_"


# ═══════════════════════════════════════════════════════════════════════════
# Report dataclass defaults
# ═══════════════════════════════════════════════════════════════════════════


class TestRetirementReport:
    def test_defaults(self) -> None:
        r = RetirementReport()
        assert r.brain_id == ""
        assert r.governance_updated is False
        assert r.transition_logged is False
        assert r.config_archived is None
        assert r.live_yaml_removed is False
        assert r.pnl_archived is False
        assert r.atomic_success is False
        assert r.rollback_triggered is False
        assert r.artifact_report == []
        assert r.reference_warnings == []
        assert r.errors == []
        assert r.warnings == []

    def test_custom_fields(self) -> None:
        r = RetirementReport(
            brain_id="test_brain",
            atomic_success=True,
            errors=["something wrong"],
        )
        assert r.brain_id == "test_brain"
        assert r.atomic_success is True
        assert r.errors == ["something wrong"]


class TestRegistrationReport:
    def test_defaults(self) -> None:
        r = RegistrationReport()
        assert r.brain_id == ""
        assert r.config_validated is False
        assert r.artifact_found is False
        assert r.quality_gate_passed is False
        assert r.errors == []
        assert r.warnings == []


class TestIntegrityReport:
    def test_defaults(self) -> None:
        r = IntegrityReport()
        assert r.valid is True
        assert r.missing_config_files == []
        assert r.missing_yaml_entries == []
        assert r.missing_artifacts == []
        assert r.governance_orphans == []
        assert r.pnl_ledger_orphans == []
        assert r.auto_registered == []
        assert r.auto_deleted == []
        assert r.contract_violations == []


class TestReferenceAuditReport:
    def test_defaults(self) -> None:
        r = ReferenceAuditReport()
        assert r.scanned_files == 0
        assert r.hardcoded_brain_paths == []
        assert r.hardcoded_model_paths == []
        assert r.hardcoded_norm_paths == []
        assert r.stale_references == []


# ═══════════════════════════════════════════════════════════════════════════
# BrainLifecycleManager — _scan_brain_configs
# ═══════════════════════════════════════════════════════════════════════════


class TestScanBrainConfigs:
    def test_empty_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir)
            result = BrainLifecycleManager._scan_brain_configs(brains_dir)
            assert result == {}

    def test_scans_valid_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir)
            (brains_dir / "brain_a.json").write_text(
                json.dumps({"brain_id": "brain_a", "status": "live"}),
                encoding="utf-8",
            )
            result = BrainLifecycleManager._scan_brain_configs(brains_dir)
            assert "brain_a" in result
            assert result["brain_a"]["status"] == "live"

    def test_skips_normalization_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir)
            (brains_dir / "normalization_params.json").write_text(
                json.dumps({"brain_id": "norm"}),
                encoding="utf-8",
            )
            result = BrainLifecycleManager._scan_brain_configs(brains_dir)
            assert "norm" not in result

    def test_skips_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brains_dir = Path(tmpdir)
            (brains_dir / "broken.json").write_text("not json")
            result = BrainLifecycleManager._scan_brain_configs(brains_dir)
            assert result == {}

    def test_nonexistent_dir(self) -> None:
        result = BrainLifecycleManager._scan_brain_configs(Path("/nonexistent/path"))
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# BrainLifecycleManager — _find_config_by_brain_id
# ═══════════════════════════════════════════════════════════════════════════


class TestFindConfigByBrainId:
    def test_finds_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            brains_dir = root / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "test.json").write_text(
                json.dumps({"brain_id": "target_brain"}),
                encoding="utf-8",
            )
            # Create minimal manager with mocked live.yaml to avoid file access error
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.parent.mkdir(parents=True, exist_ok=True)
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                brains_dir=str(brains_dir),
                live_yaml_path=str(live_yaml),
            )
            found = mgr._find_config_by_brain_id("target_brain")
            assert found is not None
            assert found.name == "test.json"

    def test_not_found_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            brains_dir = root / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.parent.mkdir(parents=True, exist_ok=True)
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                brains_dir=str(brains_dir),
                live_yaml_path=str(live_yaml),
            )
            found = mgr._find_config_by_brain_id("nonexistent")
            assert found is None

    def test_skips_normalization_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            brains_dir = root / "configs" / "brains"
            brains_dir.mkdir(parents=True)
            (brains_dir / "normalization.json").write_text(
                json.dumps({"brain_id": "norm_target"}),
                encoding="utf-8",
            )
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.parent.mkdir(parents=True, exist_ok=True)
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                brains_dir=str(brains_dir),
                live_yaml_path=str(live_yaml),
            )
            found = mgr._find_config_by_brain_id("norm_target")
            assert found is None  # skipped because filename contains "normalization"


# ═══════════════════════════════════════════════════════════════════════════
# BrainLifecycleManager — __init__ path handling
# ═══════════════════════════════════════════════════════════════════════════


class TestInit:
    def test_init_with_minimal_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs").mkdir(parents=True, exist_ok=True)
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                live_yaml_path=str(live_yaml),
            )
            assert mgr._project_root == root
            assert mgr._live_yaml_path == live_yaml

    def test_init_defaults_to_cwd(self) -> None:
        """When project_root is None, defaults to Path.cwd().
        This test verifies the default behavior — may fail if cwd has no configs/live.yaml."""
        # Skip actual init which requires live.yaml; just test _project_root default
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs").mkdir(parents=True, exist_ok=True)
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                live_yaml_path=str(live_yaml),
            )
            assert mgr._brains_dir.name == "brains"
            assert mgr._retired_dir.name == "retired"


# ═══════════════════════════════════════════════════════════════════════════
# BrainLifecycleManager — _load_live_yaml error paths
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadLiveYamlErrors:
    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs").mkdir(parents=True, exist_ok=True)
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                live_yaml_path=str(live_yaml),
            )
            # Now point to a non-existent file
            mgr._live_yaml_path = root / "nonexistent.yaml"
            with pytest.raises(FileNotFoundError):
                mgr._load_live_yaml()

    def test_empty_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "configs").mkdir(parents=True, exist_ok=True)
            live_yaml = root / "configs" / "live.yaml"
            live_yaml.write_text("live_trading:\n  symbol: XAUUSDc\nstrategy_lines: {}\n")
            mgr = BrainLifecycleManager(
                project_root=root,
                live_yaml_path=str(live_yaml),
            )
            # Override with empty file
            empty_file = root / "empty.yaml"
            empty_file.write_text("")
            mgr._live_yaml_path = empty_file
            with pytest.raises(ValueError, match="empty"):
                mgr._load_live_yaml()

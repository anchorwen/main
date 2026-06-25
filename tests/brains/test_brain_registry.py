"""Tests for core.brains.brain_registry — centralized brain metadata registry.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.brains.brain_registry import BrainEntry, BrainRegistry

# ── Helpers ────────────────────────────────────────────────────────────────


def _write_brain_json(
    config_dir: Path,
    brain_id: str,
    brain_type: str = "xgboost_json",
    contract_group: str = "barrier_12bar",
    status: str = "candidate",
    magic: int = 1000,
    **overrides,
) -> Path:
    """Write a minimal brain registry entry JSON file."""
    data = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": brain_type,
        "brain_role": "primary",
        "contract_group": contract_group,
        "training_horizon": 12,
        "feature_schema": "v9_40dim",
        "vote_weight": 1.0,
        "magic": magic,
        "status": status,
        "artifact_path": f"models/{brain_id}.json",
        "hmre_layer": "M15",
        "training_params": {},
        **overrides,
    }
    path = config_dir / f"{brain_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── BrainEntry ─────────────────────────────────────────────────────────────


class TestBrainEntry:
    def test_defaults(self) -> None:
        entry = BrainEntry(
            brain_id="test_brain",
            brain_type="xgboost_json",
            brain_role="primary",
            contract_group="barrier_12bar",
            training_horizon=12,
            feature_schema="v9_40dim",
            vote_weight=1.0,
            magic=1000,
            status="candidate",
            artifact_path="models/test.json",
        )
        assert entry.hmre_layer == ""
        assert entry.training_params == {}

    def test_is_active_candidate(self) -> None:
        entry = BrainEntry(
            brain_id="b1",
            brain_type="t",
            brain_role="r",
            contract_group="g",
            training_horizon=12,
            feature_schema="s",
            vote_weight=1.0,
            magic=1,
            status="candidate",
            artifact_path="p",
        )
        assert entry.is_active is True

    def test_is_active_live(self) -> None:
        entry = BrainEntry(
            brain_id="b1",
            brain_type="t",
            brain_role="r",
            contract_group="g",
            training_horizon=12,
            feature_schema="s",
            vote_weight=1.0,
            magic=1,
            status="live",
            artifact_path="p",
        )
        assert entry.is_active is True

    def test_is_active_retired(self) -> None:
        entry = BrainEntry(
            brain_id="b1",
            brain_type="t",
            brain_role="r",
            contract_group="g",
            training_horizon=12,
            feature_schema="s",
            vote_weight=1.0,
            magic=1,
            status="retired",
            artifact_path="p",
        )
        assert entry.is_active is False

    def test_is_active_frozen(self) -> None:
        entry = BrainEntry(
            brain_id="b1",
            brain_type="t",
            brain_role="r",
            contract_group="g",
            training_horizon=12,
            feature_schema="s",
            vote_weight=1.0,
            magic=1,
            status="frozen",
            artifact_path="p",
        )
        assert entry.is_active is False


# ── BrainRegistry Loading ──────────────────────────────────────────────────


class TestBrainRegistryLoading:
    def test_loads_valid_json_files(self, tmp_path: Path) -> None:
        _write_brain_json(tmp_path, "brain_a", brain_type="xgboost_json")
        _write_brain_json(tmp_path, "brain_b", brain_type="lightgbm_txt")
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 2

    def test_skips_wrong_schema_version(self, tmp_path: Path) -> None:
        _write_brain_json(tmp_path, "good")
        _write_brain_json(tmp_path, "bad", schema_version="old_schema.v0")
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 1

    def test_skips_normalization_files(self, tmp_path: Path) -> None:
        _write_brain_json(tmp_path, "brain_a")
        (tmp_path / "brain_a.normalization.json").write_text('{"x":1}')
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 1

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        _write_brain_json(tmp_path, "brain_a")
        (tmp_path / "bad.json").write_text("not valid json{{{")
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 1

    def test_empty_directory(self, tmp_path: Path) -> None:
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 0

    def test_nonexistent_directory(self) -> None:
        registry = BrainRegistry(config_dir="/nonexistent/path/12345")
        assert len(registry.list_all()) == 0


class TestBrainRegistryReload:
    def test_reload_picks_up_new_files(self, tmp_path: Path) -> None:
        _write_brain_json(tmp_path, "brain_a")
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 1

        _write_brain_json(tmp_path, "brain_b")
        registry.reload(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 2

    def test_reload_removes_deleted_entries(self, tmp_path: Path) -> None:
        path_a = _write_brain_json(tmp_path, "brain_a")
        _write_brain_json(tmp_path, "brain_b")
        registry = BrainRegistry(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 2

        path_a.unlink()
        registry.reload(config_dir=str(tmp_path))
        assert len(registry.list_all()) == 1


# ── BrainRegistry Lookup ───────────────────────────────────────────────────


class TestBrainRegistryLookup:
    @pytest.fixture
    def registry(self, tmp_path: Path) -> BrainRegistry:
        _write_brain_json(
            tmp_path,
            "barrier_v1",
            brain_type="xgboost_json",
            contract_group="barrier_12bar",
            magic=1001,
            status="live",
        )
        _write_brain_json(
            tmp_path,
            "barrier_v2",
            brain_type="xgboost_json",
            contract_group="barrier_12bar",
            magic=1002,
            status="candidate",
        )
        _write_brain_json(
            tmp_path,
            "swing_v1",
            brain_type="lightgbm_txt",
            contract_group="swing_daily",
            magic=2001,
            status="live",
            training_horizon=24,
            feature_schema="swing_enhanced_35",
        )
        return BrainRegistry(config_dir=str(tmp_path))

    def test_get_existing(self, registry: BrainRegistry) -> None:
        entry = registry.get("barrier_v1")
        assert entry is not None
        assert entry.brain_id == "barrier_v1"
        assert entry.status == "live"

    def test_get_missing(self, registry: BrainRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_get_by_type(self, registry: BrainRegistry) -> None:
        xgb = registry.get_by_type("xgboost_json")
        assert len(xgb) == 2

    def test_get_first_by_type(self, registry: BrainRegistry) -> None:
        entry = registry.get_first_by_type("lightgbm_txt")
        assert entry is not None
        assert entry.brain_id == "swing_v1"

    def test_get_first_by_type_missing(self, registry: BrainRegistry) -> None:
        assert registry.get_first_by_type("nonexistent") is None

    def test_get_contract_group_existing(self, registry: BrainRegistry) -> None:
        assert registry.get_contract_group("barrier_v1") == "barrier_12bar"
        assert registry.get_contract_group("swing_v1") == "swing_daily"

    def test_get_contract_group_missing(self, registry: BrainRegistry) -> None:
        assert registry.get_contract_group("nonexistent") == "barrier_12bar"

    def test_get_training_horizon(self, registry: BrainRegistry) -> None:
        assert registry.get_training_horizon("swing_v1") == 24
        assert registry.get_training_horizon("barrier_v1") == 12

    def test_get_training_horizon_missing(self, registry: BrainRegistry) -> None:
        assert registry.get_training_horizon("nonexistent") == 12

    def test_get_feature_schema(self, registry: BrainRegistry) -> None:
        assert registry.get_feature_schema("swing_v1") == "swing_enhanced_35"

    def test_get_feature_schema_missing(self, registry: BrainRegistry) -> None:
        assert registry.get_feature_schema("nonexistent") == "v9_40dim"

    def test_list_by_group(self, registry: BrainRegistry) -> None:
        barrier = registry.list_by_group("barrier_12bar")
        assert len(barrier) == 2
        ids = {e.brain_id for e in barrier}
        assert ids == {"barrier_v1", "barrier_v2"}

    def test_list_by_group_empty(self, registry: BrainRegistry) -> None:
        assert registry.list_by_group("nonexistent") == []

    def test_list_all(self, registry: BrainRegistry) -> None:
        assert len(registry.list_all()) == 3

    def test_all_groups(self, registry: BrainRegistry) -> None:
        groups = registry.all_groups
        assert "barrier_12bar" in groups
        assert "swing_daily" in groups

    def test_resolve_ids_to_group(self, registry: BrainRegistry) -> None:
        assert registry.resolve_ids_to_group(["barrier_v1"]) == "barrier_12bar"
        assert registry.resolve_ids_to_group(["swing_v1"]) == "swing_daily"

    def test_resolve_ids_to_group_first_match(self, registry: BrainRegistry) -> None:
        assert registry.resolve_ids_to_group(["barrier_v1", "swing_v1"]) == "barrier_12bar"

    def test_resolve_ids_to_group_none_found(self, registry: BrainRegistry) -> None:
        assert registry.resolve_ids_to_group(["nonexistent"]) == "unknown"


# ── BrainRegistry Singleton ─────────────────────────────────────────────────


class TestBrainRegistrySingleton:
    def test_instance_returns_same_object(self) -> None:
        BrainRegistry.reset()
        a = BrainRegistry.instance()
        b = BrainRegistry.instance()
        assert a is b

    def test_reset_clears_singleton(self) -> None:
        BrainRegistry.reset()
        a = BrainRegistry.instance()
        BrainRegistry.reset()
        b = BrainRegistry.instance()
        assert a is not b

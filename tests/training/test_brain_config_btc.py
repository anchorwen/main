"""Phase 5 / M3 hard assertions — 血缘/版本化 (lineage & versioning).

FIX-20260803-006 / IC 最高批准 (战役五 — 模型的数字指纹):
  1. build_brain_config() is the ONLY way a brain config is written.  BTC
     contract groups resolve the strategy-line magic from live_btc.yaml; the
     longest-prefix matcher keeps btc_swing_h1_v2 from collapsing to btc_swing.
  2. Every institutional brain carries a birth certificate: artifact_hash,
     trained_by_commit_hash, dataset_hash, label_contract_id, feature schema,
     magic.  Missing any required lineage field → ValueError (fail-fast).
  3. TrainingRunRecord persists the lineage columns (dataset_hash /
     label_contract_id / trained_by_commit_hash / oos_verdict).
  4. verify_lineage() is the iron gate: it PASSes a complete config, MISSes a
     legacy hand-written config (with migration guidance), and FAILs an
     integrity violation (magic mismatch / tampered artifact).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from core.training.brain_config import (
    CONTRACT_GROUP_MAGIC,
    _derive_contract_group,
    build_brain_config,
)
from core.training.training_registry import TrainingRunRecord, create_registry
from scripts.training.verify_lineage import (
    _sha256_file,
    _strategy_line_magics,
    verify_brain,
)

# ── 1. BTC contract-group magic resolution ──────────────────────────────────


class TestBtcMagicGroups:
    def test_expected_r_magic(self) -> None:
        assert CONTRACT_GROUP_MAGIC["btc_expected_r_m15"] == 90452

    def test_swing_magics_match_live_btc(self) -> None:
        assert CONTRACT_GROUP_MAGIC["btc_swing"] == 90410
        assert CONTRACT_GROUP_MAGIC["btc_swing_h1"] == 90411
        assert CONTRACT_GROUP_MAGIC["btc_swing_m30"] == 90430
        assert CONTRACT_GROUP_MAGIC["btc_swing_h1_v2"] == 90460
        assert CONTRACT_GROUP_MAGIC["btc_swing_h4"] == 904240

    def test_longest_prefix_match(self) -> None:
        # btc_swing_h1_v2 must NOT collapse to the shorter btc_swing prefix.
        assert _derive_contract_group("btc_swing_h1_v2") == "btc_swing_h1_v2"
        assert _derive_contract_group("btc_swing") == "btc_swing"
        # Contract ids are named group-first so the prefix resolves.
        assert _derive_contract_group("btc_expected_r_m15_41d_v2") == "btc_expected_r_m15"

    def test_xau_legacy_unaffected(self) -> None:
        assert _derive_contract_group("m15_swing_lightgbm_v2") == "m15_swing"
        assert _derive_contract_group("h1_swing_xgboost") == "h1_swing"


# ── 2. build_brain_config lineage contract ──────────────────────────────────


def _build_cfg(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "brain_id": "BTC_Expected_R_V5_LONG_20260803_120000",
        "brain_type": "expected_r_long",
        "feature_schema_id": "btc_macro_enhanced_41_v2",
        "artifact_path": "data_btc/models/tower_long_best.txt",
        "artifact_hash": "a" * 64,
        "features": ["f0", "f1"],
        "contract_id": "btc_expected_r_41d_v2_m15",
        "contract_group": "btc_expected_r_m15",
        "label_horizon_bars": 12,
        "metrics": {"train_sharpe": 1.0},
        "initial_status": "shadow",
        "brain_role": "expected_r_tower",
        "dataset_hash": "b" * 64,
        "label_contract_id": "label-expected-r-btc-m15",
    }
    params.update(overrides)
    return build_brain_config(**params)


class TestBuildBrainConfigLineage:
    def test_injects_birth_certificate(self) -> None:
        cfg = _build_cfg()
        assert cfg["dataset_hash"] == "b" * 64
        assert cfg["label_contract_id"] == "label-expected-r-btc-m15"
        assert cfg["magic"] == 90452
        assert cfg["contract_group"] == "btc_expected_r_m15"
        assert cfg["feature_schema_id"] == "btc_macro_enhanced_41_v2"
        assert cfg["trained_by_commit_hash"], "git hash must be injected"

    def test_swing_h1_v2_magic(self) -> None:
        cfg = _build_cfg(contract_group="btc_swing_h1_v2")
        assert cfg["magic"] == 90460

    def test_missing_dataset_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="dataset_hash"):
            _build_cfg(dataset_hash="")

    def test_missing_label_contract_id_raises(self) -> None:
        with pytest.raises(ValueError, match="label_contract_id"):
            _build_cfg(label_contract_id="")

    def test_missing_artifact_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="artifact_hash"):
            _build_cfg(artifact_hash="")


# ── 3. TrainingRunRecord lineage columns ────────────────────────────────────


class TestRegistryLineageColumns:
    def test_record_persists_lineage(self, tmp_path: Path) -> None:
        db = str(tmp_path / "registry.db")
        registry = create_registry(db)
        rec = TrainingRunRecord()
        rec.contract_id = "btc_expected_r_41d_v2_m15"
        rec.timestamp = None
        rec.status = "SHADOW"
        rec.dataset_hash = "d" * 64
        rec.label_contract_id = "label-expected-r-btc-m15"
        rec.trained_by_commit_hash = "abc12345"
        rec.oos_verdict = "PASS"
        rec.model_hash = "m" * 64
        registry.add_run(rec)

        fetched = registry.get_run(rec.run_id)
        assert fetched is not None
        assert fetched.dataset_hash == "d" * 64
        assert fetched.label_contract_id == "label-expected-r-btc-m15"
        assert fetched.trained_by_commit_hash == "abc12345"
        assert fetched.oos_verdict == "PASS"


# ── 4. verify_lineage iron gate ─────────────────────────────────────────────


def _make_complete_cfg(tmp_path: Path) -> tuple[dict[str, Any], str, Path]:
    artifact = tmp_path / "model.txt"
    artifact.write_text("institutional artifact", encoding="utf-8")
    artifact_hash = _sha256_file(artifact)
    cfg: dict[str, Any] = {
        "brain_id": "BTC_Test_Full",
        "artifact_path": str(artifact),
        "artifact_hash": artifact_hash,
        "trained_by_commit_hash": "abc12345",
        "dataset_hash": "d" * 64,
        "label_contract_id": "label-expected-r-btc-m15",
        "feature_schema_id": "btc_macro_enhanced_41_v2",
        "contract_group": "btc_expected_r_m15",
        "magic": 90452,
    }
    return cfg, artifact_hash, artifact


class TestVerifyLineage:
    def test_complete_config_all_pass(self, tmp_path: Path) -> None:
        cfg, artifact_hash, _ = _make_complete_cfg(tmp_path)
        results = verify_brain(
            cfg,
            tmp_path,
            {artifact_hash: {"run_id": "r1", "status": "SHADOW", "oos_verdict": "PASS"}},
            {"btc_expected_r_m15": 90452},
        )
        assert len(results) == 9
        assert all(r["verdict"] == "PASS" for r in results), [
            (r["check"], r["verdict"]) for r in results
        ]

    def test_legacy_config_reports_missing(self, tmp_path: Path) -> None:
        cfg, artifact_hash, _ = _make_complete_cfg(tmp_path)
        # Legacy hand-written config: no commit/dataset/label lineage fields.
        for k in ("trained_by_commit_hash", "dataset_hash", "label_contract_id"):
            cfg.pop(k)
        results = verify_brain(cfg, tmp_path, {}, {"btc_expected_r_m15": 90452})
        verdicts = {r["check"]: r["verdict"] for r in results}
        assert verdicts["registry_row"] == "MISSING"
        assert verdicts["commit_hash"] == "MISSING"
        assert verdicts["dataset_hash"] == "MISSING"
        assert verdicts["label_contract_id"] == "MISSING"
        # Integrity checks that still pass stay PASS.
        assert verdicts["artifact_hash_matches"] == "PASS"
        assert verdicts["magic_matches_line"] == "PASS"

    def test_magic_mismatch_is_fail(self, tmp_path: Path) -> None:
        cfg, artifact_hash, _ = _make_complete_cfg(tmp_path)
        cfg["magic"] = 90450  # wrong — line expects 90452
        results = verify_brain(
            cfg,
            tmp_path,
            {artifact_hash: {"run_id": "r1", "status": "SHADOW"}},
            {"btc_expected_r_m15": 90452},
        )
        magic = next(r for r in results if r["check"] == "magic_matches_line")
        assert magic["verdict"] == "FAIL"
        assert "90452" in magic["detail"]

    def test_tampered_artifact_is_fail(self, tmp_path: Path) -> None:
        cfg, _, artifact = _make_complete_cfg(tmp_path)
        # Re-write artifact AFTER hashing → sha256(file) != artifact_hash.
        artifact.write_text("tampered", encoding="utf-8")
        results = verify_brain(
            cfg,
            tmp_path,
            {cfg["artifact_hash"]: {"run_id": "r1", "status": "SHADOW"}},
            {"btc_expected_r_m15": 90452},
        )
        h = next(r for r in results if r["check"] == "artifact_hash_matches")
        assert h["verdict"] == "FAIL"
        assert "modified since training" in h["detail"]

    def test_missing_artifact_hash_is_fail(self, tmp_path: Path) -> None:
        cfg, _, _ = _make_complete_cfg(tmp_path)
        cfg["artifact_hash"] = ""
        results = verify_brain(cfg, tmp_path, {}, {"btc_expected_r_m15": 90452})
        verdicts = {r["check"]: r["verdict"] for r in results}
        assert verdicts["artifact_hash_present"] == "FAIL"
        assert verdicts["registry_row"] == "MISSING"

    def test_strategy_line_magics_parse(self) -> None:
        live = {
            "strategy_lines": {
                "btc_expected_r_m15": {"magic": 90452, "enabled": True},
                "btc_swing": {"magic": 90410, "enabled": True},
            }
        }
        assert _strategy_line_magics(live) == {
            "btc_expected_r_m15": 90452,
            "btc_swing": 90410,
        }


# ── 5. Twin-tower registration glue (dual model / single contract) ───────────


class TestTwinTowerRegistration:
    def _contract(self, tmp_path: Path):
        from core.contracts.training.training_contract import TrainingContract

        return TrainingContract.from_dict(
            {
                "schema_version": "training_contract.v2.1",
                "contract_id": "btc_expected_r_m15_41d_v2",
                "dataset": {
                    "path": "data_btc/training/btc_ssot_v2/train.npz",
                    "feature_schema": "btc_macro_enhanced_41_v2",
                },
                "label": {
                    "contract_id": "label-expected-r-btc-m15",
                    "sl_atr_mult": 1.5,
                    "tp_atr_mult": 2.5,
                    "horizon_bars": 12,
                    "profitability_calibrated": True,
                },
                "architecture": {
                    "type": "lightgbm",
                    "objective_function": "reg_huber",
                    "custom_params": {},
                },
                "validation": {"method": "cpcv"},
                "quality_gates": {},
                "output": {
                    "brain_id_template": "BTC_Expected_R_V5_{tower}_{timestamp}",
                    "model_dir": str(tmp_path / "models"),
                    "config_dir": str(tmp_path / "configs"),
                    "registry_db": str(tmp_path / "registry.db"),
                    "auto_register": False,
                    "initial_status": "shadow",
                },
            }
        )

    def test_long_tower_identity_and_registry(self, tmp_path: Path) -> None:
        from core.training.training_registry import create_registry
        from scripts.training.train_btc_expected_r_institutional import _register_tower

        model = tmp_path / "tower_long_best.txt"
        model.write_text("fake model artifact", encoding="utf-8")
        model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
        ds_hash = "d" * 64

        brain = _register_tower(
            self._contract(tmp_path),
            "LONG",
            model,
            model_hash,
            ds_hash,
            {"spearman_rho": 0.06, "r2": 0.01, "sign_match": 0.55, "n": 100, "mae": 1.0},
            {"verdict": "PASS", "spearman_rho": 0.06, "win_rate": 0.55, "expectancy": 0.1},
            None,
            None,
            None,
            {"spearman_rho": 0.07},
        )

        # LONG identity: brain_type + magic + brain_id.
        assert brain["brain_id"].startswith("BTC_Expected_R_V5_LONG_")
        assert brain["brain_type"] == "expected_r_long"
        assert brain["magic"] == 90452
        assert brain["dataset_hash"] == ds_hash
        assert brain["label_contract_id"] == "label-expected-r-btc-m15"

        # Config file written + registry row carries full lineage.
        cfg_path = tmp_path / "configs" / f"{brain['brain_id']}.json"
        assert cfg_path.exists()
        registry = create_registry(str(tmp_path / "registry.db"))
        run = registry.get_run_by_hash(model_hash)
        assert run is not None
        assert run.dataset_hash == ds_hash
        assert run.oos_verdict == "PASS"
        assert run.trained_by_commit_hash, "commit hash must be persisted"

    def test_short_tower_identity(self, tmp_path: Path) -> None:
        from scripts.training.train_btc_expected_r_institutional import _register_tower

        model = tmp_path / "tower_short_best.txt"
        model.write_text("fake model artifact", encoding="utf-8")
        model_hash = hashlib.sha256(model.read_bytes()).hexdigest()

        brain = _register_tower(
            self._contract(tmp_path),
            "SHORT",
            model,
            model_hash,
            "d" * 64,
            {"spearman_rho": 0.05, "r2": 0.01, "sign_match": 0.55, "n": 100, "mae": 1.0},
            {"verdict": "PASS", "spearman_rho": 0.05, "win_rate": 0.55, "expectancy": 0.1},
            None,
            None,
            None,
            {"spearman_rho": 0.05},
        )
        assert brain["brain_id"].startswith("BTC_Expected_R_V5_SHORT_")
        assert brain["brain_type"] == "expected_r_short"

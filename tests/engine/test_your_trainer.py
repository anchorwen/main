import json
from pathlib import Path

from scripts.training.your_trainer import (
    load_lane_templates,
)
from scripts.training.your_trainer import (
    main as trainer_main,
)


def test_load_lane_templates(tmp_path: Path):
    lane_file = tmp_path / "lane_trainers.json"
    lane_file.write_text(
        json.dumps(
            {"sur": "python sur_trainer.py --seed {train_seed}", "arb": "python arb_trainer.py"}
        ),
        encoding="utf-8",
    )
    templates = load_lane_templates(lane_file)
    assert templates == {
        "sur": "python sur_trainer.py --seed {train_seed}",
        "arb": "python arb_trainer.py",
    }


def test_load_lane_templates_missing_file():
    templates = load_lane_templates(None)
    assert templates == {}


def test_mtx_xgb_lane_resolution(tmp_path: Path):
    manifest_path = tmp_path / "m_xgb.manifest.json"
    manifest_payload = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.mtx.chlg.g2026.1@feat-mtx-xgboost-1.0.0.s47",
        "lane": "mtx",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-mtx-xgboost-1.0.0",
        "iface_semver": "1.0.0",
        "dataset_slice_id": "slice_test_xgb",
        "git_commit": "deadbeef",
        "train_started_at_utc": "2026-05-04T10:00:00Z",
        "trainer_version": "",
        "metrics": {},
        "risk_notes": [],
        "training_run_id": "run_xgb_001",
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8")
    code = trainer_main(
        [
            "--manifest",
            str(manifest_path),
            "--lane-command-template",
            "python -c \"print('xgb_lane_ok')\"",
            "--shell",
        ]
    )
    assert code == 0
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["metrics"]["trained"] is True
    assert updated["metrics"]["lane_exit_code"] == 0


def test_your_trainer_updates_manifest_and_writes_artifact(tmp_path: Path):
    manifest_path = tmp_path / "m.manifest.json"
    manifest_payload = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-v9-institutional-1.0.0",
        "iface_semver": "1.0.0",
        "dataset_slice_id": "slice_test",
        "git_commit": "deadbeef",
        "train_started_at_utc": "2026-04-30T10:00:00Z",
        "trainer_version": "batch-skeleton-0.1.0",
        "metrics": {"phase": "skeleton"},
        "risk_notes": ["stub batch row; run real trainer before promotion"],
        "training_run_id": "run_seed_1__slice_test",
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8")
    code = trainer_main(
        [
            "--manifest",
            str(manifest_path),
            "--trainer-version",
            "your-trainer-0.1.0",
            "--metric-winrate",
            "0.57",
            "--metric-sharpe",
            "1.23",
        ]
    )
    assert code == 0

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["trainer_version"] == "your-trainer-0.1.0"
    assert updated["metrics"]["trained"] is True
    assert float(updated["metrics"]["winrate"]) == 0.57
    assert float(updated["metrics"]["sharpe"]) == 1.23
    assert "stub batch row; run real trainer before promotion" not in updated["risk_notes"]
    artifact = Path(updated["artifact_primary"])
    assert artifact.exists()


def test_your_trainer_lane_command_mode(tmp_path: Path):
    manifest_path = tmp_path / "m2.manifest.json"
    manifest_payload = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-v9-institutional-1.0.0",
        "iface_semver": "1.0.0",
        "dataset_slice_id": "slice_test2",
        "git_commit": "deadbeef",
        "train_started_at_utc": "2026-04-30T10:00:00Z",
        "trainer_version": "batch-skeleton-0.1.0",
        "metrics": {"phase": "skeleton"},
        "risk_notes": ["stub batch row; run real trainer before promotion"],
        "training_run_id": "run_seed_2__slice_test2",
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8")
    code = trainer_main(
        [
            "--manifest",
            str(manifest_path),
            "--lane-command-template",
            "python -c \"print('lane_ok')\"",
            "--shell",
        ]
    )
    assert code == 0
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["metrics"]["lane_exit_code"] == 0
    assert updated["metrics"]["trained"] is True

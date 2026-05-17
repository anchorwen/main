"""Brain registration from CRT manifest contract tests."""

import json
from pathlib import Path


def test_build_brain_entry_sur_onnx(tmp_path: Path):
    from scripts.training.register_brain import build_brain_entry

    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0.s42",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
        "train_seed": 42,
        "artifact_primary": str(tmp_path / "model.onnx"),
    }
    entry = build_brain_entry(manifest, None, None)
    assert entry["schema_version"] == "brain_registry_entry.v1"
    assert entry["brain_type"] == "onnx_v9"
    assert entry["brain_role"] == "alpha_brain"
    assert entry["status"] == "shadow"
    assert entry["artifact_path"] == manifest["artifact_primary"]
    assert "model_version" in entry
    assert "feature_schema_id" in entry


def test_build_brain_entry_arb_ou(tmp_path: Path):
    from scripts.training.register_brain import build_brain_entry

    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.arb.chlg.g2026.1@feat-arb-v6-ou-sniper-1.0.0.s42",
        "lane": "arb",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-arb-v6-ou-sniper-1.0.0",
        "train_seed": 42,
        "artifact_primary": str(tmp_path / "arb_params.json"),
    }
    entry = build_brain_entry(manifest, None, None)
    assert entry["brain_type"] == "ou_params_v6"


def test_build_brain_entry_mtx_xgboost(tmp_path: Path):
    from scripts.training.register_brain import build_brain_entry

    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.mtx.chlg.g2026.1@feat-mtx-xgboost-1.0.0.s47",
        "lane": "mtx",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-mtx-xgboost-1.0.0",
        "train_seed": 47,
        "artifact_primary": str(tmp_path / "xgboost_model.json"),
    }
    entry = build_brain_entry(manifest, None, None)
    assert entry["brain_type"] == "xgboost_v4.5"


def test_build_brain_entry_custom_brain_id(tmp_path: Path):
    from scripts.training.register_brain import build_brain_entry

    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0.s42",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
        "train_seed": 42,
        "artifact_primary": str(tmp_path / "model.onnx"),
    }
    entry = build_brain_entry(manifest, None, "MyCustomBrain_V9")
    assert entry["brain_id"] == "MyCustomBrain_V9"


def test_build_brain_entry_artifact_override(tmp_path: Path):
    from scripts.training.register_brain import build_brain_entry

    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0.s42",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
        "train_seed": 42,
        "artifact_primary": str(tmp_path / "model.onnx"),
    }
    entry = build_brain_entry(manifest, str(tmp_path / "overridden.onnx"), None)
    assert entry["artifact_path"] == str(tmp_path / "overridden.onnx")


def test_register_brain_writes_file(tmp_path: Path):
    from scripts.training.register_brain import register_brain

    entry = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": "TestBrain_01",
        "brain_type": "onnx_v9",
        "brain_role": "alpha_brain",
        "model_version": "g2026.1",
        "status": "shadow",
        "artifact_path": str(tmp_path / "model.onnx"),
        "feature_schema_id": "feat-test-1.0.0",
        "deployment_scope": {
            "symbols": ["XAUUSD"],
            "sessions": ["all"],
            "regimes": ["trend"],
        },
    }
    output_dir = tmp_path / "brains"
    output_dir.mkdir()
    out_path = register_brain(entry, output_dir, skip_gate=True)
    assert out_path.exists()
    assert out_path.name == "TestBrain_01.json"
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["brain_id"] == "TestBrain_01"
    assert written["brain_type"] == "onnx_v9"


def test_cli_dry_run_prints_preview(capsys, tmp_path: Path):
    from scripts.training.register_brain import main as register_main

    manifest_path = tmp_path / "m.json"
    (tmp_path / "model.onnx").write_text("stub", encoding="utf-8")
    manifest_payload = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0.s42",
        "lane": "sur",
        "role": "chlg",
        "generation": "g2026.1",
        "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
        "train_seed": 42,
        "artifact_primary": str(tmp_path / "model.onnx"),
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8")
    code = register_main(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(tmp_path / "brains"),
            "--dry-run",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out or "[dry-run]" in captured.out.lower()
    brains_dir = tmp_path / "brains"
    assert not brains_dir.exists() or len(list(brains_dir.glob("*.json"))) == 0

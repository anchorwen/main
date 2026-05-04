import json
from pathlib import Path
from unittest.mock import patch

from scripts.training.run_train_batch import (
    collect_manifests,
    main,
    render_command,
    run_one,
)


def test_render_command_replaces_placeholders():
    job = {
        "manifest_path": "data/models/a.manifest.json",
        "model_id": "CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0",
        "training_run_id": "run_seed_42__slice_a",
        "train_seed": 42,
        "dataset_slice_id": "slice_a",
    }
    cmd = render_command("python train.py --manifest {manifest_path} --seed {train_seed}", job)
    assert "--manifest data/models/a.manifest.json" in cmd
    assert "--seed 42" in cmd


def test_collect_manifests_from_tmp_path(tmp_path: Path):
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    for i in range(3):
        m = {
            "schema_version": "crt_model_manifest.v1",
            "model_id": f"CRT.sur.chlg.g2026.1@feat-v9-inst-1.0.0.s4{i}",
            "lane": "sur",
            "generation": "g2026.1",
        }
        (manifests_dir / f"m{i}.json").write_text(json.dumps(m), encoding="utf-8")
    paths = collect_manifests(tmp_path, "", 0)
    assert len(paths) == 3
    assert all(p.suffix == ".json" for p in paths)


def test_collect_manifests_lane_filter(tmp_path: Path):
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    for lane in ("sur", "mtx", "arb"):
        m = {
            "schema_version": "crt_model_manifest.v1",
            "model_id": f"CRT.{lane}.chlg.g2026.1@feat-test-1.0.0",
            "lane": lane,
            "generation": "g2026.1",
        }
        (manifests_dir / f"{lane}.json").write_text(json.dumps(m), encoding="utf-8")
    paths = collect_manifests(tmp_path, "sur", 0)
    assert len(paths) == 1
    assert "sur" in paths[0].name


def test_run_one_dry_run(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(
        json.dumps({"model_id": "CRT.sur.chlg.g2026.1@feat-test", "lane": "sur"}),
        encoding="utf-8",
    )
    record = run_one(manifest_path, execute=False, shell=False, timeout=None)
    assert record["action"] == "dry-run"
    assert record["exit_code"] == 0
    assert record["model_id"] == "CRT.sur.chlg.g2026.1@feat-test"


def test_main_dry_run_report(tmp_path: Path):
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    m = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1@feat-test",
        "lane": "sur",
        "generation": "g2026.1",
    }
    (manifests_dir / "test.json").write_text(json.dumps(m), encoding="utf-8")
    with patch("sys.stdout") as _:
        exit_code = main(["--batch-dir", str(tmp_path)])  # default is dry-run
    assert exit_code == 0
    report_dir = tmp_path / "reports"
    reports = list(report_dir.glob("run_report_*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["total"] == 1
    assert report["mode"] == "dry-run"

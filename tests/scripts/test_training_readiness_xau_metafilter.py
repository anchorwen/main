"""TDD tests for TECH_DEBT-020 (The Empty NPZ) — stage-3 NPZ load hardening.

Three-pronged defense (IC 2026-08-21 雷霆裁决, FIX-20260821-006):
  ① contract builder_args fix  — test_xau_contract_has_structured_builder_args
  ② builder fail-fast          — builder exits non-zero → validator FAIL (never silent rc=0)
  ③ reader tolerance           — empty/corrupt NPZ → explicit FAIL verdict, NO traceback

validate_stage_3_dataset_builder is tested with a monkeypatched subprocess.run —
no real builder subprocess is spawned and no live data is touched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from scripts.check_training_readiness import StageVerdict, validate_stage_3_dataset_builder

REPO_ROOT = Path(__file__).resolve().parents[2]
XAU_CONTRACT = REPO_ROOT / "configs" / "contracts" / "training_pipeline_xau_metafilter_v1.json"

_N_FEATURES = 40
_N_SAMPLES = 600
_FEATURE_NAMES = [f"F{i}" for i in range(_N_FEATURES)]


def _xau_contract(*, with_builder_args: bool = True) -> dict:
    """Minimal XAU-metafilter-style contract (40-dim) for validator unit tests."""
    stage: dict = {
        "builder_script": str(REPO_ROOT / "scripts" / "build_btc_metafilter_v2_dataset.py"),
        "builder_output_arg": "--output",
        "outputs": {
            "dataset": {
                "format": "npz",
                "dimension": _N_FEATURES,
                "min_matched_samples": 500,
                "min_asof_join_rate": 0.80,
            }
        },
    }
    if with_builder_args:
        stage["builder_args"] = [
            "--data-dir",
            "data",
            "--symbol",
            "XAUUSDc",
            "--spread-cost-usd",
            "0.0",
        ]
    return {
        "model_target": {
            "input_dimension": _N_FEATURES,
            "feature_names_ssot": list(_FEATURE_NAMES),
            "min_training_samples": 500,
            "min_positive_label_pct": 0.15,
        },
        "stages": {"stage_3_dataset_builder": stage},
    }


def _fake_run(monkeypatch, behavior: str, rc: int = 1) -> None:
    """Monkeypatch subprocess.run to simulate builder outcomes.

    The validator appends ``--output <path>`` (or ``--output-dir <dir>``) as the
    last CLI args, so the fake writes/doesn't-write to that path.
    """

    def _run(cmd, **kwargs):
        args = list(cmd)
        out_path: Path | None = None
        out_dir: Path | None = None
        if "--output-dir" in args:
            out_dir = Path(args[args.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
        elif "--output" in args:
            out_path = Path(args[args.index("--output") + 1])

        if behavior == "builder_fail":
            return subprocess.CompletedProcess(
                args, returncode=rc, stdout="", stderr="builder boom"
            )

        if behavior == "valid":
            rng = np.random.default_rng(42)
            X = rng.standard_normal((_N_SAMPLES, _N_FEATURES))
            y = np.zeros(_N_SAMPLES, dtype=int)
            y[: _N_SAMPLES // 4] = 1  # 25% positive → ≥ min_positive_label_pct
            target = (out_dir / "train.npz") if out_dir is not None else out_path
            if target is not None:
                np.savez_compressed(
                    target,
                    X=X,
                    y=y,
                    feature_names=np.array(_FEATURE_NAMES, dtype=str),
                )
        elif behavior == "empty":
            if out_path is not None:
                out_path.write_bytes(b"")  # leave the pre-created file empty
        elif behavior == "corrupt":
            if out_path is not None:
                out_path.write_bytes(b"not-a-real-npz")
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)


def _checks(stage_result: dict, check_name: str) -> list[dict]:
    return [r for r in stage_result["results"] if r["check"] == check_name]


# ── ① Contract fix (real JSON) ────────────────────────────────────────────


def test_xau_contract_has_structured_builder_args() -> None:
    """The XAU metafilter v1 contract must carry structured builder_args.

    This is the ① immediate fix: without it the builder defaults to symbol
    BTCUSDc, finds no feature store in data/, and silently returns rc=0.
    """
    contract = json.loads(XAU_CONTRACT.read_text(encoding="utf-8"))
    stage = contract["stages"]["stage_3_dataset_builder"]
    assert stage["builder_script"] == "scripts/build_btc_metafilter_v2_dataset.py"
    assert stage["builder_output_arg"] == "--output"
    assert "--symbol" in stage["builder_args"]
    assert "XAUUSDc" in stage["builder_args"]
    assert "--spread-cost-usd" in stage["builder_args"]


# ── ③ Reader tolerance (no traceback on empty/corrupt) ────────────────────


def test_stage3_valid_npz_passes(monkeypatch, tmp_path) -> None:
    _fake_run(monkeypatch, "valid")
    result = validate_stage_3_dataset_builder(_xau_contract(), str(tmp_path))
    assert result["verdict"] == StageVerdict.PASS
    assert _checks(result, "dataset_dimension")[0]["verdict"] == StageVerdict.PASS


def test_stage3_empty_npz_degrades_to_fail_no_traceback(monkeypatch, tmp_path) -> None:
    """Builder exits 0 but wrote nothing → explicit FAIL, never EOFError."""
    _fake_run(monkeypatch, "empty")
    result = validate_stage_3_dataset_builder(_xau_contract(), str(tmp_path))
    assert result["verdict"] == StageVerdict.FAIL
    npz = _checks(result, "dataset_npz_load")[0]
    assert npz["verdict"] == StageVerdict.FAIL
    assert "EMPTY" in npz["detail"]


def test_stage3_corrupt_npz_degrades_to_fail_no_traceback(monkeypatch, tmp_path) -> None:
    """Truncated/corrupt NPZ → explicit FAIL with diagnostic, never a traceback."""
    _fake_run(monkeypatch, "corrupt")
    result = validate_stage_3_dataset_builder(_xau_contract(), str(tmp_path))
    assert result["verdict"] == StageVerdict.FAIL
    npz = _checks(result, "dataset_npz_load")[0]
    assert npz["verdict"] == StageVerdict.FAIL
    assert "unreadable" in npz["detail"].lower()


# ── ② Builder fail-fast signal surfaces as FAIL ───────────────────────────


def test_stage3_builder_nonzero_exit_is_fail(monkeypatch, tmp_path) -> None:
    _fake_run(monkeypatch, "builder_fail", rc=1)
    result = validate_stage_3_dataset_builder(_xau_contract(), str(tmp_path))
    assert result["verdict"] == StageVerdict.FAIL
    assert _checks(result, "builder_execution")[0]["verdict"] == StageVerdict.FAIL


# ── Regression locks (control groups must be unchanged) ───────────────────


def test_stage3_legacy_contract_without_builder_args_still_works(monkeypatch, tmp_path) -> None:
    """BTC metafilter v3 has no builder_args — fallback path must keep working."""
    _fake_run(monkeypatch, "valid")
    result = validate_stage_3_dataset_builder(_xau_contract(with_builder_args=False), str(tmp_path))
    assert result["verdict"] == StageVerdict.PASS


def test_stage3_output_dir_no_npz_is_warn(monkeypatch, tmp_path) -> None:
    """Swing-style --output-dir mode with no produced npz → WARN (existing behavior)."""
    stage = _xau_contract()
    stage["stages"]["stage_3_dataset_builder"]["builder_output_arg"] = "--output-dir"
    _fake_run(monkeypatch, "empty")  # writes nothing into the dir → no .npz glob
    result = validate_stage_3_dataset_builder(stage, str(tmp_path))
    npz = _checks(result, "dataset_npz_load")[0]
    assert npz["verdict"] == StageVerdict.WARN

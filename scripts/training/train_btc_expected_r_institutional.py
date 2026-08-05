"""BTC Expected-R Two-Tower INSTITUTIONAL training — dual model / single contract.

Phase 5 (血缘/版本化 — M3 战役五 / IC 最高批准, FIX-20260803-006).

The Expected-R twin-tower is a *dual-model / single-contract* special form: one
label contract (``label-expected-r-btc-m15``) drives TWO independent LightGBM
towers (LONG → E[R_long], SHORT → E[R_short]).  It cannot be run through
``train.py``'s single-model main loop, so this orchestrator provides the same
institutional guarantees per tower:

  1. Hash-lock — refuses to train on a dirty working tree (reproducible lineage)
  2. ``build_brain_config()`` — the ONLY way a brain config is written; injects
     artifact_hash, trained_by_commit_hash, dataset_hash, label_contract_id,
     magic (== live_btc.yaml strategy-line magic 90452), features from SSOT
  3. ``TrainingRunRecord`` SQLite registry — one row per tower with full lineage
  4. Phase 3 OOS blind gate — a tower that fails ρ / breakeven is HARD-VETOED
     (never enters the candidate pool; the run exits non-zero)
  5. Dual auto-registration — each passing tower → brain config + live_btc.yaml
     + data_btc/governance_state.json, with LONG/SHORT identities (brain_type
     expected_r_long / expected_r_short, brain_role expected_r_tower)

Training kernels (asymmetric Huber objective, time-decay weights, multi-seed
ensemble, metrics) are REUSED from the established ``train_btc_expected_r.py``
— not duplicated.  Registration helpers are REUSED from ``train.py``.

Usage:
  python scripts/training/train_btc_expected_r_institutional.py \
      --contract configs/training/btc_expected_r_41d_v2_m15.yaml \
      --dataset-dir data_btc/training/btc_ssot_v2
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Shared kernels (single implementation, no duplication) ───────────────────
from scripts.training.train import (  # noqa: E402  # registration helpers
    ModelQualityException,
    _auto_register_in_governance,
    _auto_register_in_live_yaml,
)
from scripts.training.train_btc_expected_r import (  # noqa: E402
    compute_metrics,
    compute_time_decay_weights,
    train_tower_multi_seed,
)

TOWERS = ("LONG", "SHORT")
Y_KEY = {"LONG": "y_long", "SHORT": "y_short"}
BRAIN_TYPE = {"LONG": "expected_r_long", "SHORT": "expected_r_short"}


def label_contract_block(contract) -> dict[str, Any]:
    """The label_contract block an enabled brain must carry (verify.py Check 3).

    FIX-20260803-007: config-consistency gate (DQAF-20260622-051) requires every
    ENABLED brain to declare its training label contract so train-serve SL/TP
    alignment is auditable.  Values come from the training contract's LabelSpec
    (the single source of truth — aligned via validate_label_vs_live hard gate).
    """
    return {
        "contract_id": contract.label.contract_id,
        "aligned_with": "live_btc.yaml",
        "sl_atr_mult": contract.label.sl_atr_mult,
        "tp_atr_mult": contract.label.tp_atr_mult,
        "horizon_bars": contract.label.horizon_bars,
        "spread_points": contract.label.spread_points,
        "slippage_points": contract.label.slippage_points,
        "tick_size": contract.label.tick_size,
        "tick_value": contract.label.tick_value,
        "output_unit": contract.label.output_unit,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Hash-lock (reproducible lineage — identical guard to train.py)
# ═══════════════════════════════════════════════════════════════════════════════


def _is_forensic_probe(path: str) -> bool:
    """IC-mandated `_audit_*.py` forensic probes stay uncommitted by design
    (ruff convention: ``scripts/_audit_*.py``, FIX-20260805-003).  They are
    read-only investigation scripts — never part of the trained lineage — so
    they must NEVER trip the hash-lock and block the 8/19 battle."""
    return path.endswith(".py") and Path(path).name.startswith("_audit_")


def _enforce_hash_lock(allow_dirty: bool, cwd: str | Path | None = None) -> None:
    """Content-based hash-lock: refuse to train on a semantically dirty tree.

    DQAF-20260805-001 (IC absolute approval 2026-08-05): the gate compares
    WORKTREE to HEAD by CONTENT (``git diff HEAD --name-only``) instead of
    ``git status --porcelain``.  Porcelain is stat-based — a process rewriting
    a tracked file with byte-identical content (mtime bump only) yields a
    phantom `` M`` that never self-heals and would false-positive the 8/19
    battle.  Content comparison is immune to stat phantoms AND to CRLF
    pseudo-diffs (FIX-20260805-005 LF contract = double insurance).

    Untracked source files (``git ls-files --others``) are blocked TOO — a new
    uncommitted module is as much a lineage break as a modified tracked one —
    except the IC-mandated ``_audit_*.py`` forensic probes (see
    ``_is_forensic_probe``).

    ``cwd`` is test-only: the regression suite runs this against a throwaway
    git repo; production always uses PROJECT_ROOT.
    """
    if allow_dirty:
        return
    root = str(cwd if cwd is not None else PROJECT_ROOT)
    try:
        dirty = subprocess.run(
            ["git", "diff", "HEAD", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=root,
        )
        if dirty.returncode == 0:
            source_dirty = [
                f
                for f in dirty.stdout.strip().split("\n")
                if f.strip()
                and f.endswith((".py", ".yaml", ".yml", ".json"))
                and not Path(f).parts[0].startswith("data")
            ]
            # Untracked source files — a lineage break just like a modified
            # tracked file, but the _audit_*.py forensic probes are exempt.
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=root,
            )
            if untracked.returncode == 0:
                source_dirty += [
                    f
                    for f in untracked.stdout.strip().split("\n")
                    if f.strip()
                    and f.endswith((".py", ".yaml", ".yml", ".json"))
                    and not Path(f).parts[0].startswith("data")
                    and not _is_forensic_probe(f)
                ]
            source_dirty = sorted(set(source_dirty))
            if source_dirty:
                print("[expected-r] [HASH-LOCK] DIRTY WORKING TREE", flush=True)
                for f in source_dirty:
                    print(f"  - {f}", flush=True)
                raise SystemExit(
                    f"Hash-lock: {len(source_dirty)} source file(s) uncommitted. "
                    "Commit changes or use --allow-dirty."
                )
    except (OSError, subprocess.TimeoutExpired):
        pass  # git unavailable — skip check


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════


def _load_split(path: Path) -> dict[str, Any]:
    d = np.load(path, allow_pickle=True)
    return {
        "X": d["X"],
        "y_long": d["y_long"].astype(np.float64),
        "y_short": d["y_short"].astype(np.float64),
        "timestamps": d.get("timestamps") if "timestamps" in d.files else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tower gate — Phase 3 OOS blind + breakeven + train spearman
# ═══════════════════════════════════════════════════════════════════════════════


def _evaluate_tower_gate(
    contract, model_path: Path, blind_path: Path, tower: str
) -> tuple[dict[str, Any], float | None]:
    """Run the Phase 3 OOS blind gate for one tower.

    Returns (blind_result, breakeven_wr).  Raises ModelQualityException with a
    FAIL verdict — the tower must NOT enter the candidate pool.
    """
    from core.contracts.training.label_from_live_yaml import label_params_from_live_yaml
    from core.training.breakeven import compute_breakeven_from_params
    from scripts.training.oos_blind_test import run_blind_test

    gates = contract.quality_gates
    breakeven_wr: float | None = None
    if gates.enforce_breakeven:
        breakeven_wr = compute_breakeven_from_params(
            contract.label.sl_atr_mult,
            contract.label.tp_atr_mult,
            spread_points=contract.label.spread_points,
            slippage_points=contract.label.slippage_points,
            tick_size=contract.label.tick_size,
            friction_model="expected_r",  # E[R] entry already costs half-spread
        ).breakeven_win_rate
        print(f"[expected-r][{tower}] OOS breakeven WR (physical friction): {breakeven_wr:.4f}")

    if not blind_path.exists():
        raise ModelQualityException(
            f"[expected-r][{tower}] Hard veto: oos_blind_path '{blind_path}' missing. "
            f"Model {contract.contract_id} must not be deployed without a blind test."
        )

    blind_result = run_blind_test(
        str(model_path),
        blind_path,
        y_key=Y_KEY[tower],
        min_rho=gates.min_oos_rho,
        min_win_rate=gates.min_oos_win_rate,
        min_expectancy=gates.min_oos_expectancy,
        breakeven_win_rate=breakeven_wr,
        min_samples=gates.min_oos_samples,
    )
    print(
        f"[expected-r][{tower}] OOS blind verdict: {blind_result['verdict']} "
        f"(rho={blind_result['spearman_rho']:.4f}, wr={blind_result['win_rate']:.4f}, "
        f"n={blind_result['n_active']})"
    )
    if blind_result["verdict"] == "FAIL":
        raise ModelQualityException(
            f"[expected-r][{tower}] Hard veto: OOS blind test FAILED for "
            f"{contract.contract_id}: "
            + "; ".join(blind_result["failures"])
            + " Tower must NOT enter the candidate pool."
        )
    return blind_result, breakeven_wr


# ═══════════════════════════════════════════════════════════════════════════════
# Brain config (Phase 5 lineage) + registry + auto-registration
# ═══════════════════════════════════════════════════════════════════════════════


def _register_tower(
    contract,
    tower: str,
    model_path: Path,
    model_hash: str,
    dataset_hash: str,
    val_metrics: dict[str, Any],
    blind_result: dict[str, Any] | None,
    breakeven_wr: float | None,
    live_yaml_path: str | Path | None,
    governance_path: str | Path | None,
    train_metrics: dict[str, Any],
) -> dict[str, Any]:
    from core.training.brain_config import (
        CONTRACT_GROUP_MAGIC,
        build_brain_config,
        resolve_feature_names_for_schema,
    )
    from core.training.model_hashing import hash_model_file
    from core.training.training_registry import TrainingRunRecord, create_registry

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    brain_id = contract.output.brain_id_template.format(tower=tower, timestamp=ts)

    features = resolve_feature_names_for_schema(contract.dataset.feature_schema)
    contract_group = "btc_expected_r_m15"
    magic = CONTRACT_GROUP_MAGIC.get(contract_group, 0)

    metrics: dict[str, Any] = {
        "spearman_rho": val_metrics.get("spearman_rho", 0.0),
        "r2": val_metrics.get("r2", 0.0),
        "sign_match": val_metrics.get("sign_match", 0.0),
        "mae": val_metrics.get("mae", 0.0),
        "n_val_samples": val_metrics.get("n", 0),
        "train_spearman_rho": train_metrics.get("spearman_rho", 0.0),
    }
    if blind_result is not None:
        metrics["oos_spearman_rho"] = blind_result.get("spearman_rho", 0.0)
        metrics["oos_win_rate"] = blind_result.get("win_rate", 0.0)
        metrics["oos_expectancy"] = blind_result.get("expectancy", 0.0)
        metrics["oos_blind_verdict"] = blind_result.get("verdict", "")
    if breakeven_wr is not None:
        metrics["breakeven_win_rate"] = breakeven_wr

    brain_config = build_brain_config(
        brain_id=brain_id,
        brain_type=BRAIN_TYPE[tower],
        feature_schema_id=contract.dataset.feature_schema,
        artifact_path=str(model_path),
        artifact_hash=model_hash,
        features=features or [],
        contract_id=contract.contract_id,
        contract_group=contract_group,
        label_horizon_bars=contract.label.horizon_bars,
        metrics=metrics,
        initial_status=contract.output.initial_status,
        brain_role="expected_r_tower",
        model_version=f"{contract.contract_id}_{tower.lower()}",
        dataset_hash=dataset_hash,
        label_contract_id=contract.label.contract_id,
        label_contract=label_contract_block(contract),
        extra={
            "strategy": "btc_expected_r",
            "timeframe": "M15",
            "activation_threshold": 0.15,
            "training_params": {
                "sl_atr_mult": contract.label.sl_atr_mult,
                "tp_atr_mult": contract.label.tp_atr_mult,
                "horizon": contract.label.horizon_bars,
                "n_estimators": contract.architecture.custom_params.get("n_estimators", 500),
                "learning_rate": contract.architecture.custom_params.get("learning_rate", 0.03),
                "max_depth": contract.architecture.custom_params.get("max_depth", 6),
                "num_leaves": contract.architecture.custom_params.get("num_leaves", 63),
                "objective": f"expected_r_{tower.lower()}",
                "overpred_penalty": contract.architecture.custom_params.get(
                    "overpred_penalty", 1.2
                ),
                "loss": "asymmetric_huber",
                "timeframe_minutes": 15,
                "n_features": len(features),
            },
            "deployment_scope": {
                "symbols": ["BTCUSDc"],
                "sessions": ["all"],
                "regimes": ["trend", "volatile_trend", "mean_reversion", "ranging"],
            },
        },
    )

    # ── Registration gate ──
    from core.deployment.brain_registration_gate import BrainRegistrationGate

    gate = BrainRegistrationGate(project_root=PROJECT_ROOT)
    gate_result = gate.validate(brain_config)
    if not gate_result.passed:
        print(f"[expected-r][{tower}] REJECTED {brain_id}:")
        for check, detail in gate_result.failures:
            print(f"  [FAIL] {check}: {detail}")
        raise RuntimeError(
            f"Registration gate rejected {brain_id}: {len(gate_result.failures)} check(s) failed"
        )

    config_dir = Path(contract.output.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{brain_id}.json"
    config_path.write_text(json.dumps(brain_config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[expected-r][{tower}] Brain config: {config_path}")

    # ── Registry ──
    try:
        registry = create_registry(contract.output.registry_db)
        record = TrainingRunRecord()
        record.contract_id = contract.contract_id
        record.timestamp = datetime.now(UTC)
        record.arch = "lightgbm"
        record.feature_schema = contract.dataset.feature_schema
        record.n_features = len(features)
        record.quality_gate_passed = True
        record.status = contract.output.initial_status
        record.model_path = str(model_path)
        record.model_hash = model_hash
        record.dataset_hash = dataset_hash
        record.label_contract_id = contract.label.contract_id
        record.trained_by_commit_hash = brain_config["trained_by_commit_hash"]
        record.oos_verdict = metrics.get("oos_blind_verdict")
        record.notes = (
            f"expected_r tower={tower} val_spearman={metrics['spearman_rho']:.4f} "
            f"r2={metrics['r2']:.4f} sign_match={metrics['sign_match']:.4f}"
        )
        registry.add_or_update(record)
        print(f"[expected-r][{tower}] Registered run: {record.run_id} (status={record.status})")
    except (OSError, ValueError) as e:
        print(f"[expected-r][{tower}] WARNING: Registry write failed (non-fatal): {e}")

    # ── Auto-register in live_btc.yaml + governance ──
    if contract.output.auto_register:
        _auto_register_in_live_yaml(brain_config, config_path, live_yaml_path)
        _auto_register_in_governance(brain_config, governance_path)

    return brain_config


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="train_btc_expected_r_institutional",
        description="BTC Expected-R two-tower institutional training (Phase 5 lineage)",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=PROJECT_ROOT / "configs/training/btc_expected_r_41d_v2_m15.yaml",
        help="Path to TrainingContract YAML",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=PROJECT_ROOT / "data_btc/training/btc_ssot_v2",
        help="Directory containing train.npz / val.npz / test.npz",
    )
    parser.add_argument("--n-seeds", type=int, default=3, help="Seeds per tower (default 3)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help="Decision gate E[R] threshold for the post-training report",
    )
    parser.add_argument(
        "--live-yaml",
        type=Path,
        default=None,
        help="Live config for auto-registration (default configs/live_btc.yaml)",
    )
    parser.add_argument(
        "--governance-path",
        type=Path,
        default=None,
        help="Governance state for auto-registration (default data_btc/governance_state.json)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="DEVELOPMENT ONLY: allow training with uncommitted source changes",
    )
    args = parser.parse_args(argv)

    live_yaml = args.live_yaml or PROJECT_ROOT / "configs/live_btc.yaml"
    governance = args.governance_path or PROJECT_ROOT / "data_btc/governance_state.json"

    _enforce_hash_lock(args.allow_dirty)

    if not args.contract.exists():
        print(f"[expected-r] ERROR: Contract not found: {args.contract}", file=sys.stderr)
        return 2

    from core.contracts.training.training_contract import TrainingContract
    from core.training.model_hashing import hash_file

    contract = TrainingContract.from_file(args.contract)
    print(f"[expected-r] Contract: {contract.contract_id}")

    train_path = args.dataset_dir / "train.npz"
    val_path = args.dataset_dir / "val.npz"
    test_path = args.dataset_dir / "test.npz"
    for p in (train_path, val_path, test_path):
        if not p.exists():
            print(f"[expected-r] ERROR: Missing dataset split: {p}", file=sys.stderr)
            return 2

    dataset_hash = hash_file(train_path)
    print(f"[expected-r] Dataset hash (train.npz): {dataset_hash}")

    train = _load_split(train_path)
    val = _load_split(val_path)
    test = _load_split(test_path)

    sample_weight = None
    if train["timestamps"] is not None:
        sample_weight = compute_time_decay_weights(train["timestamps"], half_life_days=180.0)
        print(
            f"[expected-r] Time-decay weights computed (half_life=180d, "
            f"min={sample_weight.min():.3f}, max={sample_weight.max():.3f})"
        )

    failures: list[str] = []
    for tower in TOWERS:
        print(f"\n{'=' * 72}\n[{tower}] Training tower...\n{'=' * 72}")
        models, metrics_list = train_tower_multi_seed(
            train["X"],
            train[Y_KEY[tower]],
            val["X"],
            val[Y_KEY[tower]],
            f"Tower_{tower}",
            n_seeds=args.n_seeds,
            sample_weight=sample_weight,
        )
        best_idx = int(np.argmax([m[1].get("spearman_rho", 0.0) for m in metrics_list]))
        best_model = models[best_idx]
        val_metrics = metrics_list[best_idx][1]
        print(
            f"[expected-r][{tower}] Best seed val: rho={val_metrics.get('spearman_rho', 0.0):.4f}, "
            f"r2={val_metrics.get('r2', 0.0):.4f}, sign_match={val_metrics.get('sign_match', 0.0):.4f}"
        )

        model_dir = Path(contract.output.model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / f"tower_{tower.lower()}_best.txt"
        best_model.save_model(str(model_path))
        model_hash = hash_file(model_path)
        print(f"[expected-r][{tower}] Model saved: {model_path} (hash={model_hash[:12]}...)")

        # Phase 3 gate
        try:
            blind_result, breakeven_wr = _evaluate_tower_gate(
                contract, model_path, test_path, tower
            )
        except ModelQualityException as exc:
            print(f"[expected-r][{tower}] {exc}")
            failures.append(f"{tower}: OOS blind gate FAILED")
            continue

        # Phase 5 registration
        _register_tower(
            contract,
            tower,
            model_path,
            model_hash,
            dataset_hash,
            val_metrics,
            blind_result,
            breakeven_wr,
            live_yaml,
            governance,
            metrics_list[best_idx][0],  # train metrics
        )

    if failures:
        print("\n[expected-r] === FAILED TOWERS (hard veto) ===")
        for f in failures:
            print(f"  [x] {f}")
        print(
            "[expected-r] At least one tower failed its Phase 3 gate - it did NOT enter "
            "the candidate pool.  Passing towers were registered with full lineage."
        )
        return 1

    print("\n[expected-r] [OK] Both towers passed Phase 3 gates - registered with full lineage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

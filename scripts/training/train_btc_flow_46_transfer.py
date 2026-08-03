"""train_btc_flow_46_transfer.py — OFI 46-dim residual transfer orchestrator.

M4 Phase 6.1 / IC 终局裁决 (2026-08-03).  Battle 6.1c — the orchestrator that
glues the three battles together and emits the two 46-dim shadow brains:

    dataset   build_btc_flow46_dataset.py   M5-aligned 46-dim NPZ + leak audit
    adapter   core/training/transfer_adapter.py
              frozen 41-dim base (y_A) + OFI residual (r); y = y_A + r
    gates     Phase 3 OOS blind (institutional ρ for the RESIDUAL layer) +
              breakeven physical-wear coverage
    lineage   build_brain_config + TrainingRunRecord + auto-register
              (live.yaml enabled=False — the base+residual runtime evaluator
              does not exist yet; shadow brains carry full lineage + governance
              candidate status but are NOT wired into the strategy line)

Per tower (LONG/SHORT) the pipeline is: load frozen base tower -> fit residual
on flow-only features (dead dims zero-padded, effective_flow_dim hard gate) ->
temporal OOS blind test on the freshest aligned rows -> on PASS register a
46-dim shadow brain with full Phase 5 lineage.

Usage:
  python scripts/training/train_btc_flow_46_transfer.py \
      --contract configs/training/btc_flow_46_transfer.yaml \
      --dataset data_btc/training/btc_flow46_v1/btc_flow46_aligned.npz \
      --allow-dirty
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.training.train import (  # noqa: E402  # registration helpers
    ModelQualityException,
    _auto_register_in_governance,
    _auto_register_in_live_yaml,
)
from scripts.training.train_btc_expected_r_institutional import (  # noqa: E402
    _enforce_hash_lock,
    label_contract_block,
)

TOWERS = ("LONG", "SHORT")
Y_KEY = {"LONG": "y_long", "SHORT": "y_short"}
BRAIN_TYPE = {"LONG": "expected_r_long", "SHORT": "expected_r_short"}
BRAIN_ROLE = "expected_r_tower"
CONTRACT_GROUP = "btc_expected_r_m15"  # magic 90452 — the line the flow serves


# ═══════════════════════════════════════════════════════════════════════════════
# Data — temporal split (residual OOS = the freshest aligned rows)
# ═══════════════════════════════════════════════════════════════════════════════


def temporal_split(
    timestamps: np.ndarray,
    val_ratio: float,
    test_ratio: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Chronological 3-way split of row indices — NO shuffle across time."""
    order = np.argsort(timestamps, kind="stable")
    n = int(len(order))
    n_val = int(round(n * val_ratio))
    n_test = int(round(n * test_ratio))
    n_train = n - n_val - n_test
    if n_train < 100:
        raise ValueError(f"temporal split leaves only {n_train} train rows")
    return order[:n_train], order[n_train : n_train + n_val], order[n_train + n_val :]


# ═══════════════════════════════════════════════════════════════════════════════
# One tower — fit residual + Phase 3 blind gate
# ═══════════════════════════════════════════════════════════════════════════════


def _run_residual_tower(
    contract,
    tower: str,
    X46: np.ndarray,
    y_long: np.ndarray,
    y_short: np.ndarray,
    ts: np.ndarray,
    tr_idx: np.ndarray,
    va_idx: np.ndarray,
    te_idx: np.ndarray,
    transfer_cfg: dict[str, Any],
) -> tuple[Any, dict[str, Any], dict[str, Any] | None, float | None]:
    """Train one tower's residual; returns (learner, train_meta, blind_result, breakeven_wr).

    Raises ModelQualityException (hard veto) on gate failure — the tower does
    not enter the candidate pool.
    """
    from core.training.breakeven import compute_breakeven_from_params
    from core.training.transfer_adapter import (
        FrozenBaseModel,
        ResidualTransferLearner,
    )
    from scripts.training.oos_blind_test import run_blind_test

    base_dir = Path(transfer_cfg["base_model_dir"])
    base_file = base_dir / transfer_cfg[f"base_model_{tower.lower()}"]
    base_id = transfer_cfg[f"base_brain_id_{tower.lower()}"]
    if not base_file.exists():
        raise ModelQualityException(f"[flow46][{tower}] frozen base not found: {base_file}")

    gates = contract.quality_gates
    label = contract.label
    flow_names = list(_flow_feature_names()[41:])

    learner = ResidualTransferLearner(
        FrozenBaseModel.from_file(base_file, base_id),
        flow_feature_names=flow_names,
        min_flow_dim=int(transfer_cfg.get("min_flow_dim", 2)),
    )

    y_key = Y_KEY[tower]
    y = np.asarray(y_long if tower == "LONG" else y_short, dtype=np.float64)

    params = dict(contract.architecture.custom_params)
    params.pop("objective", None)
    params.pop("n_estimators", None)
    meta = learner.fit(
        X46[tr_idx],
        y[tr_idx],
        X46[va_idx],
        y[va_idx],
        params=params,
        early_stopping_rounds=int(transfer_cfg.get("early_stopping_rounds", 50)),
    )
    print(
        f"[flow46][{tower}] residual fit: effective_flow_dim={meta['effective_flow_dim']}, "
        f"val combined rho={meta['val_combined_rho']:.4f}, "
        f"residual rho={meta['val_residual_rho']:.4f}"
    )

    # ── Phase 3 blind gate on the freshest aligned rows ──
    breakeven_wr: float | None = None
    if gates.enforce_breakeven:
        breakeven_wr = compute_breakeven_from_params(
            label.sl_atr_mult,
            label.tp_atr_mult,
            spread_points=label.spread_points,
            slippage_points=label.slippage_points,
            tick_size=label.tick_size,
            friction_model="expected_r",
        ).breakeven_win_rate
        print(f"[flow46][{tower}] OOS breakeven WR (physical friction): {breakeven_wr:.4f}")

    blind_result = run_blind_test(
        "",  # model passed directly below — path unused
        _write_split_npz(contract, tower, X46[te_idx], y_long[te_idx], y_short[te_idx], ts[te_idx]),
        y_key=y_key,
        min_rho=gates.min_oos_rho,
        min_win_rate=gates.min_oos_win_rate,
        min_expectancy=gates.min_oos_expectancy,
        breakeven_win_rate=breakeven_wr,
        min_samples=gates.min_oos_samples,
        model=learner,
    )
    print(
        f"[flow46][{tower}] OOS blind verdict: {blind_result['verdict']} "
        f"(rho={blind_result['spearman_rho']:.4f}, wr={blind_result['win_rate']:.4f}, "
        f"n={blind_result['n_active']})"
    )
    if blind_result["verdict"] == "FAIL":
        raise ModelQualityException(
            f"[flow46][{tower}] Hard veto: OOS blind FAILED for {contract.contract_id}: "
            + "; ".join(blind_result["failures"])
            + " Tower must NOT enter the candidate pool."
        )
    return learner, meta, blind_result, breakeven_wr


def _write_split_npz(
    contract, tower: str, X: np.ndarray, yl: np.ndarray, ys: np.ndarray, ts: np.ndarray
) -> Path:
    split_dir = Path(contract.output.model_dir).parent / "splits"
    split_dir.mkdir(parents=True, exist_ok=True)
    p = split_dir / f"residual_{tower.lower()}_test.npz"
    np.savez_compressed(
        p,
        X=X,
        y_long=yl,
        y_short=ys,
        timestamps=ts,
        feature_names=np.array(_flow_feature_names(), dtype=object),
        schema_id=np.array(["btc_macro_flow_46"], dtype=object),
    )
    return p


def _flow_feature_names() -> list[str]:
    from core.features.schemas.registry import get_schema_feature_names

    return list(get_schema_feature_names("btc_macro_flow_46"))


# ═══════════════════════════════════════════════════════════════════════════════
# Registration — 46-dim shadow brain + registry + auto-register (enabled=False)
# ═══════════════════════════════════════════════════════════════════════════════


def _register_flow46_tower(
    contract,
    tower: str,
    learner: Any,
    train_meta: dict[str, Any],
    blind_result: dict[str, Any],
    breakeven_wr: float | None,
    dataset_hash: str,
    transfer_cfg: dict[str, Any],
    live_yaml_path: Path,
    governance_path: Path,
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
    features = resolve_feature_names_for_schema("btc_macro_flow_46")
    magic = CONTRACT_GROUP_MAGIC.get(CONTRACT_GROUP, 0)

    residual_path = Path(contract.output.model_dir) / f"residual_{tower.lower()}_best.txt"
    learner.save_residual(residual_path)
    model_hash = hash_model_file(residual_path)

    base_id = transfer_cfg[f"base_brain_id_{tower.lower()}"]
    base_file = Path(transfer_cfg["base_model_dir"]) / transfer_cfg[f"base_model_{tower.lower()}"]
    base_hash = hash_model_file(base_file)

    metrics: dict[str, Any] = {
        "val_combined_rho": train_meta.get("val_combined_rho", 0.0),
        "val_residual_rho": train_meta.get("val_residual_rho", 0.0),
        "effective_flow_dim": train_meta.get("effective_flow_dim", 0),
        "oos_spearman_rho": blind_result.get("spearman_rho", 0.0),
        "oos_win_rate": blind_result.get("win_rate", 0.0),
        "oos_expectancy": blind_result.get("expectancy", 0.0),
        "oos_blind_verdict": blind_result.get("verdict", ""),
        "n_oos_active": blind_result.get("n_active", 0),
    }
    if breakeven_wr is not None:
        metrics["breakeven_win_rate"] = breakeven_wr

    brain_config = build_brain_config(
        brain_id=brain_id,
        brain_type=BRAIN_TYPE[tower],
        feature_schema_id="btc_macro_flow_46",
        artifact_path=str(residual_path),
        artifact_hash=model_hash,
        features=features,
        contract_id=contract.contract_id,
        contract_group=CONTRACT_GROUP,
        label_horizon_bars=contract.label.horizon_bars,
        metrics=metrics,
        initial_status=contract.output.initial_status,
        brain_role=BRAIN_ROLE,
        model_version=f"{contract.contract_id}_{tower.lower()}",
        dataset_hash=dataset_hash,
        label_contract_id=contract.label.contract_id,
        label_contract=label_contract_block(contract),
        extra={
            "strategy": "btc_expected_r",
            "timeframe": "M5",
            "transfer": {
                "kind": "freeze_and_residual",
                "frozen_base_brain_id": base_id,
                "frozen_base_artifact_hash": base_hash,
                "frozen_base_schema": "btc_macro_enhanced_41_v2",
                "residual_target": "y - y_A",
                "flow_features": transfer_cfg.get("flow_features", []),
                "live_flow_features": train_meta.get("live_flow_features", []),
                "flow_coverage": train_meta.get("flow_coverage", {}),
                "min_flow_dim": transfer_cfg.get("min_flow_dim", 2),
                "zero_pad_dead_dims": True,
                "deployment": {
                    "live_yaml_enabled": transfer_cfg.get("live_yaml_enabled", False),
                    "reason": (
                        "runtime base+residual evaluator not yet wired; "
                        "shadow brain carries lineage + governance candidate only"
                    ),
                },
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
        print(f"[flow46][{tower}] REJECTED {brain_id}:")
        for check, detail in gate_result.failures:
            print(f"  [FAIL] {check}: {detail}")
        raise RuntimeError(
            f"Registration gate rejected {brain_id}: {len(gate_result.failures)} check(s) failed"
        )

    config_dir = Path(contract.output.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{brain_id}.json"
    config_path.write_text(json.dumps(brain_config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[flow46][{tower}] Brain config: {config_path}")

    # ── Registry ──
    try:
        registry = create_registry(contract.output.registry_db)
        record = TrainingRunRecord()
        record.contract_id = contract.contract_id
        record.timestamp = datetime.now(UTC)
        record.arch = "lightgbm"
        record.feature_schema = "btc_macro_flow_46"
        record.n_features = len(features)
        record.quality_gate_passed = True
        record.status = contract.output.initial_status
        record.model_path = str(residual_path)
        record.model_hash = model_hash
        record.dataset_hash = dataset_hash
        record.label_contract_id = contract.label.contract_id
        record.trained_by_commit_hash = brain_config["trained_by_commit_hash"]
        record.oos_verdict = metrics.get("oos_blind_verdict")
        record.notes = (
            f"OFI residual transfer tower={tower} frozen_base={base_id} "
            f"eff_flow_dim={metrics['effective_flow_dim']} "
            f"combined_rho={metrics['val_combined_rho']:.4f} "
            f"oos_rho={metrics['oos_spearman_rho']:.4f}"
        )
        registry.add_or_update(record)
        print(f"[flow46][{tower}] Registered run: {record.run_id} (status={record.status})")
    except (OSError, ValueError) as e:
        print(f"[flow46][{tower}] WARNING: Registry write failed (non-fatal): {e}")

    # ── Auto-register: live.yaml enabled=False + governance candidate ──
    if contract.output.auto_register:
        _auto_register_in_live_yaml(
            brain_config,
            config_path,
            live_yaml_path,
            enabled=bool(transfer_cfg.get("live_yaml_enabled", False)),
        )
        _auto_register_in_governance(brain_config, governance_path)

    return brain_config


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="train_btc_flow_46_transfer",
        description="OFI 46-dim residual transfer (freeze base, fit OFI residual)",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=PROJECT_ROOT / "configs/training/btc_flow_46_transfer.yaml",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "data_btc/training/btc_flow46_v1/btc_flow46_aligned.npz",
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
        help="Governance state (default data_btc/governance_state.json)",
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

    if not args.contract.exists() or not args.dataset.exists():
        print(
            f"[flow46] ERROR: contract or dataset missing: {args.contract} / {args.dataset}",
            file=sys.stderr,
        )
        return 2

    from core.contracts.training.training_contract import TrainingContract
    from core.training.model_hashing import hash_file

    contract = TrainingContract.from_file(args.contract)
    print(f"[flow46] Contract: {contract.contract_id}")

    transfer_cfg = dict((contract.metadata or {}).get("transfer", {}))
    if not transfer_cfg:
        print("[flow46] ERROR: metadata.transfer missing in contract", file=sys.stderr)
        return 2

    dataset_hash = hash_file(args.dataset)
    print(f"[flow46] Dataset hash (btc_flow46_aligned.npz): {dataset_hash}")

    d = np.load(args.dataset, allow_pickle=True)
    X46 = np.asarray(d["X"], dtype=np.float64)
    y_long = np.asarray(d["y_long"], dtype=np.float64)
    y_short = np.asarray(d["y_short"], dtype=np.float64)
    ts = np.asarray(d["timestamps"], dtype=np.float64)
    if X46.shape[1] != 46:
        print(f"[flow46] ERROR: dataset is {X46.shape[1]}-dim, expected 46", file=sys.stderr)
        return 2
    print(f"[flow46] Aligned rows: {len(ts):,}")

    tr_idx, va_idx, te_idx = temporal_split(
        ts,
        contract.validation.val_ratio,
        contract.validation.test_ratio,
    )
    print(
        f"[flow46] Temporal split: train={len(tr_idx)} val={len(va_idx)} "
        f"test={len(te_idx)} (OOS = freshest rows)"
    )

    failures: list[str] = []
    for tower in TOWERS:
        print(f"\n{'=' * 72}\n[{tower}] Residual transfer tower...\n{'=' * 72}")
        try:
            learner, train_meta, blind_result, breakeven_wr = _run_residual_tower(
                contract,
                tower,
                X46,
                y_long,
                y_short,
                ts,
                tr_idx,
                va_idx,
                te_idx,
                transfer_cfg,
            )
        except ModelQualityException as exc:
            print(f"[flow46][{tower}] {exc}")
            failures.append(f"{tower}: OOS blind gate FAILED")
            continue

        if blind_result is None:
            raise RuntimeError(
                f"[flow46][{tower}] internal error: blind_result None after "
                "non-failing gate — run_blind_test must always return a verdict"
            )

        _register_flow46_tower(
            contract,
            tower,
            learner,
            train_meta,
            blind_result,
            breakeven_wr,
            dataset_hash,
            transfer_cfg,
            live_yaml,
            governance,
        )

    if failures:
        print("\n[flow46] === FAILED TOWERS (hard veto) ===")
        for f in failures:
            print(f"  [x] {f}")
        print(
            "[flow46] Passing towers were registered as 46-dim shadow brains "
            "(enabled=False).  Dry-run proves pipeline connectivity end-to-end."
        )
        return 1

    print(
        "\n[flow46] [OK] Both towers passed Phase 3 gates - 46-dim shadow brains "
        "registered with full lineage (enabled=False, governance candidate)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

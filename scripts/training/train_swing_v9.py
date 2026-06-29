#!/usr/bin/env python
"""Train XGBoost v9 swing brain from enhanced swing+micro dataset.

DEPRECATED: Use train.py with an appropriate training contract instead.
  python scripts/training/train.py --contract configs/training/m30_swing_xgboost.yaml

This script is kept because train_btc_directional_v10.py and
train_xau_directional_v1.py import it as a library for feature computation.

Usage:
  python scripts/training/train_swing_v9.py \
    --dataset data/training/swing_m30_enhanced \
    --strategy m30_swing \
    --output-dir data/models/swing

  python scripts/training/train_swing_v9.py \
    --dataset data/training/swing_m15_enhanced \
    --strategy m15_swing \
    --output-dir data/models/swing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def load_dataset(data_dir: Path) -> dict[str, Any]:
    """Load enhanced swing NPZ dataset."""
    train_path = data_dir / "train.npz"
    meta_path = data_dir / "meta.json"
    if not train_path.exists():
        raise FileNotFoundError(f"Train NPZ not found: {train_path}")

    data = np.load(train_path, allow_pickle=True)
    meta: dict[str, Any] = {}
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

    return {
        "X_train": data["X"].astype(np.float64),
        "y_train": data["y"].astype(np.int32),  # [0, 1, 2] for multi-class
        "pnl_r_train": data.get("pnl_r", np.zeros(len(data["y"]), dtype=np.float32)),
        "X_val": data["X_val"].astype(np.float64),
        "y_val": data["y_val"].astype(np.int32),
        "pnl_r_val": data.get("pnl_r_val", np.zeros(len(data["y_val"]), dtype=np.float32)),
        "X_test": data["X_test"].astype(np.float64),
        "y_test": data["y_test"].astype(np.int32),
        "pnl_r_test": data.get("pnl_r_test", np.zeros(len(data["y_test"]), dtype=np.float32)),
        "feature_names": data.get(
            "feature_names", np.array([f"f_{i}" for i in range(data["X"].shape[1])])
        ),
        "meta": meta,
    }


def compute_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute comprehensive metrics for multi-class swing predictions."""
    correct = y_pred == y_true
    wr = float(correct.mean())
    n = len(y_true)

    # Per-class accuracy
    classes = np.unique(y_true)
    per_class: dict[str, float] = {}
    for c in classes:
        mask = y_true == c
        if mask.sum() > 0:
            per_class[f"class_{c}_acc"] = float(correct[mask].mean())
            per_class[f"class_{c}_count"] = int(mask.sum())

    # Profit simulation: predict -1→SHORT, 1→LONG, 0→no trade
    # Map [0,1,2] back to [-1,0,1]
    y_true_dir = y_true - 1  # [0,1,2] → [-1,0,1]
    y_pred_dir = y_pred - 1

    # Only evaluate on directional trades
    trade_mask = y_pred_dir != 0
    if trade_mask.sum() > 0:
        trade_correct = y_pred_dir[trade_mask] == y_true_dir[trade_mask]
        trade_wr = float(trade_correct.mean())
        trade_count = int(trade_mask.sum())

        # Use actual barrier multipliers from dataset metadata (not hardcoded 1.5)
        # FIX-20260531-020: Use actual Triple Barrier PnL when available.
        # Strategy PnL = predicted_direction × actual_asset_return
        # e.g. predict SHORT(-1) × asset drops(-2%) = +2% profit
        if pnl is not None and len(pnl) > 0:
            _pnl_trade = pnl[trade_mask].astype(np.float64)
            _pred_dir_trade = y_pred_dir[trade_mask].astype(np.float64)
            sim_pnl = _pnl_trade * _pred_dir_trade
        else:
            _sl_mult = float(meta.get("sl_atr_mult", 1.5)) if meta else 1.5
            _tp_mult = float(meta.get("tp_atr_mult", 1.5)) if meta else 1.5
            sim_pnl = np.empty(len(trade_correct), dtype=np.float64)
            sim_pnl[:] = -_sl_mult  # Wrong direction = SL hit (loss)
            sim_pnl[trade_correct] = _tp_mult  # Correct direction = TP hit (profit)
        gross_profit = float(np.sum(sim_pnl[sim_pnl > 0]))
        gross_loss = float(abs(np.sum(sim_pnl[sim_pnl < 0])))
        profit_factor = gross_profit / max(gross_loss, 1e-12)

        cumsum = np.cumsum(sim_pnl)
        peak = np.maximum.accumulate(cumsum)
        max_dd = float(np.max(peak - cumsum))

        # FIX-20260531-020: Dynamic annualization — crypto 365d, forex 252d
        _strat = str(meta.get("strategy", "m30")).lower() if meta else "m30"
        _is_crypto = "btc" in _strat
        _trading_days = 365 if _is_crypto else 252
        _bars_per_day = 48  # default M30
        for _tf, _bpd in {"m15": 96, "m30": 48, "h1": 24, "h4": 6, "daily": 1}.items():
            if _tf in _strat:
                _bars_per_day = _bpd
                break
        _annual_factor = float(np.sqrt(_trading_days * _bars_per_day))
        _mean_val = float(np.mean(sim_pnl))
        _std_val = float(np.std(sim_pnl))
        sharpe = (_mean_val / max(_std_val, 1e-12)) * _annual_factor
    else:
        trade_wr = 0.0
        trade_count = 0
        profit_factor = 0.0
        max_dd = 0.0
        sharpe = 0.0

    return {
        "accuracy": round(wr, 4),
        "trade_win_rate": round(trade_wr, 4),
        "trade_count": trade_count,
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_annualized": round(sharpe, 4),
        "n_samples": n,
        "per_class": per_class,
    }


def train_xgboost_swing(
    dataset: dict[str, Any],
    *,
    n_estimators: int = 300,
    learning_rate: float = 0.05,
    max_depth: int = 5,
    subsample: float = 0.8,
    colsample_bytree: float = 0.8,
    min_child_weight: int = 5,
    reg_alpha: float = 0.1,
    reg_lambda: float = 0.1,
    early_stopping_rounds: int = 30,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train XGBoost multi-class classifier for swing trading."""
    import xgboost as xgb

    X_train = dataset["X_train"]
    y_train = dataset["y_train"]  # [0, 1, 2]
    X_val = dataset["X_val"]
    y_val = dataset["y_val"]

    # Class balancing weights
    class_counts = np.bincount(y_train, minlength=3)
    n = len(y_train)
    sample_weight = np.ones(n, dtype=np.float64)
    for c in range(3):
        if class_counts[c] > 0:
            sample_weight[y_train == c] = n / (3 * class_counts[c])

    # FIX-20260531-020: Return-magnitude weighting — large-move samples get
    # higher weight so the model focuses on high-impact trades.
    # clip(0.5, 5.0) defends against fat-tail overfit on a single extreme bar.
    _pnl_r_train = dataset.get("pnl_r_train")
    if _pnl_r_train is not None and len(_pnl_r_train) > 0:
        _pnl_abs = np.abs(np.asarray(_pnl_r_train, dtype=np.float64))
        _pnl_mean = float(_pnl_abs.mean())
        if _pnl_mean > 0:
            _pnl_weight = np.clip(_pnl_abs / _pnl_mean, 0.5, 5.0)
            sample_weight = sample_weight * _pnl_weight

    params: dict[str, Any] = {
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "max_depth": max_depth,
        "learning_rate": learning_rate,
        "subsample": subsample,
        "colsample_bytree": colsample_bytree,
        "min_child_weight": min_child_weight,
        "reg_alpha": reg_alpha,
        "reg_lambda": reg_lambda,
        "random_state": seed,
        "n_jobs": -1,
    }

    feature_names = dataset.get("feature_names")
    if isinstance(feature_names, np.ndarray):
        feature_names = feature_names.tolist()

    t0 = time.time()
    dtrain = xgb.DMatrix(
        X_train,
        label=y_train,
        weight=sample_weight,
        feature_names=feature_names,
    )
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=feature_names)

    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=n_estimators,
        evals=[(dtrain, "train"), (dval, "val")],
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=50,
    )

    train_time = time.time() - t0
    best_iter = getattr(booster, "best_iteration", booster.num_boosted_rounds())

    # Evaluate on val and test
    y_val_pred = np.asarray(booster.predict(dval)).argmax(axis=1)
    dtest = xgb.DMatrix(dataset["X_test"], label=dataset["y_test"], feature_names=feature_names)
    y_test_pred = np.asarray(booster.predict(dtest)).argmax(axis=1)

    val_metrics = compute_metrics(
        dataset["y_val"], y_val_pred, dataset["pnl_r_val"], meta=dataset.get("meta")
    )
    test_metrics = compute_metrics(
        dataset["y_test"], y_test_pred, dataset["pnl_r_test"], meta=dataset.get("meta")
    )

    return booster, {
        "best_iteration": int(best_iter),
        "train_time_seconds": round(train_time, 2),
        "params": {k: v for k, v in params.items() if k not in ("n_jobs", "random_state")},
        "val": val_metrics,
        "test": test_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train XGBoost swing brain")
    parser.add_argument("--dataset", required=True, help="Path to enhanced dataset directory")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=[
            "barrier_12bar",
            "btc_swing",
            "m15_swing",
            "m30_swing",
            "h1_swing",
            "h4_swing",
            "daily_swing",
        ],
        help="Strategy name for brain registration",
    )
    parser.add_argument(
        "--output-dir", default="data/models/swing", help="Output for model + config"
    )
    parser.add_argument("--n-estimators", type=int, default=300)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--brain-id",
        default=None,
        help="Override brain_id (default: auto-derived from strategy + version)",
    )
    parser.add_argument(
        "--no-register", action="store_true", help="Skip auto-registration after training"
    )
    args = parser.parse_args()

    data_dir = Path(args.dataset)
    print(f"=== Training {args.strategy} from {data_dir} ===")

    # Load
    dataset = load_dataset(data_dir)
    n_train = len(dataset["y_train"])
    n_val = len(dataset["y_val"])
    n_test = len(dataset["y_test"])
    n_features = dataset["X_train"].shape[1]
    print(f"  Samples: train={n_train}, val={n_val}, test={n_test}")
    print(f"  Features: {n_features}")
    label_dist = np.bincount(dataset["y_train"], minlength=3)
    print(f"  Train labels: [0]={label_dist[0]} [1]={label_dist[1]} [2]={label_dist[2]}")

    # Train
    model, metrics = train_xgboost_swing(
        dataset,
        n_estimators=args.n_estimators,
        learning_rate=args.lr,
        max_depth=args.max_depth,
        seed=args.seed,
    )

    print(f"\n  Best iteration: {metrics['best_iteration']}")
    print(f"  Train time: {metrics['train_time_seconds']}s")
    print("\n  Val metrics:")
    for k, v in metrics["val"].items():
        print(f"    {k}: {v}")
    print("\n  Test metrics:")
    for k, v in metrics["test"].items():
        print(f"    {k}: {v}")

    # Save model and brain config to separate files
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    project_root = (
        Path(__file__).resolve().parent.parent.parent
    )  # scripts/training/train_swing_v9.py → repo root

    # FIX-20260531-028: --brain-id override + auto-versioning.
    if args.brain_id:
        brain_id = args.brain_id
    elif args.strategy == "barrier_12bar":
        brain_id = "Barrier_V9_12B_V2"
    elif args.strategy == "btc_swing":
        # Auto-version: scan existing configs for latest V number
        _btc_brains = sorted((project_root / "configs" / "brains_btc").glob("BTC_Swing_V*.json"))
        _v = len(_btc_brains) + 1 if _btc_brains else 1
        brain_id = f"BTC_Swing_V{_v}"
    else:
        brain_id = f"Swing_V9_{args.strategy.split('_')[0].upper()}_V2"
    model_filename = f"{brain_id}_model.json"
    model_path = output_dir / model_filename
    model.save_model(str(model_path))
    print(f"\n  Model saved: {model_path}")

    # SHA256 artifact_hash for model integrity verification
    import hashlib

    artifact_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()

    # FIX-20260531-021: Embed real feature names into the model.
    # Without this, XGBoost serialises with f_0..f_n internal names,
    # causing BrainFactory feature-name mismatch at startup.
    _fn_list = dataset.get("feature_names")
    if _fn_list is not None and len(_fn_list) > 0:
        if isinstance(_fn_list, np.ndarray):
            _fn_list = _fn_list.tolist()
        if isinstance(_fn_list, list) and not str(_fn_list[0]).startswith("f_"):
            model.feature_names = _fn_list
            model.save_model(str(model_path))

    # Feature importance
    importance = model.get_score(importance_type="gain")
    feature_names = dataset["feature_names"]
    if isinstance(feature_names, np.ndarray):
        feature_names = feature_names.tolist()
    top_features = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:20]
    print("\n  Top 10 features by gain:")
    for i, (fname, gain) in enumerate(top_features[:10]):
        try:
            feat_name = (
                feature_names[int(fname.replace("f", ""))] if fname.startswith("f") else fname
            )
        except (ValueError, IndexError):
            feat_name = str(fname)
        print(f"    {i+1}. {feat_name}: {gain:.2f}")

    # Map strategy to magic number and training horizon
    _strategy_magic = {
        "barrier_12bar": 90001,
        "btc_swing": 90410,
        "m15_swing": 90310,
        "m30_swing": 90320,
        "h1_swing": 90330,
        "h4_swing": 90340,
        "daily_swing": 90301,
    }
    _strategy_horizon = {
        "barrier_12bar": 12,
        "btc_swing": 12,
        "m15_swing": 24,
        "m30_swing": 12,
        "h1_swing": 48,
        "h4_swing": 192,
        "daily_swing": 5,
    }
    magic = _strategy_magic.get(args.strategy, 0)
    horizon = dataset["meta"].get("horizon", _strategy_horizon.get(args.strategy, 12))

    # Generate brain config
    feature_schema_id = f"swing_enhanced_{n_features}"
    artifact_rel = (
        str(model_path.relative_to(project_root)) if model_path.is_absolute() else str(model_path)
    )
    brain_config = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": "xgboost_v9",
        "contract_group": args.strategy,
        "training_contract": args.strategy,
        "feature_schema": feature_schema_id,
        "feature_schema_id": feature_schema_id,
        "model_path": str(model_path),
        "artifact_path": artifact_rel,
        "model_version": brain_id.lower(),
        "artifact_hash": artifact_hash,
        "status": "candidate",
        "vote_weight": 1.0,
        "magic": magic,
        "brain_role": "alpha_brain",
        "training_horizon": horizon,
        "strategy": args.strategy,
        "timeframe": "M5"
        if args.strategy == "barrier_12bar"
        else args.strategy.split("_")[0].upper(),
        "training_params": {
            "sl_atr_mult": float(dataset["meta"].get("sl_atr_mult", 1.5)),
            "tp_atr_mult": float(dataset["meta"].get("tp_atr_mult", 1.5)),
            "horizon": dataset["meta"].get("horizon", 12),
            "n_estimators": args.n_estimators,
            "learning_rate": args.lr,
            "max_depth": args.max_depth,
        },
        "training_metrics": {
            "train_accuracy": metrics["val"]["accuracy"],
            "val_accuracy": metrics["val"]["accuracy"],
            "test_accuracy": metrics["test"]["accuracy"],
            "test_trade_win_rate": metrics["test"]["trade_win_rate"],
            "test_profit_factor": metrics["test"]["profit_factor"],
            "test_sharpe": metrics["test"]["sharpe_annualized"],
        },
        "features": dataset["feature_names"]
        if isinstance(dataset["feature_names"], list)
        else dataset["feature_names"].tolist(),
        "n_features": n_features,
        "n_train_samples": n_train,
        "trained_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "governance": {
            "min_confidence": 0.45,
            "cooldown_cycles": 3,
        },
    }

    _brains_subdir = "brains_btc" if args.strategy == "btc_swing" else "brains"
    config_path = project_root / "configs" / _brains_subdir / f"{brain_id}.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(brain_config, f, indent=2, ensure_ascii=False)
    print(f"  Config saved: {config_path}")

    print(f"\n  === Training complete: {brain_id} ===")
    print(f"  Test WR: {metrics['test']['trade_win_rate']:.1%}")
    print(f"  Test PF: {metrics['test']['profit_factor']:.2f}")
    print(f"  Test Sharpe: {metrics['test']['sharpe_annualized']:.2f}")
    # ── FIX-20260531-023: Auto-register after training ──
    if not args.no_register:
        import subprocess as _sp

        _reg_status = "shadow"
        print(f"\n  Auto-registering {brain_id} as {_reg_status}...")
        _reg_result = _sp.run(
            [
                sys.executable,
                str(project_root / "scripts" / "brain.py"),
                "register",
                str(config_path),
                "--status",
                _reg_status,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )
        if _reg_result.returncode == 0:
            print(f"  [OK] {brain_id} auto-registered as '{_reg_status}'")
        else:
            print(f"  [WARN] Auto-register failed: {_reg_result.stderr[:200]}")
            print(f"  Manual: python scripts/brain.py register {config_path} --status shadow")


if __name__ == "__main__":
    main()

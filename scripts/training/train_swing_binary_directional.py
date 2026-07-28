#!/usr/bin/env python
"""Train XGBoost binary directional swing brain.

Filters out NEUTRAL (class 0) samples and trains a binary classifier
(LONG vs SHORT). The binary approach avoids the "always predict NEUTRAL"
degeneracy that plagues 3-class swing models.

Usage:
  python scripts/training/train_swing_binary_directional.py \
    --dataset data/training/swing_m30_enhanced_sl2_tp3 \
    --strategy m30_swing \
    --output-dir data/models/swing \
    --brain-id Swing_V9_M30_V3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def load_and_filter_dataset(data_dir: Path) -> dict[str, Any]:
    """Load 3-class NPZ dataset, filter NEUTRAL, remap to binary (0=LONG, 1=SHORT)."""
    train_path = data_dir / "train.npz"
    meta_path = data_dir / "meta.json"
    if not train_path.exists():
        raise FileNotFoundError(f"Train NPZ not found: {train_path}")

    data = np.load(train_path, allow_pickle=True)
    meta: dict[str, Any] = {}
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

    def _filter_binary(X_arr, y_arr, pnl_arr=None):
        """Keep only directional samples (class 1=LONG, 2=SHORT), remap to 0/1."""
        mask = y_arr != 0  # exclude NEUTRAL
        X_f = X_arr[mask].astype(np.float64)
        y_f = y_arr[mask].astype(np.int32)
        # Remap: 1 (LONG) → 0, 2 (SHORT) → 1
        y_f = np.where(y_f == 2, 1, 0)
        if pnl_arr is not None and len(pnl_arr) > 0:
            pnl_f = pnl_arr[mask].astype(np.float64)
        else:
            pnl_f = np.zeros(len(y_f), dtype=np.float64)
        return X_f, y_f, pnl_f

    X_train, y_train, pnl_train = _filter_binary(data["X"], data["y"], data.get("pnl_r"))
    X_val, y_val, pnl_val = _filter_binary(data["X_val"], data["y_val"], data.get("pnl_r_val"))
    X_test, y_test, pnl_test = _filter_binary(
        data["X_test"], data["y_test"], data.get("pnl_r_test")
    )

    feature_names = data.get(
        "feature_names",
        np.array([f"f_{i}" for i in range(data["X"].shape[1])]),
    )

    return {
        "X_train": X_train,
        "y_train": y_train,
        "pnl_r_train": pnl_train,
        "X_val": X_val,
        "y_val": y_val,
        "pnl_r_val": pnl_val,
        "X_test": X_test,
        "y_test": y_test,
        "pnl_r_test": pnl_test,
        "feature_names": feature_names,
        "meta": meta,
    }


def compute_binary_metrics(
    y_true: np.ndarray,
    y_pred_prob: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    meta: dict[str, Any] | None = None,
    confidence_threshold: float = 0.5,
) -> dict[str, Any]:
    """Compute binary directional metrics.

    y_true: 0=LONG, 1=SHORT
    y_pred_prob: probability of SHORT (class 1)

    Trade direction:
      prob < (1 - threshold)  → LONG
      prob > threshold         → SHORT
      otherwise                → no trade (uncertainty zone)
    """
    n = len(y_true)
    low = 1.0 - confidence_threshold
    high = confidence_threshold

    pred_long = y_pred_prob < low
    pred_short = y_pred_prob > high
    pred_neutral = ~pred_long & ~pred_short

    # Map predictions: LONG→0, SHORT→1, NEUTRAL→no trade
    y_pred_class = np.full(n, -1, dtype=np.int32)
    y_pred_class[pred_long] = 0
    y_pred_class[pred_short] = 1

    # Per-class accuracy (on predicted samples only)
    long_mask = pred_long
    short_mask = pred_short
    long_acc = float((y_true[long_mask] == 0).mean()) if long_mask.sum() > 0 else 0.0
    short_acc = float((y_true[short_mask] == 1).mean()) if short_mask.sum() > 0 else 0.0

    # Trade-level metrics
    trade_mask = pred_long | pred_short
    if trade_mask.sum() > 0:
        trade_correct = y_pred_class[trade_mask] == y_true[trade_mask]
        trade_wr = float(trade_correct.mean())
        trade_count = int(trade_mask.sum())

        # PnL simulation: use SL/TP multipliers (matching 3-class fallback).
        # pnl_r in the dataset is synthetic encoding (NEUTRAL=-SL, LONG=0, SHORT=+TP)
        # and does NOT represent actual returns — so we always use the SL/TP method.
        _sl_mult = float(meta.get("sl_atr_mult", 1.5)) if meta else 1.5
        _tp_mult = float(meta.get("tp_atr_mult", 1.5)) if meta else 1.5
        sim_pnl = np.where(trade_correct, _tp_mult, -_sl_mult).astype(np.float64)

        gross_profit = float(np.sum(sim_pnl[sim_pnl > 0]))
        gross_loss = float(abs(np.sum(sim_pnl[sim_pnl < 0])))
        profit_factor = gross_profit / max(gross_loss, 1e-12)

        cumsum = np.cumsum(sim_pnl)
        peak = np.maximum.accumulate(cumsum)
        max_dd = float(np.max(peak - cumsum))

        # Sharpe
        _strat = str(meta.get("strategy", "m30")).lower() if meta else "m30"
        _is_crypto = "btc" in _strat
        _trading_days = 365 if _is_crypto else 252
        _bars_per_day = 48
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
        "accuracy": float((y_pred_class[trade_mask] == y_true[trade_mask]).mean())
        if trade_mask.sum() > 0
        else 0.0,
        "trade_win_rate": round(trade_wr, 4),
        "trade_count": trade_count,
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_annualized": round(sharpe, 4),
        "n_samples": n,
        "n_directional": int(trade_mask.sum()),
        "n_neutral_predicted": int(pred_neutral.sum()),
        "long_accuracy": round(long_acc, 4),
        "short_accuracy": round(short_acc, 4),
        "long_pred_count": int(long_mask.sum()),
        "short_pred_count": int(short_mask.sum()),
    }


def train_binary_xgboost(
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
    """Train XGBoost binary classifier for directional swing."""
    import xgboost as xgb

    X_train = dataset["X_train"]
    y_train = dataset["y_train"]
    X_val = dataset["X_val"]
    y_val = dataset["y_val"]

    # Class balancing weights
    class_counts = np.bincount(y_train, minlength=2)
    n = len(y_train)
    sample_weight = np.ones(n, dtype=np.float64)
    for c in range(2):
        if class_counts[c] > 0:
            sample_weight[y_train == c] = n / (2 * class_counts[c])

    # Return-magnitude weighting
    _pnl_r_train = dataset.get("pnl_r_train")
    if _pnl_r_train is not None and len(_pnl_r_train) > 0:
        _pnl_abs = np.abs(np.asarray(_pnl_r_train, dtype=np.float64))
        _pnl_mean = float(_pnl_abs.mean())
        if _pnl_mean > 0:
            _pnl_weight = np.clip(_pnl_abs / _pnl_mean, 0.5, 5.0)
            sample_weight = sample_weight * _pnl_weight

    params: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
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
    dtrain = xgb.DMatrix(X_train, label=y_train, weight=sample_weight, feature_names=feature_names)
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

    # Evaluate
    y_val_prob = booster.predict(dval)
    dtest = xgb.DMatrix(dataset["X_test"], label=dataset["y_test"], feature_names=feature_names)
    y_test_prob = booster.predict(dtest)

    val_metrics = compute_binary_metrics(
        dataset["y_val"], y_val_prob, dataset["pnl_r_val"], meta=dataset.get("meta")
    )
    test_metrics = compute_binary_metrics(
        dataset["y_test"], y_test_prob, dataset["pnl_r_test"], meta=dataset.get("meta")
    )

    return booster, {
        "best_iteration": int(best_iter),
        "train_time_seconds": round(train_time, 2),
        "params": {k: v for k, v in params.items() if k not in ("n_jobs", "random_state")},
        "val": val_metrics,
        "test": test_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train binary directional XGBoost swing brain")
    parser.add_argument(
        "--dataset", required=True, help="Path to 3-class enhanced dataset directory"
    )
    parser.add_argument("--strategy", required=True, help="Strategy name for brain registration")
    parser.add_argument(
        "--output-dir", default="data/models/swing", help="Output for model + config"
    )
    parser.add_argument("--brain-id", required=True, help="Brain ID (e.g. Swing_V9_M30_V3)")
    parser.add_argument("--n-estimators", type=int, default=300)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-register", action="store_true", help="Skip auto-registration")
    args = parser.parse_args()

    data_dir = Path(args.dataset)
    print(f"=== Binary Directional Training: {args.brain_id} ===")
    print(f"  Dataset: {data_dir}")

    # Load and filter
    dataset = load_and_filter_dataset(data_dir)
    n_train = len(dataset["y_train"])
    n_val = len(dataset["y_val"])
    n_test = len(dataset["y_test"])
    n_features = dataset["X_train"].shape[1]

    # Count class distribution
    train_long = int((dataset["y_train"] == 0).sum())
    train_short = int((dataset["y_train"] == 1).sum())

    print(f"  Samples (directional only): train={n_train}, val={n_val}, test={n_test}")
    print(f"  Features: {n_features}")
    print(
        f"  Train labels: LONG={train_long} ({train_long/n_train*100:.1f}%), "
        f"SHORT={train_short} ({train_short/n_train*100:.1f}%)"
    )

    # Train
    model, metrics = train_binary_xgboost(
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

    # Save model
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parent.parent.parent

    brain_id = args.brain_id
    model_filename = f"{brain_id}_model.json"
    model_path = output_dir / model_filename
    model.save_model(str(model_path))
    print(f"\n  Model saved: {model_path}")

    artifact_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()

    # Feature names
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

    # Strategy metadata
    _strategy_magic = {
        "m15_swing": 90310,
        "m30_swing": 90320,
        "h1_swing": 90330,
        "h4_swing": 90340,
        "daily_swing": 90301,
    }
    _strategy_horizon = {
        "m15_swing": 24,
        "m30_swing": 12,
        "h1_swing": 48,
        "h4_swing": 192,
        "daily_swing": 5,
    }
    magic = _strategy_magic.get(args.strategy, 0)
    horizon = dataset["meta"].get("horizon", _strategy_horizon.get(args.strategy, 12))

    # Brain config
    feature_schema_id = f"swing_enhanced_{n_features}"
    brain_config = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": "xgboost_binary_directional",
        "contract_group": args.strategy,
        "training_contract": args.strategy,
        "feature_schema": feature_schema_id,
        "feature_schema_id": feature_schema_id,
        "model_path": str(model_path),
        "artifact_path": str(model_path.relative_to(project_root))
        if model_path.is_absolute()
        else str(model_path),
        "model_version": brain_id.lower(),
        "artifact_hash": artifact_hash,
        "status": "candidate",
        "vote_weight": 1.0,
        "magic": magic,
        "brain_role": "alpha_brain",
        "training_horizon": horizon,
        "strategy": args.strategy,
        "timeframe": args.strategy.split("_")[0].upper(),
        "training_params": {
            "sl_atr_mult": float(dataset["meta"].get("sl_atr_mult", 1.5)),
            "tp_atr_mult": float(dataset["meta"].get("tp_atr_mult", 1.5)),
            "horizon": dataset["meta"].get("horizon", 12),
            "mode": "binary_directional",
            "n_estimators": args.n_estimators,
            "learning_rate": args.lr,
            "max_depth": args.max_depth,
        },
        "training_metrics": {
            "val_accuracy": metrics["val"]["accuracy"],
            "val_trade_win_rate": metrics["val"]["trade_win_rate"],
            "val_profit_factor": metrics["val"]["profit_factor"],
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

    config_path = project_root / "configs" / "brains" / f"{brain_id}.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(brain_config, f, indent=2, ensure_ascii=False)
    print(f"  Config saved: {config_path}")

    print(f"\n  === Training complete: {brain_id} ===")
    print(f"  Test WR: {metrics['test']['trade_win_rate']:.1%}")
    print(f"  Test PF: {metrics['test']['profit_factor']:.2f}")
    print(f"  Test Sharpe: {metrics['test']['sharpe_annualized']:.2f}")
    print(
        f"  LONG acc: {metrics['test']['long_accuracy']:.1%}  SHORT acc: {metrics['test']['short_accuracy']:.1%}"
    )

    # Auto-register
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

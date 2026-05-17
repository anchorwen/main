"""Train Stage 2 LightGBM binary classifier on PiT meta-features.

Companion to train_stage2_mlp_pit.py — same data, same split, different model.
Used in ensemble with MLP via MetaSignalFilter's weighted-average pipeline.

Usage:
    python scripts/training/train_stage2_lgb_pit.py \
        --data data/training/meta_features_pit_v3.npz \
        --output data/models/institutional/meta_stage2_lgb_pit_v3.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np


def _compute_sharpe(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    direction = 2 * y_true - 1
    confidence = 2 * np.abs(y_prob - 0.5)
    returns = direction * confidence
    if returns.std() < 1e-10:
        return 0.0
    return float(np.sqrt(252 * 24) * returns.mean() / returns.std())


def _compute_win_rate(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    pred_dir = (y_prob >= 0.5).astype(int)
    return float((pred_dir == y_true).sum() / len(y_true))


def train_lgb_stage2(
    data_path: str | Path,
    output_path: str | Path,
    *,
    n_estimators: int = 5000,
    learning_rate: float = 0.01,
    num_leaves: int = 31,
    min_child_samples: int = 20,
    subsample: float = 0.8,
    colsample_bytree: float = 0.8,
    reg_alpha: float = 0.1,
    reg_lambda: float = 0.1,
    early_stopping_rounds: int = 50,
    validation_split: float = 0.15,
    seed: int = 42,
) -> None:
    data_path = Path(data_path)
    output_path = Path(output_path)

    if not data_path.exists():
        print(f"[lgb_train] ERROR: Data not found: {data_path}")
        sys.exit(1)

    # ── Load data ──
    print(f"[lgb_train] Loading: {data_path}")
    raw = np.load(data_path, allow_pickle=True)
    X = np.asarray(raw["X"], dtype=np.float64)
    y = np.asarray(raw["y"], dtype=np.int32).ravel()
    feature_names = list(raw.get("feature_names", [f"f_{i}" for i in range(X.shape[1])]))

    print(f"[lgb_train] X: {X.shape}, y: {y.shape}")
    print(f"[lgb_train] Label distribution: {np.sum(y == 1)} TP, {np.sum(y == 0)} non-TP")

    # ── Chronological split ──
    n_val = int(len(X) * validation_split)
    if n_val < 100:
        print(f"[lgb_train] ERROR: Validation set too small ({n_val})")
        sys.exit(1)

    X_train, X_val = X[:-n_val], X[-n_val:]
    y_train, y_val = y[:-n_val], y[-n_val:]
    print(f"[lgb_train] Train: {len(X_train)}, Val: {len(X_val)}")

    # ── Build datasets ──
    dtrain = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
    dval = lgb.Dataset(X_val, label=y_val, reference=dtrain, feature_name=feature_names)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": "gbdt",
        "num_leaves": num_leaves,
        "min_child_samples": min_child_samples,
        "learning_rate": learning_rate,
        "subsample": subsample,
        "subsample_freq": 1,
        "colsample_bytree": colsample_bytree,
        "reg_alpha": reg_alpha,
        "reg_lambda": reg_lambda,
        "seed": seed,
        "feature_pre_filter": False,
        "verbosity": -1,
    }

    print(f"[lgb_train] Training {n_estimators} rounds (early_stopping={early_stopping_rounds})")

    booster = lgb.train(
        params,
        dtrain,
        num_boost_round=n_estimators,
        valid_sets=[dtrain, dval],
        valid_names=["train", "val"],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(20)],
    )

    best_iter = booster.best_iteration
    print(f"[lgb_train] Best iteration: {best_iter}")

    # ── Evaluate ──
    val_probs = booster.predict(X_val)
    val_sharpe = _compute_sharpe(y_val, val_probs)
    val_win_rate = _compute_win_rate(y_val, val_probs)

    print(f"[lgb_train] Val Sharpe (12-bar): {val_sharpe:.4f}")
    print(f"[lgb_train] Val Win Rate: {val_win_rate:.4f}")
    print(
        f"[lgb_train] Val probs: mean={float(np.mean(val_probs)):.4f}, "
        f"std={float(np.std(val_probs)):.4f}, "
        f"range=[{float(np.min(val_probs)):.4f}, {float(np.max(val_probs)):.4f}]"
    )

    # ── Save ──
    output_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(output_path))
    print(f"[lgb_train] Saved model to: {output_path}")

    meta = {
        "n_features": X_train.shape[1],
        "feature_names": feature_names,
        "val_sharpe_12bar": val_sharpe,
        "val_win_rate": val_win_rate,
        "best_iteration": best_iter,
        "training_data": str(data_path),
        "n_train": len(X_train),
        "n_val": len(X_val),
        "params": params,
    }
    meta_path = str(output_path).rsplit(".", 1)[0] + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[lgb_train] Saved metadata to: {meta_path}")
    print("[lgb_train] Done.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train Stage 2 LGB on PiT meta-features")
    ap.add_argument("--data", required=True, help="Path to PiT meta-features NPZ")
    ap.add_argument("--output", required=True, help="Output model path (.txt)")
    ap.add_argument("--n-estimators", type=int, default=5000)
    ap.add_argument("--lr", type=float, default=0.01, dest="learning_rate")
    ap.add_argument("--num-leaves", type=int, default=31)
    ap.add_argument("--min-child-samples", type=int, default=20)
    ap.add_argument("--subsample", type=float, default=0.8)
    ap.add_argument("--colsample-bytree", type=float, default=0.8)
    ap.add_argument("--reg-alpha", type=float, default=0.1)
    ap.add_argument("--reg-lambda", type=float, default=0.1)
    ap.add_argument("--early-stopping", type=int, default=50, dest="early_stopping_rounds")
    ap.add_argument("--validation-split", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    train_lgb_stage2(
        data_path=args.data,
        output_path=args.output,
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        min_child_samples=args.min_child_samples,
        subsample=args.subsample,
        colsample_bytree=args.colsample_bytree,
        reg_alpha=args.reg_alpha,
        reg_lambda=args.reg_lambda,
        early_stopping_rounds=args.early_stopping_rounds,
        validation_split=args.validation_split,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

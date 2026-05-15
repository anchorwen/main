#!/usr/bin/env python
"""Train a LightGBM meta-model for Stage 2 signal filtering.

The meta model predicts P(TP hit | Stage 1 signal, context features).
Signals with P(win) below threshold are filtered out.

Usage:
  python scripts/training/train_meta_model.py \
    --dataset data/training/meta_12bar_v1/train.npz \
    --output data/models/meta_filter_12bar_v1
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# ── Constants ────────────────────────────────────────────────────────────────

MIN_SPLITS_FOR_EVAL = 100
DEFAULT_N_TRIALS = 50
DEFAULT_N_SEEDS = 5
DEFAULT_EARLY_STOPPING = 30


# ── Evaluation ───────────────────────────────────────────────────────────────


def evaluate_threshold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    thresholds: np.ndarray | None = None,
    *,
    sl_atr: float = 3.0,
    tp_atr: float = 1.5,
) -> dict[str, float]:
    """Evaluate filtering performance across probability thresholds.

    Uses PnL-weighted evaluation: SL loss = -sl_atr, TP gain = +tp_atr.
    Net improvement measures total expected PnL change from filtering.

    Returns best_threshold and metrics at that threshold.
    """
    if thresholds is None:
        thresholds = np.linspace(0.1, 0.9, 41)

    n_total = len(y_true)
    base_win_rate = float(np.mean(y_true))
    # Expected PnL per trade (unfiltered): win_rate * tp_atr - (1-win_rate) * sl_atr
    base_expected_pnl = base_win_rate * tp_atr - (1.0 - base_win_rate) * sl_atr

    best_net_pnl_improvement = -999.0
    best_metrics: dict[str, float] = {}

    for thresh in thresholds:
        mask = y_prob >= thresh
        n_kept = int(np.sum(mask))
        if n_kept < MIN_SPLITS_FOR_EVAL:
            continue

        kept_win_rate = float(np.mean(y_true[mask]))
        signal_retention = n_kept / n_total

        # PnL-weighted: expected PnL of filtered trades
        kept_expected_pnl = kept_win_rate * tp_atr - (1.0 - kept_win_rate) * sl_atr
        # Net improvement = (filtered_pnl - base_pnl) * signal_retention
        net_improvement = (kept_expected_pnl - base_expected_pnl) * signal_retention

        if net_improvement > best_net_pnl_improvement:
            best_net_pnl_improvement = net_improvement
            best_metrics = {
                "threshold": thresh,
                "signal_retention": round(signal_retention, 4),
                "kept_win_rate": round(float(kept_win_rate), 4),
                "base_win_rate": round(float(base_win_rate), 4),
                "precision_improvement": round(float(kept_win_rate - base_win_rate), 4),
                "kept_expected_pnl": round(float(kept_expected_pnl), 4),
                "base_expected_pnl": round(float(base_expected_pnl), 4),
                "net_improvement": round(float(net_improvement), 6),
                "n_kept": n_kept,
                "n_total": n_total,
            }

    return best_metrics


# ── Training ─────────────────────────────────────────────────────────────────


def train_meta_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    sample_weight_train: np.ndarray | None = None,
    sample_weight_val: np.ndarray | None = None,
    n_trials: int = DEFAULT_N_TRIALS,
    n_seeds: int = DEFAULT_N_SEEDS,
    early_stopping: int = DEFAULT_EARLY_STOPPING,
) -> tuple[Any, dict[str, float], float]:
    """Train LightGBM meta model with optuna hyperparameter search.

    Asymmetric sample weights (SL=2.0, TP=1.0) force the model to
    prioritize avoiding catastrophic SL losses over picking extra TP wins.

    Returns (model, best_metrics, best_threshold).
    """
    import lightgbm as lgb

    best_pnl_improvement = -999.0
    best_model = None
    best_seed_metrics: dict[str, float] = {}
    best_threshold = 0.5

    for seed in range(n_seeds):
        # Fixed params that work well for meta-labeling
        params: dict[str, object] = {
            "objective": "binary",
            "metric": "binary_logloss",
            "boosting": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.03,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_data_in_leaf": 50,
            "lambda_l1": 0.1,
            "lambda_l2": 0.5,
            "verbose": -1,
            "random_state": seed,
            "num_threads": 4,
        }

        if n_trials > 0:
            try:
                import optuna
                from optuna.samplers import TPESampler

                def objective(trial, base_params=params):
                    p = dict(base_params)
                    p.update(
                        {
                            "num_leaves": trial.suggest_int("num_leaves", 7, 63),
                            "learning_rate": trial.suggest_float(
                                "learning_rate", 0.01, 0.1, log=True
                            ),
                            "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
                            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
                            "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 10, 200),
                            "lambda_l1": trial.suggest_float("lambda_l1", 0.0, 2.0),
                            "lambda_l2": trial.suggest_float("lambda_l2", 0.0, 5.0),
                        }
                    )
                    dtrain = lgb.Dataset(X_train, label=y_train, weight=sample_weight_train)
                    dval = lgb.Dataset(
                        X_val, label=y_val, weight=sample_weight_val, reference=dtrain
                    )
                    booster = lgb.train(
                        p,
                        dtrain,
                        valid_sets=[dval],
                        callbacks=[lgb.early_stopping(early_stopping), lgb.log_evaluation(0)],
                        num_boost_round=500,
                    )
                    y_prob = booster.predict(X_val)
                    metrics = evaluate_threshold(y_val, y_prob)
                    return metrics.get("net_improvement", 0.0)

                study = optuna.create_study(
                    direction="maximize",
                    sampler=TPESampler(seed=seed),
                )
                study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
                params.update(study.best_params)
                params["random_state"] = seed
            except ImportError:
                pass  # optuna not available, use defaults

        dtrain = lgb.Dataset(X_train, label=y_train, weight=sample_weight_train)
        dval = lgb.Dataset(X_val, label=y_val, weight=sample_weight_val, reference=dtrain)
        model = lgb.train(
            params,
            dtrain,
            valid_sets=[dval],
            callbacks=[lgb.early_stopping(early_stopping), lgb.log_evaluation(0)],
            num_boost_round=500,
        )

        y_prob = model.predict(X_val)
        metrics = evaluate_threshold(y_val, y_prob)

        net_imp = metrics.get("net_improvement", 0.0)
        if net_imp > best_pnl_improvement:
            best_pnl_improvement = net_imp
            best_model = model
            best_seed_metrics = metrics
            best_threshold = metrics.get("threshold", 0.5)

        print(
            f"  seed={seed}: net_pnl_improvement={net_imp:.6f}, "
            f"precision_imp={metrics.get('precision_improvement', 0):+.4f}, "
            f"threshold={metrics.get('threshold', 'N/A')}"
        )

    return best_model, best_seed_metrics, best_threshold


# ── Feature importance ───────────────────────────────────────────────────────


def compute_shap_importance(model, X_val: np.ndarray, feature_names: list[str]) -> dict[str, float]:
    """Compute SHAP-based feature importance for the meta model."""
    try:
        import shap

        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(X_val[:1000])  # sample for speed
        if isinstance(shap_values, list):
            shap_values = shap_values[1]  # class 1 values
        importance = np.abs(shap_values).mean(axis=0)
        return {
            name: round(float(imp), 6) for name, imp in zip(feature_names, importance, strict=False)
        }
    except ImportError:
        # Fallback: gain-based importance
        gain = model.feature_importance(importance_type="gain")
        total = gain.sum() or 1.0
        return {
            name: round(float(g) / float(total), 6)
            for name, g in zip(feature_names, gain, strict=False)
        }


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="train_meta_model")
    p.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to meta dataset .npz (from build_meta_labels.py)",
    )
    p.add_argument(
        "--output", type=Path, required=True, help="Output directory for model and metadata"
    )
    p.add_argument(
        "--optuna-trials",
        type=int,
        default=DEFAULT_N_TRIALS,
        help="Number of optuna trials (0 = skip)",
    )
    p.add_argument("--n-seeds", type=int, default=DEFAULT_N_SEEDS)
    p.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override probability threshold (default: auto-select)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    ds_path = Path(args.dataset)
    if not ds_path.exists():
        print(f"[ERROR] Dataset not found: {ds_path}")
        return 1

    print(f"[1/4] Loading meta dataset: {ds_path}")
    data = np.load(ds_path)
    X_train = data["X_train"]
    y_train = data["y_train"]
    X_val = data["X_val"]
    y_val = data["y_val"]
    w_train = data.get("w_train")
    w_val = data.get("w_val")
    feature_names = [str(n) for n in data["feature_names"]]

    base_train_wr = float(np.mean(y_train))
    base_val_wr = float(np.mean(y_val))
    print(
        f"       Train: {len(X_train)} samples ({base_train_wr:.1%} wins, "
        f"avg_weight={w_train.mean():.3f})"
        if w_train is not None
        else f"       Train: {len(X_train)} samples ({base_train_wr:.1%} wins)"
    )
    print(f"       Val:   {len(X_val)} samples ({base_val_wr:.1%} wins)")
    print(f"       Features: {feature_names}")

    # ── Train ──
    print(
        f"[2/4] Training LightGBM meta model (trials={args.optuna_trials}, seeds={args.n_seeds})..."
    )
    model, metrics, best_threshold = train_meta_model(
        X_train,
        y_train,
        X_val,
        y_val,
        sample_weight_train=w_train,
        sample_weight_val=w_val,
        n_trials=args.optuna_trials,
        n_seeds=args.n_seeds,
    )

    if args.threshold is not None:
        best_threshold = args.threshold
        y_prob = model.predict(X_val)
        metrics = evaluate_threshold(y_val, y_prob, thresholds=np.array([best_threshold]))

    print(f"\n  Best threshold: {best_threshold:.4f}")
    print(f"  Base win rate:     {metrics.get('base_win_rate', 'N/A')}")
    print(f"  Kept win rate:     {metrics.get('kept_win_rate', 'N/A')}")
    print(f"  Precision imp:     {metrics.get('precision_improvement', 'N/A'):+.4f}")
    print(f"  Signal retention:  {metrics.get('signal_retention', 'N/A')}")
    print(f"  Base exp PnL (R):  {metrics.get('base_expected_pnl', 'N/A')}")
    print(f"  Kept exp PnL (R):  {metrics.get('kept_expected_pnl', 'N/A')}")
    print(f"  Net PnL improvement: {metrics.get('net_improvement', 'N/A')}")

    # ── SHAP importance ──
    print("[3/4] Computing feature importance...")
    importance = compute_shap_importance(model, X_val, feature_names)
    top_features = sorted(importance.items(), key=lambda x: x[1], reverse=True)
    print("       Top features:")
    for name, imp in top_features[:8]:
        print(f"         {name}: {imp:.4f}")

    # ── Save ──
    print("[4/4] Saving model and metadata...")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    model_path = out_dir / f"meta_model_{ts}.txt"
    model.save_model(str(model_path))

    meta = {
        "schema_version": "meta_model.v2",
        "model_type": "lightgbm_binary",
        "feature_names": feature_names,
        "n_features": len(feature_names),
        "threshold": round(float(best_threshold), 4),
        "base_win_rate": round(float(base_val_wr), 4),
        "kept_win_rate": metrics.get("kept_win_rate", 0.0),
        "precision_improvement": metrics.get("precision_improvement", 0.0),
        "base_expected_pnl_r": metrics.get("base_expected_pnl", 0.0),
        "kept_expected_pnl_r": metrics.get("kept_expected_pnl", 0.0),
        "signal_retention": metrics.get("signal_retention", 0.0),
        "net_pnl_improvement": metrics.get("net_improvement", 0.0),
        "n_train": int(len(X_train)),
        "n_val": int(len(X_val)),
        "feature_importance": importance,
        "top_features": [{"name": n, "importance": i} for n, i in top_features[:10]],
        "contract": {
            "sl_atr_mult": 3.0,
            "tp_atr_mult": 1.5,
            "horizon_bars": 12,
            "risk_reward": "2:1",
            "sample_weights": "asymmetric (SL=2.0, TP=1.0)",
        },
    }
    meta_path = out_dir / f"meta_model_{ts}.meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"       Model: {model_path}")
    print(f"       Meta:  {meta_path}")
    print(
        f"\n  Pipeline complete: {'PASSED' if metrics.get('precision_improvement', 0) > 0 else 'NO_IMPROVEMENT'}"
    )

    return 0 if metrics.get("precision_improvement", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

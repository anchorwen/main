#!/usr/bin/env python3
"""M15 binary_directional XGBoost final training — optimized params.

Loads multi-class NPZ, filters NEUTRAL, trains binary XGBoost with
optimized hyperparameters and walk-forward CV evaluation.
"""

import json
import os
from datetime import UTC, datetime

import numpy as np


def load_binary(npz_path: str):
    data = np.load(npz_path)
    X, y_raw = data["X"], data["y"]
    # Filter NEUTRAL (label 1 in {0=SHORT, 1=NEUTRAL, 2=LONG})
    mask = y_raw != 1
    X_bin = X[mask]
    y_val = y_raw[mask]
    y = np.where(y_val == 0, 0, 1)  # SHORT(0)→0, LONG(2)→1
    n_short = int((y == 0).sum())
    n_long = int((y == 1).sum())
    print(f"Binary dataset: {len(y)} samples (SHORT={n_short}, LONG={n_long})")
    return X_bin, y


def walk_forward_splits(n, n_folds=6):
    splits = []
    fold_size = n // (n_folds + 1)
    for f in range(n_folds):
        test_start = n - (n_folds - f) * fold_size
        test_end = min(n, test_start + fold_size)
        if test_start <= fold_size:
            continue
        splits.append(
            {
                "fold": f,
                "train_idx": np.arange(0, test_start),
                "test_idx": np.arange(test_start, test_end),
            }
        )
    return splits


def simulate_trades(y_true, y_prob, threshold=0.5):
    """Simulate binary trades: predict LONG when P(LONG) >= threshold."""
    y_pred = (y_prob >= threshold).astype(int)
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    long_wr = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    short_wr = tn / (tn + fn) if (tn + fn) > 0 else 0.0
    bal_wr = (long_wr + short_wr) / 2.0
    acc = (tp + tn) / len(y_true)

    # Simulate PnL with directional deadzone
    deadzone = 0.05
    signal_mask = (y_prob >= 0.5 + deadzone) | (y_prob <= 0.5 - deadzone)
    if signal_mask.sum() > 0:
        sig_pred = y_pred[signal_mask]
        sig_true = y_true[signal_mask]
        sig_tp = int(((sig_pred == 1) & (sig_true == 1)).sum())
        sig_tn = int(((sig_pred == 0) & (sig_true == 0)).sum())
        sig_fp = int(((sig_pred == 1) & (sig_true == 0)).sum())
        sig_fn = int(((sig_pred == 0) & (sig_true == 1)).sum())
        d_long_wr = sig_tp / (sig_tp + sig_fp) if (sig_tp + sig_fp) > 0 else 0.0
        d_short_wr = sig_tn / (sig_tn + sig_fn) if (sig_tn + sig_fn) > 0 else 0.0
        d_wr = (d_long_wr + d_short_wr) / 2.0
        d_rate = signal_mask.sum() / len(y_true)
    else:
        d_wr, d_rate = 0.0, 0.0

    return {
        "acc": acc,
        "long_wr": long_wr,
        "short_wr": short_wr,
        "bal_wr": bal_wr,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "d_wr": d_wr,
        "d_rate": d_rate,
        "n_long_pred": int(y_pred.sum()),
        "n_short_pred": len(y_pred) - int(y_pred.sum()),
        "long_ratio": y_pred.mean(),
    }


def main():
    npz_path = "data/training/swing_m15_binary_dir_v7/train.npz"
    model_dir = "data/training/swing_m15_binary_dir_v7/models"
    os.makedirs(model_dir, exist_ok=True)

    print("[M15 BINARY] Loading dataset...")
    X, y = load_binary(npz_path)
    n = len(y)

    import lightgbm as lgb
    import xgboost as xgb

    splits = walk_forward_splits(n, n_folds=6)
    print(f"[M15 BINARY] Walk-forward CV: {len(splits)} folds")

    all_results = []
    all_lgb_results = []

    for split in splits:
        fold = split["fold"]
        tr_idx, te_idx = split["train_idx"], split["test_idx"]
        X_tr, y_tr = X[tr_idx], y[tr_idx]
        X_te, y_te = X[te_idx], y[te_idx]

        n_pos = int(y_tr.sum())
        n_neg = len(y_tr) - n_pos
        spw = n_neg / max(n_pos, 1)

        print(
            f"\n--- Fold {fold}: train={len(tr_idx)}, test={len(te_idx)} (SHORT={(y_te==0).sum()}, LONG={(y_te==1).sum()}) ---"
        )

        # ── XGBoost Optimized ──
        dtrain = xgb.DMatrix(X_tr, label=y_tr)
        dtest = xgb.DMatrix(X_te, label=y_te)

        xgb_params = {
            "objective": "binary:logistic",
            "eval_metric": ["auc", "logloss"],
            "max_depth": 6,
            "learning_rate": 0.01,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "min_child_weight": 5,
            "gamma": 0.1,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "scale_pos_weight": spw,
            "seed": 42,
            "n_jobs": -2,
            "verbosity": 0,
        }

        model_xgb = xgb.train(
            xgb_params,
            dtrain,
            num_boost_round=1000,
            evals=[(dtest, "test")],
            early_stopping_rounds=100,
            verbose_eval=False,
        )
        y_prob_xgb = model_xgb.predict(dtest)
        r_xgb = simulate_trades(y_te, y_prob_xgb)
        r_xgb["n_trees"] = model_xgb.best_iteration
        r_xgb["fold"] = fold
        all_results.append(r_xgb)
        print(
            f"  XGB: trees={r_xgb['n_trees']}, WR_bal={r_xgb['bal_wr']:.3f}, "
            f"WR_d={r_xgb['d_wr']:.3f} (rate={r_xgb['d_rate']:.1%}), "
            f"LONG_wr={r_xgb['long_wr']:.3f}, SHORT_wr={r_xgb['short_wr']:.3f}"
        )

        model_xgb.save_model(os.path.join(model_dir, f"xgb_m15_binary_fold{fold}.json"))

        # ── LightGBM Optimized ──
        dtrain_l = lgb.Dataset(X_tr, label=y_tr)
        dtest_l = lgb.Dataset(X_te, label=y_te, reference=dtrain_l)

        lgb_params = {
            "objective": "binary",
            "metric": "auc",
            "num_leaves": 63,
            "max_depth": 6,
            "learning_rate": 0.01,
            "feature_fraction": 0.85,
            "bagging_fraction": 0.85,
            "bagging_freq": 5,
            "min_data_in_leaf": 20,
            "min_gain_to_split": 0.01,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "scale_pos_weight": spw,
            "seed": 42,
            "verbosity": -1,
            "n_jobs": -2,
        }

        model_lgb = lgb.train(
            lgb_params,
            dtrain_l,
            num_boost_round=1000,
            valid_sets=[dtest_l],
            callbacks=[lgb.early_stopping(100), lgb.log_evaluation(0)],
        )
        y_prob_lgb = model_lgb.predict(X_te)
        r_lgb = simulate_trades(y_te, y_prob_lgb)
        r_lgb["n_trees"] = model_lgb.best_iteration
        r_lgb["fold"] = fold
        all_lgb_results.append(r_lgb)
        print(
            f"  LGB: trees={r_lgb['n_trees']}, WR_bal={r_lgb['bal_wr']:.3f}, "
            f"WR_d={r_lgb['d_wr']:.3f} (rate={r_lgb['d_rate']:.1%}), "
            f"LONG_wr={r_lgb['long_wr']:.3f}, SHORT_wr={r_lgb['short_wr']:.3f}"
        )

        model_lgb.save_model(os.path.join(model_dir, f"lgb_m15_binary_fold{fold}.txt"))

    # ── Summary ──
    print(f"\n{'='*60}")
    print("[M15 BINARY] CV Summary")

    for name, results in [("XGBoost", all_results), ("LightGBM", all_lgb_results)]:
        wrs = [r["bal_wr"] for r in results]
        d_wrs = [r["d_wr"] for r in results]
        trees = [r["n_trees"] for r in results]
        long_wrs = [r["long_wr"] for r in results]
        short_wrs = [r["short_wr"] for r in results]
        print(f"\n{name}:")
        print(f"  WR_bal: {np.mean(wrs)*100:.1f}% ± {np.std(wrs)*100:.1f}%")
        print(f"  WR_deadzone(θ=0.05): {np.mean(d_wrs)*100:.1f}% ± {np.std(d_wrs)*100:.1f}%")
        print(f"  LONG_WR: {np.mean(long_wrs)*100:.1f}% ± {np.std(long_wrs)*100:.1f}%")
        print(f"  SHORT_WR: {np.mean(short_wrs)*100:.1f}% ± {np.std(short_wrs)*100:.1f}%")
        print(f"  Trees: {np.mean(trees):.0f} ± {np.std(trees):.0f}")

    # Direction diversity
    best = max(all_results, key=lambda r: r["bal_wr"])
    best_path = os.path.join(model_dir, f"xgb_m15_binary_fold{best['fold']}.json")
    best_model = xgb.Booster()
    best_model.load_model(best_path)
    d_all = xgb.DMatrix(X)
    y_all_prob = best_model.predict(d_all)
    y_all_pred = (y_all_prob >= 0.5).astype(int)
    n_long_all = int(y_all_pred.sum())
    print(f"\nDirection Diversity (best fold={best['fold']}):")
    print(f"  LONG pred: {n_long_all}/{len(y_all_prob)} ({n_long_all/len(y_all_prob)*100:.1f}%)")
    if 30 <= n_long_all / len(y_all_prob) * 100 <= 70:
        print("  VERDICT: Direction-balanced [PASS]")
    else:
        print("  VERDICT: Direction-biased [WARN]")

    # Full evaluation on test set (last fold = most recent data)
    last_fold = max(all_results, key=lambda r: r["fold"])
    best_overall = max(all_results, key=lambda r: r["d_wr"])
    print(f"\nBest deadzone WR: fold={best_overall['fold']} d_wr={best_overall['d_wr']:.3f}")

    # Save summary
    summary = {
        "schema_version": "m15_binary_directional.v1",
        "npz_source": npz_path,
        "objective": "binary_directional",
        "deadzone_theta": 0.05,
        "n_samples": n,
        "n_features": X.shape[1],
        "n_short": int((y == 0).sum()),
        "n_long": int((y == 1).sum()),
        "xgb_cv": {
            "mean_wr_bal": float(np.mean([r["bal_wr"] for r in all_results])),
            "std_wr_bal": float(np.std([r["bal_wr"] for r in all_results])),
            "mean_wr_deadzone": float(np.mean([r["d_wr"] for r in all_results])),
            "std_wr_deadzone": float(np.std([r["d_wr"] for r in all_results])),
            "mean_long_wr": float(np.mean([r["long_wr"] for r in all_results])),
            "mean_short_wr": float(np.mean([r["short_wr"] for r in all_results])),
            "mean_trees": float(np.mean([r["n_trees"] for r in all_results])),
            "folds": len(all_results),
        },
        "lgb_cv": {
            "mean_wr_bal": float(np.mean([r["bal_wr"] for r in all_lgb_results])),
            "std_wr_bal": float(np.std([r["bal_wr"] for r in all_lgb_results])),
            "mean_wr_deadzone": float(np.mean([r["d_wr"] for r in all_lgb_results])),
            "mean_trees": float(np.mean([r["n_trees"] for r in all_lgb_results])),
            "folds": len(all_lgb_results),
        },
        "trained_at": datetime.now(UTC).isoformat(),
    }
    with open(os.path.join(model_dir, "final_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nSummary: {model_dir}/final_summary.json")


if __name__ == "__main__":
    main()

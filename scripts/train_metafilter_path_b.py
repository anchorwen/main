# type: ignore
#!/usr/bin/env python
"""MetaFilter Path B — Train LightGBM on 96 live BTC trade samples.

IC Hardening: max_depth=3, class_weight='balanced', feature importance report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from core.runtime.fault_handler import fail_open_guard

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent
TRAIN_PATH = ROOT / "data_btc" / "models" / "metafilter_path_b_train.jsonl"
MODEL_PATH = ROOT / "data_btc" / "models" / "metafilter_path_b_v1.json"


def main() -> int:
    print("=" * 60)
    print("  MetaFilter Path B — LightGBM Trainer")
    print("=" * 60)

    # Load dataset
    samples = []
    with open(TRAIN_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    print(f"Loaded: {len(samples)} samples")

    X = np.array([s["features"] for s in samples], dtype=np.float64)
    y = np.array([s["is_win"] for s in samples], dtype=np.int32)
    feature_names = samples[0]["feature_names"]
    print(f"Features: {X.shape}, Labels: {len(y)} (wins={y.sum()})")

    # Purged time-series split (5 folds)
    from sklearn.model_selection import TimeSeriesSplit

    cv = TimeSeriesSplit(n_splits=5)
    from lightgbm import LGBMClassifier

    # ── FIX-20260616-093: IC Hardening — max_depth=2, min_data_in_leaf=15 ──
    model = LGBMClassifier(
        n_estimators=80,
        max_depth=2,
        min_child_samples=15,
        min_data_in_leaf=15,
        subsample=0.8,
        colsample_bytree=0.6,
        class_weight="balanced",
        random_state=42,
        verbose=-1,
    )

    oof_preds = np.zeros(len(y))
    for fold, (train_idx, val_idx) in enumerate(cv.split(X)):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]
        model.fit(X_tr, y_tr)
        oof_preds[val_idx] = model.predict_proba(X_val)[:, 1]
        auc = _roc_auc(y_val, oof_preds[val_idx])
        print(f"  Fold {fold+1}: train={len(train_idx)} val={len(val_idx)} AUC={auc:.3f}")

    # Full OOF AUC
    oof_auc = _roc_auc(y, oof_preds)
    print(f"\nOOF AUC: {oof_auc:.3f}")

    # Probability distribution
    print("\nOOF p_win distribution:")
    print(f"  min={oof_preds.min():.3f} p25={np.percentile(oof_preds,25):.3f} median={np.median(oof_preds):.3f} p75={np.percentile(oof_preds,75):.3f} max={oof_preds.max():.3f}")

    # Percentile-based threshold recommendation
    for pct in [30, 40, 50, 60, 70]:
        threshold = np.percentile(oof_preds, pct)
        blocked = (oof_preds <= threshold).sum()
        blocked_wins = ((oof_preds <= threshold) & (y == 1)).sum()
        print(f"  Threshold p{pct}={threshold:.3f}: blocks {blocked}/{len(y)} trades, kills {blocked_wins} wins")

    # ── FIX-20260616-093: Precision/Recall calibration matrix ──
    print("\n=== Calibration Report: Precision/Recall Matrix ===")
    print(f"  {'Threshold':<12s} {'Blocked':>7s} {'KillsWins':>10s} {'Recall':>8s} {'Precision':>10s} {'F1':>8s}")
    print(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*8} {'-'*10} {'-'*8}")
    for pct in [25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75]:
        threshold = round(float(np.percentile(oof_preds, pct)), 3)
        blocked = int((oof_preds <= threshold).sum())
        killed_wins = int(((oof_preds <= threshold) & (y == 1)).sum())
        total_losses = int((y == 0).sum())
        blocked_losses = int(((oof_preds <= threshold) & (y == 0)).sum())
        recall = blocked_losses / max(total_losses, 1) * 100  # % of losses caught
        precision = (blocked - killed_wins) / max(blocked, 1) * 100  # % of blocks that were correct
        f1 = 2 * recall * precision / max(recall + precision, 1)
        print(f"  {threshold:<12.3f} {blocked:>7d} {killed_wins:>10d} {recall:>7.1f}% {precision:>9.1f}% {f1:>7.1f}%")

    # Recommendation
    print("\n=== Recommendation ===")
    # Find threshold that catches >=50% of losses with <=30% win kill rate
    best_pct = None
    for pct in [30, 35, 40, 45, 50]:
        threshold = np.percentile(oof_preds, pct)
        blocked_losses = int(((oof_preds <= threshold) & (y == 0)).sum())
        killed_wins = int(((oof_preds <= threshold) & (y == 1)).sum())
        recall = blocked_losses / max((y == 0).sum(), 1)
        win_kill_rate = killed_wins / max((y == 1).sum(), 1)
        if recall >= 0.50 and win_kill_rate <= 0.30:
            best_pct = pct
            break
    if best_pct:
        print(f"  Recommended: p{best_pct}={np.percentile(oof_preds, best_pct):.3f}")
        print("  Rationale: catches >=50% losses while killing <=30% wins")
    else:
        print("  No threshold meets the >=50% recall, <=30% win-kill criteria")
        print(f"  Conservative fallback: p40={np.percentile(oof_preds, 40):.3f}")

    # Train final model on full data
    model.fit(X, y)
    final_importance = dict(zip(feature_names, model.feature_importances_, strict=False))
    top_features = sorted(final_importance.items(), key=lambda x: -x[1])[:15]

    print("\n=== Feature Importance (top 15) ===")
    for name, imp in top_features:
        bar = "█" * int(imp / max(1e-9, top_features[0][1]) * 30)
        print(f"  {name:<30s} {imp:.4f} {bar}")

    # Save model metadata (convert numpy types to native Python)
    model_data = {
        "model_type": "lightgbm",
        "n_samples": int(len(samples)),
        "n_features": int(X.shape[1]),
        "feature_names": feature_names,
        "oof_auc": round(float(oof_auc), 4),
        "p50_threshold": round(float(np.median(oof_preds)), 4),
        "p30_threshold": round(float(np.percentile(oof_preds, 30)), 4),
        "top_features": [(str(n), float(v)) for n, v in top_features[:10]],
        "class_weight": "balanced",
        "max_depth": 3,
        "n_estimators": 50,
    }
    with open(MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(model_data, f, indent=2, ensure_ascii=False)
    print(f"\nModel: {MODEL_PATH}")

    # Save LightGBM booster
    booster_path = str(MODEL_PATH).replace(".json", ".txt")
    model.booster_.save_model(booster_path)
    print(f"Booster: {booster_path}")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


def _roc_auc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y_true, y_pred))
    except Exception:  # BLE001:FOG
        with fail_open_guard("train_metafilter_path_b:_roc_auc"):
            return 0.0
if __name__ == "__main__":
    raise SystemExit(main())

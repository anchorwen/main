#!/usr/bin/env python3
"""Phase C: V6 M15 LightGBM baseline — train on 37-dim dataset with SL=2.0/TP=2.5.

Usage: python scripts/training/train_v6_m15_baseline.py
"""

import json
import sys
from pathlib import Path

import numpy as np

# ── Load dataset ──
data_path = Path("data/training/swing_m15_btc_37_v6/train.npz")
if not data_path.exists():
    print(f"ERROR: dataset not found at {data_path}")
    print(
        "Run first: python scripts/training/build_swing_enhanced_dataset.py --symbol btcusdc --tf M15"
    )
    sys.exit(1)

data = np.load(data_path)
X_train = data["X"]
y_train_raw = data["y"]
X_val = data["X_val"]
y_val_raw = data["y_val"]
X_test = data["X_test"]
y_test_raw = data["y_test"]
feature_names = list(data["feature_names"])

# ── Binary classification: SL vs TP (exclude timeout=2) ──
mask_train = y_train_raw != 2
X_tr = X_train[mask_train]
y_tr = (y_train_raw[mask_train] == 1).astype(np.int32)

mask_val = y_val_raw != 2
X_va = X_val[mask_val]
y_va = (y_val_raw[mask_val] == 1).astype(np.int32)

mask_test = y_test_raw != 2
X_te = X_test[mask_test]
y_te = (y_test_raw[mask_test] == 1).astype(np.int32)

print(f"Train: {X_tr.shape[0]} samples, TP={y_tr.sum()} ({y_tr.sum()/len(y_tr)*100:.1f}%)")
print(f"Val:   {X_va.shape[0]} samples, TP={y_va.sum()} ({y_va.sum()/len(y_va)*100:.1f}%)")
print(f"Test:  {X_te.shape[0]} samples, TP={y_te.sum()} ({y_te.sum()/len(y_te)*100:.1f}%)")

# ── Train LightGBM ──
import lightgbm as lgb

params = {
    "objective": "binary",
    "metric": "auc",
    "boosting": "gbdt",
    "num_leaves": 31,
    "max_depth": 5,
    "learning_rate": 0.05,
    "n_estimators": 200,
    "min_child_samples": 50,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "random_state": 42,
    "verbose": -1,
}

print(
    f"\nTraining LightGBM: {params['n_estimators']} trees, depth<={params['max_depth']}, lr={params['learning_rate']}"
)

model = lgb.LGBMClassifier(**params)
model.fit(
    X_tr,
    y_tr,
    eval_set=[(X_tr, y_tr), (X_va, y_va)],
    eval_names=["train", "val"],
    callbacks=[
        lgb.early_stopping(stopping_rounds=30, verbose=False),
        lgb.log_evaluation(period=50),
    ],
)

# ── Evaluate ──
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

y_pred_proba = model.predict_proba(X_te)[:, 1]
y_pred = model.predict(X_te)
test_auc = roc_auc_score(y_te, y_pred_proba)
test_acc = accuracy_score(y_te, y_pred)
test_f1 = f1_score(y_te, y_pred)

print(f"\n{'='*60}")
print("  TEST RESULTS")
print(f"{'='*60}")
print(f"  Trees used:        {model.best_iteration_}")
print(f"  Test AUC:          {test_auc:.4f}")
print(f"  Test Accuracy:     {test_acc:.4f}")
print(f"  Test F1:           {test_f1:.4f}")
print(f"  Baseline (always-TP): {y_te.sum()/len(y_te)*100:.1f}%")

# ── Feature importance ──
importance = model.feature_importances_
indices = np.argsort(importance)[::-1]

print(f"\n{'='*60}")
print("  TOP 15 FEATURE IMPORTANCE")
print(f"{'='*60}")
print(f"  {'Rank':<5s} {'Feature':<35s} {'Gain':>10s}")
print(f"  {'-'*50}")
for i, idx in enumerate(indices[:15]):
    print(f"  {i+1:<5d} {feature_names[idx]:<35s} {importance[idx]:>10.1f}")

# ── Cross-asset feature ranks ──
cross_asset_features = [
    "XAUUSDc_return",
    "Cross_DXY_Return",
    "Cross_EURUSD_Return",
    "AUDJPYc_return",
    "EURUSDc_return",
    "USDJPYc_return",
    "Cross_BTC_Gold_Ratio",
    "Cross_BTC_Gold_Ratio_ROC",
]
print(f"\n{'='*60}")
print("  CROSS-ASSET FEATURE RANKS")
print(f"{'='*60}")
for fname in cross_asset_features:
    if fname in feature_names:
        idx = feature_names.index(fname)
        rank = list(indices).index(idx) + 1
        print(f"  Rank {rank:>3d}/37: {fname:<35s} gain={importance[idx]:>10.1f}")

# ── Save model ──
out_dir = Path("data_btc/brains")
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / "BTC_Swing_V6_M15_LGB_v4.txt"
model.booster_.save_model(str(out_path))

meta = {
    "brain_id": "BTC_Swing_V6_M15_LGB_v4",
    "model_type": "lightgbm",
    "feature_schema": "btc_macro_enhanced_37",
    "n_features": 37,
    "timeframe": "M15",
    "horizon": 24,
    "train_samples": int(X_tr.shape[0]),
    "val_samples": int(X_va.shape[0]),
    "test_samples": int(X_te.shape[0]),
    "n_trees": model.best_iteration_,
    "test_auc": round(test_auc, 4),
    "test_f1": round(test_f1, 4),
    "sl_atr_mult": 2.0,
    "tp_atr_mult": 2.5,
    "label_contract": "live_btc_v1",
    "top_features": [
        [int(indices[i]), feature_names[indices[i]], float(importance[indices[i]])]
        for i in range(15)
    ],
}
with open(str(out_path).replace(".txt", "_meta.json"), "w") as f:
    json.dump(meta, f, indent=2)

print(f"\nModel saved: {out_path}")
print(f"Meta saved:  {str(out_path).replace('.txt', '_meta.json')}")
print(f"\n{'='*60}")
print("  TRAINING COMPLETE")
print(f"{'='*60}")

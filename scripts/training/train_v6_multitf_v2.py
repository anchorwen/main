#!/usr/bin/env python3
"""Phase C4: V6 Multi-TF (M15+M30+H1) LightGBM joint training.

Merges three timeframe datasets with purged time isolation,
trains with strong regularization, outputs AUC + feature ranks.
"""

import json
import sys
from pathlib import Path

import numpy as np

# ── Load and merge datasets ──
datasets = {
    "M15": "data/training/swing_m15_btc_37_v6/train.npz",
    "M30": "data/training/swing_m30_btc_37_v6/train.npz",
    "H1": "data/training/swing_h1_btc_37_v6/train.npz",
}

X_train_parts, y_train_parts = [], []
X_val_parts, y_val_parts = [], []
X_test_parts, y_test_parts = [], []

for tf, path in datasets.items():
    p = Path(path)
    if not p.exists():
        print(f"ERROR: {tf} dataset not found at {p}")
        sys.exit(1)
    d = np.load(p)
    print(f"{tf}: train={d['X'].shape[0]}, val={d['X_val'].shape[0]}, test={d['X_test'].shape[0]}")
    X_train_parts.append(d["X"])
    y_train_parts.append(d["y"])
    X_val_parts.append(d["X_val"])
    y_val_parts.append(d["y_val"])
    X_test_parts.append(d["X_test"])
    y_test_parts.append(d["y_test"])

X_train = np.concatenate(X_train_parts, axis=0)
y_train_raw = np.concatenate(y_train_parts, axis=0)
X_val = np.concatenate(X_val_parts, axis=0)
y_val_raw = np.concatenate(y_val_parts, axis=0)
X_test = np.concatenate(X_test_parts, axis=0)
y_test_raw = np.concatenate(y_test_parts, axis=0)

feature_names = list(np.load(Path(datasets["M15"]))["feature_names"])

# ── Binary classification: SL vs TP (exclude timeout=2) ──
for _name, y_raw in [("Train", y_train_raw), ("Val", y_val_raw), ("Test", y_test_raw)]:
    unique, counts = np.unique(y_raw, return_counts=True)
    d = dict(zip(unique.astype(int), counts, strict=False))

mask_tr = y_train_raw != 2
X_tr = X_train[mask_tr]
y_tr = (y_train_raw[mask_tr] == 1).astype(np.int32)
mask_va = y_val_raw != 2
X_va = X_val[mask_va]
y_va = (y_val_raw[mask_va] == 1).astype(np.int32)
mask_te = y_test_raw != 2
X_te = X_test[mask_te]
y_te = (y_test_raw[mask_te] == 1).astype(np.int32)

print("\nMerged binary (SL vs TP):")
print(f"  Train: {X_tr.shape[0]:,} samples, TP={y_tr.sum():,} ({y_tr.sum()/len(y_tr)*100:.1f}%)")
print(f"  Val:   {X_va.shape[0]:,} samples, TP={y_va.sum():,} ({y_va.sum()/len(y_va)*100:.1f}%)")
print(f"  Test:  {X_te.shape[0]:,} samples, TP={y_te.sum():,} ({y_te.sum()/len(y_te)*100:.1f}%)")

# ── Train LightGBM with strong regularization ──
import lightgbm as lgb

params = {
    "objective": "binary",
    "metric": "auc",
    "boosting": "gbdt",
    "num_leaves": 31,
    "max_depth": 5,
    "learning_rate": 0.05,
    "n_estimators": 300,
    "min_child_samples": 50,
    "min_data_in_leaf": 100,
    "subsample": 0.8,
    "colsample_bytree": 0.7,
    "reg_alpha": 0.2,
    "reg_lambda": 2.0,
    "random_state": 42,
    "verbose": -1,
}

print("\nTraining Multi-TF LightGBM:")
print(f"  samples={X_tr.shape[0]:,}, trees<=300, depth<={params['max_depth']}")
print("  colsample=0.7, subsample=0.8, min_leaf=100, L1=0.2, L2=2.0")

model = lgb.LGBMClassifier(**params)
model.fit(
    X_tr,
    y_tr,
    eval_set=[(X_tr, y_tr), (X_va, y_va)],
    eval_names=["train", "val"],
    callbacks=[
        lgb.early_stopping(stopping_rounds=50, verbose=False),
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

# Per-TF breakdown
print(f"\n{'='*65}")
print("  MULTI-TF TEST RESULTS (M15+M30+H1 merged)")
print(f"{'='*65}")
print(f"  Trees used:          {model.best_iteration_}")
print(f"  Test AUC:            {test_auc:.4f}")
print(f"  Test Accuracy:       {test_acc:.4f}")
print(f"  Test F1:             {test_f1:.4f}")

# Per-TF AUC (recompute from raw test data to avoid mask misalignment)
print("  --- Per-TF AUC ---")
y_prob_all = model.predict_proba(X_te)[:, 1]
offset_bin = 0
for tf in ["M15", "M30", "H1"]:
    d_tf = np.load(Path(datasets[tf]))
    y_raw_tf = d_tf["y_test"]
    mask_tf = y_raw_tf != 2
    n_tf_bin = mask_tf.sum()
    if n_tf_bin > 0 and len(np.unique(y_te[offset_bin : offset_bin + n_tf_bin])) > 1:
        auc_tf = roc_auc_score(
            y_te[offset_bin : offset_bin + n_tf_bin],
            y_prob_all[offset_bin : offset_bin + n_tf_bin],
        )
        print(f"  {tf} AUC:            {auc_tf:.4f}  (n={n_tf_bin})")
    offset_bin += n_tf_bin

# ── Feature importance ──
importance = model.feature_importances_
indices = np.argsort(importance)[::-1]

print(f"\n{'='*65}")
print("  TOP 15 FEATURE IMPORTANCE (Multi-TF)")
print(f"{'='*65}")
print(f"  {'Rank':<5s} {'Feature':<35s} {'Gain':>10s}")
print(f"  {'-'*50}")
for i, idx in enumerate(indices[:15]):
    bar = "█" * max(1, int(importance[idx] / importance[indices[0]] * 20))
    print(f"  {i+1:<5d} {feature_names[idx]:<35s} {importance[idx]:>10.1f}  {bar}")

# ── Cross-asset ranks ──
cross = [
    "XAUUSDc_return",
    "Cross_DXY_Return",
    "Cross_EURUSD_Return",
    "AUDJPYc_return",
    "EURUSDc_return",
    "USDJPYc_return",
    "Cross_BTC_Gold_Ratio",
    "Cross_BTC_Gold_Ratio_ROC",
]
print(f"\n{'='*65}")
print("  CROSS-ASSET FEATURE RANKS")
print(f"{'='*65}")
for fname in cross:
    if fname in feature_names:
        idx = feature_names.index(fname)
        rank = list(indices).index(idx) + 1
        print(f"  Rank {rank:>3d}/37  {fname:<35s} gain={importance[idx]:>10.1f}")

# ── Compare with M15-only baseline ──
print(f"\n{'='*65}")
print("  vs M15-ONLY BASELINE")
print(f"{'='*65}")
print("  M15-only AUC:  0.6304")
print(f"  Multi-TF AUC:  {test_auc:.4f}")
print(f"  Delta:         {test_auc - 0.6304:+.4f}")

# ── Save ──
out = Path("data_btc/brains/BTC_Swing_V6_MultiTF_LGB_v2.txt")
out.parent.mkdir(parents=True, exist_ok=True)
model.booster_.save_model(str(out))
meta = {
    "brain_id": "BTC_Swing_V6_MultiTF_LGB_v2",
    "model_type": "lightgbm",
    "feature_schema": "btc_macro_enhanced_37",
    "n_features": 37,
    "timeframes": ["M15", "M30", "H1"],
    "horizon": "24/12/6",
    "train_samples": int(X_tr.shape[0]),
    "val_samples": int(X_va.shape[0]),
    "test_samples": int(X_te.shape[0]),
    "n_trees": model.best_iteration_,
    "test_auc": round(test_auc, 4),
    "test_f1": round(test_f1, 4),
    "sl_atr_mult": 2.0,
    "tp_atr_mult": 2.5,
    "label_contract": "live_btc_v1",
    "regularization": "colsample0.7_subsample0.8_minleaf100_L10.2_L22.0",
    "m15_only_baseline_auc": 0.6304,
    "top_features": [
        [int(indices[i]), feature_names[indices[i]], float(importance[indices[i]])]
        for i in range(15)
    ],
}
with open(str(out).replace(".txt", "_meta.json"), "w") as f:
    json.dump(meta, f, indent=2)

print(f"\nModel: {out}")
print(f"Meta:  {str(out).replace('.txt', '_meta.json')}")
print(f"\n{'='*65}")
print("  MULTI-TF TRAINING COMPLETE")
print(f"{'='*65}")

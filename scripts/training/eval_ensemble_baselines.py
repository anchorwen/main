"""Quick ensemble baseline: per-resolution XGBoost + Transformer fusion.

Tests two simple fusion rules:
  1. Union: signal if EITHER model predicts it
  2. Intersection: signal only if BOTH models agree

Runs on each resolution's own validation set (no cross-alignment needed).
"""

import numpy as np
import onnxruntime as ort
import xgboost as xgb

RESOLUTIONS = {
    "M5": {
        "xgb": "data/models/xgb_v4.5_micro_barrier_v2.json",
        "onnx": "data/models/transformer_v5_micro_barrier_v2.onnx",
        "val": "data/training/micro_barrier_v2/val.npz",
    },
    "M15": {
        "xgb": "data/models/xgb_v4.5_micro_barrier_m15.json",
        "onnx": "data/models/transformer_v5_micro_barrier_m15.onnx",
        "val": "data/training/micro_barrier_m15_v2/val.npz",
    },
    "H1": {
        "xgb": "data/models/xgb_v4.5_micro_barrier_h1.json",
        "onnx": "data/models/transformer_v5_micro_barrier_h1.onnx",
        "val": "data/training/micro_barrier_h1_v2/val.npz",
    },
    "H4": {
        "xgb": "data/models/xgb_v4.5_micro_barrier_h4.json",
        "onnx": "data/models/transformer_v5_micro_barrier_h4.onnx",
        "val": "data/training/micro_barrier_h4_v2/val.npz",
    },
}


def evaluate(res_name: str, cfg: dict):
    print(f"\n{'='*60}")
    print(f"  {res_name}")
    print(f"{'='*60}")

    d = np.load(cfg["val"], allow_pickle=True)
    X_flat = d["X_flat"].astype(np.float32)
    X_seq = d["X"].astype(np.float32)
    y_raw = d["y"].astype(np.int32)
    y = np.where(y_raw == -1, 2, y_raw).astype(np.int32)

    # XGBoost predictions
    booster = xgb.Booster()
    booster.load_model(cfg["xgb"])
    feat_names = [f"f_{i}" for i in range(X_flat.shape[1])]
    dmat = xgb.DMatrix(X_flat)
    dmat.feature_names = feat_names
    xgb_preds = booster.predict(dmat).astype(np.int32)

    # Transformer predictions (sequential — ONNX dynamo bug: only bs=1 works)
    sess = ort.InferenceSession(cfg["onnx"], providers=["CPUExecutionProvider"])
    tf_preds = np.zeros(len(X_seq), dtype=np.int32)
    for i in range(len(X_seq)):
        if i % 2000 == 0:
            print(f"    TF inference {i}/{len(X_seq)}...")
        sample = X_seq[i : i + 1].astype(np.float32)
        logits = sess.run(None, {"input": sample})[0]
        tf_preds[i] = int(np.argmax(logits))
    print(f"    TF inference done: {len(X_seq)} samples")

    # Compute metrics for a given prediction set
    def metrics(preds, label):
        acc = float((preds == y).mean())
        mask_tp = y == 1
        mask_sl = y == 2
        mask_to = y == 0
        tp_acc = float((preds[mask_tp] == 1).mean()) if mask_tp.sum() > 0 else 0.0
        sl_acc = float((preds[mask_sl] == 2).mean()) if mask_sl.sum() > 0 else 0.0
        to_acc = float((preds[mask_to] == 0).mean()) if mask_to.sum() > 0 else 0.0
        tp_rate = float((preds == 1).mean())
        sl_rate = float((preds == 2).mean())
        return {
            "acc": acc,
            "tp_acc": tp_acc,
            "sl_acc": sl_acc,
            "to_acc": to_acc,
            "tp_rate": tp_rate,
            "sl_rate": sl_rate,
        }

    m_xgb = metrics(xgb_preds, "XGBoost")
    m_tf = metrics(tf_preds, "Transformer")

    # Union ensemble
    union = np.zeros(len(y), dtype=np.int32)
    for i in range(len(y)):
        if xgb_preds[i] == 1 or tf_preds[i] == 1:
            union[i] = 1
        elif xgb_preds[i] == 2 or tf_preds[i] == 2:
            union[i] = 2
        else:
            union[i] = 0
    m_union = metrics(union, "Union")

    # Intersection ensemble
    inter = np.zeros(len(y), dtype=np.int32)
    for i in range(len(y)):
        if xgb_preds[i] == 1 and tf_preds[i] == 1:
            inter[i] = 1
        elif xgb_preds[i] == 2 and tf_preds[i] == 2:
            inter[i] = 2
        else:
            inter[i] = 0
    m_inter = metrics(inter, "Intersection")

    # TF→TP, XGB→SL complement: use TF for TP, XGB for SL
    complement = np.zeros(len(y), dtype=np.int32)
    for i in range(len(y)):
        if tf_preds[i] == 1:
            complement[i] = 1
        elif xgb_preds[i] == 2:
            complement[i] = 2
        else:
            complement[i] = 0
    m_comp = metrics(complement, "TF-TP+XGB-SL")

    print(
        f"  {'Model':<20} {'Acc':>7} {'TP Acc':>7} {'SL Acc':>7} {'TO Acc':>7} {'TP Rate':>7} {'SL Rate':>7}"
    )
    print(f"  {'-'*60}")
    for name, m in [
        ("XGBoost", m_xgb),
        ("Transformer", m_tf),
        ("Union", m_union),
        ("Intersection", m_inter),
        ("TF-TP+XGB-SL", m_comp),
    ]:
        print(
            f"  {name:<20} {m['acc']:7.4f} {m['tp_acc']:7.4f} {m['sl_acc']:7.4f} {m['to_acc']:7.4f} {m['tp_rate']:7.4f} {m['sl_rate']:7.4f}"
        )

    # Best single model
    best_single = max(m_xgb["acc"], m_tf["acc"])
    best_ens = max(m_union["acc"], m_inter["acc"], m_comp["acc"])
    gain = best_ens - best_single
    print(f"  Best single: {best_single:.4f} | Best ensemble: {best_ens:.4f} | Gain: {gain:+.4f}")


for res, cfg in RESOLUTIONS.items():
    evaluate(res, cfg)

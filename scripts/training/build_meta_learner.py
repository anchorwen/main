"""HMRE Meta-Learner Builder v2 — Efficient aligned cross-resolution inference.

Strategy:
  1. Retrain XGBoost models with multi:softprob for proper softmax output (fast, ~6s each)
  2. Pre-compute timestamp→bar_index maps for each resolution (O(n) once)
  3. Sample M5 entry points, find aligned bars in all resolutions
  4. Batch XGBoost inference (DMatrix all at once)
  5. Batch Transformer ONNX inference (single batch forward pass)
  6. Train lightweight XGBoost meta-learner on 24-dim softmax features

Usage:
  python scripts/training/build_meta_learner.py --max-samples 5000
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SEQ_LEN = 32
NUM_FEATURES = 9
ROLLING_WINDOW = 1000

FEATURE_NAMES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "XAGUSDc_return",
    "EURUSDc_return",
    "USDJPYc_return",
]

RESOLUTIONS = ["M5", "M15", "H1", "H4"]

RAW_DATA: dict[str, str] = {
    "M5": "data/raw/xauusdc_m5_merged.csv",
    "M15": "data/raw/xauusdc_m15_merged.csv",
    "H1": "data/raw/xauusdc_h1_merged.csv",
    "H4": "data/raw/xauusdc_h4_merged.csv",
}

CROSS_DATA: dict[str, dict[str, str]] = {
    "M5": {
        "eur": "data/raw/eurusdc_m5_merged.csv",
        "jpy": "data/raw/usdjpyc_m5_merged.csv",
        "xag": "data/raw/xagusdc_m5_merged.csv",
    },
    "M15": {
        "eur": "data/raw/eurusdc_m15_merged.csv",
        "jpy": "data/raw/usdjpyc_m15_merged.csv",
        "xag": "data/raw/xagusdc_m15_merged.csv",
    },
    "H1": {
        "eur": "data/raw/eurusdc_h1_merged.csv",
        "jpy": "data/raw/usdjpyc_h1_merged.csv",
        "xag": "data/raw/xagusdc_h1_merged.csv",
    },
    "H4": {
        "eur": "data/raw/eurusdc_h4_merged.csv",
        "jpy": "data/raw/usdjpyc_h4_merged.csv",
        "xag": "data/raw/xagusdc_h4_merged.csv",
    },
}

NPZ_TRAIN: dict[str, str] = {
    "M5": "data/training/micro_barrier_v2/train.npz",
    "M15": "data/training/micro_barrier_m15_v2/train.npz",
    "H1": "data/training/micro_barrier_h1_v2/train.npz",
    "H4": "data/training/micro_barrier_h4_v2/train.npz",
}


def load_and_prepare(resolution: str) -> tuple[pd.DataFrame, np.ndarray, dict[int, int]]:
    """Load raw data, compute features, standardize. Returns (df, scaled_feats_array, time_to_idx_map)."""
    df_xau = pd.read_csv(RAW_DATA[resolution], parse_dates=["time"]).sort_values("time")
    cross = CROSS_DATA[resolution]
    df_eur = pd.read_csv(cross["eur"], parse_dates=["time"]).sort_values("time")
    df_jpy = pd.read_csv(cross["jpy"], parse_dates=["time"]).sort_values("time")
    df_xag = pd.read_csv(cross["xag"], parse_dates=["time"]).sort_values("time")

    df = pd.merge_asof(
        df_xau, df_eur[["time", "close"]], on="time", direction="backward", suffixes=("", "_eur")
    )
    df = pd.merge_asof(
        df, df_jpy[["time", "close"]], on="time", direction="backward", suffixes=("", "_jpy")
    )
    df = pd.merge_asof(
        df, df_xag[["time", "close"]], on="time", direction="backward", suffixes=("", "_xag")
    )
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Features
    df["tick_return"] = df["close"].pct_change() * 100.0
    df["hl_ratio"] = (df["high"] - df["low"]) / (df["close"].clip(lower=1e-9))
    df["co_ratio"] = df["close"] / (df["open"].clip(lower=1e-9))
    df["avg_spread"] = df["spread"] / (df["close"].clip(lower=1e-9))
    hl_diff = df["high"] - df["low"]
    df["OIM"] = np.where(hl_diff > 1e-12, (df["close"] - df["open"]) / hl_diff, 0.0)
    df["tick_velocity"] = df["tick_volume"] / 1000.0
    df["XAGUSDc_return"] = df["close_xag"].pct_change() * 100.0
    df["EURUSDc_return"] = df["close_eur"].pct_change() * 100.0
    df["USDJPYc_return"] = df["close_jpy"].pct_change() * 100.0
    df.dropna(subset=FEATURE_NAMES, inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Rolling std
    roll_mean = df[FEATURE_NAMES].rolling(window=ROLLING_WINDOW, min_periods=SEQ_LEN).mean()
    roll_std = (
        df[FEATURE_NAMES]
        .rolling(window=ROLLING_WINDOW, min_periods=SEQ_LEN)
        .std()
        .replace(0.0, 1.0)
    )
    scaled = ((df[FEATURE_NAMES] - roll_mean) / roll_std).bfill().values.astype(np.float32)

    # Timestamp → bar index map (for alignment)
    time_to_idx: dict[int, int] = {}
    for i, ts in enumerate(df["time"]):
        time_to_idx[int(ts.timestamp())] = i

    return df, scaled, time_to_idx


def load_m5_labels() -> tuple[dict[int, list[dict]], list[int]]:
    """Load M5 barrier labels. Returns (timestamp_dict, sorted_timestamps)."""
    labels_path = Path("data/labels/micro_barrier_labels.jsonl")
    label_dict: dict[int, list[dict]] = {}
    with labels_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ts_str = rec.get("entry_time", "").strip()
            if not ts_str:
                continue
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            ts = int(datetime.fromisoformat(ts_str).timestamp())
            label_dict.setdefault(ts, []).append(rec)

    sorted_ts = sorted(label_dict.keys())
    return label_dict, sorted_ts


def find_bar_at_or_before(time_to_idx: dict[int, int], target_ts: int) -> int | None:
    """Find bar index at or immediately before target timestamp."""
    # Direct lookup
    if target_ts in time_to_idx:
        return time_to_idx[target_ts]
    # Search backwards in 1-second decrements (M5 bars are 300s apart, so this is wasteful)
    # Better: find the largest key <= target_ts
    # For efficiency, pre-sort keys and binary search
    return None


def find_bar_binary(
    time_to_idx: dict[int, int], sorted_keys: list[int], target_ts: int
) -> int | None:
    """Binary search for bar at or before target timestamp."""
    import bisect

    idx = bisect.bisect_right(sorted_keys, target_ts) - 1
    if idx < 0:
        return None
    return time_to_idx[sorted_keys[idx]]


def retrain_xgb_softprob(resolution: str) -> Any:
    """Retrain XGBoost with multi:softprob for proper probability output."""
    import xgboost as xgb

    train_path = Path(NPZ_TRAIN[resolution])
    data = np.load(train_path, allow_pickle=True)
    X = data["X_flat"].astype(np.float32)
    y_raw = data["y"].astype(np.int32)
    # Map -1→2, 0→0, 1→1
    y = y_raw.copy()
    y = np.where(y == -1, 2, y)
    y = np.where(y == 1, 1, y)

    cls_counts = np.bincount(y, minlength=3)
    cls_weights = len(y) / (3 * cls_counts.clip(min=1))
    sample_weights = cls_weights[y]

    dtrain = xgb.DMatrix(X, label=y)
    dtrain.set_weight(sample_weights)

    params = {
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "max_depth": 5,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": 42,
        "n_jobs": -1,
    }

    booster = xgb.train(params, dtrain, num_boost_round=50, verbose_eval=False)
    return booster


def main():
    parser = argparse.ArgumentParser(prog="build_meta_learner")
    parser.add_argument("--max-samples", type=int, default=5000)
    parser.add_argument(
        "--output-model", type=Path, default=Path("data/models/meta_learner_hmre_v1.json")
    )
    parser.add_argument(
        "--output-result", type=Path, default=Path("data/models/meta_learner_hmre_v1_result.json")
    )
    args = parser.parse_args()

    # ═══════════════════════════════════════════════════════════════════
    # Step 1: Retrain XGBoost models with softprob
    # ═══════════════════════════════════════════════════════════════════
    print("[meta] Step 1: Retraining XGBoost models with multi:softprob...")
    import onnxruntime as ort
    import xgboost as xgb

    xgb_models: dict[str, Any] = {}
    for res in RESOLUTIONS:
        print(f"  {res} XGBoost softprob...")
        xgb_models[res] = retrain_xgb_softprob(res)
    print("[meta] XGBoost softprob retrain done.")

    # Load Transformer ONNX models
    print("[meta] Loading Transformer ONNX models...")
    tf_sessions: dict[str, ort.InferenceSession] = {}
    onnx_paths = {
        "M5": "data/models/transformer_v5_micro_barrier_v2.onnx",
        "M15": "data/models/transformer_v5_micro_barrier_m15.onnx",
        "H1": "data/models/transformer_v5_micro_barrier_h1.onnx",
        "H4": "data/models/transformer_v5_micro_barrier_h4.onnx",
    }
    for res in RESOLUTIONS:
        tf_sessions[res] = ort.InferenceSession(onnx_paths[res], providers=["CPUExecutionProvider"])
    print("[meta] All models loaded.")

    # ═══════════════════════════════════════════════════════════════════
    # Step 2: Load and prepare data for all resolutions
    # ═══════════════════════════════════════════════════════════════════
    print("\n[meta] Step 2: Loading and preparing data for all resolutions...")
    dfs: dict[str, pd.DataFrame] = {}
    scaled_arrs: dict[str, np.ndarray] = {}
    time_maps: dict[str, dict[int, int]] = {}
    sorted_keys: dict[str, list[int]] = {}

    for res in RESOLUTIONS:
        df, scaled, t2i = load_and_prepare(res)
        dfs[res] = df
        scaled_arrs[res] = scaled
        time_maps[res] = t2i
        sorted_keys[res] = sorted(t2i.keys())
        print(f"  {res}: {len(df)} bars, {len(t2i)} timestamps")

    # ═══════════════════════════════════════════════════════════════════
    # Step 3: Load M5 labels and sample entry points
    # ═══════════════════════════════════════════════════════════════════
    print("\n[meta] Step 3: Loading M5 labels...")
    label_dict, sorted_ts = load_m5_labels()
    print(
        f"  {sum(len(v) for v in label_dict.values())} labels across {len(label_dict)} timestamps"
    )

    # Sample entry points (stratified across time)
    total_entries = len(sorted_ts)
    n_samples = min(args.max_samples, total_entries) if args.max_samples > 0 else total_entries
    # Evenly sample to cover the full time range
    if n_samples < total_entries:
        step = total_entries / n_samples
        sampled_ts = [sorted_ts[int(i * step)] for i in range(n_samples)]
    else:
        sampled_ts = sorted_ts

    # Use last 20% for meta-learner validation
    n_val = int(n_samples * 0.2)
    train_ts = sampled_ts[:-n_val] if n_val > 0 else sampled_ts
    val_ts = sampled_ts[-n_val:] if n_val > 0 else []

    print(f"  Sampled {len(sampled_ts)} entry points ({len(train_ts)} train, {len(val_ts)} val)")

    # ═══════════════════════════════════════════════════════════════════
    # Step 4: Aligned inference
    # ═══════════════════════════════════════════════════════════════════
    print("\n[meta] Step 4: Running aligned inference...")

    def collect_features(ts_list: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """For each timestamp, collect 24-dim softmax feature + label."""
        features: list[np.ndarray] = []
        labels: list[int] = []

        for i, m5_ts in enumerate(ts_list):
            if i % 500 == 0:
                print(f"  ... {i}/{len(ts_list)}")

            # Get M5 bar index
            m5_idx = find_bar_binary(time_maps["M5"], sorted_keys["M5"], m5_ts)
            if m5_idx is None or m5_idx < SEQ_LEN - 1:
                continue

            softmax_vecs: list[np.ndarray] = []

            for res in RESOLUTIONS:
                # Find aligned bar in this resolution
                res_idx = find_bar_binary(time_maps[res], sorted_keys[res], m5_ts)
                if res_idx is None or res_idx < SEQ_LEN - 1:
                    # Not enough history — uniform prior
                    softmax_vecs.append(np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32))
                    softmax_vecs.append(np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32))
                    continue

                # Extract 32-bar window
                window = scaled_arrs[res][res_idx - SEQ_LEN + 1 : res_idx + 1]
                if window.shape != (SEQ_LEN, NUM_FEATURES):
                    softmax_vecs.append(np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32))
                    softmax_vecs.append(np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32))
                    continue

                # XGBoost softprob
                xgb_dmat = xgb.DMatrix(window.flatten().reshape(1, -1))
                xgb_probs = xgb_models[res].predict(xgb_dmat)[0]  # (3,) softmax
                softmax_vecs.append(xgb_probs.astype(np.float32))

                # Transformer ONNX
                ort_input = window.reshape(1, SEQ_LEN, NUM_FEATURES).astype(np.float32)
                ort_out = tf_sessions[res].run(None, {"input": ort_input})
                logits = ort_out[0][0]
                exp_l = np.exp(logits - np.max(logits))
                tf_probs = exp_l / exp_l.sum()
                softmax_vecs.append(tf_probs.astype(np.float32))

            meta_feat = np.concatenate(softmax_vecs)  # (24,)

            # Label from M5 labels
            for lab in label_dict.get(m5_ts, []):
                y_val = int(lab.get("label_int", 0))
                if y_val == -1:
                    y_val = 2
                elif y_val == 0:
                    y_val = 0
                else:
                    y_val = 1
                features.append(meta_feat.copy())
                labels.append(y_val)

        return np.array(features, dtype=np.float32), np.array(labels, dtype=np.int32)

    t0 = time.perf_counter()
    X_train, y_train = collect_features(train_ts)
    X_val, y_val = collect_features(val_ts) if val_ts else (np.zeros((0, 24)), np.zeros(0))
    elapsed = time.perf_counter() - t0
    print(f"  Inference done in {elapsed:.1f}s")
    print(f"  Train: {X_train.shape}, Val: {X_val.shape}")

    # ═══════════════════════════════════════════════════════════════════
    # Step 5: Train Meta-Learner
    # ═══════════════════════════════════════════════════════════════════
    print("\n[meta] Step 5: Training Meta-Learner XGBoost...")

    cls_counts = np.bincount(y_train, minlength=3)
    cls_weights = len(y_train) / (3 * cls_counts.clip(min=1))
    print(f"  Class dist: timeout={cls_counts[0]}, tp={cls_counts[1]}, sl={cls_counts[2]}")
    print(f"  Class weights: {cls_weights.round(4).tolist()}")

    dtrain = xgb.DMatrix(X_train, label=y_train)
    sample_weights = cls_weights[y_train]
    dtrain.set_weight(sample_weights)

    evals = [(dtrain, "train")]
    if len(X_val) > 0:
        dval = xgb.DMatrix(X_val, label=y_val)
        val_cls = np.bincount(y_val, minlength=3)
        val_weights = len(y_val) / (3 * val_cls.clip(min=1))
        dval.set_weight(val_weights[y_val])
        evals.append((dval, "eval"))

    meta_params = {
        "objective": "multi:softmax",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "max_depth": 3,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": 42,
        "n_jobs": -1,
    }

    meta_booster = xgb.train(
        meta_params,
        dtrain,
        num_boost_round=100,
        evals=evals,
        early_stopping_rounds=15,
        verbose_eval=False,
    )

    train_preds = meta_booster.predict(dtrain).astype(np.int32)
    train_acc = float((train_preds == y_train).mean())

    metrics: dict[str, Any] = {
        "n_estimators": meta_booster.num_boosted_rounds(),
        "train_accuracy": round(train_acc, 6),
    }
    for cls_idx, cls_name in enumerate(["timeout", "tp_hit", "sl_hit"]):
        mask = y_train == cls_idx
        if mask.sum() > 0:
            metrics[f"train_acc_{cls_name}"] = round(
                float((train_preds[mask] == y_train[mask]).mean()), 6
            )

    if len(X_val) > 0:
        dval_post = xgb.DMatrix(X_val, label=y_val)
        val_preds = meta_booster.predict(dval_post).astype(np.int32)
        val_acc = float((val_preds == y_val).mean())
        metrics["val_accuracy"] = round(val_acc, 6)
        for cls_idx, cls_name in enumerate(["timeout", "tp_hit", "sl_hit"]):
            mask = y_val == cls_idx
            if mask.sum() > 0:
                metrics[f"val_acc_{cls_name}"] = round(
                    float((val_preds[mask] == y_val[mask]).mean()), 6
                )

    # Feature importance (which models matter most)
    importance = meta_booster.get_score(importance_type="gain")
    # f0 = M5 XGB timeout, f1 = M5 XGB tp, f2 = M5 XGB sl, f3 = M5 TF timeout, ...
    feature_labels = []
    for res in RESOLUTIONS:
        for mtype in ["XGB", "TF"]:
            for cls_name in ["timeout", "tp", "sl"]:
                feature_labels.append(f"{res}_{mtype}_{cls_name}")
    top_feat = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]
    metrics["top_features"] = {feature_labels[int(k[1:])]: round(v, 1) for k, v in top_feat}

    # ═══════════════════════════════════════════════════════════════════
    # Step 6: Save
    # ═══════════════════════════════════════════════════════════════════
    args.output_model.parent.mkdir(parents=True, exist_ok=True)
    meta_booster.save_model(str(args.output_model))

    result = {
        "trainer": "meta_learner_builder",
        "trainer_version": "hmre-meta-v1.0.0",
        "completed_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        "exit_code": 0,
        "artifact_primary": str(args.output_model),
        "metrics": {"train_finished": True, **metrics},
        "data": {
            "train_samples": len(X_train),
            "val_samples": len(X_val),
            "input_dim": 24,
            "input_description": "8 models x 3-class softmax (XGB+TF x M5/M15/H1/H4)",
        },
        "risk_notes": [],
    }
    args.output_result.parent.mkdir(parents=True, exist_ok=True)
    args.output_result.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n[meta] Meta-learner saved: {args.output_model}")
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

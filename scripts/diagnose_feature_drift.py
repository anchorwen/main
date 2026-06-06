#!/usr/bin/env python3
"""BTC 特征漂移诊断：训练集 vs 实盘逐特征 KS-test"""

import json
import os

import numpy as np
from scipy import stats


def load_training_features(npz_path):
    """Load training test set features with feature names if available."""
    data = np.load(npz_path, allow_pickle=True)
    X = data["X_test"]
    feature_names = None
    meta_path = npz_path.replace(".npz", ".meta.json")
    meta2_path = os.path.join(os.path.dirname(npz_path), "meta_v2.json")
    for mp in [meta_path, meta2_path]:
        if os.path.exists(mp):
            with open(mp) as f:
                meta = json.load(f)
            feature_names = meta.get("feature_names", meta.get("features", None))
            break
    return X, feature_names


def load_live_features(jsonl_path, max_samples=2000):
    """Load live features from feature store JSONL."""
    features_list = []
    with open(jsonl_path) as f:
        for line in f:
            d = json.loads(line.strip())
            vals = d.get("values", d.get("features", {}))
            if vals:
                features_list.append(vals)
            if len(features_list) >= max_samples:
                break
    if not features_list:
        return None, None
    # Get feature names from first entry
    feature_names = list(features_list[0].keys())
    X = np.array([[float(d.get(fn, 0)) for fn in feature_names] for d in features_list])
    return X, feature_names


def compute_psi(expected, actual, bins=10):
    """Population Stability Index."""
    expected = np.array(expected)
    actual = np.array(actual)
    # Remove NaN/Inf
    mask_e = np.isfinite(expected)
    mask_a = np.isfinite(actual)
    expected = expected[mask_e]
    actual = actual[mask_a]
    if len(expected) < 10 or len(actual) < 10:
        return float("nan")

    # Create bins from combined data
    combined = np.concatenate([expected, actual])
    bin_edges = np.percentile(combined, np.linspace(0, 100, bins + 1))
    bin_edges = np.unique(bin_edges)  # Remove duplicate edges
    if len(bin_edges) < 3:
        return 0.0

    e_hist, _ = np.histogram(expected, bins=bin_edges, density=True)
    a_hist, _ = np.histogram(actual, bins=bin_edges, density=True)

    # Add small epsilon to avoid division by zero
    e_hist = e_hist + 1e-10
    a_hist = a_hist + 1e-10

    psi_values = (a_hist - e_hist) * np.log(a_hist / e_hist)
    return float(np.sum(psi_values))


def main():
    print("=" * 75)
    print("BTC 特征漂移诊断：训练集 vs 实盘特征分布对比")
    print("=" * 75)

    # Load training data
    train_path = "d:/future/data_btc/training/train_v1_backup.npz"
    print(f"\n加载训练集: {train_path}")
    X_train, train_names = load_training_features(train_path)
    print(f"  训练集: {X_train.shape}")
    if train_names:
        print(f"  特征名: {len(train_names)} 维")
        for i, name in enumerate(train_names[:5]):
            print(f"    [{i}] {name}")
    else:
        print("  (无特征名元数据)")

    # Load live data
    live_path = (
        "d:/future/data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl"
    )
    print(f"\n加载实盘特征: {live_path}")
    X_live, live_names = load_live_features(live_path)
    print(f"  实盘: {X_live.shape}")
    if live_names:
        print(f"  特征名: {len(live_names)} 维")
        for i, name in enumerate(live_names[:5]):
            print(f"    [{i}] {name}")

    # ── If feature names don't match, we compare available features ──
    # Map training features to their indices
    train_feat_map = {}
    if train_names:
        train_feat_map = {name: i for i, name in enumerate(train_names)}

    live_feat_map = {}
    if live_names:
        live_feat_map = {name: i for i, name in enumerate(live_names)}

    # Compare by training feature list (what the model expects)
    if train_names and live_names:
        common_names = sorted(set(train_names) & set(live_names))
        train_only = sorted(set(train_names) - set(live_names))
        live_only = sorted(set(live_names) - set(train_names))

        print("\n特征重叠分析:")
        print(f"  训练集特征数: {len(train_names)}")
        print(f"  实盘特征数:   {len(live_names)}")
        print(f"  共同特征:     {len(common_names)}")
        print(f"  仅训练有:     {len(train_only)}")
        if train_only:
            for n in train_only:
                print(f"    ⚠️  {n}")
        print(f"  仅实盘有:     {len(live_only)}")
        if live_only:
            for n in live_only[:5]:
                print(f"    ℹ️  {n}")

        # KS-test on common features
        print(f"\n{'='*75}")
        print("逐特征 KS-test + PSI 分析 (共同特征)")
        print(f"{'='*75}")
        print(
            f"{'Feature':35s} | {'KS-stat':>8s} | {'KS-pval':>8s} | {'PSI':>8s} | {'TrainMean':>10s} | {'LiveMean':>10s} | {'Drift':>6s}"
        )
        print("-" * 105)

        results = []
        for name in common_names:
            ti = train_feat_map[name]
            li = live_feat_map[name]
            train_col = X_train[:, ti]
            live_col = X_live[:, li]

            # Remove NaN/Inf
            train_col = train_col[np.isfinite(train_col)]
            live_col = live_col[np.isfinite(live_col)]

            if len(train_col) < 10 or len(live_col) < 10:
                continue

            # KS test
            ks_stat, ks_pval = stats.ks_2samp(train_col, live_col)

            # PSI
            psi = compute_psi(train_col, live_col)

            # Mean shift
            train_mean = np.mean(train_col)
            live_mean = np.mean(live_col)
            mean_shift = (
                abs(train_mean - live_mean) / (abs(train_mean) + 1e-10)
                if abs(train_mean) > 1e-6
                else abs(train_mean - live_mean)
            )

            drift_level = ""
            if ks_stat > 0.3 or (not np.isnan(psi) and psi > 0.25):
                drift_level = "🔴HIGH"
            elif ks_stat > 0.15 or (not np.isnan(psi) and psi > 0.10):
                drift_level = "🟡MED"
            else:
                drift_level = "✅LOW"

            results.append(
                {
                    "name": name,
                    "ks_stat": ks_stat,
                    "ks_pval": ks_pval,
                    "psi": psi,
                    "train_mean": train_mean,
                    "live_mean": live_mean,
                    "mean_shift": mean_shift,
                    "drift": drift_level,
                }
            )

        # Sort by KS stat descending
        results.sort(key=lambda x: x["ks_stat"], reverse=True)

        for r in results:
            psi_str = f"{r['psi']:.4f}" if not np.isnan(r["psi"]) else "N/A"
            print(
                f"{r['name']:35s} | {r['ks_stat']:8.4f} | {r['ks_pval']:8.4f} | {psi_str:>8s} | {r['train_mean']:10.4f} | {r['live_mean']:10.4f} | {r['drift']:>6s}"
            )

        # Summary
        high = sum(1 for r in results if "HIGH" in r["drift"])
        med = sum(1 for r in results if "MED" in r["drift"])
        low = sum(1 for r in results if "LOW" in r["drift"])

        print(f"\n{'='*75}")
        print(
            f"SUMMARY: {high} HIGH drift, {med} MEDIUM, {low} LOW (out of {len(results)} common features)"
        )
        print(f"{'='*75}")

        # Top drift features
        print("\n🔴 HIGH DRIFT features:")
        for r in results:
            if "HIGH" in r["drift"]:
                print(
                    f"  {r['name']:35s} KS={r['ks_stat']:.4f} PSI={r['psi']:.4f} train_mean={r['train_mean']:.4f} live_mean={r['live_mean']:.4f}"
                )

        # Features only in training (missing in live)
        if train_only:
            print("\n⚠️  FEATURES IN TRAINING BUT MISSING/DIFFERENT IN LIVE:")
            for n in train_only:
                print(f"  {n}")


if __name__ == "__main__":
    main()

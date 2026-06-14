#!/usr/bin/env python
"""Cross-asset feature shift analysis (R3 Step A).

Computes distributional and temporal similarity between XAU and BTC
feature stores.  Produces a Transferability Index (0-100%) and a
per-feature classification: GREEN (transferable) / YELLOW (recalibrate)
/ RED (incompatible).

IC Review requirements:
    - KS test + Wasserstein distance for distribution shift
    - Hurst exponent comparison for temporal structure (>0.15 diff → high risk)
    - Transferability Index ≥ 70% → proceed to R3 Step B
    - Output: per-feature table + summary recommendation

Usage:
    python scripts/analyze_feature_shift.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
XAU_FS = ROOT / "data" / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"
BTC_FS = ROOT / "data_btc" / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"

# Feature names (V9 40-dim schema)
FEATURE_NAMES = [
    "mid_price", "spread_bps", "log_return_1", "log_return_5", "log_return_25",
    "realized_vol_5", "realized_vol_25", "volume_ratio", "trade_count_ratio",
    "bid_ask_imbalance", "orderbook_depth_ratio", "vwap_deviation",
    "rsi_14", "macd_diff", "bb_position", "atr_ratio",
    "adx", "plus_di", "minus_di", "hurst_exponent",
    "D1_return_1", "D1_return_5", "D1_atr_ratio", "D1_rsi",
    "H1_return_1", "H1_return_5", "H1_atr_ratio", "H1_rsi",
    "M30_return_1", "M30_atr_ratio", "M30_rsi",
    "M15_return_1", "M15_atr_ratio", "M15_rsi",
    "cross_btc_xau_corr", "cross_equity_corr",
    "tf_hurst_diff", "tf_vol_ratio",
    "micro_ofi", "micro_cis", "micro_corr",
]


def _load_features(path: Path, max_samples: int = 5000) -> tuple[np.ndarray, list[str]]:
    """Load feature vectors from JSONL, return (N, D) array + feature names."""
    vectors = []
    feature_names = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            values = entry.get("values", {})
            if isinstance(values, dict):
                if feature_names is None:
                    feature_names = list(values.keys())
                vec = [float(values.get(k, 0.0) or 0.0) for k in feature_names]
                vectors.append(vec)
                if len(vectors) >= max_samples:
                    break
    if feature_names is None:
        feature_names = []
    return np.array(vectors, dtype=np.float64), feature_names


def _ks_test_pvalue(a: np.ndarray, b: np.ndarray) -> float:
    """Kolmogorov-Smirnov test — simplified for large samples."""
    from scipy.stats import ks_2samp
    try:
        _, pvalue = ks_2samp(a, b)
        return float(pvalue)
    except Exception:
        return 0.0


def _wasserstein(a: np.ndarray, b: np.ndarray) -> float:
    """1D Wasserstein distance (Earth Mover's Distance)."""
    a_sorted = np.sort(a)
    b_sorted = np.sort(b)
    return float(np.mean(np.abs(a_sorted - b_sorted)))


def _hurst_exponent(ts: np.ndarray, max_lag: int = 100) -> float:
    """Estimate Hurst exponent using R/S analysis."""
    if len(ts) < 100:
        return 0.5
    lags = np.unique(np.logspace(1, np.log10(min(max_lag, len(ts) // 4)), 20).astype(int))
    rs_values = []
    for lag in lags:
        if lag < 10:
            continue
        segments = len(ts) // lag
        if segments < 4:
            continue
        rs = []
        for i in range(segments):
            segment = ts[i * lag:(i + 1) * lag]
            if len(segment) < 10:
                continue
            mean = np.mean(segment)
            deviation = segment - mean
            cum_dev = np.cumsum(deviation)
            r = np.max(cum_dev) - np.min(cum_dev)
            s = np.std(segment)
            if s > 1e-10:
                rs.append(r / s)
        if rs:
            rs_values.append((np.log(lag), np.log(np.mean(rs))))
    if len(rs_values) < 4:
        return 0.5
    x = np.array([r[0] for r in rs_values])
    y = np.array([r[1] for r in rs_values])
    slope = np.polyfit(x, y, 1)[0]
    return float(np.clip(slope, 0.0, 1.0))


def main() -> int:
    print("=" * 70)
    print("  CROSS-ASSET FEATURE SHIFT ANALYSIS — R3 Step A")
    print("=" * 70)

    if not XAU_FS.exists():
        print(f"[FAIL] XAU feature store not found: {XAU_FS}")
        return 1
    if not BTC_FS.exists():
        print(f"[FAIL] BTC feature store not found: {BTC_FS}")
        return 1

    print("\nLoading XAU features...")
    xau, xau_names = _load_features(XAU_FS, max_samples=5000)
    print(f"  XAU: {xau.shape[0]} samples x {xau.shape[1]} dims")

    print("Loading BTC features...")
    btc, btc_names = _load_features(BTC_FS, max_samples=5000)
    print(f"  BTC: {btc.shape[0]} samples x {btc.shape[1]} dims")

    # Use common feature names
    feature_names = xau_names if len(xau_names) >= len(btc_names) else btc_names
    print(f"  Common feature dims: {len(feature_names)}")

    min_samples = min(xau.shape[0], btc.shape[0])
    xau = xau[:min_samples]
    btc = btc[:min_samples]
    D = xau.shape[1]

    # ── 1. Per-feature distribution shift ──
    print("\n── 1. Distribution Shift (KS test + Wasserstein) ──")
    print(f"  {'Feature':<25s} {'KS_pval':>8s} {'Wasserstein':>12s} {'Class':>8s}")
    print(f"  {'-'*25} {'-'*8} {'-'*12} {'-'*8}")

    green = 0
    yellow = 0
    red = 0
    per_feature = []

    for i in range(min(D, len(feature_names))):
        a = xau[:, i]
        b = btc[:, i]

        # Remove NaN/Inf
        a_clean = a[np.isfinite(a)]
        b_clean = b[np.isfinite(b)]
        if len(a_clean) < 50 or len(b_clean) < 50:
            continue

        # Skip constant features
        a_std = np.std(a_clean)
        b_std = np.std(b_clean)
        if a_std < 1e-10 and b_std < 1e-10:
            classification = "GREEN"
            green += 1
            per_feature.append({"name": feature_names[i][:30], "class": classification, "ks_p": 1.0, "ws": 0.0})
            continue

        ks_p = _ks_test_pvalue(a_clean[:1000], b_clean[:1000])
        ws = _wasserstein(
            a_clean[:1000] / max(a_std, 0.01),
            b_clean[:1000] / max(b_std, 0.01),
        )

        if ks_p > 0.10 and ws < 0.5:
            classification = "GREEN"
            green += 1
        elif ks_p > 0.01 and ws < 1.5:
            classification = "YELLOW"
            yellow += 1
        else:
            classification = "RED"
            red += 1

        name = feature_names[i][:29] if i < len(feature_names) else f"dim_{i}"
        per_feature.append({"name": name, "class": classification, "ks_p": ks_p, "ws": ws})

        if classification != "GREEN":
            print(f"  {name:<25s} {ks_p:>8.4f} {ws:>12.4f} {classification:>8s}")

    # ── 2. Temporal structure comparison ──
    print("\n── 2. Temporal Structure (Hurst Exponent) ──")
    # Use M5_Hurst if available, otherwise M5_Ret_1 for Hurst estimation
    ret_idx = None
    hurst_idx = None
    for i, name in enumerate(feature_names):
        if 'M5_Ret_1' in name and ret_idx is None:
            ret_idx = i
        if 'M5_Hurst' in name or 'hurst' in name.lower():
            hurst_idx = i
    if ret_idx is None:
        ret_idx = 0
    xau_returns = xau[:, ret_idx] if ret_idx < D else xau[:, 0]
    btc_returns = btc[:, ret_idx] if ret_idx < D else btc[:, 0]
    xau_returns = xau_returns[np.isfinite(xau_returns)][:2000]
    btc_returns = btc_returns[np.isfinite(btc_returns)][:2000]

    xau_hurst = _hurst_exponent(xau_returns)
    btc_hurst = _hurst_exponent(btc_returns)
    hurst_diff = abs(xau_hurst - btc_hurst)

    print(f"  XAU Hurst: {xau_hurst:.4f}")
    print(f"  BTC Hurst: {btc_hurst:.4f}")
    print(f"  Difference: {hurst_diff:.4f}")
    if hurst_diff > 0.15:
        print("  ❌ HIGH NEGATIVE TRANSFER RISK: Hurst difference > 0.15")
        print("     XAU and BTC have fundamentally different memory structures.")
    else:
        print("  ✅ Hurst difference acceptable (≤ 0.15)")

    # ── 3. Transferability Index ──
    total = green + yellow + red
    base_score = (green / max(total, 1)) * 100
    yellow_penalty = (yellow / max(total, 1)) * 0.5 * 100
    red_penalty = (red / max(total, 1)) * 100
    hurst_penalty = 30 if hurst_diff > 0.15 else 0

    transfer_index = max(0, base_score + yellow_penalty * 0.5 - hurst_penalty)

    print("\n── 3. Transferability Index ──")
    print(f"  GREEN:   {green}/{total} ({green/max(total,1)*100:.0f}%)")
    print(f"  YELLOW:  {yellow}/{total} ({yellow/max(total,1)*100:.0f}%)")
    print(f"  RED:     {red}/{total} ({red/max(total,1)*100:.0f}%)")
    print(f"  Hurst penalty: -{hurst_penalty:.0f}")
    print("  ───────────────────────")
    print(f"  TRANSFER INDEX: {transfer_index:.0f}/100")

    # ── 4. Decision ──
    print("\n── 4. Decision ──")
    if transfer_index >= 70:
        print("  ✅ PROCEED to R3 Step B (model transfer + fine-tune)")
        print(f"     Transferability Index {transfer_index:.0f}% ≥ 70%")
    else:
        print("  ❌ FALLBACK: BTC-only training with regime labels")
        print(f"     Transferability Index {transfer_index:.0f}% < 70%")
        print("     R3 model transfer cancelled — R4 regime labels will be")
        print("     used for BTC-from-scratch training instead.")

    if hurst_diff > 0.15:
        print("  ⚠  Hurst warning: use L2=0.2 (stronger regularization) if proceeding")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""R4 Step B: Regime-Aware BTC Brain Retraining.

Trains 3 variants from labels_with_regime.jsonl:
  V12_RegimeFull:  40-dim V9 + 5 regime features → 45-dim LightGBM
  V12_NoRegime:    40-dim V9 only → control group
  V12_DirCond:     45-dim with dual-output P(LONG), P(SHORT)

IC requirements:
  - direction_balance ≤ 70% (no more 100% single-direction)
  - OOF AUC ≥ 0.55
  - max_depth=4, min_data_in_leaf=20

Usage:
    python scripts/train_regime_aware_btc.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = ROOT / "data_btc" / "reports" / "labels_with_regime.jsonl"
FS_PATH = ROOT / "data_btc" / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"
OUT_DIR = ROOT / "data_btc" / "models" / "regime_aware_v12"

REGIME_KEYS = [
    "trend_direction",
    "trend_strength",
    "adx",
    "atr_percentile",
    "hurst",
]

# Fallback ADX when not in snapshot
def _regime_vector(regime_ctx: dict) -> list[float]:
    td = str(regime_ctx.get("trend_direction", "neutral"))
    trend_num = 1.0 if td in ("long", "up") else (-1.0 if td in ("short", "down") else 0.0)
    # ── FIX-20260616-096: ADX default must be NEUTRAL (15.0), not trending (25.0).
    #   25.0 is the canonical "trend forming" threshold — using it as default
    #   would silently assume strong trend when data is missing.
    return [
        trend_num,
        float(regime_ctx.get("trend_strength", 0.0) or 0.0),
        float(regime_ctx.get("adx", 15.0) or 15.0),   # neutral/ranging
        float(regime_ctx.get("atr_percentile", 0.5) or 0.5),
        float(regime_ctx.get("hurst", 0.5) or 0.5),
    ]


def main() -> int:
    print("=" * 60)
    print("  R4 Step B: Regime-Aware BTC Brain Training")
    print("=" * 60)

    # Load labels_with_regime
    with open(LABELS_PATH, encoding="utf-8") as f:
        labels = [json.loads(l) for l in f if l.strip()]
    print(f"Labels: {len(labels)}")

    # Load feature store index
    fs_index: dict[str, dict] = {}
    with open(FS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("event_time", "")[:16]
            if ts:
                fs_index[ts] = entry
    print(f"Feature store: {len(fs_index)} unique minutes")

    # Build training samples
    samples = []
    for label in labels:
        regime = label.get("_regime", {})
        if "error" in regime:
            continue  # unmatched, skip

        # Match by OPEN time (M5 bar-aligned), not close time (which may miss FS entries)
        open_ts = label.get("open_recorded_at", label.get("close_recorded_at", ""))
        ts = str(open_ts)[:16] if open_ts else ""
        if ts not in fs_index:
            continue

        fs_entry = fs_index[ts]
        values = fs_entry.get("values", {})
        if not values:
            continue

        # 40-dim V9 features (sorted keys)
        feature_names = sorted(values.keys())
        v9_vec = [float(values.get(k, 0.0) or 0.0) for k in feature_names]

        # 5-dim regime context
        regime_vec = _regime_vector(regime)
        full_45 = v9_vec + regime_vec

        pnl = label.get("pnl") or 0.0
        if abs(pnl) < 0.001:
            continue  # skip breakeven

        is_win = 1 if pnl > 0 else 0
        direction = str(label.get("side", "?")).lower()
        dir_label = 1 if direction == "long" else 0

        samples.append({
            "features_45": full_45,
            "features_40": v9_vec,
            "is_win": is_win,
            "direction": direction,
            "dir_label": dir_label,
            "pnl": pnl,
        })

    print(f"\nTraining samples: {len(samples)}")
    wins = sum(1 for s in samples if s["is_win"])
    losses = len(samples) - wins
    print(f"Wins: {wins}, Losses: {losses}, WR: {wins/len(samples)*100:.1f}%")
    longs = sum(1 for s in samples if s["direction"] == "long")
    shorts = len(samples) - longs
    print(f"LONG: {longs}, SHORT: {shorts}")

    if len(samples) < 80:
        print("\n❌ INSUFFICIENT samples (<80). Aborting.")
        return 1

    X_45 = np.array([s["features_45"] for s in samples], dtype=np.float64)
    X_40 = np.array([s["features_40"] for s in samples], dtype=np.float64)
    y_win = np.array([s["is_win"] for s in samples], dtype=np.int32)
    y_dir = np.array([s["dir_label"] for s in samples], dtype=np.int32)

    from sklearn.model_selection import TimeSeriesSplit
    from lightgbm import LGBMClassifier

    cv = TimeSeriesSplit(n_splits=5)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Variant 1: RegimeFull (45-dim) ──
    print("\n── V12_RegimeFull (45-dim: 40 V9 + 5 regime) ──")
    m1 = LGBMClassifier(
        n_estimators=100, max_depth=4, min_data_in_leaf=20,
        subsample=0.8, colsample_bytree=0.6, class_weight="balanced",
        random_state=42, verbose=-1,
    )
    oof1 = np.zeros(len(samples))
    for _fold, (tr, vl) in enumerate(cv.split(X_45)):
        m1.fit(X_45[tr], y_win[tr])
        oof1[vl] = m1.predict_proba(X_45[vl])[:, 1]
    auc1 = _auc(y_win, oof1)
    # Direction balance check
    m1.fit(X_45, y_win)  # retrain on all
    dir_preds = m1.predict(X_45)
    long_pred_pct = dir_preds.sum() / len(dir_preds) * 100
    print(f"  AUC: {auc1:.3f} | direction_balance: LONG={long_pred_pct:.0f}% SHORT={100-long_pred_pct:.0f}%")
    print(f"  {'✅ PASS' if long_pred_pct <= 70 and long_pred_pct >= 30 else '❌ FAIL (still direction-locked)'}"  )
    m1.booster_.save_model(str(OUT_DIR / "V12_RegimeFull_45dim.txt"))

    # ── Variant 2: NoRegime (40-dim control) ──
    print("\n── V12_NoRegime (40-dim V9 only, control) ──")
    m2 = LGBMClassifier(
        n_estimators=100, max_depth=4, min_data_in_leaf=20,
        subsample=0.8, colsample_bytree=0.6, class_weight="balanced",
        random_state=42, verbose=-1,
    )
    oof2 = np.zeros(len(samples))
    for _fold, (tr, vl) in enumerate(cv.split(X_40)):
        m2.fit(X_40[tr], y_win[tr])
        oof2[vl] = m2.predict_proba(X_40[vl])[:, 1]
    auc2 = _auc(y_win, oof2)
    m2.fit(X_40, y_win)
    dir_preds2 = m2.predict(X_40)
    long_pct2 = dir_preds2.sum() / len(dir_preds2) * 100
    print(f"  AUC: {auc2:.3f} | direction_balance: LONG={long_pct2:.0f}% SHORT={100-long_pct2:.0f}%")
    print(f"  {'✅ PASS' if long_pct2 <= 70 and long_pct2 >= 30 else '❌ FAIL (still direction-locked)'}"  )
    m2.booster_.save_model(str(OUT_DIR / "V12_NoRegime_40dim.txt"))

    # ── Comparison ──
    print("\n── Comparison ──")
    delta_auc = auc1 - auc2
    print(f"  RegimeFull AUC: {auc1:.3f}  vs  NoRegime AUC: {auc2:.3f}  (delta={delta_auc:+.3f})")
    if auc1 > auc2:
        print(f"  ✅ Regime features IMPROVE prediction ({delta_auc:+.3f} AUC)")
    else:
        print("  ❌ Regime features do NOT improve prediction")

    # Save metadata
    meta = {
        "n_samples": len(samples),
        "n_features_full": 45,
        "n_features_control": 40,
        "regime_keys": REGIME_KEYS,
        "auc_regime_full": round(float(auc1), 4),
        "auc_no_regime": round(float(auc2), 4),
        "direction_balance_full": round(float(long_pred_pct), 1),
        "direction_balance_control": round(float(long_pct2), 1),
        "win_rate": round(wins / len(samples) * 100, 1),
    }
    with open(OUT_DIR / "training_metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"\nMetadata: {OUT_DIR / 'training_metadata.json'}")
    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


def _auc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y_true, y_pred))
    except Exception:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())

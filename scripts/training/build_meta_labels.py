#!/usr/bin/env python
"""Build meta-label training dataset for two-stage signal filtering.

Stage 1: Heuristic directional model (RSI + MACD + H1 trend)
Stage 2 target: P(TP hit | direction, features) — binary classifier

The meta model learns to filter Stage 1 signals: given a directional
prediction, will the trade hit TP before SL?

Usage:
  python scripts/training/build_meta_labels.py \
    --dataset data/training/calibrated_12bar_v3/train.npz \
    --output data/training/meta_12bar_v1/train.npz
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# ── Stage 1: Heuristic directional model ────────────────────────────────────


def _feature_index(feature_names: list[str], name: str) -> int:
    try:
        return feature_names.index(name)
    except ValueError:
        return -1


def heuristic_direction(
    X: np.ndarray,
    feature_names: list[str],
    *,
    rsi_oversold: float = 40.0,
    rsi_overbought: float = 60.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate directional signals from multi-timeframe heuristics.

    Returns (direction, confidence) where:
      direction: 1=long, -1=short, 0=neutral
      confidence: [0, 1] strength of the signal
    """
    n = len(X)
    direction = np.zeros(n, dtype=np.int8)
    confidence = np.zeros(n, dtype=np.float32)

    idx_m5_rsi = _feature_index(feature_names, "M5_RSI_14")
    idx_m5_macd = _feature_index(feature_names, "M5_MACD")
    idx_m5_ret = _feature_index(feature_names, "M5_Ret_1")
    idx_h1_ret = _feature_index(feature_names, "H1_Ret_1")
    idx_h1_macd = _feature_index(feature_names, "H1_MACD")

    for i in range(n):
        score = 0.0

        # RSI component: oversold → long bias, overbought → short bias
        if idx_m5_rsi >= 0:
            rsi = X[i, idx_m5_rsi]
            if rsi < rsi_oversold:
                score += (rsi_oversold - rsi) / rsi_oversold  # 0..1
            elif rsi > rsi_overbought:
                score -= (rsi - rsi_overbought) / (100.0 - rsi_overbought)  # 0..-1

        # MACD component: positive → long, negative → short
        if idx_m5_macd >= 0:
            macd = X[i, idx_m5_macd]
            score += np.clip(macd * 0.5, -1.0, 1.0)

        # H1 trend confirmation
        if idx_h1_ret >= 0:
            h1_ret = X[i, idx_h1_ret]
            score += np.clip(h1_ret * 2.0, -0.5, 0.5)

        if idx_h1_macd >= 0:
            h1_macd = X[i, idx_h1_macd]
            score += np.clip(h1_macd * 0.3, -0.5, 0.5)

        # M5 momentum
        if idx_m5_ret >= 0:
            m5_ret = X[i, idx_m5_ret]
            score += np.clip(m5_ret * 5.0, -0.3, 0.3)

        conf = min(abs(score) / 2.0, 1.0)

        if score > 0.5:
            direction[i] = 1
            confidence[i] = conf
        elif score < -0.5:
            direction[i] = -1
            confidence[i] = conf
        # else: neutral (0)

    return direction, confidence


# ── Meta feature builder ────────────────────────────────────────────────────


def _atr_percentile(atr_values: np.ndarray, window: int = 500) -> np.ndarray:
    """Compute causal rolling-window percentile rank of each ATR value.

    For each index i, computes the percentile of atr_values[i] within
    atr_values[max(0, i-window+1):i+1] — only past data, no future leak.
    """
    n = len(atr_values)
    result = np.empty(n, dtype=np.float64)
    for i in range(n):
        start = max(0, i - window + 1)
        win = atr_values[start : i + 1]
        result[i] = float(np.searchsorted(np.sort(win), atr_values[i], side="right")) / float(
            len(win)
        )
    return result


def build_meta_dataset(
    X: np.ndarray,
    y: np.ndarray,
    pnl: np.ndarray,
    side: np.ndarray,
    feature_names: list[str],
    *,
    direction: np.ndarray | None = None,
    confidence: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray]:
    """Build meta-label training data from a barrier dataset.

    For each non-neutral Stage 1 signal, constructs a meta feature vector
    and labels it 1 if the signal would have hit TP, 0 if SL.

    Returns (meta_X, meta_y, meta_feature_names, meta_weights).
    meta_weights: asymmetric — SL losses (3.0 ATR) ×2.0, TP wins (1.5 ATR) ×1.0.
    """
    if direction is None or confidence is None:
        direction, confidence = heuristic_direction(X, feature_names)

    # Pre-compute ATR percentile for context
    idx_m5_atr = _feature_index(feature_names, "M5_ATR_14")
    if idx_m5_atr >= 0:
        atr_percentile = _atr_percentile(X[:, idx_m5_atr])
    else:
        atr_percentile = np.full(len(X), 0.5)

    idx_m5_rsi = _feature_index(feature_names, "M5_RSI_14")
    idx_m5_macd = _feature_index(feature_names, "M5_MACD")
    idx_h1_macd = _feature_index(feature_names, "H1_MACD")
    idx_h1_ret = _feature_index(feature_names, "H1_Ret_1")
    idx_m5_vol_z = _feature_index(feature_names, "M5_Vol_ZScore")
    idx_m5_ou = _feature_index(feature_names, "M5_OU_Theta")
    idx_m5_hurst = _feature_index(feature_names, "M5_Hurst")
    _feature_index(feature_names, "H1_ATR_14")

    meta_feature_names = [
        # Stage 1 output
        "s1_direction",  # 1=long, -1=short
        "s1_confidence",  # [0, 1]
        # Core RSI / MACD
        "m5_rsi",
        "m5_macd",
        # H1 context
        "h1_ret",
        "h1_macd",
        # Volatility / microstructure
        "m5_vol_zscore",
        "m5_ou_theta",
        "m5_hurst",
        "atr_percentile",
        # Derived features
        "rsi_distance",  # distance from 50 (absolute)
        "h1_trend_strength",  # abs(h1_ret) / atr_percentile
        # Interaction features
        "direction_x_rsi",  # direction * (rsi - 50)
        "direction_x_macd",  # direction * macd
        "direction_x_h1",  # direction * h1_ret
    ]

    meta_X: list[list[float]] = []
    meta_y: list[int] = []
    meta_weights: list[float] = []

    # Group rows by bar (same feature vector = same bar, different sides)
    from collections import defaultdict

    bar_groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(X)):
        key = hash(X[i].tobytes())
        bar_groups[key].append(i)

    for indices in bar_groups.values():
        i0 = indices[0]
        d = int(direction[i0])
        if d == 0:
            continue  # neutral signal, skip entire bar

        conf = float(confidence[i0])

        # Find the row whose side matches the heuristic direction
        matching_idx = -1
        for idx in indices:
            actual_side = int(side[idx])
            if (d == 1 and actual_side == 1) or (d == -1 and actual_side == 0):
                matching_idx = idx
                break

        if matching_idx < 0:
            continue  # no matching side row (shouldn't happen)

        # Did this directional signal win?
        signal_won = float(pnl[matching_idx]) > 0

        # Build meta feature vector from the matching-side row
        rsi = X[matching_idx, idx_m5_rsi] if idx_m5_rsi >= 0 else 50.0
        macd = X[matching_idx, idx_m5_macd] if idx_m5_macd >= 0 else 0.0
        h1_ret = X[matching_idx, idx_h1_ret] if idx_h1_ret >= 0 else 0.0
        h1_macd = X[matching_idx, idx_h1_macd] if idx_h1_macd >= 0 else 0.0
        vol_z = X[matching_idx, idx_m5_vol_z] if idx_m5_vol_z >= 0 else 0.0
        ou = X[matching_idx, idx_m5_ou] if idx_m5_ou >= 0 else 0.0
        hurst = X[matching_idx, idx_m5_hurst] if idx_m5_hurst >= 0 else 0.5
        atr_pct = float(atr_percentile[matching_idx])

        rsi_dist = abs(rsi - 50.0)
        h1_trend = abs(h1_ret) / max(atr_pct, 0.01)

        row = [
            float(d),
            conf,
            rsi,
            macd,
            h1_ret,
            h1_macd,
            vol_z,
            ou,
            hurst,
            atr_pct,
            rsi_dist,
            h1_trend,
            float(d) * (rsi - 50.0),
            float(d) * macd,
            float(d) * h1_ret,
        ]
        meta_X.append(row)
        meta_y.append(1 if signal_won else 0)
        # Asymmetric weight: SL loss (3.0 ATR) is 2x costlier than TP win (1.5 ATR)
        meta_weights.append(2.0 if not signal_won else 1.0)

    return (
        np.array(meta_X, dtype=np.float64),
        np.array(meta_y, dtype=np.int32),
        meta_feature_names,
        np.array(meta_weights, dtype=np.float64),
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_meta_labels")
    p.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to barrier dataset .npz (must have side field)",
    )
    p.add_argument("--output", type=Path, required=True, help="Output path for meta dataset .npz")
    p.add_argument(
        "--val-split", type=float, default=0.2, help="Fraction of samples for validation"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    ds_path = Path(args.dataset)
    if not ds_path.exists():
        print(f"[ERROR] Dataset not found: {ds_path}")
        return 1

    print(f"[1/3] Loading dataset: {ds_path}")
    data = np.load(ds_path)
    X = data["X"]
    y = data["y"]
    pnl = data["pnl"]
    side = data.get("side")
    feature_names = [str(n) for n in data["feature_names"]]

    if side is None:
        print(
            "[ERROR] Dataset missing 'side' field. Rebuild with build_calibrated_dataset.py (v2 schema)."
        )
        return 1

    print(f"       Samples: {len(X)}, Features: {len(feature_names)}")
    print(f"       Pos rate: {(y==1).mean():.1%}")
    print(f"       Long rate: {(side==1).mean():.1%}")

    # ── Generate Stage 1 signals + build meta dataset ──
    print("[2/3] Generating Stage 1 signals and building meta features...")
    direction, confidence = heuristic_direction(X, feature_names)
    n_long = int(np.sum(direction == 1))
    n_short = int(np.sum(direction == -1))
    n_neutral = int(np.sum(direction == 0))
    print(
        f"       Stage 1 signals: {n_long} long, {n_short} short, {n_neutral} neutral "
        f"({n_long + n_short} tradable)"
    )

    meta_X, meta_y, meta_names, meta_weights = build_meta_dataset(
        X,
        y,
        pnl,
        side,
        feature_names,
        direction=direction,
        confidence=confidence,
    )
    win_rate = meta_y.mean()
    avg_weight = meta_weights.mean()
    print(f"       Meta samples: {len(meta_X)} ({win_rate:.1%} wins, avg_weight={avg_weight:.3f})")
    print(f"       Meta features: {len(meta_names)}")

    if len(meta_X) < 500:
        print(f"[ERROR] Only {len(meta_X)} meta samples — need at least 500")
        return 1

    # ── Split and save ──
    print("[3/3] Splitting and saving...")
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    split_idx = int(len(meta_X) * (1 - args.val_split))
    meta_X_train, meta_X_val = meta_X[:split_idx], meta_X[split_idx:]
    meta_y_train, meta_y_val = meta_y[:split_idx], meta_y[split_idx:]
    meta_w_train, meta_w_val = meta_weights[:split_idx], meta_weights[split_idx:]

    np.savez_compressed(
        out_path,
        X_train=meta_X_train,
        y_train=meta_y_train,
        w_train=meta_w_train,
        X_val=meta_X_val,
        y_val=meta_y_val,
        w_val=meta_w_val,
        feature_names=np.array(meta_names),
        schema="meta_label_v2",
    )

    train_wr = meta_y_train.mean()
    val_wr = meta_y_val.mean()
    print(f"       Train: {len(meta_X_train)} samples ({train_wr:.1%} wins)")
    print(f"       Val:   {len(meta_X_val)} samples ({val_wr:.1%} wins)")
    print(f"       Saved to: {out_path}")

    meta = {
        "schema_version": "meta_label_dataset.v1",
        "n_samples": len(meta_X),
        "n_features": len(meta_names),
        "feature_names": meta_names,
        "train_samples": len(meta_X_train),
        "val_samples": len(meta_X_val),
        "win_rate": round(float(win_rate), 4),
        "stage1_type": "heuristic_rsi_macd",
        "stage1_signal_rate": round((n_long + n_short) / len(X), 4),
        "source_dataset": str(ds_path),
    }
    meta_path = out_path.with_suffix(".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

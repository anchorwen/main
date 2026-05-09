"""Build a supervised training dataset from live trade outcomes.

Matches live_trade_journal.jsonl entries to feature_store records at entry time,
producing labels: 1 = tp_hit_first (win), 0 = sl_hit_first (loss).

Output: data/training/live_labeled_dataset.npz (X, y, meta)
Also: data/training/live_labeled_dataset.parquet (for LightGBM/XGBoost)
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_trade_labels(journal_path: Path) -> list[dict[str, Any]]:
    """Extract labeled trades (SL-hit / TP-hit) from the live journal."""
    with open(journal_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]

    opens = {e.get("message_id"): e for e in lines if e.get("action") == "open"}
    closes = [e for e in lines if e.get("action") == "close" and e.get("open_message_id")]

    trades = []
    for c in closes:
        o = opens.get(c.get("open_message_id"))
        if not o:
            continue
        label = c.get("label")
        if label not in ("sl_hit_first", "tp_hit_first"):
            continue
        ts_str = o.get("recorded_at", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue

        trades.append(
            {
                "entry_utc": ts,
                "side": c.get("side") or o.get("side", ""),
                "is_win": label == "tp_hit_first",
                "label": label,
                "pnl": c.get("pnl", 0) or 0,
            }
        )
    return trades


def match_features(
    trades: list[dict],
    feature_store_path: Path,
    window_minutes: int = 10,
) -> list[dict]:
    """Match each trade to the nearest feature vector BEFORE entry time."""
    # Collect features in the window
    feature_buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        t_naive = t["entry_utc"]
        key = t_naive.isoformat()[:16]  # minute-level bucket
        feature_buckets[key].append(t)

    matched = []

    with open(feature_store_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            ts_raw = str(rec.get("event_time", ""))
            if not ts_raw:
                continue
            try:
                feat_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, TypeError):
                continue

            values = rec.get("values", {})
            if not values or len(values) < 10:
                continue

            # Check all trade buckets within window
            for bucket_key, bucket_trades in feature_buckets.items():
                bucket_ts = datetime.fromisoformat(bucket_key)
                delta = (bucket_ts - feat_ts).total_seconds()
                if 0 <= delta < window_minutes * 60:
                    # Feature is before trade entry, within window
                    for t in bucket_trades:
                        vec = np.array(
                            [float(values.get(k, 0.0)) for k in sorted(values.keys())],
                            dtype=np.float32,
                        )
                        if len(vec) < 10:
                            continue
                        matched.append(
                            {
                                "feature_time": feat_ts.isoformat(),
                                "trade_side": t["side"],
                                "is_win": t["is_win"],
                                "pnl": t["pnl"],
                                "feature_vector": vec,
                                "feature_keys": sorted(values.keys()),
                            }
                        )
                    break  # Only match once per feature record

    return matched


def build_dataset(matched: list[dict], output_dir: Path) -> dict:
    """Build NPZ + Parquet datasets from matched features."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if not matched:
        print("[WARN] No matched records — dataset empty")
        return {"samples": 0, "wins": 0, "losses": 0}

    feature_keys = matched[0]["feature_keys"]
    X = np.array([m["feature_vector"] for m in matched], dtype=np.float32)
    y = np.array([1 if m["is_win"] else 0 for m in matched], dtype=np.int32)
    pnl = np.array([m["pnl"] for m in matched], dtype=np.float32)
    sides = np.array([1 if m["trade_side"] == "long" else 0 for m in matched], dtype=np.int32)

    n_samples, n_features = X.shape
    n_wins = int(y.sum())
    n_losses = n_samples - n_wins

    # NPZ (for MLP/DeepResMLP)
    np.savez_compressed(
        output_dir / "live_labeled_dataset.npz",
        X=X,
        y=y,
        pnl=pnl,
        sides=sides,
        feature_keys=np.array(feature_keys, dtype="S"),
    )

    # Parquet (for LightGBM/XGBoost)
    try:
        import pandas as pd

        df = pd.DataFrame(X, columns=feature_keys)
        df["label"] = y
        df["pnl"] = pnl
        df["side_long"] = sides
        df.to_parquet(output_dir / "live_labeled_dataset.parquet", index=False)
    except ImportError:
        print("[WARN] pandas not available, skipping Parquet export")

    # Metadata
    meta = {
        "n_samples": n_samples,
        "n_features": n_features,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": round(n_wins / n_samples, 4) if n_samples else 0,
        "feature_keys": feature_keys,
        "output_npz": str(output_dir / "live_labeled_dataset.npz"),
        "output_parquet": str(output_dir / "live_labeled_dataset.parquet"),
    }

    with open(output_dir / "live_labeled_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Dataset built: {n_samples} samples x {n_features} features")
    print(f"  Wins: {n_wins}, Losses: {n_losses}")
    print(f"  Win rate: {meta['win_rate']:.1%}")

    return meta


def main():
    p = argparse.ArgumentParser(description="Build training dataset from live trades")
    p.add_argument(
        "--journal",
        default="data/live_trade_journal.jsonl",
        help="Path to live trade journal",
    )
    p.add_argument(
        "--feature-store",
        default="data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl",
        help="Path to feature store JSONL",
    )
    p.add_argument(
        "--output-dir",
        default="data/training/live_labeled",
        help="Output directory for dataset",
    )
    p.add_argument(
        "--window-minutes",
        type=int,
        default=10,
        help="Max minutes before trade entry for feature matching",
    )
    args = p.parse_args()

    root = PROJECT_ROOT
    journal_path = root / args.journal
    feature_path = root / args.feature_store
    output_dir = root / args.output_dir

    if not journal_path.exists():
        print(f"[ERROR] Journal not found: {journal_path}")
        return 1
    if not feature_path.exists():
        print(f"[ERROR] Feature store not found: {feature_path}")
        return 1

    print(f"Loading trades from {journal_path}...")
    trades = load_trade_labels(journal_path)
    print(
        f"  Found {len(trades)} labeled trades (W:{sum(1 for t in trades if t['is_win'])} L:{sum(1 for t in trades if not t['is_win'])})"
    )

    print(f"Matching features from {feature_path}...")
    matched = match_features(trades, feature_path, window_minutes=args.window_minutes)
    print(f"  Matched {len(matched)} feature vectors")

    build_dataset(matched, output_dir)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

#!/usr/bin/env python3
"""Train a meta-model for dynamic exit management.

Reads the live trade journal, pairs open→close records, extracts
per-trade features, labels win/loss, and trains a LightGBM binary
classifier to predict P(win | features).

Usage:
  python scripts/training/train_exit_metamodel.py
  python scripts/training/train_exit_metamodel.py --journal data/live_trade_journal.jsonl

Output:
  data/models/meta_exit_model.txt       (LightGBM booster)
  data/models/meta_exit_model.meta.json (feature names and training stats)

Limitations (current):
  - Entry feature vectors (40-dim) are NOT stored in the journal.
    When they become available (via shadow decision recorder with
    feature_vector in extensions), the model will gain predictive power.
  - Only 50 closed trades as of 2026-05, heavily skewed toward losses.
    Model quality will improve as more trades accumulate.
  - Per-tick PnL snapshots are not recorded.  The model currently
    trains on final-outcome labels only (not time-series meta-labels).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train meta-model for exit management")
    p.add_argument(
        "--journal", default="data/live_trade_journal.jsonl", help="Path to trade journal JSONL"
    )
    p.add_argument("--output-dir", default="data/models", help="Directory for model output")
    p.add_argument(
        "--min-trades", type=int, default=10, help="Minimum closed trades required to train"
    )
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


# ── Journal pairing ──


def load_journal(path: str) -> list[dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:  # noqa: SIM105
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def pair_opens_to_closes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match each 'open' record with its corresponding 'close' record.

    Returns list of paired dicts with open + close data merged.
    """
    # Index opens by message_id
    opens: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("action") == "open":
            opens[r["message_id"]] = r

    pairs: list[dict[str, Any]] = []
    for r in records:
        if r.get("action") != "close":
            continue
        open_id = r.get("open_message_id", "")
        open_rec = opens.get(open_id)
        if open_rec is None:
            continue
        pairs.append(
            {
                "open": open_rec,
                "close": r,
                "symbol": r.get("symbol", ""),
                "side": open_rec.get("side", ""),
            }
        )

    return pairs


# ── Feature extraction ──


def extract_features(pair: dict[str, Any]) -> dict[str, Any]:
    """Extract training features from a paired open→close trade.

    Features:
      - side_short: 1 if short, 0 if long
      - sl_distance_pips: |entry - sl| in price units
      - tp_distance_pips: |tp - entry| in price units
      - rr_ratio: tp_distance / sl_distance (risk-reward)
      - volume: lot size
      - accepted: 1 if order was accepted, 0 if rejected
      - entry_hour: UTC hour of entry (0-23)
      - entry_dow: day of week (0=Mon, 6=Sun)
    """
    o = pair["open"]
    c = pair["close"]

    side = o.get("side", "long")
    side_short = 1.0 if side == "short" else 0.0

    sl = float(o.get("sl", 0) or 0)
    tp = float(o.get("tp", 0) or 0)

    # Estimate entry price from SL/TP based on side
    if side == "long":
        sl_dist = abs(tp - sl) * 0.3636 if tp > sl else abs(tp - sl) * 0.5
        tp_dist = abs(tp - sl) * 0.6364 if tp > sl else abs(tp - sl) * 0.5
    else:
        sl_dist = abs(sl - tp) * 0.3636 if sl > tp else abs(sl - tp) * 0.5
        tp_dist = abs(sl - tp) * 0.6364 if sl > tp else abs(sl - tp) * 0.5

    rr_ratio = tp_dist / max(sl_dist, 0.001)

    volume = float(o.get("volume") or o.get("effective_volume_hint", 0.01) or 0.01)
    accepted = 1.0 if o.get("ack_status") == "accepted" else 0.0

    # Temporal
    ts_str = o.get("recorded_at", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        entry_hour = float(ts.hour)
        entry_dow = float(ts.weekday())
    except Exception:  # BLE001:FOG_DEFERRED
        entry_hour = 12.0
        entry_dow = 3.0

    # PnL and outcome
    pnl = float(c.get("pnl") or 0)
    detail = c.get("detail")
    detail_pnl = detail.get("pnl") if isinstance(detail, dict) else None
    close_pnl_raw = float(detail_pnl or pnl or 0)

    # Label: win if PnL > threshold
    label = 1 if close_pnl_raw > 0.01 else 0

    # Close type
    (c.get("detail", {}).get("reason", "") if isinstance(c.get("detail"), dict) else "")

    return {
        "side_short": side_short,
        "sl_distance": round(sl_dist, 4),
        "tp_distance": round(tp_dist, 4),
        "rr_ratio": round(rr_ratio, 4),
        "volume": volume,
        "accepted": accepted,
        "entry_hour": entry_hour,
        "entry_dow": entry_dow,
        # NOTE: is_sl_hit/is_tp_hit removed from features — they are post-trade
        # outcomes (data leakage).  Including them made the model appear highly
        # predictive during training (feature importance: 512+181 gain) but
        # useless at prediction time where both are always 0.0.
        # Labels
        "pnl": round(close_pnl_raw, 4),
        "label": label,
    }


# ── Build dataset ──


def build_dataset(pairs: list[dict[str, Any]]) -> tuple[list[list[float]], list[int], list[str]]:
    """Build feature matrix X and label vector y from trade pairs.

    Returns:
        X: list of feature vectors (list of floats)
        y: list of labels (0 or 1)
        feature_names: ordered list of feature names
    """
    feature_names = [
        "side_short",
        "sl_distance",
        "tp_distance",
        "rr_ratio",
        "volume",
        "accepted",
        "entry_hour",
        "entry_dow",
    ]

    X: list[list[float]] = []
    y: list[int] = []

    for pair in pairs:
        feats = extract_features(pair)
        row = [feats[name] for name in feature_names]
        X.append(row)
        y.append(feats["label"])

    return X, y, feature_names


# ── Training ──


def train_model(
    X: list[list[float]],
    y: list[int],
    feature_names: list[str],
    output_dir: str,
    seed: int = 42,
) -> dict[str, Any]:
    """Train LightGBM binary classifier and save artifacts."""
    import lightgbm as lgb
    import numpy as np

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.int32)

    n_pos = int(y_arr.sum())
    n_neg = len(y_arr) - n_pos
    print(f"Training dataset: {len(y_arr)} samples, {n_pos} wins, {n_neg} losses")

    # Handle class imbalance via scale_pos_weight
    scale_pos_weight = n_neg / max(n_pos, 1)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": "gbdt",
        "num_leaves": 15,
        "max_depth": 4,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "min_data_in_leaf": 2,
        "scale_pos_weight": scale_pos_weight,
        "verbosity": 1,
        "seed": seed,
        "deterministic": True,
    }

    train_data = lgb.Dataset(X_arr, label=y_arr, feature_name=feature_names)

    print("Training LightGBM...")
    booster = lgb.train(
        params,
        train_data,
        num_boost_round=100,
        valid_sets=[train_data],
        valid_names=["train"],
    )

    # Save
    os.makedirs(output_dir, exist_ok=True)
    model_path = os.path.join(output_dir, "meta_exit_model.txt")
    meta_path = os.path.join(output_dir, "meta_exit_model.meta.json")

    booster.save_model(model_path)
    print(f"Model saved to {model_path}")

    # Feature importance
    importance = booster.feature_importance(importance_type="gain")
    importance_dict = {
        name: float(imp) for name, imp in zip(feature_names, importance, strict=False)
    }

    # Metadata
    meta = {
        "trained_at": datetime.now(UTC).isoformat(),
        "n_samples": len(y_arr),
        "n_wins": n_pos,
        "n_losses": n_neg,
        "win_rate": round(n_pos / max(len(y_arr), 1), 4),
        "feature_names": feature_names,
        "feature_importance_gain": importance_dict,
        "scale_pos_weight": scale_pos_weight,
        "params": {k: v for k, v in params.items() if k != "seed"},
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"Metadata saved to {meta_path}")

    return meta


# ── Main ──


def main() -> None:
    args = parse_args()

    if not os.path.exists(args.journal):
        print(f"Journal not found: {args.journal}")
        sys.exit(1)

    records = load_journal(args.journal)
    print(f"Loaded {len(records)} journal records")

    pairs = pair_opens_to_closes(records)
    print(f"Paired {len(pairs)} open→close trades")

    if len(pairs) < args.min_trades:
        print(f"Not enough closed trades ({len(pairs)} < {args.min_trades}). " "Skipping training.")
        sys.exit(0)

    X, y, feature_names = build_dataset(pairs)

    win_count = sum(y)
    print(
        f"Feature matrix: {len(X)} rows × {len(feature_names)} cols, "
        f"wins={win_count}, losses={len(y)-win_count}"
    )

    if win_count < 2:
        print(
            "Insufficient winning trades for binary classification "
            f"(need >= 2, got {win_count}).  Skipping training."
        )
        sys.exit(0)

    meta = train_model(X, y, feature_names, args.output_dir, seed=args.seed)

    print("\nTop features by gain:")
    for name, imp in sorted(
        meta["feature_importance_gain"].items(),
        key=lambda x: x[1],
        reverse=True,
    )[:5]:
        print(f"  {name}: {imp:.2f}")


if __name__ == "__main__":
    main()

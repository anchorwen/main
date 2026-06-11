#!/usr/bin/env python3
"""XAU Directional Brain Training — bidirectional labels for gold swing models.

Reuses V10 directional training architecture, adapted for XAU parameters.

Key differences from BTC:
  - SL=2.0×ATR, TP=3.5×ATR (from live.yaml)
  - spread=0.5 points (~$0.50 actual gold spread)
  - slippage=0.2 points
  - Uses XAU CSV data files

Usage:
  python scripts/training/train_xau_directional_v1.py --timeframe H1
  python scripts/training/train_xau_directional_v1.py --timeframe M15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(ROOT / "scripts" / "training"))
import train_btc_swing_v9 as v9  # noqa: E402 — reuse feature computation

# ── XAU Strategy Parameters (from live.yaml) ──
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.5
SPREAD_POINTS = 0.5   # XAU spread ~$0.50
SLIPPAGE_POINTS = 0.2  # XAU slippage ~$0.20


def _simulate_one_trade(
    o: np.ndarray, h: np.ndarray, l: np.ndarray,
    i: int, horizon: int,
    entry_price: float, sl_price: float, tp_price: float,
    direction: str,
) -> tuple[int, float, int]:
    """Simulate ONE directional trade (same logic as BTC V10)."""
    n = len(o)
    end_bar = min(i + 1 + horizon, n)
    tp_hit = sl_hit = False
    tp_bar = sl_bar = -1
    for j in range(i + 1, end_bar):
        cur_h, cur_l = h[j], l[j]
        if direction == "long":
            tp_ok, sl_ok = cur_h >= tp_price, cur_l <= sl_price
        else:
            tp_ok, sl_ok = cur_l <= tp_price, cur_h >= sl_price
        if tp_ok and sl_ok:
            tp_hit = sl_hit = True
            tp_bar = sl_bar = j
            break
        if tp_ok and not tp_hit:
            tp_hit = True
            tp_bar = j
        if sl_ok and not sl_hit:
            sl_hit = True
            sl_bar = j
        if tp_hit or sl_hit:
            break
    if tp_hit and not sl_hit:
        return 1, abs(tp_price - entry_price) / max(abs(entry_price - sl_price), 1e-9), tp_bar - i
    elif sl_hit and not tp_hit:
        return -1, -1.0, sl_bar - i
    else:
        return 0, 0.0, max(tp_bar, sl_bar) - i if (tp_bar >= 0 and sl_bar >= 0) else 0


def compute_directional_labels(
    o, h, l, c, horizon, sl_atr_mult, tp_atr_mult, spread_points, slippage_points,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """DIRECTIONAL labels for XAU."""
    n = len(o)
    labels = np.zeros(n, dtype=np.float32)
    pnl_r = np.zeros(n, dtype=np.float32)
    atr = np.zeros(n)
    for i in range(14, n):
        atr[i] = v9._atr(h[:i+1], l[:i+1], c[:i+1])
    half_sp = spread_points / 2.0
    n_long = n_short = n_neutral = 0
    for i in range(14, n - horizon - 1):
        if atr[i] <= 0:
            continue
        sl_raw = sl_atr_mult * atr[i]
        tp_raw = max(tp_atr_mult * atr[i], sl_raw * 0.3)
        entry_long = o[i+1] + half_sp + slippage_points
        lo, _, _ = _simulate_one_trade(o, h, l, i, horizon, entry_long, entry_long - sl_raw, entry_long + tp_raw, "long")
        entry_short = o[i+1] - half_sp - slippage_points
        so, _, _ = _simulate_one_trade(o, h, l, i, horizon, entry_short, entry_short + sl_raw, entry_short - tp_raw, "short")
        if lo == 1 and so != 1:
            labels[i] = 1.0; pnl_r[i] = tp_raw / max(sl_raw, 1e-9); n_long += 1
        elif so == 1 and lo != 1:
            labels[i] = -1.0; pnl_r[i] = tp_raw / max(sl_raw, 1e-9); n_short += 1
        else:
            n_neutral += 1
    total = n_long + n_short + n_neutral
    if total > 0:
        print(f"  Labels: LONG={n_long} ({n_long/total*100:.1f}%) SHORT={n_short} ({n_short/total*100:.1f}%) NEUTRAL={n_neutral} ({n_neutral/total*100:.1f}%)")
    return labels, pnl_r, np.zeros(n, dtype=np.int32)


def build_dataset(csv_path, output_dir, horizon, sl_atr_mult, tp_atr_mult, spread_points, slippage_points, cv_folds, purge_bars, timeframe_minutes):
    """Full B2 pipeline (reuses V9 pattern)."""
    print(f"[B2] Loading XAU data from {csv_path}...")
    df = pd.read_csv(csv_path)
    n_bars = len(df)
    print(f"  Loaded {n_bars:,} bars")
    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    vol_col = "tick_volume" if "tick_volume" in df.columns else "volume"
    v = df.get(vol_col, pd.Series(np.zeros(n_bars))).values.astype(np.float64)
    spreads = df.get("spread", pd.Series([0.5] * n_bars)).values.astype(np.float64)
    timestamps = pd.to_datetime(df["time"]).astype(np.int64).values // 10**9
    timestamps_f = timestamps.astype(np.float64)

    # Daily features
    print("[B2] Computing day-level context features...")
    df_dt = pd.to_datetime(df["time"])
    daily = df.set_index(df_dt).resample("D").agg({"open": "first", "high": "max", "low": "min", "close": "last", vol_col: "sum"}).dropna()
    daily_ts = daily.index.astype(np.int64).values // 10**9
    daily_ts_f = daily_ts.astype(np.float64)
    daily_o, daily_h, daily_l, daily_c = daily["open"].values, daily["high"].values, daily["low"].values, daily["close"].values
    day_features = {}
    for d_idx in range(len(daily_c)):
        end = d_idx + 1
        prev_c = daily_c[-2] if len(daily_c) >= 2 else daily_c[-1]
        day_features[d_idx] = {
            "D1_Ret_1": (daily_c[-1] - prev_c) / prev_c if prev_c > 0 else 0.0,
            "D1_Body_Ratio": abs(daily_c[-1] - daily_o[-1]) / (daily_h[-1] - daily_l[-1]) if (daily_h[-1] - daily_l[-1]) > 0 else 0.5,
            "D1_ATR_14": v9._atr(daily_h[:end], daily_l[:end], daily_c[:end]),
            "D1_RSI_14": v9._rsi(daily_c[:end]),
            "D1_MACD": v9._macd(daily_c[:end])[2],
            "D1_Vol_ZScore": v9._vol_zscore(daily_c[:end]),
            "D1_Bollinger_Width": v9._bollinger_width(daily_c[:end]),
            "D1_ADX_14": v9._adx(daily_h[:end], daily_l[:end], daily_c[:end]),
            "XAUUSDc_return": 0.0, "XAUUSDc_close": 0.0,
            "Cross_DXY_Return": 0.0, "Cross_EURUSD_Return": 0.0, "Cross_Risk_On_Off": 0.0,
            "H4_Trend_Strength": 0.0, "H4_ATR_Ratio": 0.0, "H4_RSI_Divergence": 0.0, "H4_vs_D1_Alignment": 0.0,
        }

    N_FEATURES = 37; MIN_BARS = 100
    features = np.zeros((n_bars, N_FEATURES), dtype=np.float32)
    start_bar = MIN_BARS
    print(f"[B2] Computing {N_FEATURES}-dim features...")
    for i in range(start_bar, n_bars - horizon - 1):
        if (i - start_bar) % 20000 == 0 and i > start_bar:
            print(f"  ... {i}/{n_bars} bars ({100*i/n_bars:.0f}%)")
        row = v9.compute_feature_row(i, o, h, l, c, v, spreads, day_features, daily_ts_f, daily_o, daily_h, daily_l, daily_c, c, tf_minutes=timeframe_minutes)
        features[i] = np.asarray(row, dtype=np.float32)

    labels, pnl_r, _ = compute_directional_labels(o, h, l, c, horizon, sl_atr_mult, tp_atr_mult, spread_points, slippage_points)
    valid_idx = np.arange(start_bar, n_bars - horizon - 1)
    features, labels, pnl_r = features[valid_idx], labels[valid_idx], pnl_r[valid_idx]
    ts_valid = timestamps_f[valid_idx]
    labeled_mask = labels != 0.0
    X, y, r = features[labeled_mask], labels[labeled_mask], pnl_r[labeled_mask]
    ts_labeled = ts_valid[labeled_mask]
    print(f"  Labeled: {len(X)} ({len(X)/max(len(valid_idx),1)*100:.1f}%)")

    weights = v9.compute_time_decay_weights(ts_labeled, 180.0)
    splits = v9.walk_forward_purged_splits(len(X), ts_labeled, cv_folds, purge_bars)
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(os.path.join(output_dir, "train.npz"), X=X, y=y, pnl_r=r, sample_weight=weights, timestamps=ts_labeled)
    splits_json = []
    for s in splits:
        sc = dict(s)
        for k in list(sc):
            if isinstance(sc[k], np.ndarray):
                sc[k] = sc[k].tolist()
        splits_json.append(sc)
    with open(os.path.join(output_dir, "cv_splits.json"), "w") as f:
        json.dump(splits_json, f)
    meta = {"n_samples": int(len(X)), "n_features": N_FEATURES, "n_long": int(np.sum(y > 0)), "n_short": int(np.sum(y < 0)), "timeframe_minutes": timeframe_minutes, "horizon": horizon}
    with open(os.path.join(output_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def _evaluate_model(y_true, y_pred, pnl_r):
    signal_mask = np.abs(y_pred) > 0.05
    dir_acc = float(np.mean(np.sign(y_pred[signal_mask]) == np.sign(y_true[signal_mask]))) if np.sum(signal_mask) > 0 else 0.0
    long_actual, short_actual = y_true > 0, y_true < 0
    long_pred, short_pred = y_pred > 0.05, y_pred < -0.05
    long_rec = float(np.sum(long_actual & long_pred) / max(np.sum(long_actual), 1))
    short_rec = float(np.sum(short_actual & short_pred) / max(np.sum(short_actual), 1))
    trade_rs = []
    for j in range(len(y_pred)):
        if long_pred[j]: trade_rs.append(pnl_r[j] if long_actual[j] else -1.0)
        elif short_pred[j]: trade_rs.append(pnl_r[j] if short_actual[j] else -1.0)
    return {"directional_accuracy": dir_acc, "long_recall": long_rec, "short_recall": short_rec, "long_precision": float(np.sum(long_actual & long_pred) / max(np.sum(long_pred), 1)), "short_precision": float(np.sum(short_actual & short_pred) / max(np.sum(short_pred), 1)), "pred_long": int(np.sum(long_pred)), "pred_short": int(np.sum(short_pred)), "pred_neutral": int(len(y_pred) - np.sum(long_pred) - np.sum(short_pred)), "trade_rs": trade_rs}


def train_models(data_dir, output_dir):
    data = np.load(os.path.join(data_dir, "train.npz"))
    X_all, y_all = data["X"], data["y"]
    pnl_r_all = data.get("pnl_r", np.zeros(len(y_all)))
    weights_all = data.get("sample_weight", np.ones(len(X_all)))
    with open(os.path.join(data_dir, "cv_splits.json")) as f:
        splits = json.load(f)
    os.makedirs(output_dir, exist_ok=True)
    results = {"lightgbm": [], "xgboost": []}
    for fold_idx, split in enumerate(splits):
        train_idx, val_idx = split["train_idx"], split["test_idx"]
        X_tr, y_tr = X_all[train_idx], y_all[train_idx]
        X_val, y_val = X_all[val_idx], y_all[val_idx]
        w_tr, r_val = weights_all[train_idx], pnl_r_all[val_idx]
        print(f"\n  Fold {fold_idx+1}/{len(splits)}: train={len(X_tr)}, val={len(X_val)}")
        # LightGBM
        try:
            import lightgbm as lgb
            params = {"objective": "regression", "metric": "rmse", "boosting_type": "gbdt", "num_leaves": 31, "learning_rate": 0.02, "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5, "verbose": -1, "seed": 42}
            dtrain = lgb.Dataset(X_tr, label=y_tr, weight=w_tr)
            dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)
            model = lgb.train(params, dtrain, valid_sets=[dval], num_boost_round=500, callbacks=[lgb.early_stopping(50)])
            model.save_model(os.path.join(output_dir, f"lightgbm_fold{fold_idx}_s42.txt"))
            ev = _evaluate_model(y_val, model.predict(X_val), r_val)
            results["lightgbm"].append(ev)
            print(f"    LGB: DirAcc={ev['directional_accuracy']:.3f} LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} trades={len(ev['trade_rs'])}")
        except ImportError: pass
        # XGBoost
        try:
            import xgboost as xgb
            params = {"objective": "reg:squarederror", "eval_metric": "rmse", "max_depth": 5, "learning_rate": 0.02, "subsample": 0.8, "colsample_bytree": 0.8, "seed": 42, "verbosity": 0}
            dtrain = xgb.DMatrix(X_tr, label=y_tr, weight=w_tr)
            dval = xgb.DMatrix(X_val, label=y_val)
            model = xgb.train(params, dtrain, num_boost_round=500, evals=[(dval, "val")], early_stopping_rounds=50, verbose_eval=False)
            model.save_model(os.path.join(output_dir, f"xgboost_fold{fold_idx}_s42.json"))
            ev = _evaluate_model(y_val, model.predict(dval), r_val)
            results["xgboost"].append(ev)
            print(f"    XGB: DirAcc={ev['directional_accuracy']:.3f} LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} trades={len(ev['trade_rs'])}")
        except ImportError: pass
    summary = {}
    for mn, fr in results.items():
        if not fr: continue
        agg = {}
        for k in ["directional_accuracy", "long_recall", "short_recall"]:
            vals = [f[k] for f in fr]
            agg[f"{k}_mean"] = float(np.mean(vals))
            agg[f"{k}_std"] = float(np.std(vals))
        all_rs = []
        for f in fr: all_rs.extend(f.get("trade_rs", []))
        if all_rs:
            r_arr = np.array(all_rs)
            agg["bt_total_trades"] = len(r_arr)
            agg["bt_win_rate"] = float(np.mean(r_arr > 0))
            agg["bt_total_r"] = float(np.sum(r_arr))
        summary[mn] = agg
    return summary


def main():
    parser = argparse.ArgumentParser(description="XAU Directional Brain V1")
    parser.add_argument("--timeframe", default="H1", choices=["H1", "M15", "M30", "H4"])
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--skip-train", action="store_true")
    args = parser.parse_args()
    tf = args.timeframe
    data_dir = args.data_dir or f"data/training/xau_directional_{tf.lower()}"
    csv_path = args.csv or f"data/raw/xauusdc_{tf.lower()}_merged.csv"
    tf_minutes = {"H1": 60.0, "M15": 15.0, "M30": 30.0, "H4": 240.0}[tf]
    np.random.seed(42)
    print(f"{'='*60}\nXAU Directional Brain V1 — {tf}\n  SL={SL_ATR_MULT}×ATR  TP={TP_ATR_MULT}×ATR  spread={SPREAD_POINTS}\n  data: {csv_path}\n{'='*60}")
    build_dataset(csv_path, data_dir, args.horizon, SL_ATR_MULT, TP_ATR_MULT, SPREAD_POINTS, SLIPPAGE_POINTS, args.cv_folds, 24, tf_minutes)
    if not args.skip_train:
        results = train_models(data_dir, data_dir)
        summary = {"schema_version": "xau_directional_v1.v1", "timeframe": tf, "sl_atr_mult": SL_ATR_MULT, "tp_atr_mult": TP_ATR_MULT, "models": results, "trained_at": datetime.now(UTC).isoformat()}
        with open(os.path.join(data_dir, "training_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\n{'='*60}")
        for mn, mr in results.items():
            print(f"  {mn}: DirAcc={mr.get('directional_accuracy_mean',0):.3f} LongRec={mr.get('long_recall_mean',0):.3f} ShortRec={mr.get('short_recall_mean',0):.3f} bt_WR={mr.get('bt_win_rate',0):.1%}")
        print(f"  Summary: {data_dir}/training_summary.json")


if __name__ == "__main__":
    main()

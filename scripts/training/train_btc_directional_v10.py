#!/usr/bin/env python3
"""BTC Directional Brain V10 — bidirectional label training.

Reuses V9's data loading and feature computation pipeline exactly.
Only replaces:
  1. Label computation: directional (+1=LONG win, -1=SHORT win, 0=neutral)
  2. Training objective: regression (not binary classification)

Usage:
  python scripts/training/train_btc_directional_v10.py --timeframe H1 --skip-train  # check labels
  python scripts/training/train_btc_directional_v10.py --timeframe H1               # full
  python scripts/training/train_btc_directional_v10.py --timeframe M15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

UTC = UTC
ROOT = Path(__file__).resolve().parent.parent.parent

# ── Import V9 pipeline ──
sys.path.insert(0, str(ROOT / "scripts" / "training"))
import train_btc_swing_v9 as v9  # noqa: E402

# ── Strategy Parameters (aligned with live_btc.yaml btc_swing) ──
SL_ATR_MULT = 2.0
TP_ATR_MULT = 2.5
SPREAD_POINTS = 200  # BTC typical spread (~0.3%, conservative for 60k-65k range)
SLIPPAGE_POINTS = 50


# ═══════════════════════════════════════════════════════════════════════════
# B2: Directional label computation
# ═══════════════════════════════════════════════════════════════════════════


def _simulate_one_trade(
    o: np.ndarray, h: np.ndarray, l: np.ndarray,
    i: int, horizon: int,
    entry_price: float, sl_price: float, tp_price: float,
    direction: str,
) -> tuple[int, float, int]:
    """Simulate ONE directional trade with explicit entry/SL/TP.

    TP checked BEFORE SL (favorable outcome first).
    Same-bar TP+SL → ambiguous → both triggered → outcome=0.

    Returns (outcome, r_mult, holding_bars):
      +1 = TP hit first, -1 = SL hit first, 0 = timeout/ambiguous
    """
    n = len(o)
    end_bar = min(i + 1 + horizon, n)

    tp_hit = sl_hit = False
    tp_bar = sl_bar = -1

    for j in range(i + 1, end_bar):
        cur_h, cur_l = h[j], l[j]

        if direction == "long":
            tp_ok = cur_h >= tp_price
            sl_ok = cur_l <= sl_price
        else:
            tp_ok = cur_l <= tp_price
            sl_ok = cur_h >= sl_price

        # Same-bar both → ambiguous
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
        r = abs(tp_price - entry_price) / max(abs(entry_price - sl_price), 1e-9)
        return 1, r, tp_bar - i
    elif sl_hit and not tp_hit:
        return -1, -1.0, sl_bar - i
    else:
        return 0, 0.0, max(tp_bar, sl_bar) - i if (tp_bar >= 0 and sl_bar >= 0) else 0


def compute_directional_labels(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
    horizon: int, sl_atr_mult: float, tp_atr_mult: float,
    spread_points: float, slippage_points: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """DIRECTIONAL labels: +1=LONG win, -1=SHORT win, 0=neutral.

    Independently simulates LONG and SHORT for each bar.
    Entry prices include spread and slippage per direction.
    """
    n = len(o)
    labels = np.zeros(n, dtype=np.float32)
    pnl_r = np.zeros(n, dtype=np.float32)
    hold_bars = np.zeros(n, dtype=np.int32)

    # ATR (reuse V9's implementation for exact consistency)
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

        # ── LONG entry: open + half_spread + slippage ──
        entry_long = o[i + 1] + half_sp + slippage_points
        sl_long = entry_long - sl_raw
        tp_long = entry_long + tp_raw

        lo, _, _ = _simulate_one_trade(
            o, h, l, i, horizon, entry_long, sl_long, tp_long, "long",
        )

        # ── SHORT entry: open - half_spread - slippage ──
        entry_short = o[i + 1] - half_sp - slippage_points
        sl_short = entry_short + sl_raw
        tp_short = entry_short - tp_raw

        so, _, _ = _simulate_one_trade(
            o, h, l, i, horizon, entry_short, sl_short, tp_short, "short",
        )

        if lo == 1 and so != 1:
            labels[i] = 1.0
            pnl_r[i] = tp_raw / max(sl_raw, 1e-9)
            hold_bars[i] = 1
            n_long += 1
        elif so == 1 and lo != 1:
            labels[i] = -1.0
            pnl_r[i] = tp_raw / max(sl_raw, 1e-9)
            hold_bars[i] = 1
            n_short += 1
        else:
            # Both won, both lost, or timeout — neutral
            # For neutral bars, record actual outcome for backtest evaluation
            if lo == 1 and so == 1:
                pnl_r[i] = tp_raw / max(sl_raw, 1e-9)  # both profitable
            elif lo == -1 or so == -1:
                pnl_r[i] = -1.0  # at least one loss
            n_neutral += 1

    total = n_long + n_short + n_neutral
    if total > 0:
        print(f"  Labels: LONG={n_long} ({n_long/total*100:.1f}%) "
              f"SHORT={n_short} ({n_short/total*100:.1f}%) "
              f"NEUTRAL={n_neutral} ({n_neutral/total*100:.1f}%)")
    return labels, pnl_r, hold_bars


# ═══════════════════════════════════════════════════════════════════════════
# B2: Build dataset (V9 pipeline + directional labels)
# ═══════════════════════════════════════════════════════════════════════════


def build_dataset(
    csv_path: str, output_dir: str,
    horizon: int = 24,
    sl_atr_mult: float = SL_ATR_MULT,
    tp_atr_mult: float = TP_ATR_MULT,
    spread_points: float = SPREAD_POINTS,
    slippage_points: float = SLIPPAGE_POINTS,
    decay_half_life_days: float = 180.0,
    cv_folds: int = 5,
    purge_bars: int = 24,
    timeframe_minutes: float = 60.0,
) -> dict:
    """Full B2 pipeline: CSV → features + directional labels → CV splits → NPZ."""
    print(f"[B2] Loading BTC data from {csv_path}...")
    df = pd.read_csv(csv_path)
    n_bars = len(df)
    print(f"  Loaded {n_bars:,} bars")

    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    vol_col = "tick_volume" if "tick_volume" in df.columns else "volume"
    v = df.get(vol_col, pd.Series(np.zeros(n_bars))).values.astype(np.float64)
    spreads = df.get("spread", pd.Series([10] * n_bars)).values.astype(np.float64)
    timestamps = pd.to_datetime(df["time"]).astype(np.int64).values // 10**9
    timestamps_f = timestamps.astype(np.float64)

    # ── Daily features (identical to V9) ──
    print("[B2] Computing day-level context features...")
    df_dt = pd.to_datetime(df["time"])
    daily = df.set_index(df_dt).resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        vol_col: "sum",
    }).dropna()
    daily_ts = daily.index.astype(np.int64).values // 10**9
    daily_ts_f = daily_ts.astype(np.float64)
    daily_o = daily["open"].values
    daily_h = daily["high"].values
    daily_l = daily["low"].values
    daily_c = daily["close"].values

    day_features: dict[int, dict[str, float]] = {}
    for d_idx in range(len(daily_c)):
        end = d_idx + 1
        d_o = daily_o[:end]
        d_h = daily_h[:end]
        d_l = daily_l[:end]
        d_c_s = daily_c[:end]
        prev_c = d_c_s[-2] if len(d_c_s) >= 2 else d_c_s[-1]
        feat = {
            "D1_Ret_1": (d_c_s[-1] - prev_c) / prev_c if prev_c > 0 else 0.0,
            "D1_Body_Ratio": abs(d_c_s[-1] - d_o[-1]) / (d_h[-1] - d_l[-1]) if (d_h[-1] - d_l[-1]) > 0 else 0.5,
            "D1_ATR_14": v9._atr(d_h, d_l, d_c_s),
            "D1_RSI_14": v9._rsi(d_c_s),
            "D1_MACD": v9._macd(d_c_s)[2],
            "D1_Vol_ZScore": v9._vol_zscore(d_c_s),
            "D1_Bollinger_Width": v9._bollinger_width(d_c_s),
            "D1_ADX_14": v9._adx(d_h, d_l, d_c_s),
            # Cross-asset placeholders (unavailable in training, zero-filled)
            "XAUUSDc_return": 0.0, "XAUUSDc_close": 0.0,
            "Cross_DXY_Return": 0.0, "Cross_EURUSD_Return": 0.0,
            "Cross_Risk_On_Off": 0.0,
            # H4 placeholders
            "H4_Trend_Strength": 0.0, "H4_ATR_Ratio": 0.0,
            "H4_RSI_Divergence": 0.0, "H4_vs_D1_Alignment": 0.0,
        }
        day_features[d_idx] = feat

    # ── Per-bar features (identical to V9) ──
    print(f"[B2] Computing 37-dim features for {n_bars} bars...")
    N_FEATURES = 37
    MIN_BARS = 100
    features = np.zeros((n_bars, N_FEATURES), dtype=np.float32)
    start_bar = MIN_BARS

    for i in range(start_bar, n_bars - horizon - 1):
        if (i - start_bar) % 50000 == 0 and i > start_bar:
            print(f"  ... {i}/{n_bars} bars ({100*i/n_bars:.0f}%)")
        row = v9.compute_feature_row(
            i, o, h, l, c, v, spreads, day_features,
            daily_ts_f, daily_o, daily_h, daily_l, daily_c,
            c,  # btc_price_hist
            tf_minutes=timeframe_minutes,
        )
        features[i] = np.asarray(row, dtype=np.float32)

    # ── Directional labels ──
    print(f"[B2] Computing DIRECTIONAL labels (SL={sl_atr_mult}ATR, TP={tp_atr_mult}ATR, spread={spread_points})...")
    labels, pnl_r, hold_bars = compute_directional_labels(
        o, h, l, c, horizon, sl_atr_mult, tp_atr_mult,
        spread_points, slippage_points,
    )

    # ── Filter to valid bars ──
    valid_idx = np.arange(start_bar, n_bars - horizon - 1)
    features = features[valid_idx]
    labels = labels[valid_idx]
    pnl_r = pnl_r[valid_idx]
    ts_valid = timestamps_f[valid_idx]

    # Keep labeled bars (non-neutral)
    labeled_mask = labels != 0.0
    X = features[labeled_mask]
    y = labels[labeled_mask]
    r = pnl_r[labeled_mask]
    ts_labeled = ts_valid[labeled_mask]

    print(f"  Valid bars: {len(valid_idx)}, Labeled: {len(X)} ({len(X)/max(len(valid_idx),1)*100:.1f}%)")

    # ── Time-decay weights ──
    weights = v9.compute_time_decay_weights(ts_labeled, decay_half_life_days)

    # ── Walk-forward purged CV splits ──
    splits = v9.walk_forward_purged_splits(len(X), ts_labeled, cv_folds, purge_bars)

    # ── Save ──
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(output_dir, "train.npz"),
        X=X, y=y, pnl_r=r, sample_weight=weights, timestamps=ts_labeled,
    )
    splits_json = []
    for s in splits:
        sc = dict(s)
        for k in list(sc):
            if isinstance(sc[k], np.ndarray):
                sc[k] = sc[k].tolist()
        splits_json.append(sc)
    with open(os.path.join(output_dir, "cv_splits.json"), "w") as f:
        json.dump(splits_json, f)
    meta = {
        "n_samples": int(len(X)), "n_features": N_FEATURES,
        "n_long": int(np.sum(y > 0)), "n_short": int(np.sum(y < 0)),
        "timeframe_minutes": timeframe_minutes, "horizon": horizon,
    }
    with open(os.path.join(output_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# B3: Directional model training (regression) + evaluation
# ═══════════════════════════════════════════════════════════════════════════


def _evaluate_model(y_true: np.ndarray, y_pred: np.ndarray, pnl_r: np.ndarray) -> dict:
    """Evaluate directional predictions with proper metrics + simple backtest.

    Directional accuracy: only on bars where model actually signals (|pred| > 0.05).
    Recall: TP / (TP + FN) separately for LONG and SHORT.
    Backtest: simulate trades on all prediction bars using realized pnl_r.
    """
    signal_mask = np.abs(y_pred) > 0.05
    n_signal = int(np.sum(signal_mask))

    # Directional accuracy (on signaled bars only)
    if n_signal > 0:
        dir_acc = float(np.mean(np.sign(y_pred[signal_mask]) == np.sign(y_true[signal_mask])))
    else:
        dir_acc = 0.0

    # Per-direction recall
    long_actual = y_true > 0
    short_actual = y_true < 0
    long_pred = y_pred > 0.05
    short_pred = y_pred < -0.05

    n_long = int(np.sum(long_actual))
    n_short = int(np.sum(short_actual))
    long_rec = float(np.sum(long_actual & long_pred) / max(n_long, 1))
    short_rec = float(np.sum(short_actual & short_pred) / max(n_short, 1))

    # Precision
    long_prec = float(np.sum(long_actual & long_pred) / max(np.sum(long_pred), 1))
    short_prec = float(np.sum(short_actual & short_pred) / max(np.sum(short_pred), 1))

    # Prediction distribution
    pred_long = int(np.sum(long_pred))
    pred_short = int(np.sum(short_pred))
    pred_neutral = int(len(y_pred) - pred_long - pred_short)

    # ── Simple backtest on validation set ──
    # Trade when model signals. Use realized pnl_r if direction correct, -1.0 if wrong.
    trade_rs = []
    for j in range(len(y_pred)):
        if long_pred[j]:
            trade_rs.append(pnl_r[j] if long_actual[j] else -1.0)
        elif short_pred[j]:
            trade_rs.append(pnl_r[j] if short_actual[j] else -1.0)

    return {
        "directional_accuracy": dir_acc,
        "long_recall": long_rec, "short_recall": short_rec,
        "long_precision": long_prec, "short_precision": short_prec,
        "n_long_actual": n_long, "n_short_actual": n_short,
        "pred_long": pred_long, "pred_short": pred_short, "pred_neutral": pred_neutral,
        "y_pred_mean": float(np.mean(y_pred)),
        "y_pred_std": float(np.std(y_pred)),
        "trade_rs": trade_rs,
    }


def train_models(data_dir: str, output_dir: str, n_seeds: int = 3) -> dict:
    """Walk-forward CV training: each fold trains on past, validates on future."""
    data = np.load(os.path.join(data_dir, "train.npz"))
    X_all = data["X"]
    y_all = data["y"]
    pnl_r_all = data.get("pnl_r", data.get("r", np.zeros_like(y_all)))
    weights_all = data.get("sample_weight", np.ones(len(X_all)))

    with open(os.path.join(data_dir, "cv_splits.json")) as f:
        splits = json.load(f)

    os.makedirs(output_dir, exist_ok=True)
    all_results: dict[str, list[dict[str, Any]]] = {"lightgbm": [], "xgboost": []}

    for fold_idx, split in enumerate(splits):
        train_idx = split["train_idx"]
        val_idx = split["test_idx"]
        X_tr, y_tr = X_all[train_idx], y_all[train_idx]
        X_val, y_val = X_all[val_idx], y_all[val_idx]
        w_tr = weights_all[train_idx]
        r_val = pnl_r_all[val_idx] if len(pnl_r_all) > 0 else np.zeros(len(val_idx))

        print(f"\n  ── Fold {fold_idx+1}/{len(splits)}: train={len(X_tr)}, val={len(X_val)} ──")

        # ── LightGBM ──
        try:
            import lightgbm as lgb

            params = {
                "objective": "regression", "metric": "rmse",
                "boosting_type": "gbdt", "num_leaves": 31,
                "learning_rate": 0.02, "feature_fraction": 0.8,
                "bagging_fraction": 0.8, "bagging_freq": 5,
                "verbose": -1, "seed": 42,
            }
            dtrain = lgb.Dataset(X_tr, label=y_tr, weight=w_tr)
            dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)
            model = lgb.train(params, dtrain, valid_sets=[dval],
                              num_boost_round=500,
                              callbacks=[lgb.early_stopping(50)])
            model.save_model(os.path.join(output_dir, f"lightgbm_fold{fold_idx}_s42.txt"))

            y_pred = model.predict(X_val)
            ev = _evaluate_model(y_val, y_pred, r_val)
            all_results["lightgbm"].append(ev)
            print(f"    LGB: DirAcc={ev['directional_accuracy']:.3f} "
                  f"LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} "
                  f"bt_trades={len(ev.get('trade_rs',[]))}")
        except ImportError:
            pass

        # ── XGBoost ──
        try:
            import xgboost as xgb

            params = {
                "objective": "reg:squarederror", "eval_metric": "rmse",
                "max_depth": 5, "learning_rate": 0.02,
                "subsample": 0.8, "colsample_bytree": 0.8,
                "seed": 42, "verbosity": 0,
            }
            dtrain = xgb.DMatrix(X_tr, label=y_tr, weight=w_tr)
            dval = xgb.DMatrix(X_val, label=y_val)
            model = xgb.train(params, dtrain, num_boost_round=500,
                              evals=[(dval, "val")],
                              early_stopping_rounds=50, verbose_eval=False)
            model.save_model(os.path.join(output_dir, f"xgboost_fold{fold_idx}_s42.json"))

            y_pred = model.predict(dval)
            ev = _evaluate_model(y_val, y_pred, r_val)
            all_results["xgboost"].append(ev)
            print(f"    XGB: DirAcc={ev['directional_accuracy']:.3f} "
                  f"LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} "
                  f"bt_trades={len(ev.get('trade_rs',[]))}")
        except ImportError:
            pass

    # ── Aggregate results across folds ──
    summary = {}
    for model_name, fold_results in all_results.items():
        if not fold_results:
            continue
        keys = ["directional_accuracy", "long_recall", "short_recall",
                "long_precision", "short_precision"]
        agg = {}
        for k in keys:
            vals = [f[k] for f in fold_results]
            agg[f"{k}_mean"] = float(np.mean(vals))
            agg[f"{k}_std"] = float(np.std(vals))

        # Aggregate backtest: concatenate raw trade R-values across all folds
        all_rs: list[float] = []
        for entry in fold_results:
            all_rs.extend(entry.get("trade_rs", []))
        if all_rs:
            r_arr = np.array(all_rs)
            cumulative = np.cumsum(r_arr)
            peak = np.maximum.accumulate(cumulative)
            drawdown = peak - cumulative
            agg["backtest_total_trades"] = len(r_arr)
            agg["backtest_total_r"] = float(np.sum(r_arr))
            agg["backtest_mean_r"] = float(np.mean(r_arr))
            agg["backtest_win_rate"] = float(np.mean(r_arr > 0))
            agg["backtest_sharpe"] = float(np.mean(r_arr) / np.std(r_arr)) if np.std(r_arr) > 0 else 0.0
            agg["backtest_max_drawdown_r"] = float(np.max(drawdown))
            pos_r = r_arr[r_arr > 0]
            neg_r = r_arr[r_arr < 0]
            agg["backtest_profit_factor"] = float(np.sum(pos_r) / abs(np.sum(neg_r))) if len(neg_r) > 0 else float("inf")

        summary[model_name] = agg

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="BTC Directional Brain V10")
    parser.add_argument("--timeframe", default="H1", choices=["H1", "M15", "M30", "H4"])
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--csv", default=None, help="Override CSV path")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-train", action="store_true")
    args = parser.parse_args()

    tf = args.timeframe
    data_dir = args.data_dir or f"data/training/btc_directional_{tf.lower()}"
    model_dir = args.model_dir or data_dir

    if args.csv:
        csv_path = args.csv
    else:
        csv_map = {"H1": "data/raw/btcusdc_h1_merged.csv",
                   "M15": "data/raw/btcusdc_m15_merged.csv",
                   "M30": "data/raw/btcusdc_m30_merged.csv",
                   "H4": "data/raw/btcusdc_h4_merged.csv"}
        csv_path = csv_map.get(tf, f"data/raw/btcusdc_{tf.lower()}_merged.csv")
    tf_minutes = {"H1": 60.0, "M15": 15.0, "M30": 30.0, "H4": 240.0}[tf]

    np.random.seed(42)

    print(f"{'='*60}")
    print(f"BTC Directional Brain V10 — {tf}")
    print(f"  SL={SL_ATR_MULT}×ATR  TP={TP_ATR_MULT}×ATR  spread={SPREAD_POINTS}")
    print(f"  horizon={args.horizon}  folds={args.cv_folds}")
    print(f"  data: {csv_path}")
    print(f"{'='*60}")

    if not args.skip_build:
        build_dataset(
            csv_path=csv_path, output_dir=data_dir,
            horizon=args.horizon,
            sl_atr_mult=SL_ATR_MULT, tp_atr_mult=TP_ATR_MULT,
            spread_points=SPREAD_POINTS, slippage_points=SLIPPAGE_POINTS,
            cv_folds=args.cv_folds, timeframe_minutes=tf_minutes,
        )

    if not args.skip_train:
        results = train_models(data_dir, model_dir)

        summary = {
            "schema_version": "btc_directional_v10.v1",
            "timeframe": tf, "horizon": args.horizon,
            "sl_atr_mult": SL_ATR_MULT, "tp_atr_mult": TP_ATR_MULT,
            "spread_points": SPREAD_POINTS, "slippage_points": SLIPPAGE_POINTS,
            "model_results": results,
            "trained_at": datetime.now(UTC).isoformat(),
        }
        with open(os.path.join(model_dir, "training_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        print(f"\n{'='*60}")
        for mname, mres in results.items():
            print(f"  {mname}: DirAcc={mres.get('directional_accuracy_mean',0):.3f}±{mres.get('directional_accuracy_std',0):.3f} "
                  f"LongRec={mres.get('long_recall_mean',0):.3f} ShortRec={mres.get('short_recall_mean',0):.3f} "
                  f"bt_trades={mres.get('backtest_total_trades',0)} bt_WR={mres.get('backtest_win_rate',0):.1%} "
                  f"bt_R={mres.get('backtest_total_r',0):.1f}")
        print(f"  Summary: {model_dir}/training_summary.json")


if __name__ == "__main__":
    main()

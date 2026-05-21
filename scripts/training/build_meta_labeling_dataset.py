#!/usr/bin/env python
"""Build meta-labeling dataset: OU signal trace + 49 features + MFE labels.

Generates training data for the "precision filter" ML model:
  1. Runs the OU_Params_V6_Sniper signal engine on full M5 history
  2. At each signal-fire moment, computes 49-dim features (40 V9 + 9 micro)
  3. Computes meta-labels: MFE, breakeven hit, TP hit within horizon
  4. Outputs NPZ ready for institutional_train.py

The ML model learns: P(signal_succeeds | features, signal_fired=1)
NOT: P(direction | features) — the latter has 0.03 correlation and fails.

Usage:
  python scripts/training/build_meta_labeling_dataset.py \
    --price-data data/raw/xauusdc_m5_1y.csv \
    --ou-params data/models/arb_params_v7.json \
    --output-dir data/training/meta_labeling_v1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from core.alpha.ou_optimizer import (
    KalmanHalfLifeFilter,
    calc_ou_params,
    compute_trend_mute,
)

# Reuse feature computation from calibrated dataset builder
from scripts.training.build_calibrated_dataset import (
    MICRO_FEATURE_NAMES,
    compute_features_at_bar,
    load_cross_symbol_data,
    load_ohlc_arrays,
    precompute_micro_features,
)

# XAU-only micro features (no cross-symbol dependency)
MICRO_FEATURE_NAMES_LOCAL = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
]

# ── OU signal defaults (overridden by --ou-params) ──────────────────────────
DEFAULT_OU_PARAMS: dict[str, float] = {
    "window": 250,
    "z_entry": 1.3,
    "z_exit": 1.0,
    "max_half_life": 58,
    "theta_min": 0.00142,
}

# ── Meta-label horizon config ───────────────────────────────────────────────
META_HORIZON_BARS = 12
MFE_HORIZON_BARS = 6  # shorter horizon for MFE regression target
BREAKEVEN_ATR_MULT = 1.0
STOP_ATR_MULT = 1.5
PARALLEL_FEATURE_NAME = "ou_z_entry"  # 50th feature: z_entry threshold from signal universe


def load_ou_params(params_path: Path | None) -> dict[str, float]:
    """Load optimized OU params from arb_params JSON, or use defaults."""
    if params_path is None or not params_path.exists():
        return dict(DEFAULT_OU_PARAMS)

    with open(params_path, encoding="utf-8") as f:
        data = json.load(f)

    opt = data.get("optimal_params", {})
    return {
        "window": int(opt.get("window", DEFAULT_OU_PARAMS["window"])),
        "z_entry": float(opt.get("z_entry", DEFAULT_OU_PARAMS["z_entry"])),
        "z_exit": float(opt.get("z_exit", DEFAULT_OU_PARAMS["z_exit"])),
        "max_half_life": float(opt.get("max_half_life", DEFAULT_OU_PARAMS["max_half_life"])),
        "theta_min": float(opt.get("theta_min", DEFAULT_OU_PARAMS["theta_min"])),
    }


def extract_signal_trace(
    prices: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    ou_params: dict[str, float],
    use_kalman: bool = True,
    use_trend_mute: bool = True,
) -> list[dict]:
    """Run OU signal engine and extract signal-fire metadata.

    Returns list of dicts, each containing:
      entry_idx: bar index of signal fire
      direction: 1 (long) or -1 (short)
      z_score: OU z-score at signal
      z_entry: z_entry threshold used (tagged for parallel universes)
      half_life: Kalman half-life at signal
      theta: smoothed theta at signal
      entry_price: price at signal fire
      exit_idx: bar index of signal exit (or -1 if still open at end)
      exit_price: exit price (or last price)
      exit_reason: "z_exit", "opposite_z", or "end_of_data"
    """
    window = ou_params["window"]
    z_entry = ou_params["z_entry"]
    z_exit = ou_params["z_exit"]
    max_half_life = ou_params["max_half_life"]
    theta_min = ou_params["theta_min"]

    n = len(prices)
    kf = KalmanHalfLifeFilter() if use_kalman else None

    if use_trend_mute:
        close_for_mute = prices
        trend_mute = compute_trend_mute(close_for_mute)
    else:
        trend_mute = np.ones(n)

    signals: list[dict] = []
    position = 0
    entry_price = 0.0
    entry_idx = 0
    entry_dir = 0

    for i in range(int(window), n - META_HORIZON_BARS):
        window_prices = prices[i - int(window) : i]
        current_price = prices[i]

        ou = calc_ou_params(window_prices)
        theta_raw = ou["theta"]
        z_score = ou["z_score"]

        if kf is not None:
            theta_smooth = kf.update(theta_raw)
            half_life = kf.half_life
        else:
            theta_smooth = theta_raw
            half_life = ou["half_life"]

        mute_factor = float(trend_mute[i]) if use_trend_mute else 1.0

        if position == 0:
            if half_life < max_half_life and theta_smooth > theta_min and mute_factor > 0.3:
                if z_score < -z_entry:
                    position = 1
                    entry_price = float(current_price)
                    entry_idx = i
                    entry_dir = 1
                elif z_score > z_entry:
                    position = -1
                    entry_price = float(current_price)
                    entry_idx = i
                    entry_dir = -1
        elif position == 1:
            if z_score > -z_exit or z_score > z_entry * 0.3:
                signals.append(
                    {
                        "entry_idx": entry_idx,
                        "direction": entry_dir,
                        "z_score": float(z_score),
                        "z_entry": z_entry,
                        "half_life": float(half_life),
                        "theta": float(theta_smooth),
                        "entry_price": float(entry_price),
                        "exit_idx": i,
                        "exit_price": float(current_price),
                        "exit_reason": "z_exit" if z_score > -z_exit else "opposite_z",
                    }
                )
                position = 0
        elif position == -1:
            if z_score < z_exit or z_score < -z_entry * 0.3:
                signals.append(
                    {
                        "entry_idx": entry_idx,
                        "direction": entry_dir,
                        "z_score": float(z_score),
                        "z_entry": z_entry,
                        "half_life": float(half_life),
                        "theta": float(theta_smooth),
                        "entry_price": float(entry_price),
                        "exit_idx": i,
                        "exit_price": float(current_price),
                        "exit_reason": "z_exit" if z_score < z_exit else "opposite_z",
                    }
                )
                position = 0

    if position != 0:
        signals.append(
            {
                "entry_idx": entry_idx,
                "direction": entry_dir,
                "z_score": 0.0,
                "z_entry": z_entry,
                "half_life": 0.0,
                "theta": 0.0,
                "entry_price": float(entry_price),
                "exit_idx": n - 1,
                "exit_price": float(prices[-1]),
                "exit_reason": "end_of_data",
            }
        )

    return signals


def extract_parallel_universes(
    prices: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    base_ou_params: dict[str, float],
    z_entry_list: list[float],
    use_kalman: bool = True,
    use_trend_mute: bool = True,
) -> list[dict]:
    """Parallel Universe Sampling: extract signals for multiple z_entry thresholds.

    Each z_entry value defines a parallel universe with identical microstructural
    conditions but different signal-trigger sensitivity. This expands the training
    set 3-5× without collecting new data — a standard institutional technique for
    small-sample meta-labeling.

    Deduplication: if multiple universes fire at the same bar in the same direction,
    keep only the signal from the most conservative (highest) z_entry.
    """
    all_signals: list[dict] = []

    for z_entry in z_entry_list:
        params = dict(base_ou_params)
        params["z_entry"] = z_entry
        signals = extract_signal_trace(
            prices,
            highs,
            lows,
            params,
            use_kalman=use_kalman,
            use_trend_mute=use_trend_mute,
        )
        all_signals.extend(signals)

    # Sort chronologically. Do NOT deduplicate — z_entry is a feature that
    # differentiates parallel-universe samples at the same bar.
    result = sorted(all_signals, key=lambda s: (s["entry_idx"], s["z_entry"]))

    # Report overlap stats
    keys = [(s["entry_idx"], s["direction"]) for s in result]
    unique_keys = set(keys)
    n_overlap = len(keys) - len(unique_keys)
    if n_overlap > 0:
        print(
            f"       Parallel universes: {len(result)} total signals "
            f"({len(unique_keys)} unique entry points, "
            f"{n_overlap} parallel-universe duplicates)"
        )

    return result


def compute_meta_labels(
    signals: list[dict],
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    atr_values: np.ndarray,
) -> list[dict]:
    """Compute meta-labels for each signal.

    For each signal, computes:
      - mfe_6bar: Maximum favorable excursion (in ATR units) within 6 bars
      - mfe_12bar: Maximum favorable excursion (in ATR units) within 12 bars
      - hit_breakeven: Did price reach +1.0 ATR in signal direction before -1.5 ATR?
      - signal_pnl_r: PnL at exit (if within 12 bars) in ATR units

    MFE is the max price move in the signal direction relative to entry,
    expressed in ATR units at entry.
    """
    labeled: list[dict] = []
    n = len(closes)

    for sig in signals:
        entry_idx = sig["entry_idx"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        atr = float(atr_values[entry_idx]) if entry_idx < len(atr_values) else 5.0
        if atr <= 0:
            atr = 5.0

        # Compute MFE within 6 bars
        end_6 = min(entry_idx + MFE_HORIZON_BARS + 1, n)
        prices_6 = closes[entry_idx:end_6]
        if direction == 1:
            mfe_6bar = float(np.max(prices_6) - entry_price) / atr
            mae_6bar = float(entry_price - np.min(prices_6)) / atr
        else:
            mfe_6bar = float(entry_price - np.min(prices_6)) / atr
            mae_6bar = float(np.max(prices_6) - entry_price) / atr

        # Compute MFE within 12 bars
        end_12 = min(entry_idx + META_HORIZON_BARS + 1, n)
        prices_12 = closes[entry_idx:end_12]
        if direction == 1:
            mfe_12bar = float(np.max(prices_12) - entry_price) / atr
        else:
            mfe_12bar = float(entry_price - np.min(prices_12)) / atr

        # Compute breakeven hit: did price reach +1.0 ATR before -1.5 ATR?
        # Walk forward bar by bar within horizon
        breakeven_hit = False
        sl_hit = False
        tp_level = entry_price + direction * BREAKEVEN_ATR_MULT * atr
        sl_level = entry_price - direction * STOP_ATR_MULT * atr

        for j in range(entry_idx + 1, min(entry_idx + META_HORIZON_BARS + 1, n)):
            bar_high = highs[j]
            bar_low = lows[j]
            if direction == 1:
                if bar_high >= tp_level:
                    breakeven_hit = True
                    break
                if bar_low <= sl_level:
                    sl_hit = True
                    break
            else:
                if bar_low <= tp_level:
                    breakeven_hit = True
                    break
                if bar_high >= sl_level:
                    sl_hit = True
                    break

        # Signal PnL in R-units (at actual exit or 12-bar mark, whichever comes first)
        exit_idx = min(sig["exit_idx"], entry_idx + META_HORIZON_BARS)
        exit_price_slice = closes[exit_idx] if exit_idx < n else closes[-1]
        signal_pnl_r = direction * (exit_price_slice - entry_price) / atr

        labeled.append(
            {
                **sig,
                "mfe_6bar": round(mfe_6bar, 6),
                "mfe_12bar": round(mfe_12bar, 6),
                "hit_breakeven": int(breakeven_hit),
                "hit_sl": int(sl_hit),
                "timeout": int(not breakeven_hit and not sl_hit),
                "signal_pnl_r": round(signal_pnl_r, 6),
                "atr_at_entry": round(atr, 6),
            }
        )

    return labeled


def build_meta_dataset(
    signals_labeled: list[dict],
    ohlc: dict[str, np.ndarray],
    x_micro: np.ndarray,
    micro_feature_names: list[str],
    *,
    warmup_bars: int = 500,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Build feature matrix and label vectors at signal-fire moments.

    Returns (X, y_breakeven, y_mfe_12bar, signal_pnl_r, timestamps, feature_names).
    """
    o = ohlc["open"]
    h = ohlc["high"]
    l = ohlc["low"]
    c = ohlc["close"]
    v = ohlc["volume"]
    ts_epoch = ohlc["timestamp_epoch"]
    n_bars = ohlc["n_bars"]

    # Get V9 feature names
    v9_feature_names = sorted(
        compute_features_at_bar(o, h, l, c, v, min(warmup_bars, n_bars - 1)).keys()
    )
    all_feature_names = v9_feature_names + micro_feature_names + [PARALLEL_FEATURE_NAME]

    X_rows: list[list[float]] = []
    y_breakeven: list[int] = []
    y_mfe_12bar: list[float] = []
    y_signal_pnl: list[float] = []
    y_direction: list[int] = []
    ts_rows: list[float] = []
    matched = 0
    dropped_nan = 0
    skipped_warmup = 0

    for sig in signals_labeled:
        entry_idx = sig["entry_idx"]
        if entry_idx < warmup_bars or entry_idx >= n_bars:
            skipped_warmup += 1
            continue

        # Check micro features
        micro_vec = x_micro[entry_idx]
        if np.any(np.isnan(micro_vec)):
            dropped_nan += 1
            continue

        # Compute V9 features
        feat_vec_dict = compute_features_at_bar(o, h, l, c, v, entry_idx)
        feat_vec = [float(feat_vec_dict.get(fn, 0.0)) for fn in v9_feature_names]
        feat_vec.extend(float(mv) for mv in micro_vec)
        feat_vec.append(float(sig.get("z_entry", 1.3)))  # 50th feature: z_entry threshold
        X_rows.append(feat_vec)

        y_breakeven.append(sig["hit_breakeven"])
        y_mfe_12bar.append(sig["mfe_12bar"])
        y_signal_pnl.append(sig["signal_pnl_r"])
        y_direction.append(sig["direction"])
        ts_rows.append(float(ts_epoch[entry_idx]))
        matched += 1

    print(
        f"  Signal-feature matching: {matched} matched, "
        f"{skipped_warmup} warmup-skipped, {dropped_nan} nan-dropped"
    )

    X = np.array(X_rows, dtype=np.float64)
    y_be = np.array(y_breakeven, dtype=np.int32)
    y_mfe = np.array(y_mfe_12bar, dtype=np.float64)
    y_pnl = np.array(y_signal_pnl, dtype=np.float64)
    y_dir = np.array(y_direction, dtype=np.int8)
    timestamps = np.array(ts_rows, dtype=np.float64)

    return X, y_be, y_mfe, y_pnl, y_dir, timestamps, all_feature_names


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_meta_labeling_dataset",
        description="Build meta-labeling dataset from OU signal trace + 49 features",
    )
    p.add_argument("--price-data", type=Path, required=True, help="XAUUSD M5 OHLC CSV")
    p.add_argument(
        "--ou-params",
        type=Path,
        default=None,
        help="OU params JSON (default: OU_Params_V6_Sniper built-in)",
    )
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--val-split", type=float, default=0.15)
    p.add_argument("--warmup-bars", type=int, default=500)
    p.add_argument("--no-kalman", action="store_true", help="Disable Kalman filter")
    p.add_argument("--no-trend-mute", action="store_true", help="Disable ADX trend mute")
    p.add_argument(
        "--no-cross-symbol",
        action="store_true",
        help="Skip cross-symbol features (EUR/JPY/XAG). Removes 3 features but eliminates NaN-dropping, expanding sample coverage to ALL bars.",
    )
    p.add_argument(
        "--z-entry-list",
        type=str,
        default=None,
        help="Comma-separated z_entry values for parallel universe sampling (e.g. '1.9,1.95,2.0,2.05,2.1')",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── 1. Load OU params ──────────────────────────────────────────────
    print("[1/6] Loading OU parameters...")
    ou_params = load_ou_params(args.ou_params)
    print(
        f"       window={ou_params['window']}, z_entry={ou_params['z_entry']}, "
        f"z_exit={ou_params['z_exit']}, max_hl={ou_params['max_half_life']}, "
        f"theta_min={ou_params['theta_min']}"
    )

    # ── 2. Load OHLC data ──────────────────────────────────────────────
    print("[2/6] Loading OHLC data...")
    try:
        ohlc = load_ohlc_arrays(args.price_data)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1
    print(f"       {ohlc['n_bars']} bars loaded")
    closes = ohlc["close"]

    # ── 3. Load cross-symbol data (optional) + precompute micro features ─
    if args.no_cross_symbol:
        print("[3/6] Skipping cross-symbol data (--no-cross-symbol)")
        print("[4/6] Precomputing 6 XAU-local micro features...")
        cross_data = None
        x_micro_full = precompute_micro_features(ohlc, None)
        # Only keep the 6 XAU-local micro features (first 6 columns)
        x_micro = x_micro_full[:, :6]
        micro_names = MICRO_FEATURE_NAMES_LOCAL
    else:
        print("[3/6] Loading cross-symbol data...")
        csv_dir = args.price_data.parent
        cross_data = load_cross_symbol_data(csv_dir, ohlc["timestamp"])
        print("[4/6] Precomputing 9 micro features...")
        x_micro = precompute_micro_features(ohlc, cross_data)
        micro_names = MICRO_FEATURE_NAMES

    # ── 4. Extract OU signal trace (parallel universes or single) ──────
    z_entry_list: list[float] | None = None
    if args.z_entry_list:
        z_entry_list = [float(x.strip()) for x in args.z_entry_list.split(",")]
        print(f"[5/6] Parallel Universe Sampling: z_entry ∈ {z_entry_list}")
        signals = extract_parallel_universes(
            closes,
            ohlc["high"],
            ohlc["low"],
            ou_params,
            z_entry_list,
            use_kalman=not args.no_kalman,
            use_trend_mute=not args.no_trend_mute,
        )
        for ze in z_entry_list:
            n_ze = sum(1 for s in signals if abs(s["z_entry"] - ze) < 0.001)
            print(f"       z_entry={ze}: {n_ze} signals")
    else:
        print("[5/6] Extracting OU signal trace (single universe)...")
        signals = extract_signal_trace(
            closes,
            ohlc["high"],
            ohlc["low"],
            ou_params,
            use_kalman=not args.no_kalman,
            use_trend_mute=not args.no_trend_mute,
        )
    print(f"       {len(signals)} total signals")

    if len(signals) < 50:
        print("[ERROR] Too few signals — check OU params or data range")
        return 1

    # Compute ATR values for meta-label scaling
    # Use simple ATR computation (14-period on closes as proxy)
    atr_period = 14
    atr_values = np.zeros(len(closes))
    for i in range(atr_period, len(closes)):
        tr_vals = np.maximum(
            ohlc["high"][i - atr_period + 1 : i + 1] - ohlc["low"][i - atr_period + 1 : i + 1],
            np.maximum(
                np.abs(ohlc["high"][i - atr_period + 1 : i + 1] - closes[i - atr_period : i]),
                np.abs(ohlc["low"][i - atr_period + 1 : i + 1] - closes[i - atr_period : i]),
            ),
        )
        atr_values[i] = float(np.mean(tr_vals))

    # ── 5. Compute meta-labels ────────────────────────────────────────
    signals_labeled = compute_meta_labels(signals, ohlc["high"], ohlc["low"], closes, atr_values)

    be_hit = sum(1 for s in signals_labeled if s["hit_breakeven"])
    sl_hit = sum(1 for s in signals_labeled if s["hit_sl"])
    to = sum(1 for s in signals_labeled if s["timeout"])
    print(f"       Breakeven hit: {be_hit} ({be_hit/len(signals_labeled):.1%})")
    print(f"       SL hit: {sl_hit} ({sl_hit/len(signals_labeled):.1%})")
    print(f"       Timeout: {to} ({to/len(signals_labeled):.1%})")

    # ── 6. Build feature matrix ───────────────────────────────────────
    n_micro = len(micro_names)
    print(f"[6/6] Building {40 + n_micro + 1}-dim features at signal-fire moments...")
    X, y_be, y_mfe, y_pnl, y_dir, timestamps, feature_names = build_meta_dataset(
        signals_labeled, ohlc, x_micro, micro_names, warmup_bars=args.warmup_bars
    )

    if len(X) < 50:
        print(f"[ERROR] Only {len(X)} matched signals — insufficient for training")
        return 1

    be_rate = float((y_be == 1).mean())
    print(f"       Features: {X.shape[1]}, Signals matched: {X.shape[0]}")
    print(f"       Breakeven rate: {be_rate:.1%}")
    print(f"       Mean MFE: {float(np.mean(y_mfe)):.4f} ATR")
    print(f"       Long/Short: {int(np.sum(y_dir == 1))}/{int(np.sum(y_dir == -1))}")

    # ── 7. Split and save ─────────────────────────────────────────────
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Chronological split
    split_idx = int(len(X) * (1 - args.val_split))

    for split_name, sl in [("train", slice(0, split_idx)), ("val", slice(split_idx, len(X)))]:
        np.savez_compressed(
            out_dir / f"{split_name}.npz",
            X=X[sl],
            y_breakeven=y_be[sl],
            y_mfe_12bar=y_mfe[sl],
            y_signal_pnl=y_pnl[sl],
            y_direction=y_dir[sl],
            timestamps=timestamps[sl],
            feature_names=np.array(feature_names),
            schema="meta_labeling_ou_sniper_v1",
        )

    print(f"       Train: {split_idx} signals")
    print(f"       Val:   {len(X) - split_idx} signals")
    print(f"       Saved to: {out_dir}")

    # ── 8. Meta ───────────────────────────────────────────────────────
    meta = {
        "schema_version": "meta_labeling_dataset.v1",
        "base_alpha": "OU_Params_V6_Sniper",
        "ou_params": ou_params,
        "n_signals_raw": len(signals),
        "n_signals_matched": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "feature_names": feature_names,
        "breakeven_rate": round(float(be_rate), 4),
        "mean_mfe_12bar": round(float(np.mean(y_mfe)), 4),
        "label_contract": "Meta-Label: OU signal → breakeven proxy within 12 bars",
    }
    with open(out_dir / "dataset_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

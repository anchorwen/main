"""
Task A — Directional Alpha Closure Test (M5 Barrier Prediction).

Rebuilds the dataset with correct v1.2.1 barrier parameters (SL=2.0, TP=1.25, H=12)
and runs the simplest possible baseline (Logistic Regression) to determine whether
40-dim v9_institutional features have ANY predictive power for barrier outcomes.

Gate criteria (both must pass):
  - OOS accuracy > 54% (unconditional TP rate ≈ 52.3%, requires ≥2pp lift)
  - Net EV > 0 after spread+slippage costs

If either fails: permanently bury M5-level directional prediction.
If both pass:    directional route survives, minimal signal warrants deeper investigation.

Usage:
    python scripts/task_a_directional_closure.py --data data/raw/xauusdc_m5_merged.csv
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.preprocessing import StandardScaler

# -- Project imports --
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.training.build_calibrated_dataset import compute_features_at_bar

warnings.filterwarnings("ignore", category=UserWarning)

# ── v1.2.1 Barrier contract (correct params) ──────────────────────────────────
SL_MULT = 2.0  # Stop-loss in ATR units
TP_MULT = 1.25  # Take-profit in ATR units
HORIZON = 12  # M5 bars (1 hour)
ENTRY_SPACING = 12  # bars between blind entries
ATR_PERIOD = 14
LOOKBACK = 480  # bars needed for feature computation
COST_POINTS = 40  # spread(30) + slippage(10) in XAUUSD price units

# ── Gate thresholds ───────────────────────────────────────────────────────────
MIN_ACCURACY = 0.54  # Must beat unconditional TP rate by >=2pp
MIN_NET_EV = 0.0  # Must be positive after costs


def load_ohlc(path: str) -> dict[str, np.ndarray]:
    """Load OHLC CSV → numpy arrays."""
    print(f"[1/6] Loading data: {path}")
    data = np.loadtxt(
        path,
        delimiter=",",
        skiprows=1,
        dtype={
            "names": (
                "time",
                "open",
                "high",
                "low",
                "close",
                "tick_volume",
                "spread",
                "real_volume",
            ),
            "formats": ("U19", "f8", "f8", "f8", "f8", "i8", "i8", "i8"),
        },
    )
    print(f"       {len(data)} bars: {data['time'][0]} → {data['time'][-1]}")
    return {
        "o": data["open"],
        "h": data["high"],
        "l": data["low"],
        "c": data["close"],
        "v": data["tick_volume"],
        "n": len(data),
    }


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder's ATR."""
    n = len(close)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _resolve_barrier(future_highs, future_lows, tp_price, sl_price, horizon, direction):
    """Determine which barrier hits first. Vectorized inner loop."""
    if direction == "long":
        tp_bars = np.flatnonzero(future_highs >= tp_price)
        sl_bars = np.flatnonzero(future_lows <= sl_price)
    else:
        tp_bars = np.flatnonzero(future_lows <= tp_price)
        sl_bars = np.flatnonzero(future_highs >= sl_price)

    tp_first = int(tp_bars[0]) if len(tp_bars) > 0 else horizon + 1
    sl_first = int(sl_bars[0]) if len(sl_bars) > 0 else horizon + 1

    if tp_first > horizon and sl_first > horizon:
        return "timeout"
    if tp_first < sl_first:
        return "tp"
    if sl_first < tp_first:
        return "sl"
    # Same bar — tiebreaker by breach depth
    if direction == "long":
        tp_breach = (future_highs[tp_first] - tp_price) / tp_price
        sl_breach = (sl_price - future_lows[sl_first]) / sl_price
    else:
        tp_breach = (tp_price - future_lows[tp_first]) / tp_price
        sl_breach = (future_highs[sl_first] - sl_price) / sl_price
    return "tp" if tp_breach >= sl_breach else "sl"


def generate_labels(ohlc: dict, atr: np.ndarray) -> list[dict]:
    """Generate barrier labels at blind entry points (SL=2.0, TP=1.25, H=12)."""
    print("[2/6] Generating barrier labels (SL=2.0, TP=1.25, H=12)...")
    n = ohlc["n"]
    h, l_low, o = ohlc["h"], ohlc["l"], ohlc["o"]

    labels = []
    n_skipped = 0

    for entry_idx in range(LOOKBACK, n - HORIZON, ENTRY_SPACING):
        atr_val = atr[entry_idx]
        if np.isnan(atr_val) or atr_val <= 0:
            n_skipped += 1
            continue

        entry_price = o[entry_idx]
        end_idx = min(entry_idx + HORIZON + 1, n)
        fh = h[entry_idx + 1 : end_idx]
        fl = l_low[entry_idx + 1 : end_idx]

        for direction in ("long", "short"):
            if direction == "long":
                tp_p = entry_price + TP_MULT * atr_val
                sl_p = entry_price - SL_MULT * atr_val
            else:
                tp_p = entry_price - TP_MULT * atr_val
                sl_p = entry_price + SL_MULT * atr_val

            outcome = _resolve_barrier(fh, fl, tp_p, sl_p, HORIZON, direction)
            labels.append(
                {"entry_idx": entry_idx, "direction": direction, "atr": atr_val, "outcome": outcome}
            )

    print(
        f"       {len(labels)} labels ({len(labels)//2} long + {len(labels)//2} short), "
        f"{n_skipped} skipped (NaN ATR)"
    )
    return labels


def compute_features_batch(
    ohlc: dict, labels: list[dict]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute features at each labeled entry point. Uses existing compute_features_at_bar."""
    print("[3/6] Computing features at entry points...")
    o, h, l, c, v = ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"], ohlc["v"]
    n_labels = len(labels)

    feature_list = []
    y_list = []
    pnl_list = []
    t0 = time.time()

    for i, lab in enumerate(labels):
        feats = compute_features_at_bar(o, h, l, c, v, lab["entry_idx"])
        feature_list.append(feats)

        # Binary label: 1 = TP hit, 0 = SL hit or timeout
        y_list.append(1 if lab["outcome"] == "tp" else 0)

        # Gross PnL in ATR units (before costs)
        if lab["outcome"] == "tp":
            pnl_list.append(TP_MULT if lab["direction"] == "long" else TP_MULT)
        elif lab["outcome"] == "sl":
            pnl_list.append(-SL_MULT)
        else:
            pnl_list.append(0.0)

        if (i + 1) % 5000 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (n_labels - i - 1) / rate
            print(
                f"       {i+1}/{n_labels} features computed " f"({rate:.0f} bar/s, ETA {eta:.0f}s)"
            )

    elapsed = time.time() - t0
    print(f"       Done: {n_labels} samples in {elapsed:.0f}s ({n_labels/elapsed:.0f} bar/s)")

    # Build feature matrix
    feature_names = sorted(feature_list[0].keys())
    X = np.array([[f[n] for n in feature_names] for f in feature_list], dtype=np.float64)
    y = np.array(y_list, dtype=np.int32)
    pnl = np.array(pnl_list, dtype=np.float64)

    # Handle NaN/Inf in features
    nan_mask = ~np.isfinite(X).all(axis=1)
    if nan_mask.any():
        print(f"       WARNING: {nan_mask.sum()} rows with NaN/Inf features — dropping")
        X, y, pnl = X[~nan_mask], y[~nan_mask], pnl[~nan_mask]

    return X, y, pnl


def split_time_series(X, y, pnl, train_pct=0.60, val_pct=0.20, purge_bars=12):
    """Time-series split with purge gap."""
    n = len(X)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))

    print(
        f"[4/6] Time-series split: train[0:{train_end}], val[{train_end}:{val_end}], "
        f"test[{val_end}:{n}] (purge={purge_bars} bars)"
    )

    return (
        (X[:train_end], y[:train_end], pnl[:train_end]),
        (X[train_end:val_end], y[train_end:val_end], pnl[train_end:val_end]),
        (X[val_end:], y[val_end:], pnl[val_end:]),
    )


def evaluate_model(
    model, scaler, X_train, y_train, X_val, y_val, X_test, y_test, pnl_test, atr_mean
):
    """Comprehensive evaluation with cost-adjusted net EV."""
    print("\n[5/6] Model Evaluation")
    print("=" * 70)

    # -- Accuracy --
    y_train_pred = model.predict(X_train)
    y_val_pred = model.predict(X_val)
    y_test_pred = model.predict(X_test)

    train_acc = accuracy_score(y_train, y_train_pred)
    val_acc = accuracy_score(y_val, y_val_pred)
    test_acc = accuracy_score(y_test, y_test_pred)

    # Unconditional baseline
    unconditional_tp_rate = y_train.mean()

    print(f"\n  Unconditional TP rate (always-long baseline): {unconditional_tp_rate*100:.2f}%")
    print(f"  Train accuracy:  {train_acc*100:.2f}%")
    print(f"  Val accuracy:    {val_acc*100:.2f}%")
    print(f"  Test accuracy:   {test_acc*100:.2f}%")
    print(f"  Lift over baseline: {(test_acc - unconditional_tp_rate)*100:+.2f} pp")

    # -- Trade simulation on test set --
    print(f"\n  ── Trade Simulation (Test Set, n={len(y_test_pred)}) ──")

    # Model-selected trades: only where model predicts TP (y_pred=1)
    selected_mask = y_test_pred == 1
    n_selected = selected_mask.sum()
    n_rejected = (~selected_mask).sum()

    if n_selected > 0:
        selected_pnl = pnl_test[selected_mask]
        n_tp = (selected_pnl > 0).sum()
        n_sl = (selected_pnl < 0).sum()
        n_to = (selected_pnl == 0).sum()
        gross_ev = selected_pnl.mean()

        # Cost in ATR units
        cost_atr = COST_POINTS * 0.001 / atr_mean  # 0.001 = tick_size for XAUUSD
        net_ev = gross_ev - cost_atr  # cost per trade

        cond_win_rate = n_tp / (n_tp + n_sl) * 100 if (n_tp + n_sl) > 0 else 0
    else:
        n_tp = n_sl = n_to = 0
        gross_ev = net_ev = 0.0
        cond_win_rate = 0

    # Unconditional (all test trades, no model filtering)
    n_test = len(pnl_test)
    n_all_tp = (pnl_test > 0).sum()
    n_all_sl = (pnl_test < 0).sum()
    n_all_to = (pnl_test == 0).sum()
    unconditional_gross_ev = pnl_test.mean()
    unconditional_net_ev = unconditional_gross_ev - cost_atr

    print("  Unconditional (all entries):")
    print(
        f"    TP={n_all_tp} SL={n_all_sl} TO={n_all_to} "
        f"Gross EV={unconditional_gross_ev:+.4f} ATR  Net EV={unconditional_net_ev:+.4f} ATR"
    )

    print("  Model-selected (predicted TP):")
    print(
        f"    Selected={n_selected}/{n_test} ({n_selected/n_test*100:.1f}%)  "
        f"TP={n_tp} SL={n_sl} TO={n_to}"
    )
    print(
        f"    Conditional WR={cond_win_rate:.1f}%  "
        f"Gross EV={gross_ev:+.4f} ATR  Net EV={net_ev:+.4f} ATR"
    )

    # -- Gate decision --
    print("\n  ══ GATE DECISION ══")
    accuracy_gate = test_acc >= MIN_ACCURACY
    ev_gate = net_ev > MIN_NET_EV

    print(
        f"  Accuracy gate: {test_acc*100:.2f}% >= {MIN_ACCURACY*100:.0f}% → "
        f"{'PASS' if accuracy_gate else 'FAIL'}"
    )
    print(
        f"  Net EV gate:   {net_ev:+.4f} ATR > {MIN_NET_EV:+.4f} → "
        f"{'PASS' if ev_gate else 'FAIL'}"
    )

    if accuracy_gate and ev_gate:
        print("\n  [RESULT] BOTH GATES PASSED — M5 directional prediction has MINIMAL edge.")
        print("  action: KEEP directional route under investigation.")
    else:
        print(
            "\n  [RESULT] GATE(S) FAILED — Features have NO actionable predictive power "
            "for M5 barrier outcomes."
        )
        print("  action: BURY M5 directional prediction. Redirect 100%% to Regime Classification.")

    # -- Detailed stats --
    print("\n  ── Confusion Matrix (Test) ──")
    print(f"  {confusion_matrix(y_test, y_test_pred)}")
    print("\n  ── Classification Report (Test) ──")
    print(f"  {classification_report(y_test, y_test_pred, target_names=['non-TP', 'TP'])}")

    return {
        "train_acc": train_acc,
        "val_acc": val_acc,
        "test_acc": test_acc,
        "unconditional_tp_rate": unconditional_tp_rate,
        "gross_ev": gross_ev,
        "net_ev": net_ev,
        "cost_atr": cost_atr,
        "n_selected": n_selected,
        "n_test": n_test,
        "accuracy_gate": accuracy_gate,
        "ev_gate": ev_gate,
        "passed": accuracy_gate and ev_gate,
    }


def main():
    parser = argparse.ArgumentParser(description="Task A — Directional Alpha Closure Test")
    parser.add_argument("--data", default="data/raw/xauusdc_m5_merged.csv")
    parser.add_argument("--train-pct", type=float, default=0.60)
    parser.add_argument("--val-pct", type=float, default=0.20)
    args = parser.parse_args()

    # ── 1. Load data ──
    ohlc = load_ohlc(args.data)
    atr = compute_atr(ohlc["h"], ohlc["l"], ohlc["c"], ATR_PERIOD)

    # ── 2. Generate labels ──
    labels = generate_labels(ohlc, atr)
    outcomes = [lab["outcome"] for lab in labels]
    n_tp = outcomes.count("tp")
    n_sl = outcomes.count("sl")
    n_to = outcomes.count("timeout")
    n_total = len(labels)
    print(
        f"       Label distribution: TP={n_tp} ({n_tp/n_total*100:.1f}%), "
        f"SL={n_sl} ({n_sl/n_total*100:.1f}%), "
        f"TO={n_to} ({n_to/n_total*100:.1f}%)"
    )

    # ── 3. Compute features ──
    X, y, pnl = compute_features_batch(ohlc, labels)
    print(f"       Feature matrix: {X.shape}, label balance: {y.mean()*100:.1f}% TP")

    # ── 4. Split ──
    (X_train, y_train, pnl_train), (X_val, y_val, pnl_val), (X_test, y_test, pnl_test) = (
        split_time_series(X, y, pnl, args.train_pct, args.val_pct)
    )

    # ── 5. Train ──
    print("\n[5/6] Training Logistic Regression (C=1.0, max_iter=2000)...")
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)
    X_test_scaled = scaler.transform(X_test)

    model = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced", random_state=42)
    model.fit(X_train_scaled, y_train)
    print(f"       Converged in {model.n_iter_[0]} iterations")

    # ── 6. Evaluate ──
    atr_mean = float(np.nanmean(atr[LOOKBACK:-HORIZON]))
    print(f"       Mean ATR in valid range: {atr_mean:.4f}")
    result = evaluate_model(
        model,
        scaler,
        X_train_scaled,
        y_train,
        X_val_scaled,
        y_val,
        X_test_scaled,
        y_test,
        pnl_test,
        atr_mean,
    )

    # ── Summary ──
    print("\n[6/6] DONE")
    print(f"  Gate passed: {result['passed']}")
    print(
        f"  Test accuracy: {result['test_acc']*100:.2f}% "
        f"(baseline: {result['unconditional_tp_rate']*100:.2f}%)"
    )
    print(f"  Net EV: {result['net_ev']:+.4f} ATR after costs ({result['cost_atr']:.4f} ATR/trade)")
    print(
        f"  Model selected {result['n_selected']}/{result['n_test']} trades "
        f"({result['n_selected']/result['n_test']*100:.1f}%)"
    )

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

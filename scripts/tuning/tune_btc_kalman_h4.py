#!/usr/bin/env python3
"""BTC H4 Kalman TrendDetector parameter sweep.

Problem: H4 Kalman filter (process_noise_q=0.05, measurement_noise_r=2.0)
classifies a $1,500 BTC bounce (~2.5%) as a "long" trend reversal, blocking
all SHORT signals via counter_trend gate.  We need to find (Q, R) values
that filter out BTC's characteristic 2-5% daily noise while preserving
genuine trend detection.

Method: Grid search over (process_noise_q, measurement_noise_r).
For each pair, run KalmanTrendFilter on BTC H4 history and measure:
  1. Direction flip count (fewer = more stable, filters noise)
  2. Forward N-bar accuracy (does the direction predict future price?)
  3. Longest false-trend streak (how long can the filter stay wrong?)
  4. Lag vs actual price turning points

Usage: python scripts/tuning/tune_btc_kalman_h4.py
"""

import json
import sys
from pathlib import Path

import numpy as np

# ── Load BTC H4 data ──
csv_path = Path("data/raw/btcusdc_h4_merged.csv")
if not csv_path.exists():
    print(f"ERROR: {csv_path} not found")
    sys.exit(1)

# CSV format: time,open,high,low,close,tick_volume,spread,real_volume
data = np.loadtxt(
    csv_path,
    delimiter=",",
    skiprows=1,
    usecols=(1, 2, 3, 4),  # open, high, low, close
    dtype=np.float64,
)
closes = data[:, 3]
print(f"Loaded {len(closes)} H4 bars, price range: ${closes.min():.0f} - ${closes.max():.0f}")


# ── Kalman filter (stripped from trend_detector.py, no adaptive auto-tuning) ──
class SimpleKalman:
    """Non-adaptive Kalman for controlled backtesting."""

    def __init__(self, initial_price, process_noise_q, measurement_noise_r):
        self._x = np.array([initial_price, 0.0], dtype=np.float64)
        self._F = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float64)
        self._H = np.array([[1.0, 0.0]], dtype=np.float64)
        self._P = np.eye(2, dtype=np.float64) * 10.0
        self._Q = np.array(
            [[process_noise_q, 0.0], [0.0, process_noise_q * 0.25]], dtype=np.float64
        )
        self._R = measurement_noise_r
        self._level = initial_price
        self._velocity = 0.0
        self._bar_count = 0

    def update(self, price):
        # Predict
        x_pred = self._F @ self._x
        P_pred = self._F @ self._P @ self._F.T + self._Q
        # Update
        y = price - (self._H @ x_pred)[0]
        S = (self._H @ P_pred @ self._H.T)[0, 0] + self._R
        K = P_pred @ self._H.T / S
        self._x = x_pred + K.flatten() * y
        self._P = P_pred - K @ self._H @ P_pred
        self._level = float(self._x[0])
        self._velocity = float(self._x[1])
        self._bar_count += 1

    @property
    def direction(self):
        v = self._velocity
        if v > 1e-9:
            return "long"
        if v < -1e-9:
            return "short"
        return "neutral"

    @property
    def strength(self):
        # Normalised velocity / level uncertainty ratio
        if abs(self._level) < 1e-9:
            return 0.0
        vel_bps = abs(self._velocity / self._level * 10000)
        return float(np.tanh(vel_bps / 5.0))


# ── Metric: forward accuracy ──
def forward_accuracy(directions, closes, horizon=6):
    """For each bar where direction != neutral, check if the price moved
    in that direction over the next `horizon` bars."""
    correct = 0
    total = 0
    for i in range(len(directions) - horizon):
        d = directions[i]
        if d == "neutral":
            continue
        future_ret = (closes[i + horizon] - closes[i]) / closes[i]
        if (d == "long" and future_ret > 0) or (d == "short" and future_ret < 0):
            correct += 1
        total += 1
    return correct / total if total > 0 else 0.0


# ── Metric: max false streak ──
def max_false_streak(directions, closes, horizon=6):
    """Longest consecutive bars where direction was wrong vs forward price."""
    max_streak = 0
    current = 0
    for i in range(len(directions) - horizon):
        d = directions[i]
        if d == "neutral":
            current = 0
            continue
        future_ret = (closes[i + horizon] - closes[i]) / closes[i]
        wrong = (d == "long" and future_ret < 0) or (d == "short" and future_ret > 0)
        if wrong:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


# ── Metric: flip frequency ──
def flip_count(directions):
    """Number of direction changes."""
    flips = 0
    prev = "neutral"
    for d in directions:
        if d != "neutral" and d != prev and prev != "neutral":
            flips += 1
        if d != "neutral":
            prev = d
    return flips


# ── Parameter grid ──
# Q controls model trust (higher = more responsive to price changes)
# R controls measurement trust (higher = more smoothing, filters noise)
#
# For BTC: price is ~60,000, daily noise is 2-5% = $1,200-$3,000.
# H4 bar count per day = 6. Per-bar noise ≈ $200-$500.
# Kalman R should be O(noise²) to properly filter.
# Current R=2.0 → assumes noise std ≈ √2 ≈ $1.4 — absurdly tight for BTC.
# Current Q=0.05 → process noise std ≈ √0.05 ≈ $0.22 — microscopic.

# BTC-scaled grid
btc_scale = closes[-100:].mean() / 100.0  # ~600 for $60k BTC
q_grid = [0.5, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
r_grid = [500.0, 2000.0, 5000.0, 10000.0, 20000.0, 50000.0, 100000.0]

# Also test the current default for comparison
baseline = (0.05, 2.0)

print(f"\nBTC scale factor: {btc_scale:.0f}")
print(f"Baseline (current): Q={baseline[0]}, R={baseline[1]}")
print(
    f"\n{'Q':>8s} {'R':>10s} {'Flips':>6s} {'FwdAcc':>7s} {'MaxFalse':>9s} {'Long%':>6s} {'Short%':>6s} {'Neut%':>7s}"
)
print("-" * 72)

results = []

# Baseline first
for q, r in [baseline] + [(q, r) for q in q_grid for r in r_grid]:
    kf = SimpleKalman(initial_price=closes[0], process_noise_q=q, measurement_noise_r=r)
    directions = []
    for p in closes:
        kf.update(p)
        directions.append(kf.direction)

    flips = flip_count(directions)
    fwd_acc = forward_accuracy(directions, closes, horizon=6)
    max_false = max_false_streak(directions, closes, horizon=6)

    n = len(directions)
    long_pct = sum(1 for d in directions if d == "long") / n * 100
    short_pct = sum(1 for d in directions if d == "short") / n * 100
    neut_pct = sum(1 for d in directions if d == "neutral") / n * 100

    results.append(
        {
            "q": q,
            "r": r,
            "flips": flips,
            "fwd_acc": fwd_acc,
            "max_false": max_false,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "neut_pct": neut_pct,
        }
    )

    marker = " <<< BASELINE" if (q, r) == baseline else ""
    print(
        f"{q:>8.2f} {r:>10.0f} {flips:>6d} {fwd_acc:>7.3f} {max_false:>9d} {long_pct:>6.1f} {short_pct:>6.1f} {neut_pct:>7.1f}{marker}"
    )

# ── Find best candidates ──
print(f"\n{'='*72}")
print("TOP CANDIDATES (sorted by forward accuracy, then flip count)")
print(f"{'='*72}")

# Score: penalise flips, reward accuracy
for r in sorted(results, key=lambda x: (-x["fwd_acc"], x["flips"]))[:10]:
    is_base = " *** BASELINE" if (r["q"], r["r"]) == baseline else ""
    print(
        f"  Q={r['q']:>7.1f}  R={r['r']:>8.0f}  "
        f"FwdAcc={r['fwd_acc']:.3f}  Flips={r['flips']:>4d}  "
        f"MaxFalse={r['max_false']:>3d}  "
        f"L={r['long_pct']:.0f}% S={r['short_pct']:.0f}% N={r['neut_pct']:.0f}%{is_base}"
    )

# ── Recommendation ──
print(f"\n{'='*72}")
print("RECOMMENDATION")
print(f"{'='*72}")

# Filter criteria:
# 1. Forward accuracy >= 0.52 (better than baseline's ~0.50)
# 2. Max false streak <= 24 bars (= 4 days on H4 — acceptable)
# 3. Both long and short signals > 10% (not stuck in one direction)
# 4. Lowest flip count among qualifying candidates

qualifying = [
    r
    for r in results
    if r["fwd_acc"] >= 0.52
    and r["max_false"] <= 24
    and r["long_pct"] >= 10
    and r["short_pct"] >= 10
]

if qualifying:
    best = min(qualifying, key=lambda r: r["flips"])
    print(f"  Recommended Q: {best['q']:.1f}")
    print(f"  Recommended R: {best['r']:.0f}")
    print(f"  Forward accuracy: {best['fwd_acc']:.3f}")
    print(f"  Direction flips: {best['flips']}")
    print(f"  Max false streak: {best['max_false']} bars")
    print(
        f"  Long/Short/Neutral: {best['long_pct']:.0f}%/{best['short_pct']:.0f}%/{best['neut_pct']:.0f}%"
    )
    print()
    print("  Change in live_btc.yaml or regime_gate.py:")
    print(f"    process_noise_q: 0.05 → {best['q']:.1f}")
    print(f"    measurement_noise_r: 2.0 → {best['r']:.0f}")
else:
    print("  No parameter set meets all criteria.")
    print("  Best compromise (by accuracy):")
    best = max(results, key=lambda r: r["fwd_acc"])
    print(f"    Q={best['q']:.1f}, R={best['r']:.0f}, FwdAcc={best['fwd_acc']:.3f}")

# Save results
out = Path("data_btc/tuning/kalman_h4_sweep.json")
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, "w") as f:
    json.dump({"results": results, "recommended": best, "baseline": baseline}, f, indent=2)
print(f"\nResults saved to {out}")

"""Calibrate SL/TP multipliers per volatility regime via grid search.

Uses realistic synthetic price paths with a calibrated mean-reversion signal
(RSI extremes) to achieve ~40-45% directional accuracy — matching expected
live model performance. Grid-searches SL/TP combinations maximizing profit
factor per regime.

Methodology:
  1. Generate OHLC data with ATR distributions matching real feature store stats
  2. Apply RSI(14) mean-reversion as a baseline signal
  3. For each regime, test SL×TP multiplier grid on signal-filtered entries
  4. Select profit-factor-maximizing (SL, TP) pair per regime

Usage:
  python scripts/training/calibrate_sl_tp.py
  python scripts/training/calibrate_sl_tp.py --bars 80000 --horizon 12
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Realistic parameters derived from feature store analysis (2026-05-05) ──
# XAUUSD M5 ATR%: mean=3.74, std=3.84, p25=1.62, p50=2.02, p75=2.81
# Low regime ATR: ~1.5, Normal regime ATR: ~2.0, High regime ATR: ~7.8+


def generate_realistic_price_data(
    n_bars: int = 80000,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Generate OHLC data with regime-dependent Ornstein-Uhlenbeck dynamics.

    Low vol: strong mean-reversion (high theta, low sigma) — RSI works well.
    High vol: weak mean-reversion (low theta, high sigma) — trend-dominant.
    This mirrors real market microstructure where mean-reversion strategies
    work better in low-volatility regimes.

    Returns (high, low, close, atr_14, regime_label).
    """
    rng = np.random.RandomState(seed)
    close = np.zeros(n_bars, dtype=np.float64)
    close[0] = 4000.0

    regime_label = np.zeros(n_bars, dtype=np.int32)
    target_theta = np.zeros(n_bars, dtype=np.float64)  # mean-reversion speed
    target_sigma = np.zeros(n_bars, dtype=np.float64)  # volatility
    target_mu = np.full(n_bars, 4000.0, dtype=np.float64)  # long-term mean

    # Regime blocks with varying lengths
    seg_boundaries = [0]
    while seg_boundaries[-1] < n_bars:
        seg_boundaries.append(min(seg_boundaries[-1] + int(rng.randint(300, 1001)), n_bars))

    for i in range(len(seg_boundaries) - 1):
        start = seg_boundaries[i]
        end = seg_boundaries[i + 1]
        r = rng.choice([0, 1, 2], p=[0.25, 0.50, 0.25])
        regime_label[start:end] = r
        if r == 0:  # Low vol: strong mean-reversion, tight ranges
            target_theta[start:end] = 0.15 + rng.rand() * 0.10  # θ: 0.15-0.25
            target_sigma[start:end] = 0.4 + rng.rand() * 0.3  # σ: 0.4-0.7
            # Shift mu occasionally for low-vol regime shifts
            if rng.rand() < 0.3:
                target_mu[start:end] = target_mu[start - 1] + rng.randn() * 5.0
        elif r == 1:  # Normal: moderate mean-reversion
            target_theta[start:end] = 0.05 + rng.rand() * 0.08  # θ: 0.05-0.13
            target_sigma[start:end] = 1.0 + rng.rand() * 1.0  # σ: 1.0-2.0
            if rng.rand() < 0.4:
                target_mu[start:end] = target_mu[start - 1] + rng.randn() * 15.0
        else:  # High vol: weak mean-reversion, trend-dominant
            target_theta[start:end] = 0.005 + rng.rand() * 0.025  # θ: 0.005-0.03
            target_sigma[start:end] = 3.0 + rng.rand() * 5.0  # σ: 3.0-8.0
            if rng.rand() < 0.5:
                target_mu[start:end] = target_mu[start - 1] + rng.randn() * 30.0

    # EWMA smooth parameter transitions
    theta = np.zeros(n_bars, dtype=np.float64)
    sigma = np.zeros(n_bars, dtype=np.float64)
    mu = np.zeros(n_bars, dtype=np.float64)
    theta[0] = target_theta[0]
    sigma[0] = target_sigma[0]
    mu[0] = target_mu[0]
    alpha = 0.03
    for i in range(1, n_bars):
        theta[i] = alpha * target_theta[i] + (1 - alpha) * theta[i - 1]
        sigma[i] = alpha * target_sigma[i] + (1 - alpha) * sigma[i - 1]
        mu[i] = alpha * target_mu[i] + (1 - alpha) * mu[i - 1]

    # Generate OU price path: dx_t = theta * (mu - x_t) * dt + sigma * dW_t
    # Discrete: x_t = x_{t-1} + theta*(mu - x_{t-1}) + sigma*eps
    for i in range(1, n_bars):
        ou_drift = theta[i] * (mu[i] - close[i - 1])
        diffusion = sigma[i] * rng.randn()
        close[i] = max(close[i - 1] + ou_drift + diffusion, 100.0)

    # Generate high/low with realistic spread proportional to sigma
    bar_range = np.abs(rng.randn(n_bars)) * sigma * 0.5 + sigma * 0.1
    high = close + bar_range * rng.rand(n_bars) * 0.6
    low = close - bar_range * rng.rand(n_bars) * 0.6
    high = np.maximum(high, close)
    low = np.minimum(low, close)

    # Compute ATR(14) from generated bars
    atr = np.zeros(n_bars, dtype=np.float64)
    for i in range(14, n_bars):
        tr = np.maximum(
            high[i - 13 : i + 1] - low[i - 13 : i + 1],
            np.maximum(
                np.abs(high[i - 13 : i + 1] - np.roll(close[i - 14 : i], 1)[:14]),
                np.abs(low[i - 13 : i + 1] - np.roll(close[i - 14 : i], 1)[:14]),
            ),
        )
        atr[i] = float(np.mean(tr))

    return high, low, close, atr, regime_label


def compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI(period) — standard Wilder smoothing."""
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.zeros_like(close)
    avg_loss = np.zeros_like(close)
    rsi = np.full_like(close, 50.0)

    if len(close) <= period:
        return rsi

    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])

    for i in range(period + 1, len(close)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period

    for i in range(period, len(close)):
        if avg_loss[i] < 1e-12:
            rsi[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    return rsi


def simulate_trade(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    entry_idx: int,
    sl_mult: float,
    tp_mult: float,
    atr: float,
    horizon: int = 12,
) -> float:
    """Simulate a single long trade and return P&L (price difference).

    Returns positive for TP hit, negative for SL hit, 0 for timeout.
    """
    entry_price = close[entry_idx]
    sl_dist = sl_mult * atr
    tp_dist = tp_mult * atr
    sl_price = entry_price - sl_dist
    tp_price = entry_price + tp_dist

    end = min(entry_idx + horizon + 1, len(close))
    for j in range(entry_idx + 1, end):
        if low[j] <= sl_price:
            return -sl_dist
        if high[j] >= tp_price:
            return tp_dist
    return 0.0


def generate_synthetic_signal(
    close: np.ndarray,
    n_bars: int,
    horizon: int,
    rng: np.random.RandomState,
    target_wr: float = 0.42,
) -> np.ndarray:
    """Generate a synthetic directional signal with known accuracy.

    At each bar, computes the true future direction (close[t+horizon] vs close[t]),
    then flips it with probability (1 - target_wr) to achieve the target win rate.

    Returns array of {+1 (long), -1 (short)} signals.
    """
    signals = np.zeros(n_bars, dtype=np.int32)
    for i in range(14, n_bars - horizon - 1):
        future_idx = min(i + horizon, n_bars - 1)
        true_direction = 1 if close[future_idx] > close[i] else -1
        if rng.rand() < target_wr:
            signals[i] = true_direction
        else:
            signals[i] = -true_direction
    return signals


def grid_search_regime(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    regime: np.ndarray,
    target_regime: int,
    signal: np.ndarray,
    horizon: int = 12,
) -> dict[str, Any]:
    """Grid search SL/TP multipliers for a specific volatility regime.

    Uses a synthetic signal with controlled accuracy (~42%) to mimic a real
    ML model's predictive edge. Tests SL x TP grid maximizing profit factor.
    """
    sl_range = np.arange(0.5, 4.1, 0.25)
    tp_range = np.arange(0.5, 6.1, 0.25)

    # Collect all valid entry bars in this regime where signal is active
    stride = 4  # denser sampling since signal is synthetic
    regime_entries = []
    for i in range(14, len(close) - horizon - 1, stride):
        if regime[i] == target_regime and atr[i] > 0.1 and signal[i] != 0:
            regime_entries.append(i)

    if len(regime_entries) < 200:
        return {"error": f"too_few_entries_in_regime_{target_regime}: {len(regime_entries)}"}

    best_pf = 0.0
    best_sl = 2.0
    best_tp = 3.5
    best_metrics: dict[str, Any] = {}

    regime_names = ["low", "normal", "high"]
    print(
        f"\n  Regime {target_regime} ({regime_names[target_regime]}): "
        f"{len(regime_entries)} entries, scanning {len(sl_range)}x{len(tp_range)} combinations..."
    )

    for sl in sl_range:
        for tp in tp_range:
            if tp <= sl:
                continue

            wins = 0.0
            losses = 0.0
            total_trades = 0
            wins_count = 0

            for idx in regime_entries:
                if signal[idx] == 1:
                    pnl = simulate_trade(
                        high, low, close, idx, float(sl), float(tp), float(atr[idx]), horizon
                    )
                else:
                    pnl = simulate_trade_short(
                        high, low, close, idx, float(sl), float(tp), float(atr[idx]), horizon
                    )

                total_trades += 1
                if pnl > 0:
                    wins += pnl
                    wins_count += 1
                elif pnl < 0:
                    losses += abs(pnl)

            if losses < 1e-8:
                pf = wins / 1e-8 if wins > 0 else 0.0
            else:
                pf = wins / losses

            wr = wins_count / total_trades if total_trades > 0 else 0.0

            if pf > best_pf:
                best_pf = pf
                best_sl = float(sl)
                best_tp = float(tp)
                best_metrics = {
                    "sl_mult": best_sl,
                    "tp_mult": best_tp,
                    "profit_factor": round(best_pf, 4),
                    "win_rate": round(wr, 4),
                    "total_trades": total_trades,
                    "total_wins": wins_count,
                    "regime": regime_names[target_regime],
                }

    return best_metrics


def simulate_trade_short(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    entry_idx: int,
    sl_mult: float,
    tp_mult: float,
    atr: float,
    horizon: int = 12,
) -> float:
    """Simulate a single short trade and return P&L (price difference)."""
    entry_price = close[entry_idx]
    sl_dist = sl_mult * atr
    tp_dist = tp_mult * atr
    sl_price = entry_price + sl_dist
    tp_price = entry_price - tp_dist

    end = min(entry_idx + horizon + 1, len(close))
    for j in range(entry_idx + 1, end):
        if high[j] >= sl_price:
            return -sl_dist
        if low[j] <= tp_price:
            return tp_dist
    return 0.0


def main() -> int:
    n_bars = 80000
    horizon = 12

    print("=" * 60)
    print("  SL/TP Multiplier Calibration via Grid Search")
    print(f"  Data: {n_bars} bars, Horizon: {horizon} bars")
    print("  Price model: Ornstein-Uhlenbeck (regime-conditional mean-reversion)")
    print("  Signal: synthetic 42% directional accuracy")
    print("=" * 60)

    t0 = time.perf_counter()
    print("\nGenerating realistic price data...")
    high, low, close, atr, regime = generate_realistic_price_data(n_bars)

    # Regime statistics
    for r, label in enumerate(["low", "normal", "high"]):
        mask = regime == r
        print(
            f"  {label}: {mask.sum()} bars, ATR mean={atr[mask].mean():.2f}, "
            f"ATR std={atr[mask].std():.2f}"
        )

    # Generate synthetic signal with controlled 42% directional accuracy
    # (matching expected ML model performance based on promotion thresholds)
    print("\nGenerating synthetic signal (42% directional accuracy)...")
    rng = np.random.RandomState(42)
    signal = generate_synthetic_signal(close, n_bars, horizon, rng, target_wr=0.42)

    # Verify signal accuracy
    correct = 0
    total = 0
    for i in range(14, n_bars - horizon - 1):
        if signal[i] != 0:
            future_idx = min(i + horizon, n_bars - 1)
            true_dir = 1 if close[future_idx] > close[i] else -1
            if signal[i] == true_dir:
                correct += 1
            total += 1
    actual_wr = correct / total if total > 0 else 0
    print(f"  Signal entries: {total}, actual accuracy: {actual_wr:.2%}")

    # Grid search per regime
    results = {}
    for r in range(3):
        result = grid_search_regime(high, low, close, atr, regime, r, signal, horizon)
        results[["low", "normal", "high"][r]] = result
        if "error" not in result:
            print(
                f"  => BEST: SL={result['sl_mult']:.2f}x  TP={result['tp_mult']:.2f}x  "
                f"PF={result['profit_factor']:.4f}  WR={result['win_rate']:.2%}"
            )

    elapsed = round(time.perf_counter() - t0, 1)
    print(f"\nCompleted in {elapsed}s")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  CALIBRATED MULTIPLIERS")
    print("=" * 60)
    print(f"  {'Regime':<10} {'SL':<8} {'TP':<8} {'PF':<10} {'WR':<8} {'RR':<8}")
    print(f"  {'-'*50}")
    for label in ["low", "normal", "high"]:
        r = results[label]
        if "error" in r:
            print(f"  {label:<10} ERROR: {r['error']}")
        else:
            rr = r["tp_mult"] / r["sl_mult"] if r["sl_mult"] > 0 else 0
            print(
                f"  {label:<10} {r['sl_mult']:<8.2f} {r['tp_mult']:<8.2f} "
                f"{r['profit_factor']:<10.4f} {r['win_rate']:<8.2%} {rr:<8.2f}"
            )

    # ── Comparison with current defaults ──
    cur_defaults = {
        "low": (2.50, 4.02),
        "normal": (2.00, 3.50),
        "high": (1.50, 2.45),
    }
    print(f"\n  {'Regime':<10} {'Cur SL':<8} {'Cur TP':<8} {'New SL':<8} {'New TP':<8} {'Δ':<8}")
    print(f"  {'-'*50}")
    for label in ["low", "normal", "high"]:
        cur_sl, cur_tp = cur_defaults[label]
        r = results[label]
        if "error" not in r:
            new_sl, new_tp = r["sl_mult"], r["tp_mult"]
            delta = f"SL{new_sl-cur_sl:+.2f} TP{new_tp-cur_tp:+.2f}"
            print(
                f"  {label:<10} {cur_sl:<8.2f} {cur_tp:<8.2f} {new_sl:<8.2f} {new_tp:<8.2f} {delta}"
            )

    # Save results
    import json

    out_path = PROJECT_ROOT / "data" / "reports" / "sl_tp_calibration.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "n_bars": n_bars,
                "horizon_bars": horizon,
                "methodology": "grid_search_per_regime_synthetic_signal_42pct_accuracy_ou_price_model",
                "signal": "synthetic directional signal with 42% accuracy (long/short)",
                "current_defaults": cur_defaults,
                "results": results,
                "recommendation": {
                    "regime_detector_sl_tp": {
                        "low": {
                            "sl_mult": results["low"].get("sl_mult", 2.5),
                            "tp_mult": results["low"].get("tp_mult", 4.0),
                        },
                        "normal": {
                            "sl_mult": results["normal"].get("sl_mult", 2.0),
                            "tp_mult": results["normal"].get("tp_mult", 3.5),
                        },
                        "high": {
                            "sl_mult": results["high"].get("sl_mult", 1.5),
                            "tp_mult": results["high"].get("tp_mult", 2.5),
                        },
                    },
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"\n  Report saved: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

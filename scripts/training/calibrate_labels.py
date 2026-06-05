"""Run profitability surface calibration on historical price data.

Computes the expected-value surface across a grid of (SL, TP) barrier pairs
for a given symbol/timeframe/horizon, then recommends profitable configurations.

Usage:
  python scripts/training/calibrate_labels.py \
    --symbol XAUUSDc --timeframe M5 --horizon 12 \
    --price-data data/ohlc/xauusd_m5.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from core.training.profitability_calibrator import (
    ProfitabilitySurface,
    compute_profitability_surface,
    recommend_label_contract,
    surface_to_report,
)


def load_price_data(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load OHLC price data from NPZ or Parquet file.

    Returns (highs, lows, closes) as float64 arrays.
    """
    suffix = path.suffix.lower()
    if suffix == ".npz":
        raw = np.load(path, allow_pickle=True)
        highs = raw.get("highs") or raw.get("h") or raw.get("high")
        lows = raw.get("lows") or raw.get("l") or raw.get("low")
        closes = raw.get("closes") or raw.get("c") or raw.get("close")
        if highs is None:
            # Try alternative keys
            for key in raw.files:
                arr = raw[key]
                if hasattr(arr, "shape") and arr.ndim == 2 and arr.shape[1] >= 4:
                    # Assume OHLC array: [:, 1]=high, [:, 2]=low, [:, 3]=close
                    highs = arr[:, 1]
                    lows = arr[:, 2]
                    closes = arr[:, 3]
                    break
        if highs is None:
            raise ValueError(
                f"Could not find OHLC columns in {path}. Keys: {list(raw.keys())[:10]}"
            )
    elif suffix in (".parquet",):
        import pandas as pd

        df = pd.read_parquet(path)
        for col_h in ("high", "highs", "High"):
            if col_h in df.columns:
                highs = df[col_h].values
                break
        for col_l in ("low", "lows", "Low"):
            if col_l in df.columns:
                lows = df[col_l].values
                break
        for col_c in ("close", "closes", "Close"):
            if col_c in df.columns:
                closes = df[col_c].values
                break
    elif suffix in (".csv",):
        import pandas as pd

        df = pd.read_csv(path)
        for col_h in ("high", "highs", "High"):
            if col_h in df.columns:
                highs = df[col_h].values
                break
        for col_l in ("low", "lows", "Low"):
            if col_l in df.columns:
                lows = df[col_l].values
                break
        for col_c in ("close", "closes", "Close"):
            if col_c in df.columns:
                closes = df[col_c].values
                break
    else:
        raise ValueError(f"Unsupported format: {suffix}")

    return (
        np.asarray(highs, dtype=np.float64),
        np.asarray(lows, dtype=np.float64),
        np.asarray(closes, dtype=np.float64),
    )


def calibrate_horizon(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    horizon_bars: int,
    *,
    symbol: str = "XAUUSDc",
    timeframe: str = "M5",
    spread_points: float = 30,
    slippage_points: float = 10,
    tick_value: float = 0.01,
    tick_size: float = 0.01,
    volume: float = 0.01,
    entry_stride: int = 3,
) -> ProfitabilitySurface:
    """Run profitability surface scan for a single horizon."""
    print(f"\n{'='*60}")
    print(f"Horizon: {horizon_bars} bars ({timeframe})")
    print(f"Price data: {len(closes)} bars")
    print(
        f"Cost model: spread={spread_points}pts, slippage={slippage_points}pts, tick_size={tick_size}"
    )
    print(f"{'='*60}")

    surface = compute_profitability_surface(
        highs,
        lows,
        closes,
        horizon_bars=horizon_bars,
        atr_period=14,
        side="both",
        symbol=symbol,
        timeframe=timeframe,
        entry_stride=entry_stride,
        spread_points=spread_points,
        slippage_points=slippage_points,
        tick_value=tick_value,
        tick_size=tick_size,
        volume=volume,
    )

    print(f"Entries simulated: {surface.entries_simulated}")
    print(f"Mean ATR: {surface.mean_atr:.4f}")
    print(f"Profitable configs found: {len(surface.profitable_configs())}")

    # Best config
    best = surface.best_config()
    if best:
        print(f"\nBest config: SL={best.sl_atr_mult}, TP={best.tp_atr_mult}")
        print(
            f"  EV={best.expected_pnl_r:.4f}R, TP rate={best.tp_hit_rate:.2%}, "
            f"SL rate={best.sl_hit_rate:.2%}, timeout={best.timeout_rate:.2%}"
        )
        print(f"  Sharpe estimate: {best.sharpe_estimate:.2f}")

    # Recommended contract
    recommendation = recommend_label_contract(
        surface,
        min_expected_pnl=0.05,
        min_reward_risk=1.5,
        prefer_higher_tp=True,
    )
    if recommendation:
        print("\nRecommended contract:")
        print(f"  SL={recommendation['sl_atr_mult']}, TP={recommendation['tp_atr_mult']}")
        print(
            f"  EV={recommendation['expected_pnl_r']:.4f}R, "
            f"TP rate={recommendation['tp_hit_rate']:.2%}, "
            f"RR={recommendation['reward_risk_ratio']:.1f}"
        )

    # Top 5 profitable configs
    top5 = surface.profitable_configs()[:5]
    if top5:
        print("\nTop 5 profitable configs:")
        for i, p in enumerate(top5):
            print(
                f"  {i+1}. SL={p.sl_atr_mult}, TP={p.tp_atr_mult} "
                f"EV={p.expected_pnl_r:.4f}R, TP={p.tp_hit_rate:.2%}, "
                f"RR={p.reward_risk_ratio:.1f}"
            )

    return surface


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="calibrate_labels",
        description="Run profitability surface calibration for barrier label contracts",
    )
    p.add_argument(
        "--price-data",
        type=Path,
        required=True,
        help="Path to OHLC price data (.npz, .parquet, or .csv)",
    )
    p.add_argument(
        "--symbol",
        default="XAUUSDc",
        help="Trading symbol (default: XAUUSDc)",
    )
    p.add_argument(
        "--timeframe",
        default="M5",
        help="Bar timeframe (default: M5)",
    )
    p.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=[3, 12, 24],
        help="Horizon bars to calibrate (default: 3 12 24)",
    )
    p.add_argument(
        "--spread-points",
        type=float,
        default=30,
        help="Spread in MT5 points (default: 30 for XAUUSDc, ~3 pips)",
    )
    p.add_argument(
        "--slippage-points",
        type=float,
        default=10,
        help="Slippage in MT5 points (default: 10 for XAUUSDc)",
    )
    p.add_argument(
        "--tick-value",
        type=float,
        default=0.01,
        help="MT5 SYMBOL_TRADE_TICK_VALUE (default: 0.01 for XAUUSDc)",
    )
    p.add_argument(
        "--tick-size",
        type=float,
        default=0.01,
        help="MT5 SYMBOL_TRADE_TICK_SIZE (default: 0.01 for XAUUSDc)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Save calibration report as JSON",
    )
    p.add_argument(
        "--stride",
        type=int,
        default=3,
        help="Entry stride (default: 3, test every 3rd bar)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.price_data.exists():
        print(f"Price data not found: {args.price_data}", file=sys.stderr)
        return 2

    # Load data once
    highs, lows, closes = load_price_data(args.price_data)

    results: dict[str, dict] = {}
    for horizon in args.horizons:
        surface = calibrate_horizon(
            highs,
            lows,
            closes,
            horizon_bars=horizon,
            symbol=args.symbol,
            timeframe=args.timeframe,
            spread_points=args.spread_points,
            slippage_points=args.slippage_points,
            tick_value=args.tick_value,
            tick_size=args.tick_size,
            entry_stride=args.stride,
        )
        results[str(horizon)] = surface_to_report(surface)

    # Save report
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "symbol": args.symbol,
            "timeframe": args.timeframe,
            "spread_points": args.spread_points,
            "slippage_points": args.slippage_points,
            "tick_value": args.tick_value,
            "tick_size": args.tick_size,
            "horizons": results,
        }
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\nReport saved: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

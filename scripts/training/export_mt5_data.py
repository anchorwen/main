"""Export MT5 historical OHLC data for training.

Must be run when live_intent_loop is NOT running (MT5 IPC is exclusive).

Usage:
  # Default: 1 year of M5 XAUUSDc bars
  python scripts/training/export_mt5_data.py

  # Custom timeframe and lookback
  python scripts/training/export_mt5_data.py --timeframe M15 --days 365

  # Output path
  python scripts/training/export_mt5_data.py --output data/raw/xauusd_m5_2025.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def export_rates(
    symbol: str = "XAUUSDc",
    timeframe: str = "M5",
    days_back: int = 365,
    output_path: Path | None = None,
    mt5_terminal_path: str | None = None,
) -> dict:
    """Export MT5 historical rates to CSV and JSON summary."""
    import MetaTrader5 as mt5

    init_kwargs = {}
    if mt5_terminal_path:
        init_kwargs["path"] = mt5_terminal_path
    if not mt5.initialize(**init_kwargs):
        return {"error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        mt5_tf = tf_map.get(timeframe)
        if mt5_tf is None:
            return {"error": f"unsupported_timeframe: {timeframe}"}

        end = datetime.now(UTC).replace(tzinfo=None)
        start = end - timedelta(days=days_back)

        rates = mt5.copy_rates_range(symbol, mt5_tf, start, end)
        if rates is None or len(rates) == 0:
            return {"error": "no_rates", "detail": str(mt5.last_error())}

        n = len(rates)
        times = [datetime.fromtimestamp(r[0], tz=UTC).isoformat() for r in rates]
        opens = np.array([r[1] for r in rates], dtype=np.float64)
        highs = np.array([r[2] for r in rates], dtype=np.float64)
        lows = np.array([r[3] for r in rates], dtype=np.float64)
        closes = np.array([r[4] for r in rates], dtype=np.float64)
        ticks = np.array([r[5] for r in rates], dtype=np.int64)
        spreads = (
            np.array([r[6] for r in rates], dtype=np.int64)
            if rates.dtype.names and "spread" in rates.dtype.names
            else np.zeros(n)
        )

        # Save CSV
        csv_path = output_path or (
            PROJECT_ROOT / "data" / "raw" / f"{symbol.lower()}_{timeframe.lower()}_{days_back}d.csv"
        )
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        with open(csv_path, "w") as f:
            f.write("time,open,high,low,close,tick_volume,spread\n")
            for i in range(n):
                f.write(
                    f"{times[i]},{opens[i]:.5f},{highs[i]:.5f},{lows[i]:.5f},{closes[i]:.5f},{ticks[i]},{spreads[i]}\n"
                )

        # Summary
        summary = {
            "symbol": symbol,
            "timeframe": timeframe,
            "days_back": days_back,
            "bars_exported": n,
            "date_range": [times[0], times[-1]],
            "price_range": [float(closes.min()), float(closes.max())],
            "output_path": str(csv_path),
        }

        json_path = csv_path.with_suffix(".summary.json")
        json_path.write_text(json.dumps(summary, indent=2))

        return summary

    finally:
        mt5.shutdown()


def main() -> int:
    p = argparse.ArgumentParser(prog="export_mt5_data")
    p.add_argument("--symbol", default="XAUUSDc", help="MT5 symbol")
    p.add_argument("--timeframe", default="M5", help="Bar timeframe")
    p.add_argument("--days", type=int, default=365, help="Days of history")
    p.add_argument("--output", type=Path, default=None, help="CSV output path")
    p.add_argument(
        "--mt5-terminal-path", default=None, help="Path to terminal64.exe for MT5 initialization"
    )
    args = p.parse_args()

    result = export_rates(
        args.symbol,
        args.timeframe,
        args.days,
        args.output,
        mt5_terminal_path=args.mt5_terminal_path,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    if "error" in result:
        return 1
    print("\n  To train with this data:")
    print(f"  python scripts/training/e2e_pipeline_validation.py --data {result['output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

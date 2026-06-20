"""Download OHLC data from MT5 for multi-resolution training.

Downloads M5/D1/H4 data for XAUUSDc, BTCUSDc, and cross-asset symbols.

Usage:
  python scripts/training/download_mt5_ohlc.py --timeframe M5,D1,H4 --output-dir data/raw
  python scripts/training/download_mt5_ohlc.py --timeframe M5,D1 --symbols BTCUSDc,XAUUSDc,AUDJPYc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
from core.runtime.fault_handler import fail_open_guard

SYMBOLS = ["XAUUSDc", "BTCUSDc", "EURUSDc", "USDJPYc", "XAGUSDc", "AUDJPYc"]

TIMEFRAME_MAP = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN": mt5.TIMEFRAME_MN1,
}

# Max bars to request (2 years of H1 ≈ 12,000 bars per pair)
MAX_BARS = 15000


def download_ohlc(
    symbol: str, timeframe_str: str, output_dir: Path, max_bars: int = MAX_BARS
) -> Path:
    tf = TIMEFRAME_MAP[timeframe_str]
    tf_name = timeframe_str.lower()

    print(f"[MT5] Downloading {symbol} {timeframe_str}...")

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, max_bars)
    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        print(f"[MT5] ERROR: {symbol} {timeframe_str}: {err}", file=sys.stderr)
        raise RuntimeError(f"MT5 returned no data for {symbol} {timeframe_str}: {err}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(
        columns={
            "tick_volume": "tick_volume",
            "spread": "spread",
            "real_volume": "real_volume",
        }
    )

    out_cols = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    df_out = df[out_cols].copy()

    fname = f"{symbol.lower()}_{tf_name}_merged.csv"
    out_path = output_dir / fname
    output_dir.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(out_path, index=False)

    print(f"[MT5] {symbol} {timeframe_str}: {len(df_out)} bars -> {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(prog="download_mt5_ohlc")
    parser.add_argument(
        "--timeframe", type=str, default="M5,D1,H4", help="Comma-separated timeframes (default: M5,D1,H4)"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="XAUUSDc,BTCUSDc,EURUSDc,USDJPYc,XAGUSDc,AUDJPYc",
        help="Comma-separated symbols",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--max-bars", type=int, default=MAX_BARS)
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframe.split(",") if t.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    unknown = [t for t in timeframes if t not in TIMEFRAME_MAP]
    if unknown:
        print(f"[MT5] ERROR: unknown timeframes: {unknown}")
        return 2

    if not mt5.initialize():
        print(f"[MT5] ERROR: mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        return 1

    print(f"[MT5] Connected. Terminal: {mt5.terminal_info().name if mt5.terminal_info() else '?'}")

    for tf_str in timeframes:
        for sym in symbols:
            try:
                download_ohlc(sym, tf_str, args.output_dir, args.max_bars)
            except Exception as e:  # BLE001:FOG
                with fail_open_guard("download_mt5_ohlc:main"):
                    print(f"[MT5] FAILED: {sym} {tf_str}: {e}")
    mt5.shutdown()
    print("[MT5] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

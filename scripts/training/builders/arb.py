"""Arbitrage dataset builder — OHLC pass-through for OU parameter optimization.

Produces a standardized CSV that arb_trainer reads directly.
No features, no labels — just clean OHLC data.

Usage:
  builder = ArbDatasetBuilder(
      csv_path="data/raw/xauusdc_m15_merged.csv",
      timeframe="M15",
  )
  builder.build(output_dir="data/training/arb_v6_m15")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from scripts.training.builders.base import BaseDatasetBuilder


class ArbDatasetBuilder(BaseDatasetBuilder):
    """OHLC-only pass-through. No feature computation, no labels."""

    feature_names = ["close"]

    def __init__(self, csv_path: Path, timeframe: str = "M5"):
        super().__init__(timeframe=timeframe)
        self.csv_path = csv_path

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def build(self, output_dir: Path, val_ratio: float = 0.2) -> dict[str, Any]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {self.csv_path}")

        df = pd.read_csv(self.csv_path, parse_dates=["time"]).sort_values("time")
        required = {"time", "open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        output_dir.mkdir(parents=True, exist_ok=True)
        out_csv = output_dir / f"xauusdc_{self.timeframe.lower()}_standardized.csv"
        df.to_csv(out_csv, index=False)
        print(f"[arb:{self.timeframe}] Exported {len(df)} bars → {out_csv}")

        return {
            "dataset": f"arb_{self.timeframe.lower()}",
            "bars": len(df),
            "output_csv": str(out_csv),
            "price_range": [float(df["close"].min()), float(df["close"].max())],
        }

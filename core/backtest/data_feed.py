"""Historical data feed for backtesting.

Reads from Parquet files, NPZ archives, or LocalFeatureStore,
yielding Bar objects in chronological order.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Bar:
    """Single OHLCV bar with timestamp and optional feature vector."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = "XAUUSDc"
    spread: float = 0.0
    features: list[float] | None = None

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


class DataFeed:
    """Iterator over historical bars from structured data sources.

    Supports:
    - Parquet files (via pandas)
    - NPZ files (via numpy)
    - Raw OHLCV CSV
    """

    def __init__(self, bars: list[Bar] | None = None):
        self._bars: list[Bar] = bars or []
        self._index = 0

    @classmethod
    def from_parquet(cls, path: str | Path, symbol: str = "XAUUSDc") -> DataFeed:
        """Load bars from a Parquet file.

        Expected columns: timestamp, open, high, low, close, volume (optional)
        """
        import pandas as pd

        df = pd.read_parquet(path)
        bars: list[Bar] = []
        for _, row in df.iterrows():
            bars.append(
                Bar(
                    timestamp=row.get("timestamp", row.name),
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("volume", 0)),
                    symbol=symbol,
                    spread=float(row.get("spread", 0)),
                )
            )
        return cls(bars)

    @classmethod
    def from_csv(cls, path: str | Path, symbol: str = "XAUUSDc") -> DataFeed:
        """Load bars from a CSV with OHLCV columns."""
        import csv

        bars: list[Bar] = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bars.append(
                    Bar(
                        timestamp=datetime.fromisoformat(
                            str(row.get("timestamp", row.get("time", "")))
                        ),
                        open=float(row.get("open", 0)),
                        high=float(row.get("high", 0)),
                        low=float(row.get("low", 0)),
                        close=float(row.get("close", 0)),
                        volume=float(row.get("volume", row.get("tick_volume", 0))),
                        symbol=symbol,
                        spread=float(row.get("spread", 0)),
                    )
                )
        return cls(bars)

    @classmethod
    def from_npz(cls, path: str | Path, symbol: str = "XAUUSDc") -> DataFeed:
        """Load bars from a NPZ archive with features.

        Expected arrays: timestamps, opens, highs, lows, closes, volumes,
        and optionally features (N, F) and spreads.
        """
        import numpy as np

        data = np.load(path, allow_pickle=True)
        n = len(data["opens"])
        bars: list[Bar] = []
        features_arr = data.get("features", None)
        spreads = data.get("spreads", None)
        timestamps = data.get("timestamps", None)

        for i in range(n):
            feats = None
            if features_arr is not None and i < len(features_arr):
                feats = features_arr[i].tolist()
            bars.append(
                Bar(
                    timestamp=datetime.fromisoformat(str(timestamps[i]))
                    if timestamps is not None
                    else datetime(2020, 1, 1),
                    open=float(data["opens"][i]),
                    high=float(data["highs"][i]),
                    low=float(data["lows"][i]),
                    close=float(data["closes"][i]),
                    volume=float(data["volumes"][i]) if "volumes" in data else 0.0,
                    symbol=symbol,
                    spread=float(spreads[i]) if spreads is not None else 0.0,
                    features=feats,
                )
            )
        return cls(bars)

    @property
    def bars(self) -> list[Bar]:
        return self._bars

    def __len__(self) -> int:
        return len(self._bars)

    def __iter__(self) -> Iterator[Bar]:
        self._index = 0
        return self

    def __next__(self) -> Bar:
        if self._index >= len(self._bars):
            raise StopIteration
        bar = self._bars[self._index]
        self._index += 1
        return bar

    def slice(self, start: int, end: int | None = None) -> DataFeed:
        """Return a new DataFeed covering bars[start:end]."""
        return DataFeed(self._bars[start:end])

    def add_features(self, features: list[list[float]]) -> None:
        """Attach feature vectors to bars (must match length)."""
        for i, feats in enumerate(features):
            if i < len(self._bars):
                self._bars[i].features = feats

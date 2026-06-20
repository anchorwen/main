"""Live D1 feature provider — wraps DailyFeatureComputer for real-time use.

On each call, fetches the latest D1 bar from MT5, appends to the local CSV
if it's a new bar, re-initializes the DailyFeatureComputer, and returns the
latest 24-dim daily_swing feature vector.

Usage:
    provider = LiveDailyFeatureProvider(
        mt5_module=mt5,
        symbol="XAUUSDc",
        d1_csv="data/raw/xauusdc_d1_merged.csv",
        h4_csv="data/raw/xauusdc_h4_merged.csv",
    )
    features = provider.get_latest()  # np.ndarray shape (24,)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from core.features.computers.daily_computer import DailyFeatureComputer
from core.runtime.fault_handler import fail_open_guard

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# MT5 constant — hardcoded to avoid requiring MetaTrader5 import
MT5_TIMEFRAME_D1 = 16408


class LiveDailyFeatureProvider:
    """Provides the latest D1 feature vector for swing brain inference.

    Wraps DailyFeatureComputer and keeps the source CSV in sync with MT5.
    """

    def __init__(
        self,
        mt5_module: Any,
        symbol: str = "XAUUSDc",
        d1_csv: str | Path = "data/raw/xauusdc_d1_merged.csv",
        h4_csv: str | Path | None = "data/raw/xauusdc_h4_merged.csv",
        cross_assets: dict[str, str] | None = None,
        mt5_worker: MT5Worker | None = None,
    ):
        self._mt5 = mt5_module
        self._symbol = symbol
        self._d1_csv = Path(d1_csv).resolve()
        self._h4_csv = Path(h4_csv).resolve() if h4_csv else None
        self._cross_assets = cross_assets or {}
        self._worker = mt5_worker

        self._last_bar_time: int = 0  # unix timestamp of last known D1 bar
        self._computer: DailyFeatureComputer | None = None
        self._latest_vector: np.ndarray | None = None
        self._latest_timestamp: str = ""

        self._refresh()  # initial load

    @property
    def feature_dim(self) -> int:
        return 24

    @property
    def latest_timestamp(self) -> str:
        return self._latest_timestamp

    def get_latest(self) -> np.ndarray:
        """Return the latest 24-dim D1 feature vector, refreshing if a new D1 bar arrived."""
        if self._is_new_bar_available():
            self._refresh()
        if self._latest_vector is None:
            return np.zeros(24, dtype=np.float64)
        return self._latest_vector.copy()

    # ── Internal ──

    def _is_new_bar_available(self) -> bool:
        """Check if MT5 has a new D1 bar since last refresh."""
        try:
            if self._worker is not None:
                rates = self._worker.copy_rates_from_pos(self._symbol, MT5_TIMEFRAME_D1, 0, 1)
            else:
                rates = self._mt5.copy_rates_from_pos(self._symbol, self._mt5.TIMEFRAME_D1, 0, 1)
            if rates is not None and len(rates) > 0:
                latest = int(rates[-1]["time"])
                return latest > self._last_bar_time
        except Exception:  # BLE001:FOG
            with fail_open_guard("live_daily_provider:_is_new_bar_available"):
                pass
        return False

    def _refresh(self) -> None:
        """Synchronize CSV with MT5 D1 bars and rebuild the feature computer."""
        try:
            self._sync_csv()
            self._build_computer()
            if self._computer is not None and self._computer._n > 30:
                # Get latest row
                idx = self._computer._n - 1
                self._latest_vector = np.array(self._computer._gather_row(idx), dtype=np.float64)
                self._latest_timestamp = self._computer.timestamps[idx]
                # Update last_bar_time from the latest bar
                dts = self._computer._d1_datetimes
                if dts and idx < len(dts):
                    self._last_bar_time = int(dts[idx].timestamp())
        except Exception:  # BLE001:FOG
            with fail_open_guard("live_daily_provider:_refresh"):
                pass
    def _sync_csv(self) -> None:
        """Fetch new D1 bars from MT5 and append to CSV."""
        try:
            # Read existing CSV to find last timestamp
            existing_ts: set[str] = set()
            if self._d1_csv.exists():
                import csv

                with open(self._d1_csv, encoding="utf-8-sig") as fh:
                    reader = csv.DictReader(fh)
                    if reader.fieldnames:
                        for row in reader:
                            t = (row.get("time") or row.get("datetime") or "").strip()
                            if t:
                                existing_ts.add(t[:10])  # date-only keys

            # Fetch recent D1 bars from MT5
            if self._worker is not None:
                rates = self._worker.copy_rates_from_pos(self._symbol, MT5_TIMEFRAME_D1, 0, 30)
            else:
                rates = self._mt5.copy_rates_from_pos(self._symbol, self._mt5.TIMEFRAME_D1, 0, 30)
            if rates is None or len(rates) == 0:
                return

            import pandas as pd

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")

            # Filter to new bars only
            new_rows = []
            for _, row in df.iterrows():
                ts_str = row["time"].strftime("%Y-%m-%d %H:%M:%S")
                date_key = ts_str[:10]
                if date_key not in existing_ts:
                    new_rows.append(
                        {
                            "time": ts_str,
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "tick_volume": row.get("tick_volume", 0),
                            "spread": row.get("spread", 0),
                            "real_volume": row.get("real_volume", 0),
                        }
                    )

            if not new_rows:
                return

            # Append to CSV
            self._d1_csv.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self._d1_csv.exists()
            with open(self._d1_csv, "a", encoding="utf-8-sig") as fh:
                import csv

                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "tick_volume",
                        "spread",
                        "real_volume",
                    ],
                )
                if write_header:
                    writer.writeheader()
                for row in new_rows:
                    writer.writerow(row)

            print(
                json.dumps(
                    {
                        "event": "d1_csv_synced",
                        "time": datetime.now(UTC).isoformat(),
                        "new_bars": len(new_rows),
                        "latest": new_rows[-1]["time"] if new_rows else "none",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("live_daily_provider:_sync_csv"):
                print(
                    json.dumps(
                        {
                            "event": "d1_csv_sync_error",
                            "time": datetime.now(UTC).isoformat(),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    def _build_computer(self) -> None:
        """Build a fresh DailyFeatureComputer from the current CSV."""
        if not self._d1_csv.exists():
            return
        try:
            kwargs: dict[str, Any] = {"d1_csv": str(self._d1_csv)}
            if self._h4_csv and self._h4_csv.exists():
                kwargs["h4_csv"] = str(self._h4_csv)
            if self._cross_assets:
                kwargs["cross_assets"] = self._cross_assets
            self._computer = DailyFeatureComputer(**kwargs)
        except Exception:  # BLE001:FOG
            with fail_open_guard("live_daily_provider:_build_computer"):
                self._computer = None

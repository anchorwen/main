"""Microstructure dataset builder — 9-feat × 32-bar sequences → 288-dim flat.

Usage:
  builder = MicrostructureDatasetBuilder(
      xau_csv="data/raw/xauusdc_m15_merged.csv",
      eur_csv="data/raw/eurusdc_m15_merged.csv",
      jpy_csv="data/raw/usdjpyc_m15_merged.csv",
      xag_csv="data/raw/xagusdc_m15_merged.csv",
      labels_path="data/labels/micro_barrier_labels_m15.jsonl",
      timeframe="M15",
  )
  builder.build(output_dir="data/training/micro_barrier_m15_v2")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts.training.builders.base import BaseDatasetBuilder


class MicrostructureDatasetBuilder(BaseDatasetBuilder):
    """9-feature microstructure sequences with barrier label anchoring.

    Merges 4 symbols via merge_asof(direction='backward'), computes per-bar
    OHLC + cross-asset features, rolling-standardizes, and exports
    (N, 32, 9) + (N, 288) NPZ arrays.
    """

    feature_names = [
        "tick_return",
        "hl_ratio",
        "co_ratio",
        "avg_spread",
        "OIM",
        "tick_velocity",
        "XAGUSDc_return",
        "EURUSDc_return",
        "USDJPYc_return",
    ]

    def __init__(
        self,
        xau_csv: Path,
        eur_csv: Path,
        jpy_csv: Path,
        xag_csv: Path,
        labels_path: Path,
        timeframe: str = "M5",
    ):
        super().__init__(timeframe=timeframe)
        self.xau_csv = xau_csv
        self.eur_csv = eur_csv
        self.jpy_csv = jpy_csv
        self.xag_csv = xag_csv
        self.labels_path = labels_path

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"].clip(lower=1e-9)
        df["tick_return"] = df["close"].pct_change() * 100.0
        df["hl_ratio"] = (df["high"] - df["low"]) / close
        df["co_ratio"] = df["close"] / df["open"].clip(lower=1e-9)
        df["avg_spread"] = df["spread"] / close
        hl_diff = df["high"] - df["low"]
        df["OIM"] = np.where(hl_diff > 1e-12, (df["close"] - df["open"]) / hl_diff, 0.0)
        df["tick_velocity"] = df["tick_volume"] / 1000.0
        df["XAGUSDc_return"] = df["close_xag"].pct_change() * 100.0
        df["EURUSDc_return"] = df["close_eur"].pct_change() * 100.0
        df["USDJPYc_return"] = df["close_jpy"].pct_change() * 100.0
        df.dropna(subset=self.feature_names, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def _ingest(self) -> pd.DataFrame:
        print(f"[mtx:{self.timeframe}] Ingest & merge_asof...")
        df_xau = pd.read_csv(self.xau_csv, parse_dates=["time"]).sort_values("time")
        df_eur = pd.read_csv(self.eur_csv, parse_dates=["time"]).sort_values("time")
        df_jpy = pd.read_csv(self.jpy_csv, parse_dates=["time"]).sort_values("time")
        df_xag = pd.read_csv(self.xag_csv, parse_dates=["time"]).sort_values("time")

        df = pd.merge_asof(
            df_xau,
            df_eur[["time", "close"]],
            on="time",
            direction="backward",
            suffixes=("", "_eur"),
        )
        df = pd.merge_asof(
            df, df_jpy[["time", "close"]], on="time", direction="backward", suffixes=("", "_jpy")
        )
        df = pd.merge_asof(
            df, df_xag[["time", "close"]], on="time", direction="backward", suffixes=("", "_xag")
        )
        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)
        print(f"  Merged {len(df)} aligned bars")
        return df

    def build(self, output_dir: Path, val_ratio: float = 0.2) -> dict[str, Any]:
        for p in [self.xau_csv, self.eur_csv, self.jpy_csv, self.xag_csv, self.labels_path]:
            if not p.exists():
                raise FileNotFoundError(f"Required file not found: {p}")

        df = self._ingest()
        df = self.compute_features(df)

        scaled, feat_mean, feat_std = self.rolling_standardize(df)
        X_all_seq = self.make_sequences(scaled)
        timestamps = df["time"].iloc[self.seq_len - 1 :].values

        label_dict = self.load_labels_jsonl(self.labels_path)
        X_seq, X_flat, y = self.anchor_labels(X_all_seq, timestamps, label_dict)

        total = len(y)
        tp_count = int((y == 1).sum())
        sl_count = int((y == -1).sum())
        to_count = int((y == 0).sum())
        print(
            f"  Matched {total}: tp={tp_count}({100*tp_count/total:.1f}%) "
            f"sl={sl_count}({100*sl_count/total:.1f}%) timeout={to_count}({100*to_count/total:.1f}%)"
        )

        split = self.temporal_split(X_seq, X_flat, y, val_ratio)

        output_dir.mkdir(parents=True, exist_ok=True)
        self.export_npz(
            split["X_seq_train"],
            split["X_flat_train"],
            split["y_train"],
            output_dir / "train.npz",
            feat_mean=feat_mean,
            feat_std=feat_std,
        )
        self.export_npz(
            split["X_seq_val"],
            split["X_flat_val"],
            split["y_val"],
            output_dir / "val.npz",
            feat_mean=feat_mean,
            feat_std=feat_std,
        )

        return {
            "dataset": f"micro_barrier_{self.timeframe.lower()}",
            "train_samples": len(split["y_train"]),
            "val_samples": len(split["y_val"]),
            "seq_len": self.seq_len,
            "num_features": len(self.feature_names),
            "xgb_dim": self.seq_len * len(self.feature_names),
            "train_dist": {
                "tp": int((split["y_train"] == 1).sum()),
                "timeout": int((split["y_train"] == 0).sum()),
                "sl": int((split["y_train"] == -1).sum()),
            },
            "val_dist": {
                "tp": int((split["y_val"] == 1).sum()),
                "timeout": int((split["y_val"] == 0).sum()),
                "sl": int((split["y_val"] == -1).sum()),
            },
        }

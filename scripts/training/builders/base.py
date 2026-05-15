"""Base dataset builder with rolling standardization, sequence generation, label anchoring."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def _parse_iso(ts: str) -> float:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


class BaseDatasetBuilder:
    """Abstract base: rolling standardization + sequence generation + label anchoring.

    Subclasses must set:
      - feature_names: list[str]
      - seq_len: int (default 32)
    And implement:
      - compute_features(df) -> pd.DataFrame
      - build(output_dir, val_ratio) -> dict
    """

    feature_names: list[str] = []
    seq_len: int = 32
    rolling_window: int = 1000

    def __init__(self, timeframe: str = "M5"):
        self.timeframe = timeframe

    # ── Rolling standardization (no future leakage) ─────────────────

    def rolling_standardize(
        self,
        df: pd.DataFrame,
        feature_cols: list[str] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cols = feature_cols or self.feature_names
        rmean = df[cols].rolling(window=self.rolling_window, min_periods=self.seq_len).mean()
        rstd = (
            df[cols]
            .rolling(window=self.rolling_window, min_periods=self.seq_len)
            .std()
            .replace(0.0, 1.0)
        )
        scaled = (df[cols] - rmean) / rstd
        scaled.bfill(inplace=True)
        return (
            scaled.values.astype(np.float32),
            rmean.iloc[-1].values.astype(np.float32),
            rstd.iloc[-1].values.astype(np.float32),
        )

    # ── Sequence generation ─────────────────────────────────────────

    def make_sequences(self, scaled: np.ndarray) -> np.ndarray:
        windows = sliding_window_view(scaled, window_shape=(self.seq_len, scaled.shape[1]))
        return windows.squeeze(axis=1)

    # ── Label anchoring ─────────────────────────────────────────────

    @staticmethod
    def load_labels_jsonl(path: Path) -> dict[float, list[dict[str, Any]]]:
        index: dict[float, list[dict[str, Any]]] = {}
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                et = rec.get("entry_time", "")
                if not et:
                    continue
                index.setdefault(_parse_iso(et), []).append(rec)
        print(f"  Loaded {sum(len(v) for v in index.values())} labels at {len(index)} entry times")
        return index

    def anchor_labels(
        self,
        X_all_seq: np.ndarray,
        timestamps: np.ndarray,
        label_dict: dict[float, list[dict[str, Any]]],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        X_seq_list, X_flat_list, y_list = [], [], []
        for idx in range(len(X_all_seq)):
            ts = pd.Timestamp(timestamps[idx]).timestamp()
            if ts not in label_dict:
                continue
            for lab in label_dict[ts]:
                y_list.append(int(lab.get("label_int", 0)))
                seq = X_all_seq[idx]
                X_seq_list.append(seq)
                X_flat_list.append(seq.flatten())
        if not X_seq_list:
            raise ValueError("No labels matched — check data/label alignment")
        return (
            np.stack(X_seq_list, axis=0).astype(np.float32),
            np.stack(X_flat_list, axis=0).astype(np.float32),
            np.array(y_list, dtype=np.int32),
        )

    # ── Temporal split ──────────────────────────────────────────────

    @staticmethod
    def temporal_split(
        X_seq: np.ndarray,
        X_flat: np.ndarray,
        y: np.ndarray,
        val_ratio: float = 0.2,
    ) -> dict[str, np.ndarray]:
        n = len(X_seq)
        n_val = int(n * val_ratio)
        return {
            "X_seq_train": X_seq[: n - n_val],
            "X_seq_val": X_seq[n - n_val :],
            "X_flat_train": X_flat[: n - n_val],
            "X_flat_val": X_flat[n - n_val :],
            "y_train": y[: n - n_val],
            "y_val": y[n - n_val :],
        }

    # ── Export ──────────────────────────────────────────────────────

    def export_npz(
        self,
        X_seq: np.ndarray,
        X_flat: np.ndarray,
        y: np.ndarray,
        output_path: Path,
        feat_mean: np.ndarray | None = None,
        feat_std: np.ndarray | None = None,
    ) -> Path:
        nf = len(self.feature_names)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            output_path,
            X=X_seq.astype(np.float32),
            X_flat=X_flat.astype(np.float32),
            y=y.astype(np.int32),
            seq_len=self.seq_len,
            num_features=nf,
            feature_names=np.array(self.feature_names, dtype=str),
            feat_mean=feat_mean if feat_mean is not None else np.zeros(nf, dtype=np.float32),
            feat_std=feat_std if feat_std is not None else np.ones(nf, dtype=np.float32),
        )
        print(f"  Exported {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
        return output_path

    # ── Subclass interface ──────────────────────────────────────────

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def build(self, output_dir: Path, val_ratio: float = 0.2) -> dict[str, Any]:
        raise NotImplementedError

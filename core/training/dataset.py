"""Unified Training Dataset — single abstraction over NPZ/Parquet with split logic.

Replaces scattered load+split logic across 5 trainers (xgb, lgb, deep_res_mlp,
online_mlp, arb). Supports deterministic random split and walk-forward temporal
split for time-series cross-validation.

Usage:
    ds = TrainingDataset.from_file("data/training/train.npz")
    train, val, test = ds.split(method="random", ratios=(0.7, 0.15, 0.15), seed=42)
    for fold_X, fold_y, fold_meta in ds.walk_forward(n_splits=5):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class TrainingDataset:
    """Unified dataset container with split and preprocessing utilities.

    Attributes:
        X: Feature matrix (n_samples, n_features).
        y: Labels, integer-encoded for classification.
        feature_names: Optional list of feature names.
        timestamps: Optional Unix epoch seconds per sample (for CPCV).
        metadata: Arbitrary metadata (source path, symbol, timeframe, etc.).
    """

    X: np.ndarray
    y: np.ndarray
    feature_names: list[str] = field(default_factory=list)
    timestamps: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Properties ──

    @property
    def n_samples(self) -> int:
        return len(self.X)

    @property
    def n_features(self) -> int:
        return self.X.shape[1]

    @property
    def class_balance(self) -> dict[int, float]:
        unique, counts = np.unique(self.y, return_counts=True)
        total = len(self.y)
        return {int(k): float(v / total) for k, v in zip(unique, counts, strict=False)}

    @property
    def has_timestamps(self) -> bool:
        if self.timestamps is None or len(self.timestamps) == 0:
            return False
        return bool(np.any(self.timestamps > 0))

    # ── Validation ──

    def validate(self) -> list[str]:
        """Run data quality checks. Returns list of issues (empty = clean)."""
        issues: list[str] = []
        if np.isnan(self.X).any():
            nan_cols = [i for i in range(self.X.shape[1]) if np.isnan(self.X[:, i]).any()]
            issues.append(f"NaN values found in feature columns: {nan_cols}")
        if np.isinf(self.X).any():
            inf_cols = [i for i in range(self.X.shape[1]) if np.isinf(self.X[:, i]).any()]
            issues.append(f"Inf values found in feature columns: {inf_cols}")
        if self.n_samples < 20:
            issues.append(f"Too few samples: {self.n_samples} (need >= 20)")
        unique_vals, counts = np.unique(self.y, return_counts=True)
        class_counts = counts
        if len(counts) > 0 and counts.min() < 3:
            issues.append(f"Minority class has fewer than 3 samples: {class_counts.tolist()}")
        return issues

    # ── Splitting ──

    def split(
        self,
        method: str = "random",
        ratios: tuple[float, float, float] = (0.70, 0.15, 0.15),
        seed: int = 42,
    ) -> tuple[TrainingDataset, TrainingDataset | None, TrainingDataset | None]:
        """Split into train / val / test subsets.

        Args:
            method: "random" (shuffled) or "sequential" (first-N).
            ratios: (train_ratio, val_ratio, test_ratio). Sum must be ≤ 1.0.
            seed: RNG seed for reproducibility.

        Returns:
            (train, val, test) — val and/or test may be None if ratio is 0.
        """
        train_r, val_r, test_r = ratios
        n = self.n_samples

        if method == "random":
            rng = np.random.RandomState(seed)
            idx = rng.permutation(n)
        else:
            idx = np.arange(n)

        n_train = int(n * train_r)
        n_val = int(n * val_r) if val_r > 0 else 0

        def _make_ds(ids: np.ndarray) -> TrainingDataset:
            return TrainingDataset(
                X=self.X[ids].copy(),
                y=self.y[ids].copy(),
                feature_names=list(self.feature_names),
                metadata={**self.metadata, "split_method": method, "split_seed": seed},
            )

        train_ids = idx[:n_train]
        val_ids = idx[n_train : n_train + n_val] if n_val > 0 else np.array([], dtype=int)
        test_ids = idx[n_train + n_val :] if test_r > 0 else np.array([], dtype=int)

        train = _make_ds(train_ids)
        val = _make_ds(val_ids) if len(val_ids) > 0 else None
        test = _make_ds(test_ids) if len(test_ids) > 0 else None
        return train, val, test

    def walk_forward(
        self,
        n_splits: int = 5,
        train_ratio: float = 0.6,
        val_ratio: float = 0.15,
        min_train_size: int = 200,
    ):
        """Generator yielding (train, val, test) folds for time-series CV.

        Each fold extends the training window and slides the test window forward.
        """
        n = self.n_samples
        fold_size = n // n_splits

        for i in range(n_splits):
            test_end = min(n, int(n * (train_ratio + val_ratio)) + fold_size * (i + 1))
            test_start = max(min_train_size, test_end - fold_size)
            train_end = test_start

            train_slice = self.X[:train_end], self.y[:train_end]
            test_slice = (
                self.X[test_start:test_end],
                self.y[test_start:test_end],
            )
            yield train_slice, test_slice, i

    def purged_walk_forward(
        self,
        n_splits: int = 5,
        train_ratio: float = 0.6,
        purge_gap: int = 10,
        min_train_size: int = 200,
    ):
        """Generator yielding (train, test, fold_idx) with purge gap.

        A ``purge_gap`` of N samples is removed between the end of the training
        set and the start of the test set to prevent information leakage through
        overlapping observations (e.g., labels that look ahead).
        """
        n = self.n_samples
        fold_size = (n - min_train_size - purge_gap * n_splits) // n_splits
        if fold_size < 10:
            fold_size = max(10, n // (n_splits + 1))

        for i in range(n_splits):
            test_start = min_train_size + i * (fold_size + purge_gap)
            test_end = min(n, test_start + fold_size)
            train_end = max(0, test_start - purge_gap)

            if train_end < min_train_size or test_end - test_start < 10:
                continue

            train_slice = self.X[:train_end], self.y[:train_end]
            test_slice = (
                self.X[test_start:test_end],
                self.y[test_start:test_end],
            )
            yield train_slice, test_slice, i

    def embargo_walk_forward(
        self,
        n_splits: int = 5,
        train_ratio: float = 0.6,
        purge_gap: int = 10,
        embargo_gap: int = 5,
        min_train_size: int = 200,
    ):
        """Generator yielding (train, test, fold_idx) with purge + embargo gaps.

        Purge removes samples immediately before the test set (preventing
        train→test leakage from look-ahead labels).  Embargo removes samples
        immediately *after* the test set from subsequent training folds,
        preventing test→train leakage when test observations are correlated
        with future samples (common in financial time series with overlapping
        returns or multi-bar labels).

        Reference: De Prado, "Advances in Financial Machine Learning" (2018),
        Chapter 7 — Cross-Validation in Finance.
        """
        n = self.n_samples
        chunk = (n - min_train_size - (purge_gap + embargo_gap) * n_splits) // n_splits
        if chunk < 10:
            chunk = max(10, n // (n_splits + 1))

        for i in range(n_splits):
            test_start = min_train_size + i * (chunk + purge_gap + embargo_gap)
            test_end = min(n, test_start + chunk)
            train_end = max(0, test_start - purge_gap)

            if train_end < min_train_size or test_end - test_start < 10:
                continue

            train_slice = self.X[:train_end], self.y[:train_end]
            test_slice = (
                self.X[test_start:test_end],
                self.y[test_start:test_end],
            )
            yield train_slice, test_slice, i

    # ── Factory ──

    @classmethod
    def from_file(cls, path: str | Path) -> TrainingDataset:
        """Load from NPZ or Parquet.

        When the NPZ contains a ``timestamps`` array (Unix epoch seconds),
        it is loaded into the dataset for temporal validation and CPCV.
        """
        path = Path(path)
        ext = path.suffix.lower()
        ts: np.ndarray | None = None

        if ext == ".npz":
            d = np.load(path, allow_pickle=True)
            X, y = d["X"], d["y"]
            feat_raw = d.get("feature_names")
            if feat_raw is None:
                feature_names = [f"f_{i}" for i in range(X.shape[1])]
            elif isinstance(feat_raw, np.ndarray):
                feature_names = feat_raw.tolist()
            else:
                feature_names = list(feat_raw)
            # Load timestamps if present (dataset v2)
            ts_raw = d.get("timestamps")
            if ts_raw is not None:
                ts = np.asarray(ts_raw, dtype=np.float64)
        elif ext == ".parquet":
            import pandas as pd

            df = pd.read_parquet(path)
            feature_cols = [c for c in df.columns if c.startswith("f_")]
            if not feature_cols:
                feature_cols = [f"f_{i}" for i in range(df.shape[1])]
            X = df[feature_cols].to_numpy(dtype=np.float64)
            y_col = next((c for c in df.columns if c in ("label", "y", "target")), feature_cols[-1])
            if y_col in feature_cols:
                y = np.zeros(len(X), dtype=np.int32)
            else:
                y_raw = df[y_col]
                if y_raw.dtype == object:
                    y = (
                        y_raw.map({"win": 1, "loss": -1, "neutral": 0})
                        .fillna(0)
                        .to_numpy(dtype=np.int32)
                    )
                else:
                    y = y_raw.to_numpy(dtype=np.int32)
                y = np.where(y == -1, 0, np.where(y == 1, 2, 1)).astype(np.int64)
            feature_names = feature_cols
        else:
            raise ValueError(f"Unsupported format: {ext}")

        return cls(
            X=np.asarray(X, dtype=np.float64),
            y=np.asarray(y, dtype=np.int64),
            feature_names=feature_names,
            timestamps=ts,
            metadata={"source_path": str(path.resolve()), "format": ext},
        )

    # ── Preprocessing ──

    def normalize(
        self, strategy: str = "standard", scaler_params: dict | None = None
    ) -> TrainingDataset:
        """Apply normalization in-place. Returns self for chaining."""
        if scaler_params and strategy == "fixed":
            mean = np.array(scaler_params["mean"])
            std = np.array(scaler_params["std"])
            self.X = (self.X - mean) / np.maximum(std, 1e-8)
        elif strategy == "standard":
            mean = self.X.mean(axis=0, keepdims=True)
            std = self.X.std(axis=0, keepdims=True)
            self.X = (self.X - mean) / np.maximum(std, 1e-8)
        elif strategy == "minmax":
            xmin = self.X.min(axis=0, keepdims=True)
            xmax = self.X.max(axis=0, keepdims=True)
            self.X = (self.X - xmin) / np.maximum(xmax - xmin, 1e-8)
        return self

    def compute_scaler_params(self) -> dict[str, Any]:
        """Compute StandardScaler mean/std for export with model."""
        mean = self.X.mean(axis=0).tolist()
        std = self.X.std(axis=0).tolist()
        std = [max(s, 1e-8) for s in std]
        return {"mean": mean, "std": std, "n_features": self.n_features}


# ── Module-level convenience functions ──


def train_val_test_split(
    X: np.ndarray,
    y: np.ndarray,
    ratios: tuple[float, float, float] = (0.70, 0.15, 0.15),
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray, np.ndarray | None]:
    """Quick train/val/test split without creating a Dataset object."""
    ds = TrainingDataset(X=X, y=y)
    train, val, test = ds.split(method="random", ratios=ratios, seed=seed)
    return (
        train.X,
        train.y,
        (val.X if val else None),
        (val.y if val else None),
        (test.X if test else None),
        (test.y if test else None),
    )  # type: ignore[return-value]


def walk_forward_splits(
    X: np.ndarray,
    y: np.ndarray,
    n_splits: int = 5,
    train_ratio: float = 0.6,
):
    """Generator yielding (X_train, y_train, X_test, y_test) for each fold."""
    ds = TrainingDataset(X=X, y=y)
    for (X_tr, y_tr), (X_te, y_te), _fold in ds.walk_forward(
        n_splits=n_splits, train_ratio=train_ratio
    ):
        yield X_tr, y_tr, X_te, y_te


# ── Timestamp utilities ──


def validate_temporal_order(timestamps: np.ndarray) -> bool:
    """Verify that samples are chronologically ordered (non-decreasing)."""
    if len(timestamps) < 2:
        return True
    return bool(np.all(np.diff(timestamps) >= 0))


def get_date_range(timestamps: np.ndarray) -> tuple[str, str]:
    """Return (min_date, max_date) as ISO-8601 strings."""
    from datetime import UTC, datetime

    valid = timestamps[timestamps > 0]
    if len(valid) == 0:
        return ("", "")
    min_dt = datetime.fromtimestamp(float(valid.min()), tz=UTC).isoformat()
    max_dt = datetime.fromtimestamp(float(valid.max()), tz=UTC).isoformat()
    return (min_dt, max_dt)

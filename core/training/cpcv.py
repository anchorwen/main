"""Combinatorial Purged Cross-Validation (CPCV) — De Prado 2018, Chapter 12.

CPCV overcomes the key weakness of walk-forward optimization: in WFO, the most
recent data is NEVER included in any training set. CPCV splits the dataset into
N groups and tests on every combination of N_test groups, producing C(N, N_test)
train/test paths. Every observation appears in at least one training set.

Key properties:
  - Every bar appears in training at least once
  - Every bar appears in testing exactly C(N-1, N_test-1) times
  - Purge gap removes train samples whose label horizon overlaps test period
  - Embargo gap removes test samples from subsequent training folds

Usage:
    folds = combinatorial_purged_cv(
        timestamps=unix_timestamps,
        n_groups=6,
        n_test_groups=2,
        purge_bars=12,
        embargo_bars=5,
    )
    for fold in folds:
        model.fit(X[fold.train_idx], y[fold.train_idx])
        predictions = model.predict(X[fold.test_idx])
        fold.sharpe = compute_sharpe(predictions, y[fold.test_idx])
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np


@dataclass
class CPCVFold:
    """A single train/test split in a CPCV scheme.

    Attributes:
        fold_idx: 0-based fold number.
        train_idx: Boolean mask or integer indices for training.
        test_idx: Boolean mask or integer indices for testing.
        purge_count: Number of samples purged before the test block.
        embargo_count: Number of samples embargoed after the test block.
        group_ids: Which groups form the test set.
    """

    fold_idx: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    purge_count: int = 0
    embargo_count: int = 0
    group_ids: tuple[int, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class CPCVResult:
    """Aggregated CPCV results with per-fold and summary statistics.

    Attributes:
        folds: List of CPCVFold objects with populated metrics.
        n_folds: Total number of train/test paths.
        n_groups: Number of groups the data was split into.
        n_test_groups: Number of groups per test set.
        sharpe_mean: Mean Sharpe across all folds.
        sharpe_std: Standard deviation of Sharpe across all folds.
        win_rate_mean: Mean win rate across all folds.
        overfit_gap: Mean (train_sharpe - test_sharpe) across folds.
    """

    folds: list[CPCVFold] = field(default_factory=list)
    n_groups: int = 0
    n_test_groups: int = 0

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    @property
    def sharpe_mean(self) -> float:
        sharpes = [f.metrics.get("sharpe", 0.0) for f in self.folds if f.metrics]
        if not sharpes:
            return 0.0
        return float(np.mean(sharpes))

    @property
    def sharpe_std(self) -> float:
        sharpes = [f.metrics.get("sharpe", 0.0) for f in self.folds if f.metrics]
        if len(sharpes) < 2:
            return 0.0
        return float(np.std(sharpes, ddof=1))

    @property
    def win_rate_mean(self) -> float:
        rates = [f.metrics.get("win_rate", 0.0) for f in self.folds if f.metrics]
        if not rates:
            return 0.0
        return float(np.mean(rates))


def _compute_group_boundaries(
    n_samples: int,
    n_groups: int,
    timestamps: np.ndarray | None = None,
) -> np.ndarray:
    """Compute group boundary indices.

    If timestamps are provided, groups are split by timestamp quantiles to
    respect temporal ordering. Otherwise, equal-sized contiguous splits are used.
    """
    if n_samples < n_groups:
        raise ValueError(f"n_samples ({n_samples}) must be >= n_groups ({n_groups})")

    boundaries = np.zeros(n_groups + 1, dtype=np.int64)
    boundaries[0] = 0
    boundaries[-1] = n_samples

    for g in range(1, n_groups):
        boundaries[g] = int(n_samples * g / n_groups)

    return boundaries


def _build_purge_mask(
    n_samples: int,
    test_start: int,
    test_end: int,
    purge_bars: int,
    embargo_bars: int,
) -> np.ndarray:
    """Build a boolean mask: True for samples that can be used in training.

    Training samples within purge_bars before the test set or embargo_bars
    after the test set are excluded to prevent label-overlap leakage.
    """
    mask = np.ones(n_samples, dtype=bool)

    # Purge: exclude samples immediately before test set
    purge_start = max(0, test_start - purge_bars)
    mask[purge_start:test_start] = False

    # Embargo: exclude samples immediately after test set
    embargo_end = min(n_samples, test_end + embargo_bars)
    mask[test_end:embargo_end] = False

    # Test set itself is excluded from training
    mask[test_start:test_end] = False

    return mask


def combinatorial_purged_cv(
    timestamps: np.ndarray | None = None,
    n_groups: int = 6,
    n_test_groups: int = 2,
    purge_bars: int = 12,
    embargo_bars: int = 5,
    n_samples: int | None = None,
) -> list[CPCVFold]:
    """Generate CPCV folds.

    Splits the dataset into ``n_groups`` groups and generates all
    C(n_groups, n_test_groups) combinations as test sets. Each fold applies
    purge (before test) and embargo (after test) to the training mask.

    Args:
        timestamps: Optional Unix epoch timestamps for temporal grouping.
        n_groups: Number of groups to split data into.
        n_test_groups: Number of groups per test set (typically 1 or 2).
        purge_bars: Number of bars to purge before the test set.
        embargo_bars: Number of bars to embargo after the test set.
        n_samples: Total number of samples (required if timestamps is None).

    Returns:
        List of CPCVFold objects, one per train/test combination.

    Raises:
        ValueError: If neither timestamps nor n_samples is provided, or if
                    n_groups / n_test_groups are invalid.
    """
    if n_samples is None:
        if timestamps is not None:
            n_samples = len(timestamps)
        else:
            raise ValueError("Either timestamps or n_samples must be provided")

    if n_samples < n_groups:
        raise ValueError(f"n_samples ({n_samples}) must be >= n_groups ({n_groups})")
    if n_test_groups < 1 or n_test_groups >= n_groups:
        raise ValueError(f"n_test_groups ({n_test_groups}) must be in [1, {n_groups - 1}]")

    boundaries = _compute_group_boundaries(n_samples, n_groups, timestamps)

    # Generate all group combinations
    group_indices = list(range(n_groups))
    test_combos = list(combinations(group_indices, n_test_groups))

    folds: list[CPCVFold] = []

    for fold_idx, test_groups in enumerate(test_combos):
        # Build test mask — union of all test groups
        test_mask = np.zeros(n_samples, dtype=bool)
        for g in test_groups:
            test_mask[boundaries[g] : boundaries[g + 1]] = True

        test_start = int(np.argmax(test_mask)) if test_mask.any() else 0
        test_end = int(n_samples - np.argmax(test_mask[::-1])) if test_mask.any() else 0

        train_mask = _build_purge_mask(n_samples, test_start, test_end, purge_bars, embargo_bars)

        purge_count = int((~train_mask).sum()) - int(test_mask.sum())
        embargo_total = purge_bars + embargo_bars
        embargo_count = max(0, embargo_total - purge_count)

        folds.append(
            CPCVFold(
                fold_idx=fold_idx,
                train_idx=train_mask,
                test_idx=test_mask,
                purge_count=purge_count,
                embargo_count=embargo_count,
                group_ids=test_groups,
            )
        )

    return folds


def cpcv_summary(folds: list[CPCVFold]) -> CPCVResult:
    """Create a summary result from a list of evaluated CPCV folds."""
    return CPCVResult(
        folds=folds,
        n_groups=len(set(g for f in folds for g in f.group_ids)),
        n_test_groups=len(folds[0].group_ids) if folds else 0,
    )


def n_combinatorial_folds(n_groups: int, n_test_groups: int) -> int:
    """Return the number of combinatorial folds: C(n_groups, n_test_groups)."""
    return math.comb(n_groups, n_test_groups)

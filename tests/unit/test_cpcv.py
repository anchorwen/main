"""Unit tests for Combinatorial Purged Cross-Validation (CPCV)."""

from __future__ import annotations

import numpy as np
import pytest

from core.training.cpcv import (
    CPCVFold,
    combinatorial_purged_cv,
    cpcv_summary,
    n_combinatorial_folds,
)


class TestCombinatorialFolds:
    """Tests for n_combinatorial_folds."""

    def test_6_choose_2(self):
        assert n_combinatorial_folds(6, 2) == 15

    def test_4_choose_1(self):
        assert n_combinatorial_folds(4, 1) == 4

    def test_3_choose_1(self):
        assert n_combinatorial_folds(3, 1) == 3

    def test_5_choose_2(self):
        assert n_combinatorial_folds(5, 2) == 10


class TestCPCV:
    """Tests for combinatorial_purged_cv."""

    def test_basic_folds_count(self):
        folds = combinatorial_purged_cv(
            n_samples=1000,
            n_groups=6,
            n_test_groups=2,
        )
        assert len(folds) == 15  # C(6,2)

    def test_folds_count_4_choose_1(self):
        folds = combinatorial_purged_cv(
            n_samples=1000,
            n_groups=4,
            n_test_groups=1,
        )
        assert len(folds) == 4

    def test_every_sample_in_some_train(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=5,
            n_test_groups=1,
            purge_bars=0,
            embargo_bars=0,
        )
        # Every sample should appear in at least one training set
        train_coverage = np.zeros(100, dtype=bool)
        for fold in folds:
            train_coverage[fold.train_idx] = True
        assert train_coverage.all(), f"Missing from training: {np.where(~train_coverage)[0]}"

    def test_no_overlap_train_test(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=5,
            n_test_groups=1,
            purge_bars=0,
            embargo_bars=0,
        )
        for fold in folds:
            overlap = fold.train_idx & fold.test_idx
            assert not overlap.any(), f"Train/test overlap: {overlap.sum()} samples"

    def test_group_ids_unique_per_fold(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=3,
            n_test_groups=1,
        )
        group_sets = [fold.group_ids for fold in folds]
        assert len(group_sets) == len(set(group_sets))  # all unique

    def test_purge_excludes_recent_samples(self):
        # With a small dataset and larger purge, samples near test boundary
        # should be excluded from training
        folds = combinatorial_purged_cv(
            n_samples=30,
            n_groups=3,
            n_test_groups=1,
            purge_bars=5,
            embargo_bars=0,
        )
        for fold in folds:
            # Test set indices
            test_positions = np.where(fold.test_idx)[0]
            if len(test_positions) == 0:
                continue
            test_start = test_positions[0]
            # Samples within purge_bars before test start should NOT be in train
            purge_zone = np.arange(max(0, test_start - 5), test_start)
            for p in purge_zone:
                if p < len(fold.train_idx):
                    assert not fold.train_idx[p], f"Sample {p} in purge zone but in training set"

    def test_embargo_excludes_post_test_samples(self):
        folds = combinatorial_purged_cv(
            n_samples=30,
            n_groups=3,
            n_test_groups=1,
            purge_bars=0,
            embargo_bars=5,
        )
        for fold in folds:
            test_positions = np.where(fold.test_idx)[0]
            if len(test_positions) == 0:
                continue
            test_end = test_positions[-1] + 1
            # Samples within embargo_bars after test end should NOT be in train
            embargo_zone = np.arange(test_end, min(30, test_end + 5))
            for e in embargo_zone:
                if e < len(fold.train_idx):
                    assert not fold.train_idx[e], f"Sample {e} in embargo zone but in training set"

    def test_with_timestamps(self):
        rng = np.random.RandomState(42)
        timestamps = np.arange(1000, dtype=np.float64) + rng.randn(1000) * 0.1
        timestamps = np.sort(timestamps)

        folds = combinatorial_purged_cv(
            timestamps=timestamps,
            n_groups=6,
            n_test_groups=2,
            purge_bars=12,
            embargo_bars=5,
        )
        assert len(folds) == 15
        # All folds should have valid indices
        for fold in folds:
            assert len(fold.train_idx) <= len(timestamps)
            assert len(fold.test_idx) <= len(timestamps)

    def test_n_samples_less_than_n_groups(self):
        with pytest.raises(ValueError, match="n_samples"):
            combinatorial_purged_cv(
                n_samples=5,
                n_groups=10,
                n_test_groups=2,
            )

    def test_n_test_groups_out_of_range(self):
        with pytest.raises(ValueError, match="n_test_groups"):
            combinatorial_purged_cv(
                n_samples=100,
                n_groups=5,
                n_test_groups=5,  # must be < n_groups
            )

    def test_n_test_groups_zero(self):
        with pytest.raises(ValueError, match="n_test_groups"):
            combinatorial_purged_cv(
                n_samples=100,
                n_groups=5,
                n_test_groups=0,
            )

    def test_missing_both_timestamps_and_n_samples(self):
        with pytest.raises(ValueError, match="timestamps or n_samples"):
            combinatorial_purged_cv(
                n_groups=5,
                n_test_groups=1,
            )

    def test_fold_attributes(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=4,
            n_test_groups=1,
            purge_bars=5,
            embargo_bars=3,
        )
        for i, fold in enumerate(folds):
            assert fold.fold_idx == i
            assert isinstance(fold.train_idx, np.ndarray)
            assert isinstance(fold.test_idx, np.ndarray)
            assert isinstance(fold.group_ids, tuple)
            assert fold.train_idx.dtype == bool
            assert fold.test_idx.dtype == bool


class TestCPCVSummary:
    """Tests for cpcv_summary."""

    def test_empty_folds(self):
        result = cpcv_summary([])
        assert result.n_folds == 0
        assert result.sharpe_mean == 0.0
        assert result.sharpe_std == 0.0

    def test_with_metrics(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=3,
            n_test_groups=1,
        )
        for i, fold in enumerate(folds):
            fold.metrics = {"sharpe": 1.0 + i * 0.1, "win_rate": 0.5 + i * 0.02}

        result = cpcv_summary(folds)
        assert result.n_folds == 3
        assert result.n_groups == 3
        assert result.n_test_groups == 1
        assert result.sharpe_mean > 0

    def test_single_fold_std_is_zero(self):
        folds = combinatorial_purged_cv(
            n_samples=100,
            n_groups=2,
            n_test_groups=1,
        )
        folds[0].metrics = {"sharpe": 1.5}
        result = cpcv_summary(folds)
        assert result.sharpe_std == 0.0  # single fold → no std


class TestCPCVFold:
    """Tests for CPCVFold dataclass."""

    def test_defaults(self):
        fold = CPCVFold(
            fold_idx=0,
            train_idx=np.ones(10, dtype=bool),
            test_idx=np.zeros(10, dtype=bool),
        )
        assert fold.fold_idx == 0
        assert fold.purge_count == 0
        assert fold.embargo_count == 0
        assert fold.group_ids == ()
        assert fold.metrics == {}

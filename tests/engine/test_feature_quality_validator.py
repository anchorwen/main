"""Feature quality validator contract tests."""

import numpy as np

from scripts.validators.feature_quality_validator import (
    FEATURE_NAMES,
    check_feature_vector,
    compute_distribution_shift,
    compute_per_feature_stats,
    validate_sample_quality,
)


def test_check_feature_vector_valid():
    vec = np.arange(40, dtype=np.float64) * 0.01
    result = check_feature_vector(vec)
    assert result["valid"] is True
    assert result["dim"] == 40
    assert result["nan_count"] == 0
    assert result["inf_count"] == 0
    assert result["is_zero_vector"] is False


def test_check_feature_vector_nan():
    vec = np.zeros(40, dtype=np.float64)
    vec[5] = np.nan
    result = check_feature_vector(vec)
    assert result["valid"] is False
    assert result["nan_count"] == 1
    assert result["inf_count"] == 0
    assert len(result["nan_features"]) == 1
    assert result["nan_features"][0] == FEATURE_NAMES[5]


def test_check_feature_vector_inf():
    vec = np.zeros(40, dtype=np.float64)
    vec[10] = np.inf
    vec[11] = -np.inf
    result = check_feature_vector(vec)
    assert result["valid"] is False
    assert result["inf_count"] == 2
    assert len(result["inf_features"]) == 2


def test_check_feature_vector_zero():
    vec = np.zeros(40, dtype=np.float64)
    result = check_feature_vector(vec)
    assert result["valid"] is False
    assert result["is_zero_vector"] is True
    assert result["zero_fraction"] == 1.0


def test_check_feature_vector_empty():
    result = check_feature_vector(np.array([]))
    assert result["valid"] is False
    assert result["error"] == "empty_vector"


def test_validate_sample_quality():
    vectors = [
        np.arange(40, dtype=np.float64) * 0.01,
        np.arange(40, dtype=np.float64) * 0.02,
        np.zeros(40, dtype=np.float64),
    ]
    quality = validate_sample_quality(vectors)
    assert quality["total_vectors"] == 3
    assert quality["valid_vectors"] == 2
    assert quality["zero_vectors"] == 1
    assert quality["nan_vectors"] == 0
    assert abs(quality["valid_rate"] - 2 / 3) < 0.001


def test_validate_sample_quality_empty():
    quality = validate_sample_quality([])
    assert quality["total_vectors"] == 0
    assert quality["valid_rate"] == 0.0


def test_compute_per_feature_stats():
    vectors = [
        np.array([1.0, 2.0, 3.0], dtype=np.float64),
        np.array([2.0, 3.0, 4.0], dtype=np.float64),
        np.array([3.0, 4.0, 5.0], dtype=np.float64),
    ]
    stats = compute_per_feature_stats(vectors, feature_names=["A", "B", "C"])
    assert stats["sample_size"] == 3
    assert len(stats["features"]) == 3
    assert stats["features"]["A"]["mean"] == 2.0
    assert stats["features"]["A"]["min"] == 1.0
    assert stats["features"]["A"]["max"] == 3.0
    assert stats["features"]["B"]["mean"] == 3.0


def test_compute_per_feature_stats_empty():
    stats = compute_per_feature_stats([])
    assert stats["sample_size"] == 0
    assert stats["features"] == {}


def test_compute_per_feature_stats_missing_rate():
    vectors = [
        np.array([0.0, 5.0, 0.0], dtype=np.float64),
        np.array([1.0, 0.0, 0.0], dtype=np.float64),
    ]
    stats = compute_per_feature_stats(vectors, feature_names=["A", "B", "C"])
    assert stats["features"]["A"]["missing_rate"] == 0.5
    assert stats["features"]["B"]["missing_rate"] == 0.5
    assert stats["features"]["C"]["missing_rate"] == 1.0


def test_distribution_shift_no_shift():
    stats = {
        "features": {
            "M5_Ret_1": {"mean": 0.0001, "std": 0.0007},
        }
    }
    norm = {
        "mean": [0.000003],
        "std": [0.00067],
    }
    shift = compute_distribution_shift(stats, norm, shift_threshold=3.0)
    assert shift["shift_detected"] is False
    assert shift["shifted_count"] == 0


def test_distribution_shift_detected():
    stats = {
        "features": {
            "M5_Ret_1": {"mean": 0.005, "std": 0.001},
        }
    }
    norm = {
        "mean": [0.000003],
        "std": [0.00067],
    }
    shift = compute_distribution_shift(stats, norm, shift_threshold=2.0)
    assert shift["shift_detected"] is True
    assert shift["shifted_count"] == 1
    assert shift["shifted_features"][0]["feature"] == "M5_Ret_1"


def test_distribution_shift_no_baseline():
    shift = compute_distribution_shift({"features": {}}, {})
    assert shift["shift_detected"] is False
    assert "error" in shift


def test_check_feature_vector_smaller_dim():
    vec = np.array([1.0, 2.0], dtype=np.float64)
    result = check_feature_vector(vec)
    assert result["dim"] == 2
    assert result["valid"] is True

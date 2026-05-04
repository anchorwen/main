"""Feature vector quality validation: NaN/Inf, zero-vector, range, distribution shift.

Pure-function validator for V9 40-dim feature vectors. Works on raw (pre-normalization)
vectors — compares against normalization config baseline for shift detection.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

FEATURE_DIM = 40
FEATURE_NAMES = V9_INSTITUTIONAL_40_FEATURES


def _is_valid_finite(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def check_feature_vector(vector: np.ndarray) -> dict[str, Any]:
    """Validate a single feature vector. Returns per-record quality flags."""
    if vector.size == 0:
        return {
            "valid": False,
            "error": "empty_vector",
            "dim": 0,
            "nan_count": 0,
            "inf_count": 0,
            "is_zero_vector": False,
            "zero_fraction": 0.0,
            "finite_count": 0,
        }

    flat = vector.flatten().astype(np.float64)
    dim = flat.shape[0]

    nan_mask = np.isnan(flat)
    inf_mask = np.isinf(flat)
    finite_mask = ~nan_mask & ~inf_mask

    nan_count = int(nan_mask.sum())
    inf_count = int(inf_mask.sum())
    finite_count = int(finite_mask.sum())

    is_zero = bool((np.abs(flat) < 1e-9).all())
    zero_count = int((np.abs(flat) < 1e-9).sum())
    zero_fraction = round(zero_count / dim, 4) if dim > 0 else 0.0

    return {
        "valid": nan_count == 0 and inf_count == 0 and not is_zero,
        "dim": dim,
        "nan_count": nan_count,
        "inf_count": inf_count,
        "is_zero_vector": is_zero,
        "zero_fraction": zero_fraction,
        "finite_count": finite_count,
        "nan_features": [FEATURE_NAMES[i] for i in range(min(dim, 40)) if nan_mask[i]]
        if nan_count > 0
        else [],
        "inf_features": [FEATURE_NAMES[i] for i in range(min(dim, 40)) if inf_mask[i]]
        if inf_count > 0
        else [],
    }


def compute_per_feature_stats(
    vectors: list[np.ndarray],
    *,
    feature_names: list[str] | None = None,
) -> dict[str, Any]:
    """Compute per-feature statistics across a sample of vectors.

    Returns per-feature mean, std, min, max, missing_rate (zero-fraction).
    """
    names = feature_names or FEATURE_NAMES
    if not vectors:
        return {"sample_size": 0, "features": {}}

    dim = vectors[0].flatten().shape[0]
    matrix = np.zeros((len(vectors), dim), dtype=np.float64)
    for i, v in enumerate(vectors):
        flat = v.flatten().astype(np.float64)
        if flat.shape[0] >= dim:
            matrix[i, :] = flat[:dim]
        else:
            matrix[i, : flat.shape[0]] = flat

    features = {}
    for j in range(min(dim, len(names))):
        col = matrix[:, j]
        finite = col[np.isfinite(col)]
        zero_rate = float((np.abs(col) < 1e-9).sum()) / len(col)

        if len(finite) == 0:
            features[names[j]] = {
                "mean": None,
                "std": None,
                "min": None,
                "max": None,
                "missing_rate": round(zero_rate, 4),
                "finite_count": 0,
            }
        else:
            features[names[j]] = {
                "mean": round(float(np.mean(finite)), 6),
                "std": round(float(np.std(finite)), 6),
                "min": round(float(np.min(finite)), 6),
                "max": round(float(np.max(finite)), 6),
                "missing_rate": round(zero_rate, 4),
                "finite_count": len(finite),
            }

    return {"sample_size": len(vectors), "features": features}


def compute_distribution_shift(
    sample_stats: dict[str, Any],
    norm_config: dict[str, Any],
    *,
    shift_threshold: float = 2.0,
) -> dict[str, Any]:
    """Compare sample feature distributions against normalization baseline.

    Computes z-score of sample_mean relative to baseline (mean, std).
    Flags features where |z_shift| > shift_threshold.
    """
    baseline_mean = norm_config.get("mean", [])
    baseline_std = norm_config.get("std", [])
    features_stats = sample_stats.get("features", {})

    if not baseline_mean or not baseline_std:
        return {
            "shift_detected": False,
            "shift_threshold": shift_threshold,
            "shifted_features": [],
            "error": "no_baseline_data",
        }

    shifted: list[dict[str, Any]] = []
    for i, name in enumerate(FEATURE_NAMES):
        feat = features_stats.get(name)
        if feat is None or feat["mean"] is None:
            continue
        if i >= len(baseline_mean) or i >= len(baseline_std):
            continue
        baseline_mu = baseline_mean[i]
        baseline_sigma = baseline_std[i]
        sample_mu = feat["mean"]

        if baseline_sigma < 1e-9:
            continue

        z_shift = (sample_mu - baseline_mu) / baseline_sigma
        if abs(z_shift) > shift_threshold:
            shifted.append(
                {
                    "feature": name,
                    "index": i,
                    "sample_mean": sample_mu,
                    "baseline_mean": round(baseline_mu, 6),
                    "baseline_std": round(baseline_sigma, 6),
                    "z_shift": round(z_shift, 4),
                }
            )

    return {
        "shift_detected": len(shifted) > 0,
        "shift_threshold": shift_threshold,
        "shifted_count": len(shifted),
        "shifted_features": shifted,
    }


def validate_sample_quality(vectors: list[np.ndarray]) -> dict[str, Any]:
    """Aggregate quality check across multiple feature vectors."""
    if not vectors:
        return {
            "total_vectors": 0,
            "valid_vectors": 0,
            "zero_vectors": 0,
            "nan_vectors": 0,
            "inf_vectors": 0,
            "valid_rate": 0.0,
        }

    results = [check_feature_vector(v) for v in vectors]
    total = len(results)
    valid = sum(1 for r in results if r["valid"])
    zero = sum(1 for r in results if r["is_zero_vector"])
    nan = sum(1 for r in results if r["nan_count"] > 0)
    inf = sum(1 for r in results if r["inf_count"] > 0)

    return {
        "total_vectors": total,
        "valid_vectors": valid,
        "zero_vectors": zero,
        "nan_vectors": nan,
        "inf_vectors": inf,
        "valid_rate": round(valid / total, 4) if total > 0 else 0.0,
    }

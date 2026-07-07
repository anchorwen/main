"""Feature-Space OOD Gateway — Mahalanobis distance regime-shift detector.

Institutional mandate (DQAF-20260705-064 P2): The system must possess
immunity against trading in unknown feature manifolds.  When the live
feature vector drifts beyond the training distribution (OOD), the model
must automatically fall silent — confidence=0, direction=neutral.

Architecture
------------
Offline:  ``scripts/export_ood_params.py`` reads the feature store,
          computes centroid + covariance + inverse per feature schema,
          and saves ``data_btc/models/ood_{schema}.json``.

Online:   ``OODGateway.check(feature_vector)`` loads the schema's OOD
          params once (cached), computes the Mahalanobis distance, and
          returns an ``OODVerdict``.

Integration point: ``strategy_evaluator.py`` Cut 2 pre-inference gates,
after ``repair_feature_vector()`` + ``check_feature_vector()``.

Algorithm
---------
Mahalanobis distance:  d = sqrt((x - mu)^T · inv_cov · (x - mu))

Under multivariate normality, d² ~ chi²(k) where k = feature dimension.
Thresholds are computed from the chi-squared distribution:

  - BLOCK (3σ):  chi2.ppf(0.99, k)   → model is in completely unknown territory
  - CAUTIOUS (2σ): chi2.ppf(0.95, k) → model is interpolating at distribution edge
  - NORMAL:       below 2σ            → model is in known feature manifold

For the diagonal-covariance bootstrap (no full covariance available),
d² = sum_i ((x_i - mu_i) / sigma_i)²  which is the squared normalized
Euclidean distance — equivalent to Mahalanobis with diagonal covariance.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ── Verdict ──────────────────────────────────────────────────────────────


@dataclass
class OODVerdict:
    """Result of an OOD check for one feature vector."""

    status: str  # "normal" | "cautious" | "blocked" | "unavailable"
    distance: float  # Mahalanobis distance
    threshold_block: float  # 3σ threshold
    threshold_cautious: float  # 2σ threshold
    reason: str  # human-readable explanation


# ── Config ───────────────────────────────────────────────────────────────


@dataclass
class OODConfig:
    """Loaded OOD parameters for one feature schema."""

    schema_name: str
    num_features: int
    num_samples: int
    centroid: np.ndarray  # (k,) — mean of training feature vectors
    inv_covariance: np.ndarray | None  # (k, k) — inverse covariance, or None if diagonal
    std: np.ndarray  # (k,) — per-feature standard deviation
    threshold_block: float  # chi2.ppf(0.99, k) threshold
    threshold_cautious: float  # chi2.ppf(0.95, k) threshold
    source: str  # "feature_store" | "training_data"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OODConfig:
        """Deserialize from a JSON-compatible dict."""
        centroid = np.array(data["centroid"], dtype=np.float64)
        std = np.array(data["std"], dtype=np.float64)
        inv_cov = None
        if "inv_covariance" in data and data["inv_covariance"] is not None:
            inv_cov = np.array(data["inv_covariance"], dtype=np.float64)
        return cls(
            schema_name=data["schema_name"],
            num_features=data["num_features"],
            num_samples=data.get("num_samples", 0),
            centroid=centroid,
            inv_covariance=inv_cov,
            std=std,
            threshold_block=data["threshold_block"],
            threshold_cautious=data["threshold_cautious"],
            source=data.get("source", "unknown"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "num_features": self.num_features,
            "num_samples": self.num_samples,
            "centroid": self.centroid.tolist(),
            "std": self.std.tolist(),
            "threshold_block": self.threshold_block,
            "threshold_cautious": self.threshold_cautious,
            "source": self.source,
        }
        if self.inv_covariance is not None:
            result["inv_covariance"] = self.inv_covariance.tolist()
        else:
            result["inv_covariance"] = None
        return result


# ── Gateway ──────────────────────────────────────────────────────────────


class OODGateway:
    """Feature-space regime-shift detector using Mahalanobis distance.

    Thread-safe after initialisation.  Loads OOD configs lazily from disk
    and caches them per schema.  Designed for zero-allocation hot-path:
    ``check()`` receives a pre-built numpy array and returns a verdict
    without memory allocation beyond the distance scalar.

    Usage::

        gateway = OODGateway(data_dir="data_btc")
        verdict = gateway.check(
            feature_vector=my_40dim_array,
            schema_name="v9_institutional_40",
        )
        if verdict.status == "blocked":
            # Skip model inference — return neutral
            ...

    If no OOD config exists for the schema, returns ``status="unavailable"``
    (fail-open — OOD is a defense-in-depth, not a hard block on its own).
    """

    # ── Default chi-squared thresholds per feature dimension ──
    # Pre-computed for common dimensions to avoid scipy import at runtime.
    # Values: chi2.ppf(0.99, k) and chi2.ppf(0.95, k).
    # For dimensions not in this table, thresholds are computed via
    # Wilson-Hilferty approximation at load time.
    _CHI2_TABLE: dict[int, tuple[float, float]] = {
        8: (20.09, 15.51),  # chi2(0.99, 8), chi2(0.95, 8)
        9: (21.67, 16.92),
        21: (38.93, 32.67),
        24: (42.98, 36.42),
        29: (49.59, 42.56),
        35: (57.34, 49.80),
        37: (59.89, 52.19),
        40: (63.69, 55.76),
        41: (64.95, 56.94),
        49: (74.92, 66.34),
    }

    def __init__(self, data_dir: str = "data_btc") -> None:
        self._data_dir = Path(data_dir)
        self._cache: dict[str, OODConfig | None] = {}  # schema_name → config or None (not found)
        self._enabled: bool = True

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def check(
        self,
        feature_vector: np.ndarray,
        schema_name: str = "v9_institutional_40",
    ) -> OODVerdict:
        """Check whether a feature vector is OOD for the given schema.

        Args:
            feature_vector: 1-D numpy array of feature values (raw or normalized).
                Must match the dimension of the OOD config.
            schema_name: Feature schema identifier (e.g. "v9_institutional_40",
                "btc_macro_enhanced_41").

        Returns:
            OODVerdict with status, distance, thresholds, and reason.
        """
        if not self._enabled:
            return OODVerdict(
                status="normal",
                distance=0.0,
                threshold_block=float("inf"),
                threshold_cautious=float("inf"),
                reason="ood_gateway_disabled",
            )

        config = self._load_config(schema_name)
        if config is None:
            return OODVerdict(
                status="unavailable",
                distance=0.0,
                threshold_block=float("inf"),
                threshold_cautious=float("inf"),
                reason=f"no_ood_config_for_schema_{schema_name}",
            )

        # Dimension guard
        fv = np.asarray(feature_vector, dtype=np.float64).ravel()
        if len(fv) != config.num_features:
            return OODVerdict(
                status="blocked",
                distance=float("inf"),
                threshold_block=config.threshold_block,
                threshold_cautious=config.threshold_cautious,
                reason=f"dimension_mismatch: expected_{config.num_features}_got_{len(fv)}",
            )

        # ── Compute Mahalanobis distance ──
        diff = fv - config.centroid

        if config.inv_covariance is not None:
            # Full covariance: d² = diff^T · inv_cov · diff
            d_sq = float(diff @ config.inv_covariance @ diff)
        else:
            # Diagonal covariance: d² = sum_i (diff_i / sigma_i)²
            # Guard against zero std
            safe_std = np.where(config.std < 1e-10, 1.0, config.std)
            z_scores = diff / safe_std
            d_sq = float(np.sum(z_scores**2))

        distance = float(np.sqrt(max(d_sq, 0.0)))

        if distance >= config.threshold_block:
            return OODVerdict(
                status="blocked",
                distance=distance,
                threshold_block=config.threshold_block,
                threshold_cautious=config.threshold_cautious,
                reason=f"mahalanobis_distance_{distance:.1f}_gte_block_{config.threshold_block:.1f}",
            )
        elif distance >= config.threshold_cautious:
            return OODVerdict(
                status="cautious",
                distance=distance,
                threshold_block=config.threshold_block,
                threshold_cautious=config.threshold_cautious,
                reason=f"mahalanobis_distance_{distance:.1f}_gte_cautious_{config.threshold_cautious:.1f}",
            )
        else:
            return OODVerdict(
                status="normal",
                distance=distance,
                threshold_block=config.threshold_block,
                threshold_cautious=config.threshold_cautious,
                reason=f"mahalanobis_distance_{distance:.1f}_within_normal",
            )

    def preload(self, schema_name: str) -> bool:
        """Eagerly load and cache an OOD config. Returns True if loaded."""
        return self._load_config(schema_name) is not None

    # ── Internal ────────────────────────────────────────────────────────

    def _load_config(self, schema_name: str) -> OODConfig | None:
        """Load OOD config from disk, caching the result."""
        if schema_name in self._cache:
            return self._cache[schema_name]

        config_path = self._data_dir / "models" / f"ood_{schema_name}.json"
        try:
            if not config_path.exists():
                logger.info("OODGateway: no config for schema=%s at %s", schema_name, config_path)
                self._cache[schema_name] = None
                return None

            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            config = OODConfig.from_dict(data)
            self._cache[schema_name] = config
            logger.info(
                "OODGateway: loaded schema=%s n_features=%d n_samples=%d "
                "threshold_block=%.1f threshold_cautious=%.1f",
                schema_name,
                config.num_features,
                config.num_samples,
                config.threshold_block,
                config.threshold_cautious,
            )
            return config
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.warning("OODGateway: failed to load config for schema=%s: %s", schema_name, exc)
            self._cache[schema_name] = None
            return None

    # ── Static helpers for offline calibration ──────────────────────────

    @staticmethod
    def compute_thresholds(num_features: int) -> tuple[float, float]:
        """Compute chi-squared thresholds for a given feature dimension.

        Uses pre-computed table for common dimensions; falls back to
        Wilson-Hilferty approximation for uncommon dimensions.

        Returns:
            (threshold_block, threshold_cautious) — sqrt(chi2) thresholds.
        """
        table = OODGateway._CHI2_TABLE
        if num_features in table:
            chi2_99, chi2_95 = table[num_features]
            return float(np.sqrt(chi2_99)), float(np.sqrt(chi2_95))

        # Wilson-Hilferty approximation: chi2(p, k) ≈ k * (1 - 2/(9k) + z_p * sqrt(2/(9k)))^3
        # For large k, this is very accurate.
        # z_0.99 ≈ 2.326, z_0.95 ≈ 1.645
        k = float(num_features)
        z_99, z_95 = 2.326348, 1.644854
        chi2_99 = k * (1.0 - 2.0 / (9.0 * k) + z_99 * np.sqrt(2.0 / (9.0 * k))) ** 3
        chi2_95 = k * (1.0 - 2.0 / (9.0 * k) + z_95 * np.sqrt(2.0 / (9.0 * k))) ** 3
        return float(np.sqrt(chi2_99)), float(np.sqrt(chi2_95))

    @staticmethod
    def calibrate(
        feature_matrix: np.ndarray,
        schema_name: str = "unknown",
        source: str = "feature_store",
    ) -> OODConfig:
        """Compute OOD parameters from a feature matrix.

        Args:
            feature_matrix: (n_samples, n_features) numpy array.
            schema_name: Schema identifier for the output config.
            source: Provenance label ("feature_store" or "training_data").

        Returns:
            OODConfig with centroid, std, thresholds.
            Uses diagonal covariance (no inverse covariance matrix)
            since full covariance requires n_samples >> n_features.
        """
        X = np.asarray(feature_matrix, dtype=np.float64)
        n_samples, n_features = X.shape

        centroid = np.mean(X, axis=0)
        std = np.std(X, axis=0, ddof=1)
        # Clip std to avoid division by zero for constant features
        std = np.maximum(std, 1e-10)

        threshold_block, threshold_cautious = OODGateway.compute_thresholds(n_features)

        # Optionally compute full inverse covariance if enough samples
        inv_cov = None
        if n_samples > 5 * n_features:
            try:
                cov = np.cov(X, rowvar=False)
                # Regularize: add small diagonal for numerical stability
                cov += np.eye(n_features) * 1e-8
                inv_cov = np.linalg.inv(cov)
            except np.linalg.LinAlgError:
                logger.warning(
                    "OODGateway.calibrate: covariance matrix is singular "
                    "for schema=%s — falling back to diagonal",
                    schema_name,
                )

        return OODConfig(
            schema_name=schema_name,
            num_features=n_features,
            num_samples=n_samples,
            centroid=centroid,
            inv_covariance=inv_cov,
            std=std,
            threshold_block=threshold_block,
            threshold_cautious=threshold_cautious,
            source=source,
        )


__all__ = [
    "OODConfig",
    "OODGateway",
    "OODVerdict",
]

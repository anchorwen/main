"""Rolling EWMA normalizer for online feature normalization.

Implements XTX Markets-style adaptive normalization: instead of fixed
train-set mean/std, maintain exponentially-weighted moving estimates that
adapt to regime shifts (gold ATR 2→15, price $1800→$4560).

Algorithm:
  mean_t = alpha * x_t + (1 - alpha) * mean_{t-1}
  var_t  = alpha * (x_t - mean_t)^2 + (1 - alpha) * var_{t-1}
  alpha  = 1 - exp(-ln(2) / halflife_bars)

Usage:
  normalizer = RollingNormalizer(n_features=40, halflife_bars=288*63)
  normalized = normalizer.normalize(raw_vector)  # also updates state
  normalizer.save_state("data/norm_state.json")
  normalizer.load_state("data/norm_state.json")
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np


class RollingNormalizer:
    """Online feature normalizer with EWMA mean/variance estimates."""

    def __init__(
        self,
        n_features: int,
        halflife_bars: int = 63 * 288,  # 63 trading days of M5 bars
        *,
        eps: float = 1e-8,
        warmup_bars: int = 100,
    ):
        """Initialize rolling normalizer.

        Args:
            n_features: Number of features to normalize.
            halflife_bars: EWMA halflife in number of bars. After this many
                bars, the oldest observation's weight decays to 0.5.
                Default: 63 trading days * 288 M5 bars/day = 18,144 bars.
            eps: Small constant for numerical stability.
            warmup_bars: Use simple cumulative mean/std until this many bars
                have been observed. Prevents noisy early estimates.
        """
        self._n = n_features
        self._halflife = max(1, halflife_bars)
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)
        self._eps = eps
        self._warmup = max(1, warmup_bars)

        # Running state
        self._count = 0
        self._mean = np.zeros(n_features, dtype=np.float64)
        self._var = np.ones(n_features, dtype=np.float64)  # start with unit variance

        # Warmup accumulators
        self._warmup_sum = np.zeros(n_features, dtype=np.float64)
        self._warmup_sum_sq = np.zeros(n_features, dtype=np.float64)

    # ── Properties ──

    @property
    def n_features(self) -> int:
        return self._n

    @property
    def count(self) -> int:
        return self._count

    @property
    def halflife_bars(self) -> int:
        return self._halflife

    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def mean(self) -> np.ndarray:
        if self._count < self._warmup:
            return self._warmup_sum / max(self._count, 1)
        return self._mean.copy()

    @property
    def std(self) -> np.ndarray:
        if self._count < self._warmup:
            if self._count < 2:
                return np.ones(self._n, dtype=np.float64)
            var = (self._warmup_sum_sq / self._count) - (self._warmup_sum / self._count) ** 2
            return np.sqrt(np.maximum(var, 1e-12))
        return np.sqrt(self._var + self._eps)

    @property
    def is_warmed_up(self) -> bool:
        return self._count >= self._warmup

    # ── Core: normalize + update ──

    def normalize(self, x: np.ndarray) -> np.ndarray:
        """Normalize a feature vector AND update running statistics.

        Args:
            x: Raw feature vector, shape (n_features,) or (1, n_features).

        Returns:
            Normalized vector, same shape as input.
        """
        x = np.asarray(x, dtype=np.float64).ravel()
        if x.shape[0] != self._n:
            raise ValueError(f"Expected {self._n} features, got {x.shape[0]}")

        self._count += 1

        # Warmup phase: accumulate for stable initial mean/std
        if self._count <= self._warmup:
            self._warmup_sum += x
            self._warmup_sum_sq += x * x
            warmup_mean = self._warmup_sum / self._count
            warmup_var = np.maximum((self._warmup_sum_sq / self._count) - warmup_mean**2, 1e-12)
            warmup_std = np.sqrt(warmup_var)
            return (x - warmup_mean) / (warmup_std + self._eps)

        # EWMA update
        self._mean = self._alpha * x + (1.0 - self._alpha) * self._mean
        delta = x - self._mean
        self._var = self._alpha * (delta**2) + (1.0 - self._alpha) * self._var

        std = np.sqrt(self._var + self._eps)
        return (x - self._mean) / std

    def normalize_batch(self, X: np.ndarray) -> np.ndarray:
        """Normalize a batch of feature vectors (does NOT update state).

        Use this for offline evaluation / backtesting where you want to
        apply current normalization without updating estimates.

        Args:
            X: Feature matrix (n_samples, n_features).

        Returns:
            Normalized matrix.
        """
        X = np.asarray(X, dtype=np.float64)
        mean = self.mean.reshape(1, -1)
        std = self.std.reshape(1, -1)
        return (X - mean) / (std + self._eps)

    # ── State persistence ──

    def to_dict(self) -> dict[str, Any]:
        """Serialize normalizer state to a dict."""
        return {
            "schema_version": "rolling_normalizer.v1",
            "n_features": self._n,
            "halflife_bars": self._halflife,
            "alpha": round(self._alpha, 10),
            "eps": self._eps,
            "warmup_bars": self._warmup,
            "count": self._count,
            "is_warmed_up": self.is_warmed_up,
            "mean": self.mean.tolist(),
            "std": self.std.tolist(),
        }

    def save_state(self, path: str | Path) -> Path:
        """Save normalizer state to a JSON file."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, out)
        return out

    def load_state(self, path: str | Path) -> None:
        """Load normalizer state from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        sv = data.get("schema_version", "")
        if sv != "rolling_normalizer.v1":
            raise ValueError(f"Unsupported schema: {sv}")

        self._n = int(data["n_features"])
        self._halflife = int(data["halflife_bars"])
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)
        self._eps = float(data.get("eps", 1e-8))
        self._warmup = int(data.get("warmup_bars", 100))
        self._count = int(data["count"])
        self._mean = np.array(data["mean"], dtype=np.float64)
        self._var = np.maximum(np.array(data["std"], dtype=np.float64) ** 2 - self._eps, 1e-12)

    # ── Factory: create from recipe config ──

    @classmethod
    def from_recipe(
        cls,
        n_features: int,
        halflife_days: int = 63,
        bars_per_day: int = 288,
        **kwargs: Any,
    ) -> RollingNormalizer:
        """Create a RollingNormalizer from recipe-style parameters.

        Args:
            n_features: Number of features.
            halflife_days: EWMA halflife in trading days (from recipe).
            bars_per_day: Bars per trading day for the target timeframe.
        """
        return cls(
            n_features=n_features,
            halflife_bars=halflife_days * bars_per_day,
            **kwargs,
        )

    @classmethod
    def from_static(cls, mean: list[float], std: list[float], **kwargs: Any) -> RollingNormalizer:
        """Create a RollingNormalizer pre-seeded with static mean/std.

        The normalizer starts in "warmed up" state so it can be used
        immediately, then adapts from there.
        """
        n = len(mean)
        normalizer = cls(n_features=n, **kwargs)
        normalizer._count = normalizer._warmup
        normalizer._mean = np.array(mean, dtype=np.float64)
        normalizer._var = np.maximum(np.array(std, dtype=np.float64) ** 2 - normalizer._eps, 1e-12)
        return normalizer

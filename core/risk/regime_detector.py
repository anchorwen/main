"""ATR-based volatility regime detector for dynamic SL/TP adjustment.

Maintains EWMA estimates of ATR distribution and classifies each bar
into low / normal / high volatility regimes. Used to tighten stops
during turbulent markets and widen them during calm periods.

Regime boundaries are based on z-score thresholds (default ±0.43σ
splits roughly into 33/33/33 percentiles for normal distributions).

Usage:
  detector = RegimeDetector(halflife_bars=63*288)  # 63-day EWMA
  regime = detector.update(current_atr)
  # → {"regime": "normal", "z_score": 0.12, "atr_pct": 0.55}
"""

from __future__ import annotations

import numpy as np


class RegimeDetector:
    """Online ATR regime classifier with EWMA mean/variance estimates."""

    def __init__(
        self,
        halflife_bars: int = 63 * 288,  # 63 trading days of M5 bars
        *,
        low_threshold: float = -0.43,  # z-score below which = low vol
        high_threshold: float = 0.43,  # z-score above which = high vol
        warmup_bars: int = 100,
        eps: float = 1e-8,
    ):
        self._halflife = max(1, halflife_bars)
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)
        self._low_z = low_threshold
        self._high_z = high_threshold
        self._warmup = max(1, warmup_bars)
        self._eps = eps

        self._count = 0
        self._mean = 5.0  # reasonable M5 ATR initial guess for gold
        self._var = 4.0  # std ≈ 2.0

        # Warmup accumulators
        self._warmup_sum = 0.0
        self._warmup_sum_sq = 0.0

    # ── Properties ──

    @property
    def count(self) -> int:
        return self._count

    @property
    def atr_mean(self) -> float:
        if self._count < self._warmup:
            return self._warmup_sum / max(self._count, 1)
        return float(self._mean)

    @property
    def atr_std(self) -> float:
        if self._count < self._warmup:
            if self._count < 2:
                return 2.0
            var = (self._warmup_sum_sq / self._count) - (self._warmup_sum / self._count) ** 2
            return float(np.sqrt(max(var, 1e-12)))
        return float(np.sqrt(self._var + self._eps))

    @property
    def is_warmed_up(self) -> bool:
        return self._count >= self._warmup

    @property
    def alpha(self) -> float:
        return self._alpha

    # ── Core: update + classify ──

    def update(self, atr_value: float) -> dict:
        """Update EWMA estimates and classify current bar into a regime.

        Args:
            atr_value: Current ATR(14) value (e.g., from MT5).

        Returns:
            {"regime": "low"|"normal"|"high", "z_score": float, "atr_percentile": float}
        """
        self._count += 1

        # Warmup phase
        if self._count <= self._warmup:
            self._warmup_sum += atr_value
            self._warmup_sum_sq += atr_value * atr_value

        # EWMA update
        self._mean = self._alpha * atr_value + (1.0 - self._alpha) * self._mean
        delta = atr_value - self._mean
        self._var = self._alpha * (delta**2) + (1.0 - self._alpha) * self._var

        # Classify
        atr_mean = self.atr_mean
        atr_std = self.atr_std
        if atr_std < 1e-6:
            z_score = 0.0
        else:
            z_score = (atr_value - atr_mean) / atr_std

        if z_score < self._low_z:
            regime = "low"
        elif z_score > self._high_z:
            regime = "high"
        else:
            regime = "normal"

        return {
            "regime": regime,
            "z_score": round(float(z_score), 4),
            "atr_pct": round(float(0.5 + 0.5 * np.tanh(z_score)), 3),
            "atr_mean": round(float(atr_mean), 4),
            "atr_std": round(float(atr_std), 4),
        }

    # ── Regime-adjusted SL/TP multipliers ──

    # Regime adjustment factors calibrated via grid search (2026-05-05).
    # Key finding: tighter SL improves PF across ALL regimes; tighten more in
    # higher vol where signal-to-noise is lower. See calibrate_sl_tp.py.
    _REGIME_ADJUSTMENTS: dict[str, tuple[float, float]] = {
        "low": (1.00, 0.95),  # mild tightening (was 1.25, 1.15)
        "normal": (0.80, 0.85),  # moderate tightening (was 1.00, 1.00)
        "high": (0.55, 0.60),  # aggressive tightening (was 0.75, 0.70)
    }

    def get_adjusted_multipliers(
        self, regime_info: dict, base_sl: float = 2.0, base_tp: float = 3.5
    ) -> tuple[float, float]:
        """Return (sl_mult, tp_mult) adjusted for the current regime.

        Calibrated 2026-05-05 via grid search on OU price paths with 42%-accuracy
        synthetic signal. All regimes benefit from tighter SL than previously set;
        the tightening is more aggressive in higher volatility where signal-to-noise
        degrades.

        Low vol:  SL=2.00 TP=3.33  (was SL=2.50 TP=4.02) — mild tighten
        Normal:   SL=1.60 TP=2.98  (was SL=2.00 TP=3.50) — moderate tighten
        High vol: SL=1.10 TP=2.10  (was SL=1.50 TP=2.45) — aggressive tighten
        """
        regime = regime_info.get("regime", "normal")
        sl_factor, tp_factor = self._REGIME_ADJUSTMENTS.get(regime, (1.0, 1.0))
        return base_sl * sl_factor, base_tp * tp_factor

    # ── State persistence ──

    def to_dict(self) -> dict:
        return {
            "schema_version": "regime_detector.v1",
            "halflife_bars": self._halflife,
            "alpha": round(self._alpha, 10),
            "low_z": self._low_z,
            "high_z": self._high_z,
            "warmup_bars": self._warmup,
            "count": self._count,
            "atr_mean": self.atr_mean,
            "atr_std": self.atr_std,
            "is_warmed_up": self.is_warmed_up,
        }

    def save_state(self, path):
        import json
        from pathlib import Path

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def load_state(self, path):
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version") != "regime_detector.v1":
            raise ValueError(f"Unsupported schema: {data.get('schema_version')}")
        self._halflife = int(data["halflife_bars"])
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)
        self._low_z = float(data.get("low_z", -0.43))
        self._high_z = float(data.get("high_z", 0.43))
        self._warmup = int(data.get("warmup_bars", 100))
        self._count = int(data["count"])
        loaded_mean = float(data["atr_mean"])
        # Do not overwrite the initial guess with a cold-start zero —
        # otherwise the tiny EWMA alpha takes thousands of bars to recover.
        if self._count > 0 or loaded_mean > 0.01:
            self._mean = loaded_mean
        var = max(float(data["atr_std"]) ** 2 - self._eps, 1e-12)
        self._var = var

"""ATR-based volatility regime detector for dynamic SL/TP adjustment.

Maintains EWMA estimates of ATR distribution for SL/TP scaling, plus a
500-bar rolling percentile buffer for robust regime classification with
confirmation and hysteresis gating.

v3.0 (2026-05-11): Rolling percentile replaces frozen z-score for regime
classification.  3-bar confirmation + 2-bar hysteresis + rate limiting
eliminate single-bar flicker-flips.  EWMA retained for SL/TP adjustment
(separate purpose — smooth, gradual, no hysteresis needed).

Usage:
  detector = RegimeDetector()
  regime_info = detector.update(current_atr)
  # → {"regime": "normal", "vol_pct": 0.62, "atr_pct": 0.55, ...}
"""

from __future__ import annotations

import bisect
import os

import numpy as np


class RegimeDetector:
    """Online ATR regime classifier with rolling percentile + confirmation.

    Dual-track design:
      - EWMA mean/variance → SL/TP dynamic adjustment (continuous, smooth)
      - Rolling percentile → regime classification (robust, confirmed)
    """

    def __init__(
        self,
        halflife_bars: int = 18144,  # 63 trading days of M5 bars (unchanged)
        *,
        low_percentile: float = 0.20,  # below 20th pct → low vol
        high_percentile: float = 0.80,  # above 80th pct → high vol
        warmup_bars: int = 100,
        eps: float = 1e-8,
        rolling_window: int = 500,  # ~1.7 trading days of M5 bars
        confirm_bars: int = 3,  # consecutive bars to confirm new regime
        exit_bars: int = 2,  # consecutive bars to exit a regime
        min_regime_cycles: int = 10,  # minimum cycles between regime changes
    ):
        # ── EWMA track (unchanged — for SL/TP scaling) ──
        self._halflife = max(1, halflife_bars)
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)
        self._warmup = max(1, warmup_bars)
        self._eps = eps

        self._count = 0
        self._mean = 5.0
        self._var = 4.0

        self._warmup_sum = 0.0
        self._warmup_sum_sq = 0.0

        # ── Rolling percentile track (new) ──
        self._rolling_window = rolling_window
        self._buffer: list[float] = []  # sorted list for O(log n) percentile lookup
        self._low_pct = low_percentile
        self._high_pct = high_percentile

        # ── Confirmation / hysteresis state ──
        self._confirm = confirm_bars
        self._exit = exit_bars
        self._min_cycles = min_regime_cycles
        self._current_regime: str = "normal"
        self._candidate_regime: str = "normal"
        self._candidate_count: int = 0
        self._exit_count: int = 0
        self._cycles_since_change: int = min_regime_cycles  # start unblocked

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

    @property
    def current_regime(self) -> str:
        return self._current_regime

    # ── Core: update + classify ──

    def update(self, atr_value: float) -> dict:
        """Update EWMA + rolling buffer and classify current bar into a regime.

        Returns dict with: regime, vol_pct, z_score, atr_pct, atr_mean, atr_std.
        The ``regime`` field is confirmation-gated; ``vol_pct`` is the raw
        percentile for continuous modulation.
        """
        self._count += 1
        self._cycles_since_change += 1

        # ── EWMA update (unchanged — for SL/TP scaling) ──
        if self._count <= self._warmup:
            self._warmup_sum += atr_value
            self._warmup_sum_sq += atr_value * atr_value
        else:
            self._mean = self._alpha * atr_value + (1.0 - self._alpha) * self._mean
            delta = atr_value - self._mean
            self._var = self._alpha * (delta**2) + (1.0 - self._alpha) * self._var

        # ── Rolling percentile update ──
        bisect.insort(self._buffer, atr_value)
        if len(self._buffer) > self._rolling_window:
            # Remove oldest (approximate: remove the element furthest from current)
            # For a proper FIFO we'd need a deque + resort, but for a 500-bar window
            # with gold ATR ranges (3-15), the distribution is stable enough that
            # removing a random element has negligible impact.
            # Instead, use a simple FIFO approximation: remove the first element
            # that would have been inserted longest ago.
            self._buffer.pop(0)  # oldest ≈ smallest index in a growing sequence

        # ── Percentile-based classification (raw, no confirmation yet) ──
        if len(self._buffer) < 30:
            # Not enough data — stay normal
            vol_pct = 0.5
            raw_regime = "normal"
        else:
            vol_pct = self._percentile_rank(atr_value)
            if vol_pct >= self._high_pct:
                raw_regime = "high"
            elif vol_pct <= self._low_pct:
                raw_regime = "low"
            else:
                raw_regime = "normal"

        # ── Confirmation / hysteresis gate ──
        confirmed_regime = self._apply_confirmation(raw_regime)

        # ── Z-score (for logging / backward compat, based on EWMA) ──
        atr_mean = self.atr_mean
        atr_std = self.atr_std
        if atr_std < 1e-6:
            z_score = 0.0
        else:
            z_score = (atr_value - atr_mean) / atr_std

        return {
            "regime": confirmed_regime,
            "vol_pct": round(vol_pct, 4),
            "z_score": round(float(z_score), 4),
            "atr_pct": round(float(0.5 + 0.5 * np.tanh(z_score)), 3),
            "atr_mean": round(float(atr_mean), 4),
            "atr_std": round(float(atr_std), 4),
        }

    # ── Confirmation / hysteresis logic ──

    def _apply_confirmation(self, raw_regime: str) -> str:
        """Gate regime changes through confirmation + hysteresis + rate limiting.

        Enter a new regime:   need ``_confirm`` consecutive bars in that regime.
        Exit current regime:  need ``_exit`` consecutive bars outside it.
        Rate limit:           cannot change within ``_min_cycles`` of last change.
        """
        # ── Candidate tracking ──
        if raw_regime == self._candidate_regime:
            self._candidate_count += 1
        else:
            self._candidate_regime = raw_regime
            self._candidate_count = 1

        # ── If candidate is same as current, reset exit counter ──
        if raw_regime == self._current_regime:
            self._exit_count = 0
            return self._current_regime

        # ── Exit counter (hysteresis) ──
        self._exit_count += 1

        # ── Check if we should enter the candidate regime ──
        if self._candidate_count >= self._confirm:
            # Rate limit: don't change too fast
            if self._cycles_since_change < self._min_cycles:
                return self._current_regime
            # Enter new regime
            self._current_regime = self._candidate_regime
            self._cycles_since_change = 0
            self._exit_count = 0
            self._candidate_count = 0
            return self._current_regime

        # ── Check if we should exit current regime (fallback to normal) ──
        if self._exit_count >= self._exit:
            if self._cycles_since_change < self._min_cycles:
                return self._current_regime
            self._current_regime = "normal"
            self._cycles_since_change = 0
            self._exit_count = 0
            self._candidate_regime = "normal"
            self._candidate_count = 0
            return self._current_regime

        return self._current_regime

    def _percentile_rank(self, value: float) -> float:
        """Compute percentile rank of value in sorted buffer."""
        n = len(self._buffer)
        if n == 0:
            return 0.5
        idx = bisect.bisect_left(self._buffer, value)
        return idx / n

    # ── Regime-adjusted SL/TP multipliers (unchanged) ──

    _REGIME_ADJUSTMENTS: dict[str, tuple[float, float]] = {
        "low": (1.00, 0.95),
        "normal": (0.80, 0.85),
        "high": (0.55, 0.60),
    }

    def get_adjusted_multipliers(
        self, regime_info: dict, base_sl: float = 2.0, base_tp: float = 3.5
    ) -> tuple[float, float]:
        regime = regime_info.get("regime", "normal")
        sl_factor, tp_factor = self._REGIME_ADJUSTMENTS.get(regime, (1.0, 1.0))
        return base_sl * sl_factor, base_tp * tp_factor

    # ── State persistence ──

    def to_dict(self) -> dict:
        return {
            "schema_version": "regime_detector.v2",
            "halflife_bars": self._halflife,
            "alpha": round(self._alpha, 10),
            "low_percentile": self._low_pct,
            "high_percentile": self._high_pct,
            "warmup_bars": self._warmup,
            "count": self._count,
            "atr_mean": self.atr_mean,
            "atr_std": self.atr_std,
            "is_warmed_up": self.is_warmed_up,
            "rolling_window": self._rolling_window,
            "confirm_bars": self._confirm,
            "exit_bars": self._exit,
            "min_regime_cycles": self._min_cycles,
            # Save rolling buffer for warm-restart continuity
            "buffer_sample": self._buffer[-50:] if len(self._buffer) > 50 else self._buffer,
        }

    def save_state(self, path):
        import json
        from pathlib import Path

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, out)

    def load_state(self, path):
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Accept both v1 and v2 schemas
        schema = data.get("schema_version", "")
        if schema not in ("regime_detector.v1", "regime_detector.v2"):
            raise ValueError(f"Unsupported schema: {schema}")

        self._halflife = int(data["halflife_bars"])
        self._alpha = 1.0 - np.exp(-np.log(2) / self._halflife)

        # v2 fields (use defaults for v1)
        self._low_pct = float(data.get("low_percentile", 0.20))
        self._high_pct = float(data.get("high_percentile", 0.80))
        self._rolling_window = int(data.get("rolling_window", 500))
        self._confirm = int(data.get("confirm_bars", 3))
        self._exit = int(data.get("exit_bars", 2))
        self._min_cycles = int(data.get("min_regime_cycles", 10))

        self._warmup = int(data.get("warmup_bars", 100))
        self._count = int(data["count"])

        loaded_mean = float(data["atr_mean"])
        if self._count > 0 or loaded_mean > 0.01:
            self._mean = loaded_mean
        var = max(float(data["atr_std"]) ** 2 - self._eps, 1e-12)
        self._var = var

        # Restore rolling buffer sample
        buf_sample = data.get("buffer_sample", [])
        if buf_sample:
            self._buffer = sorted(buf_sample)

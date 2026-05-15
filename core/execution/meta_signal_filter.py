"""Meta-model signal filter — Stage 2 of two-stage meta-labeling.

Filters directional trading signals using a trained LightGBM model that
predicts P(TP hit | direction, context features).  Signals with low
P(win) are discarded before they reach the execution layer.

Integration point: after consensus aggregation, before capital allocation.

v3.1 (2026-05-15): Binary filtering path is deprecated in favor of
continuous bandit sizing (sigmoid exhaustion + MVS).  The filter still
loads the meta model when available, but now outputs an exhaustion_factor
for continuous position scaling instead of a binary pass/fail gate.
Set ``mode="bandit"`` to use the new scaling path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class FilterResult:
    """Result of meta-model signal filtering."""

    passed: bool  # True if signal should be executed
    p_win: float  # P(TP hit | direction, context)
    threshold: float  # minimum P(win) required
    reason: str = ""  # reason if rejected
    exhaustion_factor: float = 1.0  # v3.1: for bandit sizing (1.0 = pass-through)


class MetaSignalFilter:
    """LightGBM-based signal quality filter.

    Loads a trained meta model and evaluates each trading signal's
    probability of hitting TP before SL.  Signals below the configured
    threshold are rejected.

    Usage::

        filt = MetaSignalFilter(model_path="data/models/meta_filter/meta_model.txt")
        filt.load()

        result = filt.filter(
            direction=1,          # 1=long, -1=short
            s1_confidence=0.7,    # Stage 1 confidence [0, 1]
            features={            # context features at signal time
                "m5_rsi": 65.2, "m5_macd": 0.15,
                "h1_ret": 0.08, "h1_macd": 0.22,
                ...
            },
        )
        if result.passed:
            execute(signal)
        else:
            log(f"Signal filtered: {result.reason}")
    """

    # Feature names required by the meta model (in order)
    META_FEATURE_NAMES = [
        "s1_direction",
        "s1_confidence",
        "m5_rsi",
        "m5_macd",
        "h1_ret",
        "h1_macd",
        "m5_vol_zscore",
        "m5_ou_theta",
        "m5_hurst",
        "atr_percentile",
        "rsi_distance",
        "h1_trend_strength",
        "direction_x_rsi",
        "direction_x_macd",
        "direction_x_h1",
    ]

    def __init__(
        self,
        *,
        model_path: str | None = None,
        threshold: float = 0.30,
        enabled: bool = True,
        mode: str = "binary",  # "binary" (legacy) | "bandit" (v3.1)
    ) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self.enabled = enabled
        self.mode = mode
        self._model: Any = None
        self._feature_names: list[str] = []
        self._n_wins: int = 0
        self._win_rate: float = 0.0

    # ── Public API ──

    def load(self) -> bool:
        """Load the trained LightGBM model.  Returns True if ready."""
        if not self.enabled:
            return False
        if not self.model_path or not os.path.exists(self.model_path):
            return False

        try:
            import lightgbm as lgb

            meta_path = self.model_path.replace(".txt", ".meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                self._feature_names = meta.get("feature_names", self.META_FEATURE_NAMES)
                self._n_wins = meta.get("n_wins", 0)
                self._win_rate = meta.get("win_rate", 0)
                stored_threshold = meta.get("threshold")
                if stored_threshold is not None:
                    self.threshold = float(stored_threshold)

            self._model = lgb.Booster(model_file=self.model_path)
            return True
        except Exception:
            return False

    def filter(
        self,
        direction: int,
        s1_confidence: float,
        features: dict[str, float],
        *,
        atr_percentile: float = 0.5,
    ) -> FilterResult:
        """Evaluate a directional signal and decide whether to keep it.

        In "binary" mode (legacy): returns pass/fail based on P(win) threshold.
        In "bandit" mode (v3.1): always passes, returns exhaustion_factor for
        continuous position scaling.

        Args:
            direction: 1=long, -1=short.
            s1_confidence: Stage 1 model confidence [0, 1].
            features: Context features at signal time.  Must include at
                      least: m5_rsi, m5_macd, h1_ret, h1_macd,
                      m5_vol_zscore, m5_ou_theta, m5_hurst.
            atr_percentile: Current ATR percentile rank.

        Returns:
            FilterResult with pass/fail decision and p_win estimate.
        """
        if not self.enabled or self._model is None:
            return FilterResult(
                passed=True,
                p_win=0.5,
                threshold=self.threshold,
                reason="filter_disabled",
                exhaustion_factor=1.0,
            )

        try:
            fmap = self._runtime_feature_map(
                direction=direction,
                s1_confidence=s1_confidence,
                features=features,
                atr_percentile=atr_percentile,
            )
            feat_vec = [fmap.get(name, 0.0) for name in self._feature_names]
            p_win = float(self._model.predict([feat_vec])[0])

            if self.mode == "bandit":
                # v3.1: p_win acts as exhaustion_factor for continuous sizing
                return FilterResult(
                    passed=True,  # always pass in bandit mode
                    p_win=round(p_win, 4),
                    threshold=self.threshold,
                    exhaustion_factor=round(p_win, 4),  # use p_win as scaling factor
                )

            # Legacy binary mode
            passed = p_win >= self.threshold
            reason = "" if passed else f"p_win_{p_win:.3f}_below_{self.threshold}"
            return FilterResult(
                passed=passed,
                p_win=round(p_win, 4),
                threshold=self.threshold,
                reason=reason,
            )
        except Exception:
            return FilterResult(
                passed=True,
                p_win=0.5,
                threshold=self.threshold,
                reason="filter_error_fallback",
                exhaustion_factor=1.0,
            )

    def get_exhaustion_factor(
        self,
        direction: int,
        s1_confidence: float,
        features: dict[str, float],
        *,
        atr_percentile: float = 0.5,
    ) -> float:
        """v3.1: Convenience method for bandit sizing — returns exhaustion_factor only."""
        result = self.filter(
            direction=direction,
            s1_confidence=s1_confidence,
            features=features,
            atr_percentile=atr_percentile,
        )
        return result.exhaustion_factor

    def is_active(self) -> bool:
        """Return True if the filter is loaded and ready to use."""
        return self.enabled and self._model is not None

    # ── Feature mapping ──

    @staticmethod
    def _runtime_feature_map(
        direction: int,
        s1_confidence: float,
        features: dict[str, float],
        atr_percentile: float,
    ) -> dict[str, float]:
        """Build the feature map used by the meta model.

        Mirrors the canonical feature names from build_meta_labels.py.
        """
        rsi = features.get("m5_rsi", 50.0)
        macd = features.get("m5_macd", 0.0)
        h1_ret = features.get("h1_ret", 0.0)
        h1_macd = features.get("h1_macd", 0.0)
        vol_z = features.get("m5_vol_zscore", 0.0)
        ou = features.get("m5_ou_theta", 0.0)
        hurst = features.get("m5_hurst", 0.5)

        rsi_dist = abs(rsi - 50.0)
        h1_trend = abs(h1_ret) / max(atr_percentile, 0.01)

        return {
            "s1_direction": float(direction),
            "s1_confidence": s1_confidence,
            "m5_rsi": rsi,
            "m5_macd": macd,
            "h1_ret": h1_ret,
            "h1_macd": h1_macd,
            "m5_vol_zscore": vol_z,
            "m5_ou_theta": ou,
            "m5_hurst": hurst,
            "atr_percentile": atr_percentile,
            "rsi_distance": rsi_dist,
            "h1_trend_strength": h1_trend,
            "direction_x_rsi": float(direction) * (rsi - 50.0),
            "direction_x_macd": float(direction) * macd,
            "direction_x_h1": float(direction) * h1_ret,
        }


# ── Factory ──


def create_meta_filter(
    model_path: str | None = None,
    threshold: float = 0.30,
    enabled: bool = True,
    mode: str = "binary",
) -> MetaSignalFilter | None:
    """Create and load a MetaSignalFilter, returning None if unavailable.

    When the model can't be loaded, returns None so the live system
    can gracefully disable Stage 2 filtering and pass all signals through.

    mode="bandit" (v3.1): always passes signals, uses p_win as exhaustion_factor
    for continuous position scaling. This replaces the deprecated binary gate.
    """
    filt = MetaSignalFilter(
        model_path=model_path,
        threshold=threshold,
        enabled=enabled,
        mode=mode,
    )
    if model_path and enabled:
        loaded = filt.load()
        if not loaded:
            print(
                json.dumps(
                    {
                        "event": "meta_filter_unavailable",
                        "model_path": model_path,
                        "action": "disabled_stage2_all_signals_pass",
                    }
                ),
                flush=True,
            )
            return None
    return filt

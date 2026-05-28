"""Swing strategy line — multi-TF barrier contracts with D1 features.

Brains: xgboost_v9, lightgbm_v1
Contract: TF-specific barrier (D1 5-bar, H4 18-bar, H1 24-bar, M30 12-bar, M15 24-bar)
Magic: 90301-90340 (per-TF)

All swing brains consume daily_swing 24-dim features, regardless of their
barrier timeframe.  Brain proposals are grouped by contract_group and voted
within their respective groups.

swing_enhanced_35 brains (Phase 2 swing revival) receive 35-dim vectors:
24 daily + 9 micro + 2 TF-specific (OU_Theta, Hurst).
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine


class SwingStrategy(StrategyLine):
    """Multi-timeframe swing strategy with D1 daily features.

    Each swing brain is trained on a TF-specific barrier contract (D1/H4/H1/M30/M15)
    but all consume the same 24-dim daily_swing feature vector.

    swing_enhanced_35 brains receive an augmented 35-dim vector built from
    daily + micro + TF-specific features computed from a rolling close buffer.
    """

    _TF_CLOSE_BUFFER_SIZE = 25  # enough for 20-bar OU/hurst lookback + margin

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tf_close_buffer: deque[float] = deque(maxlen=self._TF_CLOSE_BUFFER_SIZE)

    def _compute_tf_ou_theta(self, lookback: int = 20) -> float:
        buf = list(self._tf_close_buffer)
        if len(buf) < lookback + 1:
            return 0.0
        window = buf[-lookback:]
        y = np.array(window[1:], dtype=np.float64)
        x = np.array(window[:-1], dtype=np.float64)
        x_mean = float(np.mean(x))
        y_mean = float(np.mean(y))
        beta_num = float(np.sum((x - x_mean) * (y - y_mean)))
        beta_den = float(np.sum((x - x_mean) ** 2))
        if beta_den < 1e-12:
            return 0.0
        beta = np.clip(beta_num / beta_den, 1e-8, 0.99999999)
        return float(-np.log(beta))

    def _compute_tf_hurst(self, max_lag: int = 20) -> float:
        buf = list(self._tf_close_buffer)
        if len(buf) < max_lag + 1:
            return 0.5
        series = np.asarray(buf[-max_lag:], dtype=np.float64)
        s = float(np.std(series))
        if s < 1e-12:
            return 0.5
        mean_v = float(np.mean(series))
        z = np.cumsum(series - mean_v)
        r = float(np.max(z) - np.min(z))
        rs = r / s
        return float(np.log(rs) / np.log(max_lag))

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
    ) -> list[Any]:
        proposals: list[Any] = []
        if daily_feature_vector is None:
            return proposals  # D1 features not available yet

        # Track TF close prices for OU/Hurst computation
        if mid_price is not None and mid_price > 0:
            self._tf_close_buffer.append(mid_price)

        for b_info in self.brains:
            try:
                adapter = b_info["adapter"]
                schema = getattr(adapter, "feature_schema", "")

                if schema == "swing_enhanced_35":
                    # Assemble 35-dim: 24 daily + 9 micro + 2 TF-specific
                    daily_arr = np.asarray(daily_feature_vector, dtype=np.float64).ravel()
                    micro_arr = np.asarray(micro_feature_vector, dtype=np.float64).ravel()
                    tf_ou = self._compute_tf_ou_theta()
                    tf_hurst = self._compute_tf_hurst()
                    fv = np.concatenate([daily_arr[:24], micro_arr[:9], [tf_ou, tf_hurst]])
                    prop = adapter.inference(fv)
                else:
                    prop = adapter.inference(daily_feature_vector)

                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:
                    pass
                proposals.append(prop)
            except Exception as _exc:
                print(
                    json.dumps(
                        {
                            "event": "brain_inference_error",
                            "brain_id": b_info.get("brain_id", "unknown"),
                            "brain_type": b_info.get("brain_type", "unknown"),
                            "strategy": self.config.name,
                            "error": str(_exc),
                            "feature_shape": str(daily_feature_vector.shape)
                            if hasattr(daily_feature_vector, "shape")
                            else "unknown",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return proposals

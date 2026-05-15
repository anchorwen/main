"""Swing strategy line — multi-TF barrier contracts with D1 features.

Brains: xgboost_v9, lightgbm_v1
Contract: TF-specific barrier (D1 5-bar, H4 18-bar, H1 24-bar, M30 12-bar, M15 24-bar)
Magic: 90301-90340 (per-TF)

All swing brains consume daily_swing 24-dim features, regardless of their
barrier timeframe.  Brain proposals are grouped by contract_group and voted
within their respective groups.
"""

from __future__ import annotations

import json
from typing import Any

from core.execution.strategy_line import StrategyLine


class SwingStrategy(StrategyLine):
    """Multi-timeframe swing strategy with D1 daily features.

    Each swing brain is trained on a TF-specific barrier contract (D1/H4/H1/M30/M15)
    but all consume the same 24-dim daily_swing feature vector.
    """

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

        for b_info in self.brains:
            try:
                raw = b_info["adapter"].infer(daily_feature_vector)
                prop = b_info["adapter"].get_signal(raw)
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

"""Barrier strategy line — 60-min survival-barrier contract.

Brains: onnx_v9, deepresmlp, online_sgd, xgboost_v9, lightgbm_v1
Contract: survival_barrier_2.0sl_3.5tp_12bar
Magic: 90001

Uses V9 40-dim institutional features for all brains.
"""

from __future__ import annotations

import json
from typing import Any

from core.execution.strategy_line import StrategyLine


class BarrierStrategy(StrategyLine):
    """60-min barrier prediction strategy.

    All 5 brains are trained on the same 12-bar M5 survival-barrier contract.
    They all consume the same V9 40-dim feature vector, so their outputs are
    directly commensurate for within-group voting.
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
        for b_info in self.brains:
            try:
                prop = b_info["adapter"].inference(feature_vector)
                # Stamp brain_id
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
                            "strategy": "barrier_12bar",
                            "error": str(_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return proposals

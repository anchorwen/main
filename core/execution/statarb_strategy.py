"""StatArb strategy line — OU mean-reversion.

Brain: ou_params_v6
Contract: ou_mean_reversion_zscore
Magic: 90003

Uses raw mid-price as input (not feature vectors).
Only trades when Z-score exceeds thresholds.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine


class StatArbStrategy(StrategyLine):
    """OU mean-reversion strategy.

    Single brain (ou_params_v6).  Direction is derived from Z-score:
      Z > 2.0  → short (overbought, expect reversion down)
      Z < -2.0 → long  (oversold, expect reversion up)
      |Z| < 1.0 → neutral
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
                price = float(mid_price) if mid_price else 0.0
                prop = b_info["adapter"].inference(np.array([price], dtype=np.float32))
                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:  # noqa: BLE001
                    pass
                proposals.append(prop)
            except Exception as _exc:  # noqa: BLE001
                print(
                    json.dumps(
                        {
                            "event": "brain_inference_error",
                            "brain_id": b_info.get("brain_id", "unknown"),
                            "brain_type": b_info.get("brain_type", "unknown"),
                            "strategy": "statarb_dynamic",
                            "error": str(_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return proposals

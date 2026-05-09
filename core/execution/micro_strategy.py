"""Microstructure strategy line — short-horizon tick-bar forward return.

Brains: xgboost_v4.5, transformer_v4.3, transformer_v5
Contract: tick_bar_forward_return_5bars
Magic: 90002

Uses microstructure 9-dim feature vector for all brains.
"""

from __future__ import annotations

from typing import Any

from core.execution.strategy_line import StrategyLine


class MicroStrategy(StrategyLine):
    """3-bar tick microstructure strategy.

    Brains predict directional return over ~5 tick-bars (3-15 minute horizon).
    All use the same microstructure feature set.
    """

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
    ) -> list[Any]:
        proposals: list[Any] = []
        for b_info in self.brains:
            try:
                raw = b_info["adapter"].infer(micro_feature_vector)
                prop = b_info["adapter"].get_signal(raw)
                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:
                    pass
                proposals.append(prop)
            except Exception:
                pass
        return proposals

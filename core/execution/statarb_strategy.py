"""StatArb strategy line — OU mean-reversion.

Brain: ou_params_v6
Contract: ou_mean_reversion_zscore
Magic: 90003

Uses raw mid-price as input (not feature vectors).
Direction is derived from Z-score sign (negative z → long/oversold,
positive z → short/overbought).

Entry decisions are dynamically evaluated by the Meta Filter ensemble
and Conformal OU gate.  Z-score is used as a continuous feature for
p_win penalty (pwin_chain.py) and volume scaling (sigmoid_exhaustion,
z_depth_penalty), NOT as a hard binary gate.  The system has evolved
beyond static |Z| > 2.0 heuristics — the ML pipeline owns the final
entry decision.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine
from core.runtime.fault_handler import fail_open_guard


class StatArbStrategy(StrategyLine):
    """OU mean-reversion strategy.

    Single brain (ou_params_v6).  Direction is derived from Z-score sign
    (negative z → oversold/long, positive z → overbought/short).

    Entry gating is performed by MetaFilter (p_win threshold) and
    ConformalOUGate (quality scoring).  Z-score magnitude affects p_win
    via continuous penalty functions (pwin_chain.py) and position sizing
    via sigmoid_exhaustion + z_depth_penalty — no hard binary threshold.
    """

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
    ) -> list[Any]:
        proposals: list[Any] = []
        for b_info in self.brains:
            try:
                price = float(mid_price) if mid_price else 0.0
                prop = b_info["adapter"].inference(np.array([price], dtype=np.float32))
                bid = b_info.get("brain_id", "unknown")
                with fail_open_guard(f"StatArb:ProposalBuild:{bid}"):
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                proposals.append(prop)
            except Exception as _exc:  # BLE001:REVIEWED (logged, Phase 3b)
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

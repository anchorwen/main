"""Microstructure strategy line — short-horizon tick-bar forward return.

Brains: xgboost_v4.5, transformer_v4.3, transformer_v5
Contract: tick_bar_forward_return_5bars
Magic: 90002

Uses microstructure 9-dim feature vector for all brains.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine
from core.runtime.fault_handler import fail_open_guard


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
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
    ) -> list[Any]:
        proposals: list[Any] = []
        for b_info in self.brains:
            try:
                hmre_layer = b_info.get("hmre_layer")
                if hmre_layer and micro_sequences:
                    # HMRE brain: use per-TF (32,9) sequence → adapter.run()
                    # run() returns BrainDecisionProposal directly (not raw dict)
                    seq = micro_sequences.get(hmre_layer)
                    if seq is not None:
                        try:
                            prop = b_info["adapter"].run(None, seq)
                        except Exception as _hmre_exc:
                            # Fallback: reshape and use infer_sequence() directly,
                            # bypassing the rolling buffer (infer() expects 9-dim
                            # vectors; passing a flat ravel would corrupt the buffer).
                            try:
                                seq_batch = seq.astype(np.float32).reshape(1, seq.shape[0], 9)
                                raw = b_info["adapter"].infer_sequence(seq_batch)
                                prop = b_info["adapter"].get_signal(raw)
                            except Exception:
                                raise _hmre_exc from None
                    else:
                        continue  # sequence not available for this TF
                else:
                    # Legacy M5 brain: use 9-dim feature vector → adapter.inference()
                    prop = b_info["adapter"].inference(micro_feature_vector)
                bid = b_info.get("brain_id", "unknown")
                with fail_open_guard(f"Micro:ProposalBuild:{bid}"):
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
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
                            "feature_shape": str(micro_feature_vector.shape)
                            if hasattr(micro_feature_vector, "shape")
                            else "unknown",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return proposals

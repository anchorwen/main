"""Swing strategy line — multi-TF barrier contracts with D1 features.

Brains: xgboost_v9, lightgbm_v1
Contract: TF-specific barrier (D1 5-bar, H4 18-bar, H1 24-bar, M30 12-bar, M15 24-bar)
Magic: 90301-90340 (per-TF)

All swing brains consume daily_swing 24-dim features, regardless of their
barrier timeframe.  Brain proposals are grouped by contract_group and voted
within their respective groups.

swing_enhanced_35 brains (Phase 2 swing revival) receive 35-dim vectors:
24 daily + 9 micro + 2 TF-specific (OU_Theta, Hurst).

FIX-20260601-039: Feature assembly is now delegated to the central factory
(``core.features.feature_assembler.assemble_features_by_schema()``).
TF close buffer and OU/Hurst computation are inherited from the StrategyLine
base class — no more copy-paste between SwingStrategy and BarrierStrategy.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine


class SwingStrategy(StrategyLine):
    """Multi-timeframe swing strategy with D1 daily features.

    Each swing brain is trained on a TF-specific barrier contract (D1/H4/H1/M30/M15)
    but all consume the same 24-dim daily_swing feature vector.

    swing_enhanced_35 brains receive an augmented 35-dim vector built from
    daily + micro + TF-specific features computed from a rolling close buffer.

    FIX-20260601-039: Feature assembly delegated to the central factory.
    TF buffer and OU/Hurst inherited from StrategyLine base class.
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
        # FIX-20260602-050: only require daily features for swing_enhanced
        # schemas.  v9_institutional brains (BTC_Swing_V4) don't need D1 data.
        # Blocking all brains when daily_feature_vector is None caused the
        # model to go blind → neutral_consensus for hours → restart unblinds.
        _needs_daily = any(
            "swing_enhanced" in getattr(b.get("adapter", None), "feature_schema", "")
            or "daily_swing" in getattr(b.get("adapter", None), "feature_schema", "")
            for b in self.brains
        )
        if _needs_daily and daily_feature_vector is None:
            return proposals  # D1 features not available yet — only for swing brains

        # Track TF close prices for OU/Hurst computation (buffer in base class)
        if mid_price is not None and mid_price > 0:
            self._tf_close_buffer.append(mid_price)

        from core.features.feature_assembler import assemble_features_by_schema

        for b_info in self.brains:
            try:
                adapter = b_info["adapter"]
                # ── FIX-20260610-009: Schema resolution anchored to brain config ──
                # PREVIOUSLY queried the runtime Adapter instance for a
                # 'feature_schema' property that does NOT exist on
                # LightGBM/XGBoost adapters.  getattr silently returned "",
                # cascading into ``or "v9_institutional"`` → 40-dim V9 vector
                # for ALL brains regardless of their training contract.
                # BTC brains (37-dim btc_macro_enhanced_37) received 40-dim
                # vectors → dimension mismatch → raw_score=0.0 fallback.
                # This single line spawned RC-06 across 5 failed patches
                # (FIX-022, FIX-025, FIX-017, FIX-081, FIX-135).
                schema = b_info.get("feature_schema_id", "")
                if not schema:
                    raise ValueError(
                        f"[FATAL_CONTRACT] Brain {b_info.get('brain_id', 'unknown')} "
                        f"missing explicit feature_schema_id in config — "
                        f"cannot determine feature schema for inference"
                    )

                fv = assemble_features_by_schema(
                    schema,
                    legacy_v9_vector=np.asarray(feature_vector, dtype=np.float64).ravel(),
                    daily_features=daily_feature_vector,
                    micro_features=micro_feature_vector,
                    tf_ou=self._compute_tf_ou_theta(),
                    tf_hurst=self._compute_tf_hurst(),
                    btc_augment=btc_augment if "btc_macro" in schema else None,
                )
                prop = adapter.inference(fv)

                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "Brain proposal build failed brain_id=%s", bid
                    )
                proposals.append(prop)
            except Exception as _exc:  # noqa: BLE001
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

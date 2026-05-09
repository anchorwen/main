"""LightGBM Brain Adapter — loads LightGBM Booster from .txt artifact.

Implements BaseBrainAdapter.load() / infer() / get_signal().
Maps raw LightGBM regression scores onto BrainDecisionProposal via score→direction conversion.

LightGBM uses leaf-wise tree growth with 40-dim V9 institutional features.
Operates alongside XGBoost V4.5 (microstructure features) for tree-ensemble diversity.
"""

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal


class LightGBMBrainAdapter(BaseBrainAdapter):
    """Adapter for LightGBM models serialized as .txt (booster format).

    Produced by lgb_trainer, artifact: LightGBM_V1_Core.txt.
    Uses 40-dim V9 institutional features via V9FeatureAdapter.
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._booster = None
        self._num_features: int | None = None

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        artifact_path = self._brain_entry.get("artifact_path")
        if not artifact_path:
            self._backend = "stub:no_artifact_path"
            return

        try:
            import lightgbm as lgb

            self._lgb = lgb
            self._booster = lgb.Booster(model_file=artifact_path)
            self._num_features = self._booster.num_feature()
            self._backend = "lightgbm:txt"
        except Exception as exc:
            self._backend = f"stub:{type(exc).__name__}"
            self._booster = None

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        if self._booster is None:
            return {"raw_score": 0.0, "feature_count": len(feature_vector), "fallback": True}

        started = perf_counter()
        vec = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        raw_score = float(self._booster.predict(vec)[0])
        runtime_ms = (perf_counter() - started) * 1000.0

        return {
            "raw_score": raw_score,
            "feature_count": len(feature_vector),
            "runtime_ms": runtime_ms,
            "fallback": False,
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainDecisionProposal:
        from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
        from core.contracts.ids import new_proposal_id

        raw_score = raw_output.get("raw_score", 0.0)
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback_used = raw_output.get("fallback", self._backend.startswith("stub"))

        direction_bias, up_prob, down_prob = self._score_to_direction(raw_score)

        return BrainDecisionProposal(
            schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
            proposal_id=new_proposal_id(),
            snapshot_id="",
            brain_id=self._brain_entry.get("brain_id", ""),
            brain_role=self._brain_entry.get("brain_role", ""),
            brain_status=self._brain_entry.get("status", ""),
            model_version=self._brain_entry.get("model_version", "unknown"),
            event_time=datetime.now(UTC).replace(tzinfo=None),
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            prediction={
                "direction_bias": direction_bias,
                "up_probability": up_prob,
                "down_probability": down_prob,
                "confidence": max(up_prob, down_prob),
                "uncertainty": 1.0 - max(up_prob, down_prob),
                "expected_edge_bps": None,
                "expected_hold_seconds": None,
            },
            applicability={
                "regime_tags": self._brain_entry.get("deployment_scope", {}).get("regimes", []),
                "symbol_tags": self._brain_entry.get("deployment_scope", {}).get("symbols", []),
            },
            rationale={
                "reason_tags": ["v1_lightgbm_institutional"],
                "warnings": [] if not fallback_used else ["lightgbm_unavailable_using_stub"],
            },
            health={
                "input_ok": True,
                "fallback_used": fallback_used,
                "runtime_ms": runtime_ms,
                "risk_score": abs(raw_score) * 0.1,
                "volatility_score": 0.5,
                "backend": self._backend,
            },
            vote_weight=self._brain_entry.get("vote_weight", 1.0),
            extensions={
                "raw_outputs": {
                    "raw_score": raw_score,
                    "lgb_num_features": raw_output.get("feature_count"),
                }
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_to_direction(raw_score: float) -> tuple[str, float, float]:
        confidence = float(np.tanh(abs(raw_score)))
        if raw_score > 0.1:
            return "long", confidence, max(0.0, 1.0 - confidence)
        elif raw_score < -0.1:
            return "short", max(0.0, 1.0 - confidence), confidence
        else:
            return "neutral", 0.5, 0.5

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = self._num_features
        base["booster_loaded"] = self._booster is not None
        base["uses_feature_adapter"] = self._feature_adapter is not None
        return base

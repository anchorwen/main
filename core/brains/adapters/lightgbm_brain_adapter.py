"""LightGBM Brain Adapter — loads LightGBM Booster from .txt artifact.

Implements BaseBrainAdapter.load() / run() / infer() / get_signal().
Maps raw LightGBM regression scores onto BrainDecisionProposal via score→direction conversion.

Feature extraction is metadata-driven: reads ``features`` from the brain config
(populated at training time).  No hardcoded schema imports — the brain config is
the single source of truth for feature names and their order.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.deployment.brain_alert import emit_brain_alert

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal


class LightGBMBrainAdapter(BaseBrainAdapter):
    """Adapter for LightGBM models serialized as .txt (booster format).

    Produced by lgb_trainer, artifact: LightGBM_V1_Core.txt.
    Feature names are read from the brain config ``features`` field.
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
            booster = lgb.Booster(model_file=artifact_path)
            self._booster = booster
            self._num_features = booster.num_feature()
            self._backend = "lightgbm:txt"
        except Exception as exc:
            self._backend = f"stub:{type(exc).__name__}"
            self._booster = None
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "model_load_failed",
                {"artifact": artifact_path, "error": f"{type(exc).__name__}: {exc}"},
            )

    def run(self, snapshot, feature_source: dict | None = None) -> BrainSignal:
        """Metadata-driven feature extraction with three defense lines.

        1. **Name-based extraction**: Read ``features`` from brain config
           (populated at training time).  Extracts values in that exact order
           from the feature_source dict.  Missing keys default to 0.0.

        2. **Optional normalization**: Applied only when
           ``self._feature_adapter`` exists.  The adapter's ``normalize()``
           method respects its own ``normalize`` flag.

        3. **Dimension assertion**: Final vector length must match
           ``self._num_features`` (from ``booster.num_feature()``).
        """
        if feature_source is None:
            return self.inference(np.zeros(self._num_features, dtype=np.float64))

        # ── Defense Line 1: Metadata-driven feature extraction ──
        feature_names = self._brain_entry.get("features")
        if not feature_names and self._booster is not None:
            feature_names = list(self._booster.feature_name())

        if not feature_names:
            import logging

            logging.error(
                "LightGBMBrainAdapter [%s]: no features in config and booster "
                "unavailable — cannot produce predictions",
                self._brain_entry.get("brain_id", "?"),
            )
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "feature_missing",
                {"message": "No feature names in config or booster — zero vector used"},
            )
            return self.inference(np.zeros(self._num_features, dtype=np.float64))

        raw_vector = np.array(
            [float(feature_source.get(name, 0.0)) for name in feature_names],
            dtype=np.float64,
        )

        # ── Defense Line 2: Optional normalization ──
        if self._feature_adapter is not None:
            try:
                raw_vector = self._feature_adapter.normalize(raw_vector)
            except Exception:
                pass  # best-effort; dim mismatch caught by line 3

        # ── Defense Line 3: Dimension assertion ──
        if self._num_features and len(raw_vector) != self._num_features:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "feature_dimension_mismatch",
                {"expected": self._num_features, "got": len(raw_vector)},
            )
            return self.inference(np.zeros(self._num_features, dtype=np.float64))

        return self.inference(raw_vector)

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        if self._booster is None:
            return {"raw_score": 0.0, "feature_count": len(feature_vector), "fallback": True}

        # ── Zero-vector guard — catches silent FeatureService fallback ──
        vec_arr = np.asarray(feature_vector, dtype=np.float64)
        if np.max(np.abs(vec_arr)) < 1e-10:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "zero_feature_vector",
                {"feature_count": len(feature_vector)},
            )
            return {
                "raw_score": 0.0,
                "feature_count": len(feature_vector),
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": "zero_feature_vector",
            }

        # ── Dimension guard — catches direct infer() calls that bypass run() ──
        n_cols = feature_vector.shape[0] if feature_vector.ndim == 1 else feature_vector.shape[1]
        if self._num_features and n_cols != self._num_features:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "feature_dimension_mismatch",
                {"expected": self._num_features, "got": n_cols},
            )
            return {
                "raw_score": 0.0,
                "feature_count": n_cols,
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": (f"dim_mismatch_expected_{self._num_features}_got_{n_cols}"),
            }

        started = perf_counter()
        vec = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        raw_pred = self._booster.predict(vec)
        if raw_pred.ndim == 2 and raw_pred.shape[1] > 1:
            # Multiclass: class 0=short, class 2=long → directional score [-1, 1]
            raw_score = float(raw_pred[0, 2] - raw_pred[0, 0])
        else:
            raw_score = float(raw_pred[0])
        runtime_ms = (perf_counter() - started) * 1000.0

        return {
            "raw_score": raw_score,
            "feature_count": len(feature_vector),
            "runtime_ms": runtime_ms,
            "fallback": False,
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        from core.schemas.trading_contracts import BrainSignal

        raw_score = raw_output.get("raw_score", 0.0)
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback_used = raw_output.get("fallback", self._backend.startswith("stub"))

        direction_bias, up_prob, down_prob = self._score_to_direction(
            raw_score,
            objective=self._brain_entry.get("training_params", {}).get("objective", "regression"),
        )

        return BrainSignal(
            brain_id=self._brain_entry.get("brain_id", ""),
            direction=direction_bias,
            confidence=max(up_prob, down_prob),
            raw_score=raw_score,
            fallback=fallback_used,
            runtime_ms=runtime_ms,
            diagnostics={
                k: v
                for k, v in raw_output.items()
                if k not in ("raw_score", "runtime_ms", "fallback")
            },
        )

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = self._num_features
        base["booster_loaded"] = self._booster is not None
        base["uses_feature_adapter"] = self._feature_adapter is not None
        return base

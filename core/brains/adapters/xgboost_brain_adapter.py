"""XGBoost Brain Adapter — loads JSON-serialized XGBoost models from
mtx_trainer (lane=mtx_xgb).

Implements BaseBrainAdapter.load() / infer() / get_signal().
Maps raw XGBoost regression scores onto BrainDecisionProposal via score→direction conversion.
"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.deployment.brain_alert import emit_brain_alert
from core.runtime.fault_handler import fail_open_guard

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal


class XGBoostBrainAdapter(BaseBrainAdapter):
    """Adapter for XGBoost models serialized as .json (booster format).

    Produced by mtx_trainer --mode xgboost, artifact: V4.X_XGBoost_Core.json.
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._booster: Any = None
        self._num_features: int | None = None
        self._num_class: int = 1  # 1=regression/binary, 3=multi:softprob

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def run(self, snapshot, feature_source=None) -> BrainSignal:
        """Override to handle dict, (n_bars, 9) sequence, or (288,) flat array.

        - dict → build_model_input → (9,) vector (backward-compat)
        - (n_bars, 9) ndarray → build_flat_input → (288,) vector
        - (n_bars*9,) ndarray → use directly (pre-flattened)
        """
        if isinstance(feature_source, np.ndarray):
            if feature_source.ndim == 2 and feature_source.shape[0] > 1:
                # (n_bars, 9) → flatten via adapter
                if self._feature_adapter is not None and hasattr(
                    self._feature_adapter, "build_flat_input"
                ):
                    feature_vector = self._feature_adapter.build_flat_input(feature_source).ravel()
                else:
                    feature_vector = feature_source.ravel().astype(np.float64)
            else:
                feature_vector = feature_source.ravel().astype(np.float64)
        elif self._feature_adapter is not None and feature_source is not None:
            # Path A: feature_adapter handles dict→vector with normalization
            feature_vector = self._feature_adapter.build_model_input(feature_source).ravel()
        elif isinstance(feature_source, dict) and feature_source:
            # ── Path B: Named lookup from brain config features (SSOT) ──
            # FIX-20260612-002: Replace dict-order-dependent .values()
            # extraction with name-ordered projection from brain_entry["features"].
            # This is the authoritative feature order guaranteed by BrainFactory
            # at load time (validated against model .meta.json feature_names).
            # Shadow validation: compare old vs new, alert on mismatch.
            feature_names = self._brain_entry.get("features")
            if feature_names:
                new_vector = np.asarray(
                    [float(feature_source.get(n, 0.0)) for n in feature_names],
                    dtype=np.float64,
                )
                # ── Shadow validation (48h transitional, remove after 2026-06-14) ──
                try:
                    old_vector = np.asarray(
                        list(feature_source.values()), dtype=np.float64
                    )
                    if not np.array_equal(old_vector[:len(new_vector)], new_vector):
                        logger.warning(
                            "[SHADOW_MISMATCH] XGBoost feature order skew detected "
                            "for brain=%s schema=%s. "
                            "Expected first 3: %s. "
                            "Got raw dict keys first 3: %s.",
                            self._brain_entry.get("brain_id", "?"),
                            self._brain_entry.get("feature_schema_id", "?"),
                            feature_names[:3],
                            list(feature_source.keys())[:3],
                        )
                except Exception:  # BLE001:FOG
                    with fail_open_guard("xgboost_brain_adapter:run"):
                        pass  # Shadow validation is non-blocking
                feature_vector = new_vector
            else:
                # Legacy fallback: dict-order-dependent (fragile).
                # Should never be hit — BrainFactory validates features field at load.
                logger.warning(
                    "XGBoostBrainAdapter: no 'features' in brain_entry for %s — "
                    "falling back to dict.values() positional extraction",
                    self._brain_entry.get("brain_id", "unknown"),
                )
                feature_vector = np.asarray(
                    list(feature_source.values()), dtype=np.float64
                )
        else:
            feature_vector = np.zeros(self._num_features, dtype=np.float64)
        return self.inference(feature_vector)

    def load(self) -> None:
        """Load XGBoost booster from JSON artifact."""
        artifact_path = self._brain_entry.get("artifact_path")
        if not artifact_path:
            self._backend = "stub:no_artifact_path"
            return

        try:
            import xgboost as xgb

            self._xgb = xgb
            self._booster = xgb.Booster()
            self._booster.load_model(artifact_path)
            config = json.loads(self._booster.save_config())
            learner_cfg = config.get("learner", {})
            lmp = learner_cfg.get("learner_model_param", {})
            self._num_class = int(lmp.get("num_class", "1") or "1")
            # Infer num_features: brain config is SSOT (FIX-20260531-021).
            # XGBoost JSON does NOT reliably persist feature_names across
            # save/load cycles.  We FORCE the booster to use brain config
            # features so that infer() always sees the correct names.
            _expected = self._brain_entry.get("features")
            if _expected and isinstance(_expected, list):
                self._num_features = len(_expected)
                self._booster.feature_names = _expected
            else:
                feature_names = self._booster.feature_names
                if feature_names:
                    self._num_features = len(feature_names)
            # ── Emit alert if model was trained without feature_names ──
            if not self._booster.feature_names:
                emit_brain_alert(
                    self._brain_entry.get("brain_id", "unknown"),
                    "model_lacks_feature_names",
                    {
                        "artifact": artifact_path,
                        "note": "Retrain with feature_names in DMatrix",
                    },
                )
                raw = lmp.get(
                    "num_feature",
                    learner_cfg.get("gradient_booster", {})
                    .get("model_param", {})
                    .get("num_feature"),
                )
                self._num_features = int(raw) if raw is not None else None
            # Update _feature_dimension from model not hardcoded default
            if self._num_features is not None:
                self._feature_dimension = self._num_features
            self._backend = "xgboost:json"
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("xgboost_brain_adapter:load"):
                self._backend = f"stub:{type(exc).__name__}"
                self._booster = None
                emit_brain_alert(
                    self._brain_entry.get("brain_id", "unknown"),
                    "model_load_failed",
                    {"artifact": artifact_path, "error": f"{type(exc).__name__}: {exc}"},
                )
    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        """Run XGBoost inference on a 1-D feature vector.

        Returns {"raw_score": float, "feature_count": int}.
        """
        if self._booster is None:
            return {"raw_score": 0.0, "feature_count": len(feature_vector), "fallback": True}

        started = perf_counter()

        # ── Zero-vector guard — catches silent FeatureService fallback ──
        n_cols = feature_vector.shape[0] if feature_vector.ndim == 1 else feature_vector.shape[1]
        vec_arr = np.asarray(feature_vector, dtype=np.float64)
        if np.max(np.abs(vec_arr)) < 1e-10:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "zero_feature_vector",
                {"feature_count": n_cols},
            )
            return {
                "raw_score": 0.0,
                "feature_count": n_cols,
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": "zero_feature_vector",
            }

        # Guard: model expects _num_features features.  Attempt to recover
        # from common mismatch patterns before returning stub.
        if self._num_features and n_cols != self._num_features:
            # swing_enhanced_21 (21-dim) ← daily_swing_24 (24-dim):
            # Remove 3 XAU cross indices (12,13,14) from 24-dim daily vector.
            if self._num_features == 21 and n_cols == 24:
                _xau = {12, 13, 14}
                vec_arr = np.array([vec_arr[i] for i in range(24) if i not in _xau], dtype=np.float64)
                n_cols = len(vec_arr)
            # swing_enhanced_29 (29-dim) ← daily_swing_24 (24-dim):
            # Rebuild from 24 daily + zeros for 6 micro + 2 TF, then drop XAU indices.
            elif self._num_features == 29 and n_cols == 24:
                fv_35 = np.concatenate([vec_arr, np.zeros(11, dtype=np.float64)])
                _xau = {12, 13, 14, 30, 31, 32}
                vec_arr = np.array([fv_35[i] for i in range(35) if i not in _xau], dtype=np.float64)
                n_cols = len(vec_arr)
            else:
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
                    "fallback_reason": f"dim_mismatch_expected_{self._num_features}_got_{n_cols}",
                }

        booster_fn = self._booster.feature_names
        brain_fn = self._brain_entry.get("features")
        if booster_fn and len(booster_fn) == n_cols:
            feature_names = booster_fn
        elif isinstance(brain_fn, list) and len(brain_fn) == n_cols:
            feature_names = brain_fn
        else:
            feature_names = None

        dmatrix = self._xgb.DMatrix(
            feature_vector.reshape(1, -1),
            feature_names=feature_names,
        )
        preds = self._booster.predict(dmatrix)
        if self._num_class > 2:
            # multi:softprob — shape (1, num_class) → [P(SHORT), P(NEUTRAL), P(LONG)]
            probs = preds[0]  # shape (num_class,)
            raw_score = float(probs[2] - probs[0])  # LONG prob - SHORT prob
        else:
            raw_score = float(preds[0])
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

        # Map raw score to direction (FIX-20260526-030: binary vs regression path)
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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = self._num_features
        base["booster_loaded"] = self._booster is not None
        base["uses_feature_adapter"] = self._feature_adapter is not None
        return base

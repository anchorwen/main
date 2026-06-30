"""Transformer Brain Adapter — loads ONNX-exported QuantTransformer models from
mtx_trainer (lane=mtx, mode=transformer).

Implements BaseBrainAdapter.load() / infer() / get_signal().
Maintains a rolling 64-bar sequence buffer for the 9 microstructure features
and maps regression scores onto BrainDecisionProposal via tanh squashing.
"""

from __future__ import annotations

import logging
from collections import deque
from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.deployment.brain_alert import emit_brain_alert

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal

FALLBACK_SEQ_LEN = 64
NUM_FEATURES = 9

MICROSTRUCTURE_9_FEATURES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "XAGUSDc_return",
    "EURUSDc_return",
    "USDJPYc_return",
]


class TransformerBrainAdapter(BaseBrainAdapter):
    """Adapter for QuantTransformer models exported as ONNX.

    Produced by mtx_trainer --mode transformer, artifact: mtx_transformer_core.onnx.
    Input: (batch=1, seq_len=64, features=9) → output: regression score.
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._session: Any = None  # onnxruntime.InferenceSession | None
        self._input_name = "input"
        self._seq_len = FALLBACK_SEQ_LEN
        self._buffer: deque = deque(maxlen=self._seq_len)
        self._onnx_model_path: Any = None  # str | None
        self._guard: Any = None  # InferenceGuard | None
        self._num_features: int | None = NUM_FEATURES

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def bootstrap_buffer(self, feature_vectors: list) -> None:
        """Pre-fill the sequence buffer from historical features to avoid cold start."""
        if not feature_vectors:
            return
        self._buffer.clear()
        for fv in feature_vectors[-self._seq_len :]:
            self._buffer.append(np.asarray(fv, dtype=np.float32))

    def load(self) -> None:
        """Load ONNX model from artifact path.

        Auto-detects sequence length from the ONNX model input shape so that
        both seq_len=32 and seq_len=64 models work without code changes.
        """
        artifact_path = self._brain_entry.get("artifact_path")
        if not artifact_path:
            self._backend = "stub:no_artifact_path"
            return

        # ── Optional subprocess isolation ──
        if self._brain_entry.get("inference_isolation", False):
            try:
                from core.brains.services.inference_guard import InferenceGuard

                self._guard = InferenceGuard(artifact_path, timeout=5.0, max_restarts=3)
                self._onnx_model_path = artifact_path
                self._backend = "onnxruntime:transformer:isolated"
                return
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                self._guard = None
                # Fall through to in-process loading
        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(artifact_path)
            self._input_name = self._session.get_inputs()[0].name
            # Auto-detect sequence length and features from ONNX input shape
            input_shape = self._session.get_inputs()[0].shape
            if len(input_shape) >= 2 and isinstance(input_shape[1], int) and input_shape[1] > 0:
                self._seq_len = input_shape[1]
                self._buffer = deque(maxlen=self._seq_len)
            if len(input_shape) >= 3 and isinstance(input_shape[2], int) and input_shape[2] > 0:
                self._num_features = input_shape[2]
            self._onnx_model_path = artifact_path
            self._backend = "onnxruntime:transformer"
        except Exception as exc:  # noqa: BLE001  # BLE001:FOG
            self._backend = f"stub:{type(exc).__name__}"
            self._session = None
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "model_load_failed",
                {"artifact": artifact_path, "error": f"{type(exc).__name__}: {exc}"},
            )

    def run(self, snapshot, feature_source=None) -> BrainSignal:
        """Override to handle dict (single bar) or (n_bars, 9) pre-built sequence.

        - dict → extract 9 features → append to buffer → inference when buffer full
        - (n_bars, 9) ndarray → skip buffer, use directly (Transformer + XGBoost compat)

        When feature_source is a pre-built sequence, the adapter does NOT use
        the internal rolling buffer — it passes the sequence directly to infer().
        """
        if isinstance(feature_source, np.ndarray) and feature_source.ndim == 2:
            # Pre-built (n_bars, 9) sequence — use directly (skip buffer)
            if self._feature_adapter is not None and hasattr(
                self._feature_adapter, "build_sequence_input"
            ):
                seq_batch = self._feature_adapter.build_sequence_input(feature_source)
            else:
                seq = feature_source.astype(np.float32)
                seq_batch = seq.reshape(1, seq.shape[0], 9)
            # Trim to model's expected sequence length (most recent bars)
            if seq_batch.shape[1] > self._seq_len:
                seq_batch = seq_batch[:, -self._seq_len :, :]
            raw_output = self.infer_sequence(seq_batch)
            return self.get_signal(raw_output)

        if self._feature_adapter is not None and feature_source is not None:
            feature_vector = self._feature_adapter.build_model_input(feature_source).ravel()
        elif isinstance(feature_source, dict) and feature_source:
            # ── Named lookup from brain config features (SSOT) ──
            # FIX-20260612-002: Replace dict-order-dependent .values() with
            # name-ordered projection from brain_entry["features"].
            feature_names = self._brain_entry.get("features")
            if feature_names:
                feature_vector = np.asarray(
                    [float(feature_source.get(n, 0.0)) for n in feature_names],
                    dtype=np.float64,
                )
            else:
                # Legacy fallback (fragile — should never be reached)
                logger.warning(
                    "TransformerBrainAdapter: no 'features' in brain_entry for %s — "
                    "falling back to dict.values() positional extraction",
                    self._brain_entry.get("brain_id", "unknown"),
                )
                feature_vector = np.asarray(list(feature_source.values()), dtype=np.float64)
        else:
            feature_vector = np.zeros(NUM_FEATURES, dtype=np.float64)
        return self.inference(feature_vector)

    def _run_transformer(self, model_input: np.ndarray) -> list | None:
        """Run ONNX inference through guard or in-process session.

        Returns list of output arrays from ONNX, or None on failure.
        """
        if self._guard is not None and self._guard.is_alive:
            result = self._guard.infer(self._input_name, None, model_input)
            if result is not None:
                return result
        if self._session is not None:
            return self._session.run(None, {self._input_name: model_input})
        return None

    def infer_sequence(self, sequence_batch: np.ndarray) -> dict[str, Any]:
        """Run ONNX inference on a pre-built (1, seq_len, 9) sequence.

        Unlike infer(), this skips the internal rolling buffer and uses
        the provided sequence directly.  Used when feature_source is a
        pre-computed numpy array.
        """
        started = perf_counter()
        output = self._run_transformer(sequence_batch)
        runtime_ms = (perf_counter() - started) * 1000.0

        if output is not None:
            return {
                "raw_score": float(output[0].ravel()[0]),
                "feature_count": int(sequence_batch.size),
                "runtime_ms": runtime_ms,
                "fallback": False,
            }
        return {
            "raw_score": 0.0,
            "feature_count": int(sequence_batch.size),
            "fallback": True,
            "fallback_reason": "no_onnx_session",
        }

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        """Run Transformer inference on accumulated sequence.

        Appends feature_vector to the internal 64-bar buffer.  When the buffer
        is full, runs ONNX inference on the entire sequence and returns a
        regression score.  If the buffer isn't full yet, returns a fallback
        stub so the brain doesn't vote until it has enough data.

        Args:
            feature_vector: 1-D array of 9 microstructure features (single bar,
            pre-normalised by MicrostructureFeatureAdapter).
        """
        self._buffer.append(np.asarray(feature_vector, dtype=np.float32))

        if len(self._buffer) < self._seq_len:
            return {
                "raw_score": 0.0,
                "feature_count": len(feature_vector),
                "fallback": True,
                "fallback_reason": "buffer_not_full",
                "buffer_size": len(self._buffer),
            }

        # Build sequence batch: (1, self._seq_len, NUM_FEATURES)
        sequence = np.stack(list(self._buffer), axis=0, dtype=np.float32)
        sequence = sequence.reshape(1, self._seq_len, NUM_FEATURES)

        started = perf_counter()
        output = self._run_transformer(sequence)
        runtime_ms = (perf_counter() - started) * 1000.0

        if output is not None:
            return {
                "raw_score": float(output[0].ravel()[0]),
                "feature_count": len(feature_vector),
                "runtime_ms": runtime_ms,
                "fallback": False,
            }
        return {
            "raw_score": 0.0,
            "feature_count": len(feature_vector),
            "fallback": True,
            "fallback_reason": "no_onnx_session",
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        from core.schemas.trading_contracts import BrainSignal

        raw_score = raw_output.get("raw_score", 0.0)
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback_used = raw_output.get("fallback", self._backend.startswith("stub"))

        direction_bias, up_prob, down_prob = self._score_to_direction(
            raw_score,
            objective=self._brain_entry.get("training_params", {}).get("objective", "regression"),
            threshold=float(self._brain_entry.get("activation_threshold", 0.1)),
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
            vote_weight=float(self._brain_entry.get("vote_weight", 1.0) or 1.0),
        )

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = NUM_FEATURES
        base["seq_len"] = self._seq_len
        base["session_loaded"] = self._session is not None
        base["uses_feature_adapter"] = self._feature_adapter is not None
        base["buffer_size"] = len(self._buffer)
        base["onnx_model_path"] = self._onnx_model_path
        return base

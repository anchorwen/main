"""Transformer Brain Adapter — loads ONNX-exported QuantTransformer models from
mtx_trainer (lane=mtx, mode=transformer).

Implements BaseBrainAdapter.load() / infer() / get_signal().
Maintains a rolling 64-bar sequence buffer for the 9 microstructure features
and maps regression scores onto BrainDecisionProposal via tanh squashing.
"""

from collections import deque
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal

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
        self._session = None
        self._input_name = "input"
        self._seq_len = FALLBACK_SEQ_LEN  # will be updated by load() from ONNX input shape
        self._buffer: deque = deque(maxlen=self._seq_len)
        self._onnx_model_path = None

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

        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(artifact_path)
            self._input_name = self._session.get_inputs()[0].name
            # Auto-detect sequence length from ONNX input shape: (batch, seq, features)
            input_shape = self._session.get_inputs()[0].shape
            if len(input_shape) >= 2 and isinstance(input_shape[1], int) and input_shape[1] > 0:
                self._seq_len = input_shape[1]
                self._buffer = deque(maxlen=self._seq_len)
            self._onnx_model_path = artifact_path
            self._backend = "onnxruntime:transformer"
        except Exception as exc:
            self._backend = f"stub:{type(exc).__name__}"
            self._session = None

    def run(self, snapshot, feature_source: dict | None = None) -> BrainDecisionProposal:
        """Override to use MicrostructureFeatureAdapter for feature extraction.

        When feature_adapter is available, extracts 9 microstructure features
        in canonical order and applies StandardScaler normalization before
        appending to the sequence buffer.
        """
        if self._feature_adapter is not None and feature_source is not None:
            feature_vector = self._feature_adapter.build_model_input(feature_source).ravel()
        elif feature_source is not None:
            feature_vector = np.asarray(list(feature_source.values()), dtype=np.float64)
        else:
            feature_vector = np.zeros(NUM_FEATURES, dtype=np.float64)
        return self.inference(feature_vector)

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

        if self._session is None:
            return {
                "raw_score": 0.0,
                "feature_count": len(feature_vector),
                "fallback": True,
                "fallback_reason": "no_onnx_session",
            }

        # Build sequence batch: (1, self._seq_len, NUM_FEATURES)
        sequence = np.stack(list(self._buffer), axis=0, dtype=np.float32)
        sequence = sequence.reshape(1, self._seq_len, NUM_FEATURES)

        started = perf_counter()
        output = self._session.run(None, {self._input_name: sequence})
        raw_score = float(output[0].ravel()[0])
        runtime_ms = (perf_counter() - started) * 1000.0

        return {
            "raw_score": raw_score,
            "feature_count": len(feature_vector),
            "runtime_ms": runtime_ms,
            "fallback": False,
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainDecisionProposal:
        """Convert Transformer raw_score into BrainDecisionProposal.

        Score > 0 → long bias; score < 0 → short bias.  Magnitude via tanh → confidence.
        """
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
                "reason_tags": ["v4_3_microstructure_transformer"],
                "warnings": ([] if not fallback_used else ["transformer_unavailable_using_stub"]),
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
                    "num_features": raw_output.get("feature_count"),
                    "buffer_size": raw_output.get("buffer_size", len(self._buffer)),
                }
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_to_direction(raw_score: float) -> tuple[str, float, float]:
        """Map regression score to (direction_bias, up_prob, down_prob).

        Uses tanh to squash the raw score into pseudo-probabilities:
          - score >  0.1 → long  (up_prob=conf, down_prob=1-conf)
          - score < -0.1 → short (up_prob=1-conf, down_prob=conf)
          - otherwise    → neutral (0.5, 0.5)
        """
        confidence = float(np.tanh(abs(raw_score)))

        if raw_score > 0.1:
            return "long", confidence, max(0.0, 1.0 - confidence)
        elif raw_score < -0.1:
            return "short", max(0.0, 1.0 - confidence), confidence
        else:
            return "neutral", 0.5, 0.5

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = NUM_FEATURES
        base["seq_len"] = self._seq_len
        base["session_loaded"] = self._session is not None
        base["uses_feature_adapter"] = self._feature_adapter is not None
        base["buffer_size"] = len(self._buffer)
        base["onnx_model_path"] = self._onnx_model_path
        return base

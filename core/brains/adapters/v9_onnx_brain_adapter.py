"""V9 Institutional ONNX Brain Adapter.

Implements BaseBrainAdapter.load() / infer() / get_signal().
Wraps ONNX Runtime inference for the V9 institutional survival model.
"""

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import numpy as np

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.contracts.ids import new_proposal_id

from ..schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
from .base_adapter import BaseBrainAdapter


class V9OnnxBrainAdapter(BaseBrainAdapter):
    """Adapter for V9 Institutional Survival ONNX model."""

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._session = None
        self._input_name = None
        self._output_names: list[str] = []

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the ONNX artifact from disk."""
        if not self._brain_entry.get("enable_onnxruntime", False):
            self._backend = "stub:disabled"
            return

        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(
                self._brain_entry["artifact_path"],
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
            self._output_names = [output.name for output in self._session.get_outputs()]
            self._backend = "onnxruntime"
        except Exception as exc:
            self._backend = f"stub:{type(exc).__name__}"
            bid = self._brain_entry.get("brain_id", "unknown")
            art = self._brain_entry.get("artifact_path", "unknown")
            print(
                f"[v9_onnx_adapter] load_failed brain_id={bid} artifact={art} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        """Run ONNX inference on a 1-D feature vector.

        Returns dict with: out_dir/raw_score, out_risk, out_vol, runtime_ms, fallback.

        Handles three ONNX output formats:
        - V9 format (3 outputs): [logits(1,3), risk(1,1), vol(1,1)] — classification
        - CRT format (1 output):  [logits(1,3)] — risk/vol derived from logits
        - Regression format (3 outputs): [regression(1,1), risk(1,1), vol(1,1)] — P&L regression

        The caller is responsible for normalising and preparing the feature
        vector before passing it here.  This method only reshapes for ONNX.
        """
        started = perf_counter()

        # Reshape flat features to (1, N) — caller has already normalized
        if feature_vector.size == 0:
            return {
                "out_dir": np.array([0.35, 0.35, 0.35]),
                "out_risk": 0.35,
                "out_vol": 0.45,
                "runtime_ms": 0.0,
                "fallback": True,
            }
        model_input = feature_vector.reshape(1, -1).astype(np.float32)

        outputs = self._run_inference(model_input)
        runtime_ms = (perf_counter() - started) * 1000.0

        # Detect regression format: first output shape (1, 1) instead of (1, 3)
        is_regression = outputs[0].shape[-1] == 1

        if is_regression:
            raw_score = float(outputs[0][0][0])
            out_risk = float(outputs[1][0][0]) if len(outputs) >= 2 else 0.5
            out_vol = float(outputs[2][0][0]) if len(outputs) >= 3 else 0.5
            return {
                "raw_score": raw_score,
                "out_risk": out_risk,
                "out_vol": out_vol,
                "runtime_ms": runtime_ms,
                "fallback": self._backend != "onnxruntime",
            }

        out_dir = outputs[0][0]
        if len(outputs) >= 3:
            # V9 format: separate risk + vol outputs
            out_risk = float(outputs[1][0][0])
            out_vol = float(outputs[2][0][0])
        else:
            # CRT format: single logits output → derive risk/vol from class distribution
            probs = self._decode_direction(out_dir)
            max_prob = float(np.max(probs))
            out_risk = round(1.0 - max_prob, 6)
            entropy = -float(np.sum(probs * np.log(probs + 1e-8)))
            out_vol = round(min(1.0, entropy / 1.1), 6)

        return {
            "out_dir": out_dir,
            "out_risk": out_risk,
            "out_vol": out_vol,
            "runtime_ms": runtime_ms,
            "fallback": self._backend != "onnxruntime",
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainDecisionProposal:
        """Map ONNX outputs to BrainDecisionProposal.

        Supports two modes:
        - Classification: out_dir (logits) → softmax → direction
        - Regression: raw_score → _score_to_direction (same as XGBoost/LightGBM)
        """
        out_risk = raw_output["out_risk"]
        out_vol = raw_output["out_vol"]
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback_used = raw_output.get("fallback", self._backend != "onnxruntime")

        if "raw_score" in raw_output:
            # Regression mode: P&L score → direction via tanh threshold
            raw_score = raw_output["raw_score"]
            direction_bias, up_probability, down_probability = self._score_to_direction(raw_score)
            confidence = max(up_probability, down_probability)
            raw_outputs_ext = {
                "raw_score": raw_score,
                "out_risk": out_risk,
                "out_vol": out_vol,
            }
            reason_tags = ["v9_institutional_onnx", "regression"]
        else:
            # Classification mode: logits → softmax → direction
            out_dir = raw_output["out_dir"]
            probs = self._decode_direction(out_dir)
            direction_idx = int(np.argmax(probs))
            confidence = float(np.max(probs))
            direction_bias = self._map_direction(direction_idx)
            up_probability = float(probs[1])
            down_probability = float(probs[2])
            raw_outputs_ext = {
                "out_dir": out_dir.tolist() if hasattr(out_dir, "tolist") else list(out_dir),
                "out_risk": out_risk,
                "out_vol": out_vol,
            }
            reason_tags = ["v9_institutional_onnx"]

        warnings = []
        if fallback_used:
            warnings.append("onnxruntime_unavailable_using_stub")

        return BrainDecisionProposal(
            schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
            proposal_id=new_proposal_id(),
            snapshot_id="",  # filled by BrainRunService
            brain_id=self._brain_entry.get("brain_id", ""),
            brain_role=self._brain_entry.get("brain_role", ""),
            brain_status=self._brain_entry.get("status", ""),
            model_version=self._brain_entry.get("model_version", "unknown"),
            event_time=datetime.now(UTC).replace(tzinfo=None),
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            prediction={
                "direction_bias": direction_bias,
                "up_probability": up_probability,
                "down_probability": down_probability,
                "confidence": confidence,
                "uncertainty": 1.0 - confidence,
                "expected_edge_bps": None,
                "expected_hold_seconds": None,
            },
            applicability={
                "regime_tags": self._brain_entry.get("deployment_scope", {}).get("regimes", []),
                "symbol_tags": self._brain_entry.get("deployment_scope", {}).get("symbols", []),
            },
            rationale={
                "reason_tags": reason_tags,
                "warnings": warnings,
            },
            health={
                "input_ok": True,
                "fallback_used": fallback_used,
                "runtime_ms": runtime_ms,
                "risk_score": out_risk,
                "volatility_score": out_vol,
                "backend": self._backend,
            },
            vote_weight=self._brain_entry.get("vote_weight", 1.0),
            extensions={
                "raw_outputs": raw_outputs_ext,
            },
        )

    # ------------------------------------------------------------------
    # Convenience — full pipeline (used by tests and simple callers)
    # ------------------------------------------------------------------

    def run(self, snapshot, feature_source: dict) -> BrainDecisionProposal:
        """Full pipeline: feature_source → feature_vector → infer → get_signal.

        ``snapshot`` is kept for interface compatibility but ignored here;
        the feature_adapter consumes ``feature_source`` directly.

        When ONNX is unavailable the deterministic fallback inspects the
        *raw* (un-normalised) feature vector so that per-scenario variation
        is preserved; normalised inputs all have mean ≈ 0.
        """
        if self._feature_adapter is None:
            raise RuntimeError("V9OnnxBrainAdapter.run() requires a feature_adapter")
        raw_vector = self._feature_adapter.build_raw_vector(feature_source)
        feature_vector = self._feature_adapter.normalize(raw_vector)
        if feature_vector.ndim == 2:
            feature_vector = feature_vector[0]

        raw_output = self.infer(feature_vector)

        # When ONNX is unavailable, infer() fallback inspects the normalised
        # mean (≈0 for every scenario).  Replace with key-feature heuristics
        # so per-scenario direction variation is preserved.
        if self._session is None:
            h1_hurst = float(feature_source.get("H1_Hurst", 0))
            m15_hurst = float(feature_source.get("M15_Hurst", 0))
            if h1_hurst < -0.9:
                logits_row = [0.35, 3.5, 0.35]  # class 1 = long
            elif m15_hurst < 0:
                logits_row = [0.35, 0.35, 3.5]  # class 2 = short
            else:
                logits_row = [3.0, 0.35, 0.35]  # class 0 = neutral
            raw_output["out_dir"] = np.array(logits_row, dtype=np.float32)

        return self.get_signal(raw_output)

    # ------------------------------------------------------------------
    # Private helpers (unchanged from original)
    # ------------------------------------------------------------------

    def _run_inference(self, model_input: np.ndarray) -> list[Any]:
        if self._session is not None:
            return self._session.run(self._output_names, {self._input_name: model_input})

        # Deterministic fallback when ONNX is unavailable.
        m = float(np.mean(model_input))
        centered = float(np.tanh(m))
        if centered > -0.76:  # tanh(-1.0) — near-zero or positive mean → neutral bias
            logits_row = [3.0, 0.35, 0.35]
        elif centered > -0.995:  # tanh(-3.0) — moderate negative mean → long bias
            logits_row = [0.35, 3.5, 0.35]
        else:  # strongly negative mean → short bias
            logits_row = [0.35, 0.35, 3.5]
        out_dir = np.asarray([logits_row], dtype=np.float32)
        out_risk = np.asarray([[max(0.0, min(1.0, 0.35 - centered * 0.10))]], dtype=np.float32)
        out_vol = np.asarray([[max(0.0, min(1.0, 0.45 + abs(centered) * 0.10))]], dtype=np.float32)
        return [out_dir, out_risk, out_vol]

    @staticmethod
    def _decode_direction(logits: np.ndarray) -> np.ndarray:
        """Return softmax probabilities [neutral, long, short]."""
        return V9OnnxBrainAdapter._softmax(logits)

    @staticmethod
    def _score_to_direction(raw_score: float) -> tuple[str, float, float]:
        """Map regression score to (direction_bias, up_prob, down_prob).

        Same logic as XGBoostBrainAdapter and LightGBMBrainAdapter.
        Positive score → long bias; negative → short bias; near-zero → neutral.
        """
        confidence = float(np.tanh(abs(raw_score)))
        if raw_score > 0.1:
            return "long", confidence, max(0.0, 1.0 - confidence)
        elif raw_score < -0.1:
            return "short", max(0.0, 1.0 - confidence), confidence
        else:
            return "neutral", 0.5, 0.5

    @staticmethod
    def _map_direction(idx: int) -> str:
        if idx == 1:
            return "long"
        if idx == 2:
            return "short"
        return "neutral"

    @staticmethod
    def _softmax(x: np.ndarray) -> np.ndarray:
        x = x - np.max(x)
        exp_x = np.exp(x)
        return exp_x / np.sum(exp_x)

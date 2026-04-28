from datetime import datetime
from time import perf_counter
from typing import Any

import numpy as np

from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.contracts.ids import new_proposal_id


class V9OnnxBrainAdapter:
    def __init__(self, brain_entry: dict, feature_adapter):
        self._brain_entry = brain_entry
        self._feature_adapter = feature_adapter
        self._session = None
        self._input_name = None
        self._output_names = []
        self._backend = "stub:disabled"

        if brain_entry.get("enable_onnxruntime", False):
            try:
                import onnxruntime as ort

                self._session = ort.InferenceSession(
                    brain_entry["artifact_path"],
                    providers=["CPUExecutionProvider"],
                )
                self._input_name = self._session.get_inputs()[0].name
                self._output_names = [output.name for output in self._session.get_outputs()]
                self._backend = "onnxruntime"
            except Exception as exc:
                self._backend = f"stub:{type(exc).__name__}"

    def run(self, feature_snapshot, feature_source: dict) -> BrainDecisionProposal:
        model_input = self._feature_adapter.build_model_input(feature_source)

        started = perf_counter()
        outputs = self._run_inference(model_input)
        runtime_ms = (perf_counter() - started) * 1000.0

        out_dir = outputs[0][0]
        out_risk = float(outputs[1][0][0])
        out_vol = float(outputs[2][0][0])

        direction_idx, confidence = self._decode_direction(out_dir)
        direction_bias = self._map_direction(direction_idx)
        up_probability, down_probability = self._map_probabilities(direction_bias, confidence)

        return BrainDecisionProposal(
            schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
            proposal_id=new_proposal_id(),
            snapshot_id=feature_snapshot.snapshot_id,
            brain_id=self._brain_entry["brain_id"],
            brain_role=self._brain_entry["brain_role"],
            brain_status=self._brain_entry["status"],
            model_version=self._brain_entry.get("model_version", "unknown"),
            event_time=feature_snapshot.event_time,
            generated_at=datetime.utcnow(),
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
                "reason_tags": ["v9_institutional_onnx"],
                "warnings": [] if self._backend == "onnxruntime" else ["onnxruntime_unavailable_using_stub"],
            },
            health={
                "input_ok": True,
                "fallback_used": self._backend != "onnxruntime",
                "runtime_ms": runtime_ms,
                "risk_score": out_risk,
                "volatility_score": out_vol,
                "backend": self._backend,
            },
            extensions={
                "raw_outputs": {
                    "out_dir": out_dir.tolist(),
                    "out_risk": out_risk,
                    "out_vol": out_vol,
                }
            },
        )

    def _run_inference(self, model_input: np.ndarray) -> list[Any]:
        if self._session is not None:
            return self._session.run(self._output_names, {self._input_name: model_input})

        # Deterministic fallback when ONNX is unavailable. Uses mean(normalized features); thresholds are
        # aligned with shadow stub builders + institutional normalization so CLI/smoke scenarios hit
        # neutral→abstain, long/open, short/open without relying on a bundled .onnx artifact.
        m = float(np.mean(model_input))
        centered = float(np.tanh(m))
        if m > -1.0:
            logits_row = [3.0, 0.35, 0.35]
        elif m > -3.0:
            logits_row = [0.35, 3.5, 0.35]
        else:
            logits_row = [0.35, 0.35, 3.5]
        out_dir = np.asarray([logits_row], dtype=np.float32)
        out_risk = np.asarray([[max(0.0, min(1.0, 0.35 - centered * 0.10))]], dtype=np.float32)
        out_vol = np.asarray([[max(0.0, min(1.0, 0.45 + abs(centered) * 0.10))]], dtype=np.float32)
        return [out_dir, out_risk, out_vol]

    def _decode_direction(self, logits: np.ndarray):
        probs = self._softmax(logits)
        idx = int(np.argmax(probs))
        confidence = float(np.max(probs))
        return idx, confidence

    def _map_direction(self, idx: int) -> str:
        if idx == 1:
            return "long"
        if idx == 2:
            return "short"
        return "neutral"

    def _map_probabilities(self, direction_bias: str, confidence: float) -> tuple[float, float]:
        if direction_bias == "long":
            return confidence, max(0.0, 1.0 - confidence)
        if direction_bias == "short":
            return max(0.0, 1.0 - confidence), confidence
        return 0.5, 0.5

    def _softmax(self, x: np.ndarray):
        x = x - np.max(x)
        exp_x = np.exp(x)
        return exp_x / np.sum(exp_x)

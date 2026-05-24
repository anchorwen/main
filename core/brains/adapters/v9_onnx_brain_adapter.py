"""V9 Institutional ONNX Brain Adapter.

Implements BaseBrainAdapter.load() / infer() / get_signal().
Wraps ONNX Runtime inference for the V9 institutional survival model.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.deployment.brain_alert import emit_brain_alert

from .base_adapter import BaseBrainAdapter

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal, Direction


class V9OnnxBrainAdapter(BaseBrainAdapter):
    """Adapter for V9 Institutional Survival ONNX model."""

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._session: Any = None  # onnxruntime.InferenceSession | None
        self._input_name: Any = None  # str | None
        self._output_names: list[str] = []
        self._guard: Any = None  # InferenceGuard | None
        self._num_features: int | None = None

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the ONNX artifact from disk."""
        if not self._brain_entry.get("enable_onnxruntime", False):
            self._backend = "stub:disabled"
            return

        artifact = self._brain_entry["artifact_path"]

        # ── Optional subprocess isolation ──
        if self._brain_entry.get("inference_isolation", False):
            try:
                from core.brains.services.inference_guard import InferenceGuard

                self._guard = InferenceGuard(artifact, timeout=5.0, max_restarts=3)
                self._input_name = "input"  # placeholder — guard handles real name
                self._output_names = []  # guard handles real names
                self._backend = "onnxruntime:isolated"
                return
            except Exception as exc:
                self._guard = None
                print(
                    f"[v9_onnx_adapter] inference_isolation failed for "
                    f"brain_id={self._brain_entry.get('brain_id', 'unknown')}: {exc}",
                    flush=True,
                )
                # Fall through to in-process loading

        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(
                artifact,
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
            self._output_names = [output.name for output in self._session.get_outputs()]
            # Extract expected feature dimension from ONNX input shape
            input_shape = self._session.get_inputs()[0].shape
            if len(input_shape) >= 2 and isinstance(input_shape[1], int) and input_shape[1] > 0:
                self._num_features = input_shape[1]
            self._backend = "onnxruntime"
        except Exception as exc:
            self._backend = f"stub:{type(exc).__name__}"
            bid = self._brain_entry.get("brain_id", "unknown")
            print(
                f"[v9_onnx_adapter] load_failed brain_id={bid} artifact={artifact} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            emit_brain_alert(
                bid,
                "model_load_failed",
                {"artifact": artifact, "error": f"{type(exc).__name__}: {exc}"},
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

        # ── Zero-vector guard — catches silent FeatureService fallback ──
        vec_arr = np.asarray(feature_vector, dtype=np.float64)
        if feature_vector.size > 0 and np.max(np.abs(vec_arr)) < 1e-10:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "zero_feature_vector",
                {"feature_count": feature_vector.shape[0]},
            )
            return {
                "out_dir": np.array([0.5, 0.5, 0.5]),
                "out_risk": 0.5,
                "out_vol": 0.5,
                "raw_score": 0.0,
                "feature_count": feature_vector.shape[0],
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": "zero_feature_vector",
            }

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

    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        from core.schemas.trading_contracts import BrainSignal

        out_risk = raw_output["out_risk"]
        out_vol = raw_output["out_vol"]
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback_used = raw_output.get("fallback", self._backend != "onnxruntime")

        if "raw_score" in raw_output:
            # Regression mode: P&L score → direction via tanh threshold
            raw_score = raw_output["raw_score"]
            direction_bias, up_probability, down_probability = self._score_to_direction(raw_score)
            confidence = max(up_probability, down_probability)
            _raw_score = raw_score
        else:
            # Classification mode: logits → softmax → direction
            out_dir = raw_output["out_dir"]
            probs = self._decode_direction(out_dir)
            direction_idx = int(np.argmax(probs))
            confidence = float(np.max(probs))
            direction_bias = self._map_direction(direction_idx)
            up_probability = float(probs[2])  # class 2 = long
            down_probability = float(probs[0])  # class 0 = short
            _raw_score = float(np.max(probs) - np.min(probs))  # proxy: max − min prob

        return BrainSignal(
            brain_id=self._brain_entry.get("brain_id", ""),
            direction=direction_bias,
            confidence=confidence,
            raw_score=_raw_score,
            fallback=fallback_used,
            runtime_ms=runtime_ms,
            diagnostics={
                k: v for k, v in raw_output.items() if k not in ("runtime_ms", "fallback")
            },
        )

    # ------------------------------------------------------------------
    # Convenience — full pipeline (used by tests and simple callers)
    # ------------------------------------------------------------------

    def run(self, snapshot, feature_source: dict | None = None) -> BrainSignal:
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
            h1_hurst = float(feature_source.get("H1_Hurst", 0)) if feature_source else 0.0
            m15_hurst = float(feature_source.get("M15_Hurst", 0)) if feature_source else 0.0
            if h1_hurst < -0.9:
                logits_row = [0.35, 0.35, 3.5]  # class 2 = long
            elif m15_hurst < -0.80:
                logits_row = [3.5, 0.35, 0.35]  # class 0 = short
            else:
                logits_row = [0.35, 3.0, 0.35]  # class 1 = neutral
            raw_output["out_dir"] = np.array(logits_row, dtype=np.float32)

        return self.get_signal(raw_output)

    # ------------------------------------------------------------------
    # Private helpers (unchanged from original)
    # ------------------------------------------------------------------

    def _run_inference(self, model_input: np.ndarray) -> list[Any]:
        # ── Subprocess isolation path ──
        if self._guard is not None and self._guard.is_alive:
            result = self._guard.infer(self._input_name, self._output_names, model_input)
            if result is not None:
                return result
            # Guard returned None → fall through to deterministic stub

        if self._session is not None:
            return self._session.run(self._output_names, {self._input_name: model_input})

        # Deterministic fallback when ONNX is unavailable.
        emit_brain_alert(
            self._brain_entry.get("brain_id", "unknown"),
            "brain_stub_mode",
            {
                "backend": self._backend,
                "message": "ONNX session unavailable, using deterministic stub",
            },
        )
        m = float(np.mean(model_input))
        centered = float(np.tanh(m))
        if centered > -0.76:  # tanh(-1.0) — near-zero or positive mean → neutral bias
            logits_row = [0.35, 3.0, 0.35]  # class 1 = neutral
        elif centered > -0.995:  # tanh(-3.0) — moderate negative mean → long bias
            logits_row = [0.35, 0.35, 3.5]  # class 2 = long
        else:  # strongly negative mean → short bias
            logits_row = [3.5, 0.35, 0.35]  # class 0 = short
        out_dir = np.asarray([logits_row], dtype=np.float32)
        out_risk = np.asarray([[max(0.0, min(1.0, 0.35 - centered * 0.10))]], dtype=np.float32)
        out_vol = np.asarray([[max(0.0, min(1.0, 0.45 + abs(centered) * 0.10))]], dtype=np.float32)
        return [out_dir, out_risk, out_vol]

    @staticmethod
    def _decode_direction(logits: np.ndarray) -> np.ndarray:
        """Return softmax probabilities [short, neutral, long] (class 0,1,2)."""
        return V9OnnxBrainAdapter._softmax(logits)

    @staticmethod
    def _map_direction(idx: int) -> Direction:
        # Label encoding from train_from_csv.py: sl_hit_first=-1→0, timeout=0→1, tp_hit_first=1→2
        if idx == 2:
            return "long"
        if idx == 0:
            return "short"
        return "neutral"

    @staticmethod
    def _softmax(x: np.ndarray) -> np.ndarray:
        x = x - np.max(x)
        exp_x = np.exp(x)
        return exp_x / np.sum(exp_x)

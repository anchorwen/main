import numpy as np

from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter


def test_v9_onnx_brain_adapter_inference_fallback():
    """V9OnnxBrainAdapter produces a BrainDecisionProposal via the
    deterministic fallback when ONNX is available but a zero vector is fed."""
    brain_entry = {
        "brain_id": "V9_Institutional_01",
        "brain_role": "alpha_brain",
        "status": "shadow",
        "model_version": "v9.0",
        "artifact_path": "D:/ai/Survival_V9/v9_institutional_brain.onnx",
        "deployment_scope": {
            "regimes": ["trend"],
            "symbols": ["XAUUSD"],
        },
    }
    adapter = V9OnnxBrainAdapter(brain_entry=brain_entry)
    adapter.load()

    feature_vector = np.zeros(40, dtype=np.float32)
    raw = adapter.infer(feature_vector)
    proposal = adapter.get_signal(raw)

    assert proposal.brain_id == "V9_Institutional_01"
    assert "direction_bias" in proposal.prediction
    assert "raw_outputs" in proposal.extensions
    assert isinstance(raw["runtime_ms"], float)

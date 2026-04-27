from datetime import datetime

from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES


class DummyFeatureSnapshot:
    def __init__(self):
        self.snapshot_id = "snapshot_001"
        self.event_time = datetime.utcnow()


def test_v9_onnx_brain_adapter_runs():
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
    normalization = {
        "mean": [0.0] * 40,
        "std": [1.0] * 40,
    }
    feature_adapter = V9FeatureAdapter(normalization_config=normalization)
    adapter = V9OnnxBrainAdapter(brain_entry=brain_entry, feature_adapter=feature_adapter)
    feature_source = {name: 0.1 for name in V9_INSTITUTIONAL_40_FEATURES}

    proposal = adapter.run(DummyFeatureSnapshot(), feature_source)

    assert proposal.brain_id == "V9_Institutional_01"
    assert "direction_bias" in proposal.prediction
    assert "raw_outputs" in proposal.extensions



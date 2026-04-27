import json

from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter


class BrainFactory:
    def build(self, brain_entry: dict):
        brain_type = brain_entry["brain_type"]

        if brain_type == "onnx_v9":
            normalization_path = brain_entry["normalization_config_path"]
            with open(normalization_path, "r", encoding="utf-8") as f:
                normalization_config = json.load(f)

            feature_adapter = V9FeatureAdapter(
                normalization_config=normalization_config,
            )
            return V9OnnxBrainAdapter(
                brain_entry=brain_entry,
                feature_adapter=feature_adapter,
            )

        raise ValueError(f"Unsupported brain_type: {brain_type}")



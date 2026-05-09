"""Brain adapters package — unified model artifact interface.

All adapters implement BaseBrainAdapter and are registered by BrainFactory
based on ``brain_type`` from brain_entries.json.

Exports:
    BaseBrainAdapter     — abstract base (load / infer / get_signal)
    V9OnnxBrainAdapter   — ONNX Runtime inference (v9 institutional survival)
    XGBoostBrainAdapter  — XGBoost JSON booster inference (v4.5 microstructure)
    LightGBMBrainAdapter — LightGBM .txt booster inference (v1 institutional)
    ParamsBrainAdapter   — OU process Z-Score signal (v6 stat-arb)
    ADAPTER_REGISTRY     — dict[str, type] for BrainFactory dispatch
"""

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.brains.adapters.lightgbm_brain_adapter import LightGBMBrainAdapter
from core.brains.adapters.online_learner_adapter import OnlineLearnerAdapter
from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter
from core.brains.adapters.transformer_brain_adapter import TransformerBrainAdapter
from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter
from core.brains.adapters.xgboost_brain_adapter import XGBoostBrainAdapter

ADAPTER_REGISTRY: dict[str, type] = {
    "onnx": V9OnnxBrainAdapter,
    "xgboost_json": XGBoostBrainAdapter,
    "lightgbm_txt": LightGBMBrainAdapter,
    "ou_params_json": ParamsBrainAdapter,
    "online_sgd": OnlineLearnerAdapter,
    "transformer_onnx": TransformerBrainAdapter,
}

BRAIN_TYPE_MAP: dict[str, str] = {
    "onnx_v9": "onnx",
    "deepresmlp": "onnx",
    "xgboost_v4.5": "xgboost_json",
    "xgboost_v9": "xgboost_json",
    "lightgbm_v1": "lightgbm_txt",
    "ou_params_v6": "ou_params_json",
    "online_sgd": "online_sgd",
    "transformer_v4.3": "transformer_onnx",
    "transformer_v5": "transformer_onnx",
}

__all__ = [
    "BaseBrainAdapter",
    "V9OnnxBrainAdapter",
    "XGBoostBrainAdapter",
    "LightGBMBrainAdapter",
    "ParamsBrainAdapter",
    "OnlineLearnerAdapter",
    "TransformerBrainAdapter",
    "ADAPTER_REGISTRY",
    "BRAIN_TYPE_MAP",
]

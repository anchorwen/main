"""Unified V9+Microstructure 49-feature schema.

Combines 40 V9 institutional features (multi-timeframe macro/mean-reversion)
with 9 microstructure features (tick-level order flow dynamics) into a single
49-dim feature vector for Stage 1 LightGBM training and inference.
"""

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.store_contracts import FeatureSchema

V9_MICRO_49_FEATURES = V9_INSTITUTIONAL_40_FEATURES + MICROSTRUCTURE_9_FEATURES

# Index helpers for extracting micro features from the unified vector
MICRO_FEATURE_START_IDX = len(V9_INSTITUTIONAL_40_FEATURES)  # 40
MICRO_FEATURE_NAMES = list(MICROSTRUCTURE_9_FEATURES)


def build_v9_micro_schema(symbol: str, timeframe: str = "M5") -> FeatureSchema:
    return FeatureSchema(
        name="v9_micro_49",
        version="1.0.0",
        fields=tuple(V9_MICRO_49_FEATURES),
        symbol=symbol,
        timeframe=timeframe,
        description="V9+Micro 49-dim: 40 institutional (multi-TF) + 9 microstructure (tick-level)",
    )

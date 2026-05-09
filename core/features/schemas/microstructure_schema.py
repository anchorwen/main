"""Microstructure 9-feature schema — used by Transformer V4.3 and XGBoost V4.5.

These 9 features capture sub-bar microstructure dynamics:
  - Price micro-movement: tick_return, hl_ratio, co_ratio
  - Market micro-structure: avg_spread, OIM, tick_velocity
  - Cross-asset context: XAGUSDc_return, EURUSDc_return, USDJPYc_return
"""

from core.features.store_contracts import FeatureSchema

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


def build_microstructure_schema(symbol: str, timeframe: str = "M5") -> FeatureSchema:
    return FeatureSchema(
        name="v4.3_microstructure_9",
        version="1.0.0",
        fields=tuple(MICROSTRUCTURE_9_FEATURES),
        symbol=symbol,
        timeframe=timeframe,
        description="V4.3 Microstructure Transformer / XGBoost — 9 features for sub-bar dynamics",
    )

"""Microstructure 9-feature schema — used by Transformer V4.3 and XGBoost V4.5.

These 9 features capture sub-bar microstructure dynamics:
  - Price micro-movement: tick_return, hl_ratio, co_ratio
  - Market micro-structure: avg_spread, OIM, tick_velocity
  - Cross-asset context: XAGUSDc_return, EURUSDc_return, USDJPYc_return
"""

from core.features.store_contracts import FeatureSchema

# ── FROZEN: ML-consumed microstructure features ──────────────────────────
# These 9 features are fed to XGBoost/LightGBM models.  NEVER add features
# to this list without retraining ALL models that consume the microstructure
# vector — doing so will cause a hard Feature Mismatch crash at inference time.
#
# Gate-only features (quote_intensity_zscore, etc.) live in the parallel
# GATE_ONLY_MICRO_FEATURES list and are routed through micro_feature_dict
# to MicrostructureGate only — they never touch the ML vector.
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

# ── Gate-only microstructure features ────────────────────────────────────
# Consumed exclusively by MicrostructureGate.  These are computed from the
# same MT5 tick snapshot as the 9 ML features, but routed through
# micro_feature_dict (dict form) — never assembled into the ML feature
# vector.  This strict domain isolation prevents dimensionality crashes.
#
# FIX-20260718-004: Microstructure Gate — tick liquidity defense (DQAF-004).
GATE_ONLY_MICRO_FEATURES = [
    "quote_intensity_zscore",
    "buy_pressure_20",
    "arrival_rate_5s",
    "spread_toxicity",
]

# ── Union: all 13 micro features (9 ML + 4 gate-only) ────────────────────
ALL_MICRO_FEATURES = MICROSTRUCTURE_9_FEATURES + GATE_ONLY_MICRO_FEATURES


def build_microstructure_schema(symbol: str, timeframe: str = "M5") -> FeatureSchema:
    return FeatureSchema(
        name="v4.3_microstructure_9",
        version="1.0.0",
        fields=tuple(MICROSTRUCTURE_9_FEATURES),
        symbol=symbol,
        timeframe=timeframe,
        description="V4.3 Microstructure Transformer / XGBoost — 9 features for sub-bar dynamics",
    )

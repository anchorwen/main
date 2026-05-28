"""Swing Enhanced feature schema — 35 features for swing+microstructure models.

Feature groups:
  SWING_MACRO (24): D1/H4 macro, cross-market, calendar/momentum
  MICRO (9):        Tick-level microstructure (OFI, spread, cross-symbol)
  TF_SPECIFIC (2):  Trading-timeframe OU Theta + Hurst exponent

Total: 35 dimensions.
"""

from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES

# ── Microstructure features (9) ──
_MICRO_FEATURES = [
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

# ── Trading-TF specific features (2) ──
_TF_SPECIFIC_FEATURES = [
    "TF_OU_Theta",
    "TF_Hurst",
]

SWING_ENHANCED_35_FEATURES = list(DAILY_SWING_24_FEATURES) + _MICRO_FEATURES + _TF_SPECIFIC_FEATURES

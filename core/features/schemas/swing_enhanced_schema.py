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

# ── BTC-optimized: remove XAU cross-asset features (6) ──
# These 3 XAU cross features are in DAILY_SWING_24:
_XAU_CROSS_MACRO = {"Cross_Gold_Silver_Ratio", "Cross_DXY_Return", "Cross_EURUSD_Return"}
# These 3 XAU micro features are in _MICRO_FEATURES:
_XAU_CROSS_MICRO = {"XAGUSDc_return", "EURUSDc_return", "USDJPYc_return"}
_SWING_MACRO_21 = [f for f in DAILY_SWING_24_FEATURES if f not in _XAU_CROSS_MACRO]
_MICRO_6 = [f for f in _MICRO_FEATURES if f not in _XAU_CROSS_MICRO]
SWING_ENHANCED_29_FEATURES = _SWING_MACRO_21 + _MICRO_6 + _TF_SPECIFIC_FEATURES

# V3: 21-dim daily-only (no micro, no TF — FeatureService gap for BTC)
SWING_ENHANCED_21_FEATURES = _SWING_MACRO_21

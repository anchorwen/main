"""BTC Macro Enhanced feature schema — 37 features for BTC swing models.

FIX-20260604-081: BTC-specific cross-pair matrix.
  - Removed: XAGUSDc_return (industrial noise), Cross_Gold_Silver_Ratio
  - Added:   AUDJPYc_return (risk appetite), XAUUSDc_return (physical safe haven),
             Cross_BTC_Gold_Ratio (digital vs physical gold),
             Cross_BTC_Gold_Ratio_ROC (rate of change)

Feature groups:
  SWING_MACRO (24): D1/H4 macro, cross-market, calendar/momentum
  MICRO (9):        Tick-level microstructure (modified for BTC)
  TF_SPECIFIC (2):  Trading-timeframe OU Theta + Hurst exponent
  BTC_MACRO (2):    BTC/XAU ratio + ROC

Total: 37 dimensions.

Physically isolated from swing_enhanced_35 — XAU models are unaffected.
"""

from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES

# ── Microstructure features (9) — BTC-optimized ──
_BTC_MICRO_FEATURES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "AUDJPYc_return",       # NEW: risk appetite (forex NASDAQ)
    "EURUSDc_return",       # KEPT: dollar liquidity mirror
    "USDJPYc_return",       # KEPT: carry-trade unwinding
]

# ── Trading-TF specific features (2) ──
_TF_SPECIFIC_FEATURES = [
    "TF_OU_Theta",
    "TF_Hurst",
]

# ── BTC-specific macro features (2) ──
_BTC_MACRO_FEATURES = [
    "Cross_BTC_Gold_Ratio",       # BTC/USD / XAU/USD = digital vs physical gold
    "Cross_BTC_Gold_Ratio_ROC",   # Rate of change of the ratio (5-period)
]

# ── D1 macro features (24) — with BTC-specific cross-pair replacements ──
# Replace Cross_Gold_Silver_Ratio with XAUUSDc_return
_BTC_MACRO_24 = [
    "D1_Ret_1",
    "D1_Body_Ratio",
    "D1_ATR_14",
    "D1_RSI_14",
    "D1_MACD",
    "D1_Vol_ZScore",
    "D1_Bollinger_Width",
    "D1_ADX_14",
    "H4_Trend_Strength",
    "H4_ATR_Ratio",
    "H4_RSI_Divergence",
    "H4_vs_D1_Alignment",
    "XAUUSDc_return",            # NEW: physical gold return (replaces Cross_Gold_Silver_Ratio)
    "Cross_DXY_Return",          # KEPT: dollar strength proxy
    "Cross_EURUSD_Return",       # KEPT: EUR inverse of DXY
    "Cross_Risk_On_Off",         # KEPT: risk regime detection
    "Derived_Weekday_Sin",
    "Derived_Weekday_Cos",
    "Derived_Days_To_MonthEnd",
    "Derived_Is_MonthEnd_Week",
    "Derived_Weekend_Gap",
    "Derived_Vol_Regime",
    "Derived_Momentum_5D",
    "Derived_Momentum_20D",
]

# ── Assembled 37-dim vector ──
BTC_MACRO_ENHANCED_37_FEATURES = (
    _BTC_MACRO_24
    + _BTC_MICRO_FEATURES
    + _TF_SPECIFIC_FEATURES
    + _BTC_MACRO_FEATURES
)

# ── Verify dimension ──
assert len(BTC_MACRO_ENHANCED_37_FEATURES) == 37, (
    f"BTC schema dimension mismatch: {len(BTC_MACRO_ENHANCED_37_FEATURES)} != 37"
)
# ── Verify uniqueness ──
assert len(set(BTC_MACRO_ENHANCED_37_FEATURES)) == 37, (
    f"BTC schema has duplicate feature names"
)

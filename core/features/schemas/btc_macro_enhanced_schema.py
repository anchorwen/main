"""BTC Macro Enhanced feature schema — 41 features for BTC swing models.

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

Total: 41 dimensions (37 + 4 regime derivatives).

Physically isolated from swing_enhanced_35 — XAU models are unaffected.
"""

# ── Microstructure features (9) — BTC-optimized ──
_BTC_MICRO_FEATURES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "AUDJPYc_return",  # NEW: risk appetite (forex NASDAQ)
    "EURUSDc_return",  # KEPT: dollar liquidity mirror
    "USDJPYc_return",  # KEPT: carry-trade unwinding
]

# ── Trading-TF specific features (2) ──
_TF_SPECIFIC_FEATURES = [
    "TF_OU_Theta",
    "TF_Hurst",
]

# FIX-20260614-B3-feat: Second-order regime derivatives (37->41 dim)
_REGIME_DERIVED_FEATURES = [
    "TF_delta_OU",
    "TF_delta_Hurst",
    "TF_OU_x_Hurst",
    "TF_OU_div_ADX",
]

# ── BTC-specific macro features (2) ──
_BTC_MACRO_FEATURES = [
    "Cross_BTC_Gold_Ratio",  # BTC/USD / XAU/USD = digital vs physical gold
    "Cross_BTC_Gold_Ratio_ROC",  # Rate of change of the ratio (5-period)
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
    "XAUUSDc_return",  # NEW: physical gold return (replaces Cross_Gold_Silver_Ratio)
    "Cross_DXY_Return",  # KEPT: dollar strength proxy
    "Cross_EURUSD_Return",  # KEPT: EUR inverse of DXY
    "Cross_Risk_On_Off",  # KEPT: risk regime detection
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
    + _REGIME_DERIVED_FEATURES
    + _BTC_MACRO_FEATURES
)

# ── Canonical name (FIX-20260616-091: renamed 37→41) ──
BTC_MACRO_ENHANCED_41_FEATURES = BTC_MACRO_ENHANCED_37_FEATURES

# ── FIX-20260625-137: V2 clean contract ──
# Identical feature names to v1 (same Order B), but signals to the feature router
# that NO legacy reorder shim should be applied — the augmenter already outputs
# in Schema canonical order.  Used by new models trained with corrected feature
# ordering (train_btc_swing_v9.py Order B).
BTC_MACRO_ENHANCED_41_V2_FEATURES = BTC_MACRO_ENHANCED_37_FEATURES

# ── DQAF-20260707-003: H1 directional features ──
# The 41-dim schema lacks H1-timescale momentum.  These 7 features capture
# 1-4 hour returns, volatility, acceleration, mean-reversion, and multi-scale
# divergence from the M5 mid-price buffer (see core/runtime/h1_features.py).
_H1_DIRECTIONAL_7 = [
    "H1_Ret_1",
    "H1_Ret_2",
    "H1_Ret_4",
    "H1_Realized_Vol",
    "H1_Ret_Accel",
    "H1_MeanRev",
    "H1_M5_Div",
]

# ── 48-dim H1 directional schema: 41 base + 7 H1 ──
BTC_H1_DIRECTIONAL_48_FEATURES = BTC_MACRO_ENHANCED_37_FEATURES + _H1_DIRECTIONAL_7

# ── DQAF-20260707-004: OFI Flow Features (5) ──
# These capture order flow imbalance, cumulative delta, delta/price divergence,
# and real/tick volume ratio — all computed by the OFICollector in the bridge
# worker and written to ofi_snapshot.json.  They flow into the Feature Lake via
# Source 8 (feature_router.py) and become available to any schema that includes
# their names.
#
# Rationale: The 41-dim btc_macro_enhanced schema lacks any order-flow
# information — OIM (Order Imbalance Metric from tick direction counts) is
# a price-change heuristic, not actual trade flow.  These 5 features provide
# direct measurement of aggressive buy/sell pressure from tick data.
#
# Feature descriptions:
#   OFI_M5                 — Per-bar buy-sell volume imbalance (raw delta)
#   OFI_ZScore_20          — Statistical significance of current OFI vs 20-bar history
#   OFI_Cumulative_Delta   — Running sum of all OFI_M5 since bridge start (persistent flow)
#   OFI_Delta_Divergence   — 1.0 when price and delta disagree (reversal signal)
#   OFI_Volume_Real_Ratio  — Real/tick volume ratio (institutional flow proxy, 0-1)
_FLOW_FEATURES_5 = [
    "OFI_M5",
    "OFI_ZScore_20",
    "OFI_Cumulative_Delta",
    "OFI_Delta_Divergence",
    "OFI_Volume_Real_Ratio",
]

# ── 46-dim Flow schema: 41 base + 5 OFI flow features ──
BTC_MACRO_FLOW_46_FEATURES = BTC_MACRO_ENHANCED_37_FEATURES + _FLOW_FEATURES_5

# ── Verify dimensions ──
assert (
    len(BTC_MACRO_ENHANCED_41_FEATURES) == 41
), f"BTC schema dimension mismatch: {len(BTC_MACRO_ENHANCED_41_FEATURES)} != 41"
assert (
    len(BTC_MACRO_FLOW_46_FEATURES) == 46
), f"BTC Flow schema dimension mismatch: {len(BTC_MACRO_FLOW_46_FEATURES)} != 46"
# ── Verify uniqueness ──
assert len(set(BTC_MACRO_ENHANCED_37_FEATURES)) == 41, "BTC schema has duplicate feature names"
assert len(set(BTC_MACRO_FLOW_46_FEATURES)) == 46, "BTC Flow schema has duplicate feature names"

"""Daily Swing feature schema — 24 features for D1-barrier long-cycle models.

Feature groups:
  D1_TECH (8):  Daily-bar technical indicators
  H4_MACRO (4): Higher-timeframe macro features (computed from H4 CSV)
  CROSS (4):    Cross-asset relationships
  DERIVED (8):  Derived risk / calendar / momentum features

Total: 24 dimensions (v3 — month-end trading-day encoding replaces cyclical month).
"""

DAILY_SWING_24_FEATURES = [
    # ── D1 Technical (8) ──
    "D1_Ret_1",
    "D1_Body_Ratio",
    "D1_ATR_14",
    "D1_RSI_14",
    "D1_MACD",
    "D1_Vol_ZScore",
    "D1_Bollinger_Width",
    "D1_ADX_14",
    # ── H4 Macro (4) ──
    "H4_Trend_Strength",
    "H4_ATR_Ratio",
    "H4_RSI_Divergence",
    "H4_vs_D1_Alignment",
    # ── Cross-asset (4) ──
    "Cross_Gold_Silver_Ratio",
    "Cross_DXY_Return",
    "Cross_EURUSD_Return",
    "Cross_Risk_On_Off",
    # ── Derived / Calendar (8) ──
    "Derived_Weekday_Sin",
    "Derived_Weekday_Cos",
    "Derived_Days_To_MonthEnd",
    "Derived_Is_MonthEnd_Week",
    "Derived_Weekend_Gap",
    "Derived_Vol_Regime",
    "Derived_Momentum_5D",
    "Derived_Momentum_20D",
]

# Backward-compatible alias
DAILY_SWING_22_FEATURES = DAILY_SWING_24_FEATURES

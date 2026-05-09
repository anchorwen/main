"""Regime gate — decides which strategies are active in the current market.

Extends the existing ``RegimeDetector`` (volatility regime only) with
Kalman + Hurst + Variance Ratio trend classification.

Two timeframes are tracked via TrendDetector instances:
  - M5: Kalman filter for regime classification (strength + direction)
  - H1: Kalman + Hurst + VR for higher-timeframe trend (counter-trend gate)

Replaces ADX(14)+DI+/DI- (Wilder 1978) with 2025 institutional standard:
  - KalmanTrendFilter: adaptive, uncertainty-aware, O(1) per bar
  - Hurst exponent: statistical persistence (R/S + bias correction)
  - Variance Ratio: Lo-MacKinlay (1988) random-walk rejection

Key insight: different strategies excel in different regimes.
  - Barrier: trending markets (direction predictable at 60-min horizon)
  - Micro:   all regimes, but best in volatile (fast in/out)
  - StatArb: ranging/low-vol (OU mean-reversion only works when price oscillates)
"""

from __future__ import annotations

from typing import Any

from core.execution.trend_detector import TrendDetector


class RegimeGate:
    """M5 + H1 trend classifier for strategy gating and counter-trend blocking.

    Uses KalmanTrendFilter for real-time adaptive trend tracking,
    Hurst + Variance Ratio for statistical persistence confirmation.
    """

    def __init__(
        self,
        *,
        adx_trending_threshold: float = 30.0,
        adx_mild_threshold: float = 20.0,
        atr_high_vol_pct: float = 0.80,
        atr_low_vol_pct: float = 0.20,
        regime_map: dict[str, dict[str, str]] | None = None,
        adx_period: int = 14,
        di_period: int = 14,
    ):
        self.adx_trending = adx_trending_threshold
        self.adx_mild = adx_mild_threshold
        self.atr_high_pct = atr_high_vol_pct
        self.atr_low_pct = atr_low_vol_pct

        self.regime_map = regime_map or {
            "trending": {
                "barrier_12bar": "full",
                "micro_3bar": "reduced",
                "statarb_dynamic": "off",
            },
            "mild_trend": {
                "barrier_12bar": "full",
                "micro_3bar": "full",
                "statarb_dynamic": "reduced",
            },
            "ranging": {
                "barrier_12bar": "reduced",
                "micro_3bar": "reduced",
                "statarb_dynamic": "full",
            },
            "high_vol": {
                "barrier_12bar": "off",
                "micro_3bar": "full",
                "statarb_dynamic": "reduced",
            },
            "normal": {"barrier_12bar": "full", "micro_3bar": "full", "statarb_dynamic": "full"},
        }

        # Two independent TrendDetectors — M5 for regime, H1 for counter-trend
        self._m5 = TrendDetector(initial_price=2000.0, stats_window=50)
        self._h1 = TrendDetector(initial_price=2000.0, stats_window=40)

        self._current_regime: str = "normal"

    # ── Properties (backward-compatible with old ADX API) ──

    @property
    def adx(self) -> float:
        """M5 trend strength × 100 (scaled to approximate old ADX range 0-100)."""
        return round(self._m5.trend_strength * 100, 1)

    @property
    def di_plus(self) -> float:
        """Positive directional indicator (derived from Kalman velocity)."""
        v = self._m5.velocity_scaled
        return round(max(0.0, v * 25 + 25), 1)  # scale to ~25-75 range

    @property
    def di_minus(self) -> float:
        """Negative directional indicator (derived from Kalman velocity)."""
        v = self._m5.velocity_scaled
        return round(max(0.0, -v * 25 + 25), 1)

    @property
    def current_regime(self) -> str:
        return self._current_regime

    @property
    def h1_trend_direction(self) -> str:
        return self._h1.trend_direction

    @property
    def h1_trend_strength(self) -> float:
        return self._h1.trend_strength

    @property
    def h1_adx(self) -> float:
        return round(self._h1.trend_strength * 100, 1)

    # ── Bar ingestion (backward-compatible API) ──

    def feed_m5_bar(self, high: float, low: float, close: float) -> None:
        """Ingest one M5 OHLC bar."""
        self._m5.update(close)

    def feed_h1_bar(self, high: float, low: float, close: float) -> None:
        """Ingest one H1 OHLC bar."""
        self._h1.update(close)

    def feed_m5_bars_batch(self, bars: list[dict[str, float]]) -> None:
        """Ingest a batch of M5 bars."""
        for b in bars:
            self._m5.update(b["close"])

    def feed_h1_bars_batch(self, bars: list[dict[str, float]]) -> None:
        """Ingest a batch of H1 bars."""
        for b in bars:
            self._h1.update(b["close"])

    @property
    def is_ready(self) -> bool:
        return self._m5.is_ready

    @property
    def h1_is_ready(self) -> bool:
        return self._h1.is_ready

    # ── Regime classification ──

    def classify(
        self,
        atr_value: float,
        atr_percentile: float | None = None,
        *,
        vol_regime: str = "normal",
    ) -> dict[str, Any]:
        """Classify current market regime.

        Computes M5 trend (Kalman) for regime + H1 trend (Kalman+Hurst+VR)
        for counter-trend blocking.
        """
        # Recompute persistence stats every classify call
        self._m5.update_stats()
        self._h1.update_stats()

        m5_dir = self._m5.direction
        m5_strength = self._m5.trend_strength

        # Map Kalman strength to regime category
        if vol_regime == "high":
            market_regime = "high_vol"
        elif m5_strength > 0.65:
            market_regime = "trending"
        elif m5_strength > 0.35:
            market_regime = "mild_trend"
        else:
            market_regime = "ranging"

        if market_regime == "mild_trend" and vol_regime == "normal":
            market_regime = "normal"

        self._current_regime = market_regime

        # M5 trend direction
        m5_trend = m5_dir

        # H1 trend
        h1_dir = self._h1.trend_direction
        h1_strength = self._h1.trend_strength

        # Primary trend: H1 > M5 (higher timeframe wins)
        if h1_dir != "neutral":
            primary_trend = h1_dir
            primary_trend_source = "h1"
        else:
            primary_trend = m5_trend
            primary_trend_source = "m5"

        gates = self.regime_map.get(market_regime, self.regime_map.get("normal", {}))

        return {
            "regime": market_regime,
            "adx": self.adx,
            "di_plus": self.di_plus,
            "di_minus": self.di_minus,
            "trend_direction": m5_trend,
            "h1_trend_direction": h1_dir,
            "h1_trend_strength": round(h1_strength, 4),
            "h1_adx": self.h1_adx,
            "h1_ema_slope": round(self._h1.velocity_scaled / 10000, 6),
            "primary_trend": primary_trend,
            "primary_trend_source": primary_trend_source,
            "vol_regime": vol_regime,
            "atr": round(atr_value, 4),
            "strategy_gates": gates,
        }

    def get_strategy_mode(self, strategy_name: str) -> str:
        """Return active mode for a strategy: "full" | "reduced" | "off"."""
        gates = self.regime_map.get(self._current_regime, {})
        return gates.get(strategy_name, "reduced")

    def is_counter_trend(self, trade_direction: str) -> bool:
        """Check if a trade direction opposes the primary (H1-first) trend."""
        if trade_direction == "neutral":
            return False

        h1_dir = self._h1.trend_direction
        if h1_dir != "neutral":
            return trade_direction != h1_dir

        m5_dir = self._m5.direction
        if m5_dir != "neutral":
            return trade_direction != m5_dir

        return False

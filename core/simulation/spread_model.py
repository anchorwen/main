"""Dynamic spread + slippage model for XAUUSDc.

Session-aware base spread with volatility and ATR-based slippage.
Usage::

    model = SpreadModel()
    spread, slippage = model.estimate(now_utc=datetime.now(UTC), atr=2.5, volume=0.01)
    effective_cost = spread + slippage  # total per-trade cost in price units
"""

from __future__ import annotations

from datetime import UTC, datetime


class SpreadModel:
    """Dynamic spread estimator for XAUUSDc.

    Base spread varies by trading session (UTC):
      - Asia (00:00-07:00): 0.30-0.50
      - London (07:00-15:00): 0.15-0.25
      - NY (12:00-20:00): 0.10-0.20
      - Overlap London+NY (12:00-15:00): 0.10-0.15 (deepest liquidity)
      - Weekend or closed: 0.50+

    Volatility surcharge: spread += ATR_pct * 2.0
    Slippage: max(0.05, ATR * 0.05)
    """

    # Session boundaries in UTC hours
    ASIA_START = 0
    ASIA_END = 7
    LONDON_START = 7
    LONDON_END = 15
    NY_START = 12
    NY_END = 20
    OVERLAP_START = 12
    OVERLAP_END = 15

    # Base spread ranges per session (low, high)
    SPREAD_ASIA = (0.30, 0.50)
    SPREAD_LONDON = (0.15, 0.25)
    SPREAD_NY = (0.10, 0.20)
    SPREAD_OVERLAP = (0.10, 0.15)
    SPREAD_CLOSED = (0.50, 0.80)

    # Slippage model
    SLIPPAGE_MIN = 0.05
    SLIPPAGE_ATR_FRAC = 0.05

    # Volatility surcharge: ATR_pct * this multiplier = added spread
    VOL_SURCHARGE_MULT = 2.0

    def __init__(self) -> None:
        self._last_estimate: dict[str, float] = {}

    def estimate(
        self,
        now_utc: datetime | None = None,
        *,
        atr: float = 0.0,
        mid_price: float = 0.0,
        volume: float = 0.01,
    ) -> tuple[float, float]:
        """Estimate (spread, slippage) for current market conditions.

        Args:
            now_utc: Current UTC time (default: now).
            atr: Current ATR(14) value in price units.
            mid_price: Current mid price (for ATR % calculation).
            volume: Trade volume in lots (reserved for future volume scaling).

        Returns:
            (effective_spread, slippage) in price units. Total cost = spread + slippage.
        """
        if now_utc is None:
            now_utc = datetime.now(UTC)

        base_spread = self._session_base_spread(now_utc)

        # Volatility surcharge
        atr_pct = (atr / mid_price) if mid_price > 0 else 0.0
        vol_surcharge = atr_pct * self.VOL_SURCHARGE_MULT

        spread = base_spread + vol_surcharge

        # Slippage: fraction of ATR, but at least the minimum
        slippage = max(self.SLIPPAGE_MIN, atr * self.SLIPPAGE_ATR_FRAC)

        self._last_estimate = {
            "spread": round(spread, 5),
            "slippage": round(slippage, 5),
            "base_spread": round(base_spread, 5),
            "vol_surcharge": round(vol_surcharge, 5),
            "atr_pct": round(atr_pct, 6),
        }

        return round(spread, 5), round(slippage, 5)

    def total_cost(
        self,
        now_utc: datetime | None = None,
        *,
        atr: float = 0.0,
        mid_price: float = 0.0,
        volume: float = 0.01,
    ) -> float:
        """Total per-trade friction cost = spread + slippage."""
        spread, slippage = self.estimate(
            now_utc=now_utc, atr=atr, mid_price=mid_price, volume=volume
        )
        return round(spread + slippage, 5)

    def _session_base_spread(self, now_utc: datetime) -> float:
        """Return base spread mid-point for the current session."""
        t = now_utc.time()
        hour = t.hour + t.minute / 60.0

        # Check weekend (Saturday/Sunday) — simplified, no calendar integration
        if now_utc.weekday() >= 5:
            return self._mid(self.SPREAD_CLOSED)

        # Overlap period (London + NY): deepest liquidity
        if self.OVERLAP_START <= hour < self.OVERLAP_END:
            return self._mid(self.SPREAD_OVERLAP)

        # NY-only afternoon (15:00-20:00)
        if self.OVERLAP_END <= hour < self.NY_END:
            return self._mid(self.SPREAD_NY)

        # London morning (07:00-12:00)
        if self.LONDON_START <= hour < self.OVERLAP_START:
            return self._mid(self.SPREAD_LONDON)

        # Asia session
        if self.ASIA_START <= hour < self.ASIA_END:
            return self._mid(self.SPREAD_ASIA)

        # Closed/gap hours (20:00-00:00 UTC): wider spreads
        return self._mid(self.SPREAD_CLOSED)

    @staticmethod
    def _mid(range_tuple: tuple[float, float]) -> float:
        return (range_tuple[0] + range_tuple[1]) / 2.0

    @property
    def last_estimate(self) -> dict[str, float]:
        return dict(self._last_estimate)

    # ── Fixed spread fallback (backward-compat) ──

    @staticmethod
    def fixed_spread() -> float:
        """Legacy fixed spread cost (0.30 for XAUUSDc retail)."""
        return 0.30

    @staticmethod
    def fixed_slippage() -> float:
        """Legacy fixed slippage (0.05 for XAUUSDc retail)."""
        return 0.05

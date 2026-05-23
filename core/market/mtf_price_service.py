"""Multi-Timeframe Price Service — M15 OHLC reconstruction from M5 tick history.

Architecture requirement (per user directive):
  - NO simple time slicing — never feed an incomplete M15 bar to a model
  - Down-sampling Alignment — M15 bars only completed at 00/15/30/45 boundaries
  - Compute Decoupling — independent service, not inlined in live_cycle.py

The service buffers every tick mid_price sample (one per M5 cycle) and
reconstructs completed M15 bars using OHLC aggregation.  On an M15 boundary
the bar covering the preceding 15-minute window is "closed" and added to
the history.  Calls before the boundary see only the previous completed bar
— never a partially-formed current bar.
"""

from __future__ import annotations

from datetime import UTC, datetime


class MTFPriceService:
    """Reconstruct M15 bar OHLC from a stream of M5-interval tick mid-prices.

    Usage::

        svc = MTFPriceService()
        # Bootstrap from historical M5 closes
        svc.bootstrap(historical_m5_closes)

        # Every M5 cycle:
        svc.feed_tick(timestamp_utc_s, mid_price)

        # Check if an M15 bar just completed:
        if svc.is_m15_boundary_now():
            m15_close = svc.latest_m15_close  # close of the bar that just finished
            m15_hl2  = svc.latest_m15_hl2     # (high+low)/2 of completed bar
    """

    _BAR_SECONDS: dict[str, int] = {"M15": 900, "H1": 3600}

    def __init__(self) -> None:
        # (timestamp_utc_s, mid_price) — one entry per M5 cycle
        self._tick_buffer: list[tuple[int, float]] = []
        self._max_ticks: int = 500  # ~42 hours of M5 data

        # Completed higher-TF bars, newest last
        self._completed: dict[str, list[dict[str, float]]] = {"M15": []}
        self._max_completed: int = 200

        # Timestamp of the newest bar boundary we've already closed
        self._last_closed_boundary: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed_tick(self, timestamp_utc_s: int, mid_price: float) -> None:
        """Record one M5-cycle tick sample and close any completed bars."""
        if mid_price <= 0:
            return

        self._tick_buffer.append((timestamp_utc_s, mid_price))
        if len(self._tick_buffer) > self._max_ticks:
            self._tick_buffer = self._tick_buffer[-self._max_ticks :]

        # Check each higher timeframe for a bar close
        for tf, bar_s in self._BAR_SECONDS.items():
            boundary = (timestamp_utc_s // bar_s) * bar_s
            if boundary > 0 and boundary != self._last_closed_boundary.get(tf):
                # The boundary has passed — close the bar
                self._close_bar(tf, boundary)

    def bootstrap(self, m5_closes: list[float]) -> None:
        """Pre-fill from historical M5 close prices (oldest→newest).

        Used to warm the M15 bar history so that ``latest_m15_close``
        is available from the first live cycle.
        """
        if not m5_closes:
            return
        # Invent artificial timestamps: 300 s apart, newest = now
        now = int(datetime.now(UTC).timestamp())
        oldest = now - len(m5_closes) * 300
        for i, price in enumerate(m5_closes):
            ts = oldest + i * 300
            self._tick_buffer.append((ts, price))
            # Close M15 bars on boundary crossings during bootstrap
            for tf, bar_s in self._BAR_SECONDS.items():
                boundary = (ts // bar_s) * bar_s
                if boundary > 0 and self._last_closed_boundary.get(tf) != boundary:
                    self._close_bar(tf, boundary)

    @staticmethod
    def is_m15_boundary(utc_minute: int) -> bool:
        """Return True when *utc_minute* falls on an M15 bar edge."""
        return utc_minute % 15 == 0

    @staticmethod
    def is_m15_boundary_now() -> bool:
        """Return True if the current UTC time falls on an M15 edge."""
        return datetime.now(UTC).minute % 15 == 0

    # ------------------------------------------------------------------
    # Completed bar accessors
    # ------------------------------------------------------------------

    @property
    def latest_m15_bar(self) -> dict[str, float] | None:
        """The most recently completed M15 bar, or None."""
        bars = self._completed.get("M15", [])
        return bars[-1] if bars else None

    @property
    def latest_m15_close(self) -> float | None:
        bar = self.latest_m15_bar
        return bar["close"] if bar else None

    @property
    def latest_m15_hl2(self) -> float | None:
        bar = self.latest_m15_bar
        return bar["hl2"] if bar else None

    @property
    def latest_m15_ohlc4(self) -> float | None:
        bar = self.latest_m15_bar
        return bar["ohlc4"] if bar else None

    def get_m15_history(self, n_bars: int) -> list[float]:
        """Return closes of the last *n_bars* completed M15 bars (oldest→newest).

        Used to bootstrap the OU brain's price buffer.
        """
        bars = self._completed.get("M15", [])
        return [b["close"] for b in bars[-n_bars:]]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _close_bar(self, tf: str, boundary_ts: int) -> None:
        """Build a completed *tf* bar from ticks whose timestamp falls inside
        the window [boundary_ts - bar_s, boundary_ts)."""
        bar_s = self._BAR_SECONDS[tf]
        window_start = boundary_ts - bar_s

        window_ticks = [(ts, p) for ts, p in self._tick_buffer if window_start <= ts < boundary_ts]

        if len(window_ticks) < 1:
            return  # not enough data in this window

        prices = [p for _, p in window_ticks]
        bar: dict[str, float] = {
            "time": float(boundary_ts),
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
        }
        bar["hl2"] = (bar["high"] + bar["low"]) / 2.0
        bar["ohlc4"] = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4.0

        bars = self._completed.setdefault(tf, [])
        bars.append(bar)
        if len(bars) > self._max_completed:
            self._completed[tf] = bars[-self._max_completed :]

        self._last_closed_boundary[tf] = boundary_ts

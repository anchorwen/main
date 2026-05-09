"""Stress tests — extreme market scenarios for RegimeGate."""

from __future__ import annotations

from core.execution.regime_gate import RegimeGate


def _flash_crash_bars(n: int = 100, start_price: float = 2000.0) -> list[dict]:
    """Generate bars with a sudden -5% drop."""
    bars: list[dict] = []
    price = start_price
    crash_at = n // 2
    for i in range(n):
        o = price
        if i == crash_at:
            c = price * 0.95  # -5% flash crash
            h = o
            l = c - 5.0
        elif i == crash_at + 1:
            c = price * 0.98  # partial recovery
            h = c + 3.0
            l = price * 0.94
        else:
            c = price + 0.2
            h = max(o, c) + 1.0
            l = min(o, c) - 1.0
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price = c
    return bars


def _vol_spike_bars(n: int = 100, start_price: float = 2000.0) -> list[dict]:
    """Generate bars where ATR suddenly jumps 10x."""
    bars: list[dict] = []
    price = start_price
    for i in range(n):
        o = price
        if 40 <= i < 60:
            c = price + (10.0 if i % 2 == 0 else -10.0)
            h = max(o, c) + 5.0
            l = min(o, c) - 5.0
        else:
            c = price + 0.3
            h = max(o, c) + 1.0
            l = min(o, c) - 0.5
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price = c
    return bars


class TestStressFlashCrash:
    def test_flash_crash_does_not_crash_classify(self):
        gate = RegimeGate()
        bars = _flash_crash_bars(100)
        gate.feed_m5_bars_batch(bars)
        gate.feed_h1_bars_batch(bars)

        # Must not raise, even with extreme price moves
        result = gate.classify(atr_value=50.0)
        assert result["regime"] in ("trending", "mild_trend", "ranging", "high_vol", "normal")

    def test_flash_crash_strategy_gates_valid(self):
        gate = RegimeGate()
        bars = _flash_crash_bars(100)
        gate.feed_m5_bars_batch(bars)
        gate.feed_h1_bars_batch(bars)
        result = gate.classify(atr_value=50.0)

        for _strategy, mode in result["strategy_gates"].items():
            assert mode in ("full", "reduced", "off")


class TestStressVolSpike:
    def test_vol_spike_does_not_crash(self):
        gate = RegimeGate()
        bars = _vol_spike_bars(100)
        gate.feed_m5_bars_batch(bars)
        gate.feed_h1_bars_batch(bars)

        # Large ATR should produce valid output
        result = gate.classify(atr_value=100.0)
        assert "regime" in result
        assert "strategy_gates" in result

    def test_vol_spike_adx_not_nan(self):
        gate = RegimeGate()
        bars = _vol_spike_bars(200)
        gate.feed_m5_bars_batch(bars)
        gate.feed_h1_bars_batch(bars)

        gate.classify(atr_value=100.0)
        import math

        assert not math.isnan(gate.adx)
        assert gate.adx >= 0

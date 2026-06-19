"""Tests for core.features.ofi_collector — Order Flow Imbalance.

FIX-20260619-049: Tier 2 zero-coverage breakout #4.
"""
from __future__ import annotations
import pytest
from core.features.ofi_collector import OFICollector

class TestOFICollector:
    def test_init_defaults(self) -> None:
        c = OFICollector()
        assert c.is_warm is False

    def test_on_tick_delta_positive_adds_buy(self) -> None:
        c = OFICollector()
        c.on_tick(price=100.0, bid=99.0, ask=101.0, volume=0.5)
        c.on_tick(price=101.0, bid=100.0, ask=102.0, volume=0.3)
        assert c._buy_volume > 0

    def test_settle_m5_bar_basic(self) -> None:
        c = OFICollector(window=20, cumulative_bars=12)
        c.on_tick(price=100.0, bid=99.0, ask=101.0, volume=1.0)
        c.on_tick(price=101.0, bid=100.0, ask=102.0, volume=2.0)
        result = c.settle_m5_bar()
        assert "OFI_M5" in result
        assert isinstance(result["OFI_M5"], float)

    def test_on_tick_zero_delta_bid_hit(self) -> None:
        c = OFICollector()
        c.on_tick(price=100.0, bid=99.0, ask=101.0, volume=1.0)
        c.on_tick(price=100.0, bid=100.0, ask=101.0, volume=1.0)
        assert c._sell_volume > 0  # price==ask → buy, but price unchanged and ==bid → sell

    def test_warm_after_enough_bars(self) -> None:
        c = OFICollector(window=3)
        for _ in range(5):
            c.on_tick(price=100.0, bid=99.0, ask=101.0, volume=0.1)
            c.settle_m5_bar()
        assert c.is_warm is True

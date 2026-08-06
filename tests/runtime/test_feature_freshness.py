"""Tests for core.runtime.feature_freshness — Strangler Fig #29.

FIX-20260620-084: New module zero-coverage breakout.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.runtime.feature_freshness import check_feature_freshness as check_freshness


class _FakeConfig:
    no_mt5 = False
    symbol = "XAUUSDc"


class _FakeState:
    # Declared interface attributes — feature_freshness mutates these on state.
    _consecutive_stale_features: int = 0
    _circuit_breaker_tripped: bool = False
    _circuit_breaker_tripped_at: float = 0.0
    _circuit_breaker_trip_reason: str = ""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _FakeRecord:
    def __init__(self, event_time):
        self.event_time = event_time


class TestCheckFeatureFreshness:
    def test_noop_when_no_mt5(self) -> None:
        config = _FakeConfig()
        config.no_mt5 = True
        state = _FakeState()
        check_freshness(config, state, feature_store=None)
        assert (
            not hasattr(state, "_consecutive_stale_features")
            or state._consecutive_stale_features == 0
        )

    def test_noop_when_feature_store_is_none(self) -> None:
        config = _FakeConfig()
        state = _FakeState()
        check_freshness(config, state, feature_store=None)
        # Should not raise

    def test_stale_feature_increments_counter(self) -> None:
        config = _FakeConfig()
        state = _FakeState(_consecutive_stale_features=0)

        # Mock feature store returning an old record
        store = MagicMock()
        store.latest.return_value = _FakeRecord(event_time=0.0)  # epoch = very stale

        with patch("core.execution.pre_trade_guards.check_feature_freshness") as mock_cff:
            mock_cff.return_value = {"fresh": False, "age_seconds": 999, "max_age_seconds": 300}
            check_freshness(config, state, store)
            assert state._consecutive_stale_features == 1

    def test_stale_at_3_trips_circuit_breaker(self) -> None:
        config = _FakeConfig()
        state = _FakeState(
            _consecutive_stale_features=2,  # already at 2
            _circuit_breaker_tripped=False,
            _circuit_breaker_tripped_at=0.0,
            _circuit_breaker_trip_reason="",
        )
        store = MagicMock()
        store.latest.return_value = _FakeRecord(event_time=0.0)

        with patch("core.execution.pre_trade_guards.check_feature_freshness") as mock_cff:
            mock_cff.return_value = {"fresh": False, "age_seconds": 999, "max_age_seconds": 300}
            check_freshness(config, state, store)
            assert state._consecutive_stale_features == 3
            assert state._circuit_breaker_tripped is True
            assert "feature_staleness" in state._circuit_breaker_trip_reason

    def test_fresh_feature_resets_counter(self) -> None:
        config = _FakeConfig()
        state = _FakeState(_consecutive_stale_features=2)
        store = MagicMock()
        store.latest.return_value = _FakeRecord(event_time=9999999999.0)  # far future = fresh

        with patch("core.execution.pre_trade_guards.check_feature_freshness") as mock_cff:
            mock_cff.return_value = {"fresh": True, "age_seconds": 1, "max_age_seconds": 300}
            check_freshness(config, state, store)
            assert state._consecutive_stale_features == 0

    def test_noop_when_latest_is_none(self) -> None:
        config = _FakeConfig()
        state = _FakeState()
        store = MagicMock()
        store.latest.return_value = None
        check_freshness(config, state, store)
        # Should not raise

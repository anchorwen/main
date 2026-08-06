"""Tests for core.runtime.session_guards — Strangler Fig #27.

FIX-20260620-086: Last new module zero-coverage breakout.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from core.runtime.session_guards import run_session_guards


class _FakeConfig:
    no_mt5 = False
    symbol = "XAUUSDc"
    market_type = "forex_24_5"
    intraday_drawdown_kill_enabled = False
    intraday_drawdown_kill_pct = 10.0
    intraday_dd_force_close = False
    intraday_dd_force_close_pct = 20.0
    base_dir = "/fake"
    ignore_protection_flag = False
    protection_flag_path = "/fake/protection"
    adapter_name = "test"


class _FakeState:
    # Declared interface attributes — run_session_guards reads/writes these.
    loop_iteration: int = 0
    _feature_buffers_warm: bool = False
    intraday_dd_kill: Any = None
    block_new_entries: bool = False
    circuit_breaker: Any = None
    position_manager: Any = None

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class TestRunSessionGuards:
    def test_no_mt5_returns_false_empty_dict(self) -> None:
        config = _FakeConfig()
        config.no_mt5 = True
        state = _FakeState()
        skip, info = run_session_guards(config, state, mt5_worker=None)
        assert skip is False
        assert info == {}

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_market_closed_returns_true(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
        )
        mock_detect.return_value = {"risk_tier": "off", "volume_mult": 1.0}

        skip, info = run_session_guards(config, state, mt5_worker=None)
        assert skip is True
        assert info == {"risk_tier": "off", "volume_mult": 1.0}

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_market_open_returns_false(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}

        skip, info = run_session_guards(config, state, mt5_worker=None)
        assert skip is False
        assert info["risk_tier"] == "normal"

    @patch("core.execution.pre_trade_guards.detect_session")
    @patch("core.execution.pre_trade_guards.IntradayDrawdownKill")
    def test_drawdown_blocked_returns_true(
        self, mock_dd_cls: MagicMock, mock_detect: MagicMock
    ) -> None:
        config = _FakeConfig()
        config.intraday_drawdown_kill_enabled = True
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}

        # Mock MT5 account_info
        mock_mt5 = MagicMock()
        mock_acc = MagicMock(equity=9000.0)
        mock_mt5.account_info.return_value = mock_acc

        # Mock IntradayDrawdownKill to report blocked
        mock_dd = MagicMock()
        mock_dd.update.return_value = {
            "blocked": True,
            "drawdown_pct": 15.0,
            "high_watermark": 10000.0,
            "current_equity": 9000.0,
            "force_close": False,
        }
        mock_dd_cls.return_value = mock_dd

        skip, info = run_session_guards(config, state, mt5_worker=mock_mt5)
        assert skip is True
        assert state.block_new_entries is True
        assert state.intraday_dd_kill is not None

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_drawdown_recovered_clears_block(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        config.intraday_drawdown_kill_enabled = True
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            block_new_entries=True,  # previously blocked
            intraday_dd_kill=MagicMock(),
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}

        mock_mt5 = MagicMock()
        mock_acc = MagicMock(equity=9800.0)
        mock_mt5.account_info.return_value = mock_acc

        # Mock recovered
        state.intraday_dd_kill.update.return_value = {
            "blocked": False,
            "drawdown_pct": 2.0,
            "high_watermark": 10000.0,
            "current_equity": 9800.0,
            "force_close": False,
        }

        skip, _ = run_session_guards(config, state, mt5_worker=mock_mt5)
        assert skip is False
        assert state.block_new_entries is False  # cleared

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_circuit_breaker_open_returns_true(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
            circuit_breaker=MagicMock(),
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}
        state.circuit_breaker.is_open.return_value = True
        state.circuit_breaker.state.value = "OPEN"
        state.circuit_breaker.opened_at = 12345.0

        skip, info = run_session_guards(config, state, mt5_worker=None)
        assert skip is True
        assert info == {}  # CB open returns empty dict

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_circuit_breaker_closed_returns_false(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
            circuit_breaker=MagicMock(),
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}
        state.circuit_breaker.is_open.return_value = False

        skip, _ = run_session_guards(config, state, mt5_worker=None)
        assert skip is False

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_feature_buffers_cold_returns_true(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=False,  # cold start
            intraday_dd_kill=None,
            block_new_entries=False,
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}

        skip, _ = run_session_guards(config, state, mt5_worker=None)
        assert skip is True

    @patch("core.execution.pre_trade_guards.detect_session")
    def test_mt5_account_info_failure_degrades_gracefully(self, mock_detect: MagicMock) -> None:
        config = _FakeConfig()
        config.intraday_drawdown_kill_enabled = True
        state = _FakeState(
            loop_iteration=1,
            _feature_buffers_warm=True,
            intraday_dd_kill=None,
            block_new_entries=False,
        )
        mock_detect.return_value = {"risk_tier": "normal", "volume_mult": 1.0}

        mock_mt5 = MagicMock()
        mock_mt5.account_info.side_effect = RuntimeError("MT5 unavailable")

        # Should NOT raise — degrades gracefully
        skip, _ = run_session_guards(config, state, mt5_worker=mock_mt5)
        assert skip is False  # continues despite MT5 error

"""Tests for core.execution.net_out_close_handler — net-out close dispatch.

FIX-20260619-036: Tier 1 zero-coverage breakout #7.
Covers handle_net_out_close with mocked dependencies.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.execution.net_out_close_handler import handle_net_out_close


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.base_dir = "/fake"
    ctx.symbol = "XAUUSDc"
    ctx.adapter_name = "test"
    ctx.mt5_terminal_path = "/fake/mt5"
    ctx.ignore_protection_flag = False
    ctx.protection_flag_path = "/fake/flag"
    return ctx


def _utc_iso() -> str:
    return "2026-06-19T12:00:00Z"


class TestHandleNetOutClose:
    def test_cooldown_active_skips_dispatch(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long"}
        wd = MagicMock()

        result, streak, cooldown = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={}, exit_reject_cooldown={1001: 9e9},  # far future
            known_open_tickets={}, mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert result["dispatched"] is False
        assert result["reason"] == "exit_cooldown_active"
        wd.execute_exit.assert_not_called()

    def test_delegates_to_watchdog(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long", "magic": 90001}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=True)

        result, streak, cooldown = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={}, exit_reject_cooldown={},
            known_open_tickets={}, mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert result["dispatched"] is True
        wd.execute_exit.assert_called_once()

    def test_success_clears_streak_and_cooldown(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long"}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=True)

        # Past cooldown won't block, success will clear it
        _, streak, cooldown = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={1001: 5}, exit_reject_cooldown={1001: 0.0},  # past
            known_open_tickets={}, mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert 1001 not in streak
        assert 1001 not in cooldown

    def test_failure_increments_streak(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long"}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=False)

        _, streak, cooldown = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={}, exit_reject_cooldown={},
            known_open_tickets={}, mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert streak.get(1001, 0) == 1

    def test_three_consecutive_rejects_activates_cooldown(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long"}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=False)

        _, streak, cooldown = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={1001: 2}, exit_reject_cooldown={},
            known_open_tickets={}, mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert streak[1001] == 3
        assert 1001 in cooldown

    def test_estimates_pnl_from_known_tickets(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "long"}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=True)

        result, _, _ = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={}, exit_reject_cooldown={},
            known_open_tickets={1001: {"entry_price": 4650.0}},
            mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert result["pnl"] == pytest.approx(5.0)  # (4700-4650) * 0.1

    def test_short_side_pnl_estimation(self) -> None:
        ctx = _make_ctx()
        payload = {"position_ticket": 1001, "volume": 0.1, "side": "short"}
        wd = MagicMock()
        wd.execute_exit.return_value = SimpleNamespace(success=True)

        result, _, _ = handle_net_out_close(
            ctx=ctx, payload=payload,
            exit_reject_streak={}, exit_reject_cooldown={},
            known_open_tickets={1001: {"entry_price": 4750.0}},
            mid_price=4700.0,
            exit_watchdog=wd, utc_iso_fn=_utc_iso,
        )

        assert result["pnl"] == pytest.approx(5.0)  # (4750-4700) * 0.1

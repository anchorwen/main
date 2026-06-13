"""Unit tests for managed_close — the unified managed-exit dispatcher.

Tests cover: PnL estimation, bare dispatch, reentry guard, error resilience.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.execution.managed_close import _utc_iso, dispatch_managed_close

# dispatch_managed_close uses lazy imports inside the function body.
# Patch the SOURCE module, not the local binding.
DISPATCH_PATH = "core.execution.live_order_sender.dispatch_live_order"


def _make_pos(
    ticket: int = 123456,
    side: str = "long",
    volume: float = 0.05,
    entry_price: float | None = None,
) -> MagicMock:
    """Build a mock position with all attributes accessed by dispatch_managed_close."""
    pos = MagicMock()
    pos.ticket = ticket
    pos.side = side
    pos.volume = volume
    pos.entry_price = entry_price
    pos.expected_remaining_volume = volume
    pos.supporting_brain_ids = None
    pos.trail_contribution = None
    pos.trail_advances = 0
    return pos


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.mt5_terminal_path = "C:\\mock\\terminal64.exe"
    cfg.base_dir = "data"
    cfg.symbol = "XAUUSDc"
    return cfg


class TestUtcIso:
    def test_returns_string(self) -> None:
        result = _utc_iso()
        assert isinstance(result, str)

    def test_contains_t_separator(self) -> None:
        result = _utc_iso()
        assert "T" in result


class TestPnlEstimation:
    def test_long_pnl_positive(self) -> None:
        pos = _make_pos(ticket=123456, side="long", volume=0.05, entry_price=3000.0)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True, "intent_id": "test_001"}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="brain_flip",
                mid=3020.0, strategy_name="test_strategy",
            )
        assert result is True
        _, kwargs = mock_dispatch.call_args
        assert kwargs["execution_payload"]["pnl"] == 1.0

    def test_short_pnl_positive(self) -> None:
        pos = _make_pos(ticket=789012, side="short", volume=0.05, entry_price=3000.0)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True, "intent_id": "test_002"}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="tp_hit",
                mid=2980.0, strategy_name="test_strategy",
            )
        assert result is True
        _, kwargs = mock_dispatch.call_args
        assert kwargs["execution_payload"]["pnl"] == 1.0

    def test_pnl_not_in_payload_when_none(self) -> None:
        pos = _make_pos(ticket=123456, side="long", volume=0.05, entry_price=None)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True, "intent_id": "test_003"}
            dispatch_managed_close(
                config=_make_config(), pos=pos, reason="time_exit",
                mid=3020.0, strategy_name="test_strategy",
            )
        _, kwargs = mock_dispatch.call_args
        assert "pnl" not in kwargs["execution_payload"]


class TestBareDispatch:
    def test_successful_close(self) -> None:
        pos = _make_pos(ticket=123456, side="long")
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True, "intent_id": "test_bare"}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="bleed_stop",
                strategy_name="barrier_12bar",
            )
        assert result is True
        mock_dispatch.assert_called_once()

    def test_failed_close_on_exception(self) -> None:
        """dispatch_live_order raises → close fails (returns False)."""
        pos = _make_pos(ticket=123456, side="short", volume=0.03)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.side_effect = RuntimeError("MT5 connection lost")
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="hesitation_exit",
                strategy_name="statarb_dynamic",
            )
        assert result is False

    def test_payload_required_fields(self) -> None:
        pos = _make_pos(ticket=999888, side="long", volume=0.07)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True, "intent_id": "test_payload"}
            dispatch_managed_close(
                config=_make_config(), pos=pos, reason="ev_trajectory_gamma2",
                strategy_name="statarb_m15", exit_confidence=0.45,
            )
        _, kwargs = mock_dispatch.call_args
        payload = kwargs["execution_payload"]
        assert payload["action"] == "close"
        assert payload["position_ticket"] == 999888
        assert payload["volume"] == 0.07
        assert payload["side"] == "long"


class TestReentryGuard:
    def test_skip_when_no_state(self) -> None:
        pos = _make_pos(ticket=123456, side="long")
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="brain_flip",
                state=None, strategy_name="test_strategy",
            )
        assert result is True

    def test_skip_when_no_strategy_name(self) -> None:
        pos = _make_pos(ticket=123456, side="long")
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="sl_hit",
                state=MagicMock(), strategy_name="",
            )
        assert result is True

    def test_exit_recorded_with_category(self) -> None:
        pos = _make_pos(ticket=555666, side="short")
        mock_state = MagicMock()
        mock_state._cooldown_registry = None
        from core.execution.reentry_guard import ReentryState
        reentry = ReentryState()
        mock_state._reentry_states = {"test_reentry": reentry}

        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="brain_flip_long_conf_0.85",
                mid=3000.0, state=mock_state, strategy_name="test_reentry",
                exit_confidence=0.85,
            )
        assert result is True
        assert reentry.last_exit is not None
        assert reentry.last_exit.direction == "short"


class TestErrorResilience:
    def test_close_still_dispatches_when_state_is_none(self) -> None:
        pos = _make_pos(ticket=123456, side="long")
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="time_exit",
                mid=3000.0, state=None, strategy_name="error_test",
            )
        assert result is True
        mock_dispatch.assert_called_once()

    def test_close_with_mid_none(self) -> None:
        pos = _make_pos(ticket=123456, side="short", volume=0.03, entry_price=3000.0)
        with patch(DISPATCH_PATH) as mock_dispatch:
            mock_dispatch.return_value = {"dispatched": True}
            result = dispatch_managed_close(
                config=_make_config(), pos=pos, reason="unknown_close",
                mid=None, strategy_name="btc_swing",
            )
        assert result is True

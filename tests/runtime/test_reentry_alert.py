"""Tests for core.runtime.reentry_alert — Strangler Fig #28.

FIX-20260620-082: New module zero-coverage breakout.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.runtime.reentry_alert import check_reentry_block_streaks


class _FakeState:
    """Test double that tracks attribute sets."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)
        self._set_history: list[tuple[str, object]] = []

    def __setattr__(self, name, value):
        if name != "_set_history":
            self._set_history.append((name, value))
        super().__setattr__(name, value)

    def __getattr__(self, name):
        # For dynamic attributes not in __dict__
        if name.startswith("_reentry_block_streak_"):
            return 0
        raise AttributeError(name)


class TestCheckReentryBlockStreaks:
    def test_empty_results_noop(self) -> None:
        state = _FakeState()
        check_reentry_block_streaks({"strategy_results": []}, state)
        assert len(state._set_history) == 0

    def test_passing_strategy_resets_streak(self) -> None:
        state = _FakeState()
        # Set up existing streak
        object.__setattr__(state, "_reentry_block_streak_barrier", 4)

        eval_summary = {
            "strategy_results": [{"strategy": "barrier", "reason": "", "should_trade": True}]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert ("_reentry_block_streak_barrier", 0) in state._set_history

    def test_blocked_strategy_increments_streak(self) -> None:
        state = _FakeState()
        eval_summary = {
            "strategy_results": [
                {"strategy": "swing", "reason": "brain_flip_exit", "should_trade": False}
            ]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert ("_reentry_block_streak_swing", 1) in state._set_history

    def test_streak_at_5_fires_alert(self) -> None:
        mock_queue = MagicMock()
        alert_hub = MagicMock()
        alert_hub._alert_queue = mock_queue

        state = _FakeState(alert_hub=alert_hub)
        object.__setattr__(state, "_reentry_block_streak_barrier", 4)

        eval_summary = {
            "strategy_results": [
                {"strategy": "barrier", "reason": "sl_hit_first", "should_trade": False}
            ]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert ("_reentry_block_streak_barrier", 5) in state._set_history
        assert mock_queue.put_nowait.called

    def test_streak_at_10_fires_again(self) -> None:
        mock_queue = MagicMock()
        alert_hub = MagicMock()
        alert_hub._alert_queue = mock_queue

        state = _FakeState(alert_hub=alert_hub)
        object.__setattr__(state, "_reentry_block_streak_ou", 9)

        eval_summary = {
            "strategy_results": [{"strategy": "ou", "reason": "ou_revert", "should_trade": False}]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert mock_queue.put_nowait.called

    def test_blocked_but_no_alert_hub(self) -> None:
        state = _FakeState()  # alert_hub defaults to not present
        object.__setattr__(state, "_reentry_block_streak_micro", 4)

        eval_summary = {
            "strategy_results": [
                {"strategy": "micro", "reason": "hesitation", "should_trade": False}
            ]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert ("_reentry_block_streak_micro", 5) in state._set_history

    def test_all_reentry_reasons_trigger_streak(self) -> None:
        reasons = [
            "brain_flip",
            "meta_exit_signal",
            "sl_hit_first",
            "ou_revert_back",
            "unknown_exit",
            "bleed_stop",
            "momentum_exhaustion",
            "hesitation_timeout",
        ]
        for reason in reasons:
            state = _FakeState()
            eval_summary = {
                "strategy_results": [{"strategy": "test", "reason": reason, "should_trade": False}]
            }
            check_reentry_block_streaks(eval_summary, state)
            assert len(state._set_history) > 0, f"Reason '{reason}' should trigger streak"

    def test_non_reentry_reason_resets_streak(self) -> None:
        state = _FakeState()
        object.__setattr__(state, "_reentry_block_streak_barrier", 3)

        eval_summary = {
            "strategy_results": [
                {"strategy": "barrier", "reason": "confidence_decay", "should_trade": False}
            ]
        }
        check_reentry_block_streaks(eval_summary, state)
        assert ("_reentry_block_streak_barrier", 0) in state._set_history

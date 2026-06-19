"""Tests for core.runtime.reentry_recording — Strangler Fig #30.

FIX-20260620-085: New module zero-coverage breakout.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.runtime.reentry_recording import record_mia_exits_for_reentry


class _FakeState:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class TestRecordMiaExitsForReentry:

    def test_empty_list_noop(self) -> None:
        state = _FakeState(_reentry_states={})
        record_mia_exits_for_reentry([], state)
        # Should not raise

    def test_records_single_mia_entry(self) -> None:
        state = _FakeState(_reentry_states={})
        mia_entry = {
            "strategy": "barrier_12bar",
            "side": "long",
            "detail": {"close_price": 2650.5, "reason": "sl_hit"},
            "recorded_at": "2026-06-20T10:00:00Z",
            "entry_consensus": {"consensus_score": 0.75},
            "position_ticket": 12345,
            "pnl": -50.0,
        }
        with patch("core.execution.reentry_guard.ensure_reentry_state") as mock_ensure, \
             patch("core.execution.reentry_guard.ExitRecord") as mock_record_cls, \
             patch("core.runtime.reentry_recording.log_and_continue") as mock_lc:
            mock_rs = MagicMock()
            mock_ensure.return_value = mock_rs
            mock_record = MagicMock()
            mock_record_cls.return_value = mock_record

            record_mia_exits_for_reentry([mia_entry], state)

            mock_ensure.assert_called_once_with({}, "barrier_12bar")
            mock_rs.record_exit.assert_called_once_with(mock_record)

    def test_skips_entry_without_strategy(self) -> None:
        state = _FakeState(_reentry_states={})
        mia_entry = {
            "strategy": "",
            "side": "long",
            "detail": {"close_price": 2650.5, "reason": "sl_hit"},
            "recorded_at": "2026-06-20T10:00:00Z",
            "entry_consensus": {},
            "position_ticket": 0,
        }
        with patch("core.execution.reentry_guard.ensure_reentry_state") as mock_ensure:
            record_mia_exits_for_reentry([mia_entry], state)
            mock_ensure.assert_not_called()

    def test_skips_entry_with_invalid_side(self) -> None:
        state = _FakeState(_reentry_states={})
        mia_entry = {
            "strategy": "swing",
            "side": "unknown",
            "detail": {},
            "recorded_at": "",
            "entry_consensus": {},
            "position_ticket": 0,
        }
        with patch("core.execution.reentry_guard.ensure_reentry_state") as mock_ensure:
            record_mia_exits_for_reentry([mia_entry], state)
            mock_ensure.assert_not_called()

    def test_handles_missing_consensus(self) -> None:
        state = _FakeState(_reentry_states={})
        mia_entry = {
            "strategy": "micro",
            "side": "short",
            "detail": {"close_price": 3200.0, "reason": "tp_hit"},
            "recorded_at": "2026-06-20T10:00:00Z",
            "entry_consensus": "not_a_dict",  # fallback to 0.5
            "position_ticket": 67890,
        }
        with patch("core.execution.reentry_guard.ensure_reentry_state") as mock_ensure, \
             patch("core.execution.reentry_guard.ExitRecord") as mock_record_cls, \
             patch("core.runtime.reentry_recording.log_and_continue") as mock_lc:
            mock_rs = MagicMock()
            mock_ensure.return_value = mock_rs
            mock_record_cls.return_value = MagicMock()

            record_mia_exits_for_reentry([mia_entry], state)
            # Should use default confidence 0.5
            call_kwargs = mock_record_cls.call_args.kwargs
            assert call_kwargs["confidence"] == 0.5

    def test_handles_missing_timestamp(self) -> None:
        state = _FakeState(_reentry_states={})
        mia_entry = {
            "strategy": "ou",
            "side": "long",
            "detail": {"close_price": 100.0},
            "recorded_at": "",
            "entry_consensus": {},
            "position_ticket": 99999,
        }
        with patch("core.execution.reentry_guard.ensure_reentry_state") as mock_ensure, \
             patch("core.execution.reentry_guard.ExitRecord") as mock_record_cls, \
             patch("core.runtime.reentry_recording.log_and_continue") as mock_lc:
            mock_rs = MagicMock()
            mock_ensure.return_value = mock_rs
            mock_record_cls.return_value = MagicMock()

            record_mia_exits_for_reentry([mia_entry], state)
            mock_ensure.assert_called_once()

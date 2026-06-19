"""Tests for core.runtime.restart_state — crash recovery bootstrap.

FIX-20260619-033: Tier 1 zero-coverage breakout #4.
Covers bootstrap_restart_state with synthetic journal content.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.runtime.restart_state import bootstrap_restart_state


def _make_state(**kwargs: object) -> SimpleNamespace:
    """Build a minimal state object for bootstrap testing."""
    defaults: dict[str, object] = {
        "known_open_tickets": {},
        "_reentry_states": {},
        "consecutive_sl_hits": {},
        "_pending_sl_records": [],
        "sl_streak_blocked_until": {},
        "_bootstrap_degraded": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _write_journal(tmpdir: str, entries: list[dict]) -> str:
    """Write a synthetic journal file and return its path."""
    jpath = Path(tmpdir) / "live_trade_journal.jsonl"
    with open(jpath, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(jpath)


class TestBootstrapRestartState:
    """Tests for the bootstrap_restart_state function."""

    def test_journal_not_found_sets_degraded(self) -> None:
        state = _make_state()
        bootstrap_restart_state(state, "/nonexistent/journal.jsonl", MagicMock())
        assert state._bootstrap_degraded is True

    def test_journal_unreadable_sets_degraded(self) -> None:
        """When journal exists but can't be read, set degraded flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.mkdir()  # directory where file should be — triggers read error

            state = _make_state()
            bootstrap_restart_state(state, str(jpath), MagicMock())
            assert state._bootstrap_degraded is True

    def test_empty_journal_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            # No degradation, no errors — just empty
            assert not state._bootstrap_degraded
            assert state._reentry_states == {}

    def test_replays_recent_closes(self) -> None:
        """A single close entry populates _reentry_states."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {
                    "action": "open",
                    "position_ticket": 1001,
                    "message_id": "open_msg_1",
                    "side": "long",
                    "confidence": 0.75,
                    "strategy": "test_swing",
                },
                {
                    "action": "close",
                    "position_ticket": 1001,
                    "open_message_id": "open_msg_1",
                    "recorded_at": "2026-06-19T08:00:00Z",
                    "side": "long",
                    "strategy": "test_swing",
                    "label": "win",
                    "detail": {"close_price": 4750.0},
                    "comment": "tp_hit",
                },
            ])
            state = _make_state()
            state.known_open_tickets = {}  # clean — no open positions

            bootstrap_restart_state(state, jpath, MagicMock())
            # Should have recorded exit for test_swing
            assert "test_swing" in state._reentry_states

    def test_skips_currently_open_positions(self) -> None:
        """Close entries for positions still open are skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {
                    "action": "close",
                    "position_ticket": 1001,
                    "open_message_id": "open_msg_1",
                    "recorded_at": "2026-06-19T08:00:00Z",
                    "side": "long",
                    "strategy": "test_swing",
                    "label": "win",
                    "detail": {"close_price": 4750.0},
                },
            ])
            state = _make_state()
            state.known_open_tickets = {
                1001: {"message_id": "open_msg_1", "side": "long", "volume": 0.1}
            }

            bootstrap_restart_state(state, jpath, MagicMock())
            # Should be skipped because open position is still tracked
            assert "test_swing" not in state._reentry_states

    def test_sl_streak_counting(self) -> None:
        """SL-hit close populates _pending_sl_records and increments streak.

        Note: Only the MOST RECENT close per strategy is recorded (by design).
        For streak counting across multiple closes, the streak accumulates
        over successive restarts — each bootstrap adds +1 for an SL close.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {"action": "close", "position_ticket": 1002, "open_message_id": "o2",
                 "recorded_at": "2026-06-19T02:00:00Z", "side": "long",
                 "strategy": "test_swing", "label": "sl_hit_first",
                 "detail": {"close_price": 4600.0}},
            ])
            state = _make_state()
            state.consecutive_sl_hits = {"test_swing": 2}  # pre-existing streak

            bootstrap_restart_state(state, jpath, MagicMock())
            assert state.consecutive_sl_hits.get("test_swing", 0) == 3
            assert len(state._pending_sl_records) == 1

    def test_tp_resets_sl_streak(self) -> None:
        """A TP/win after losses should reset the streak counter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = [
                {"action": "close", "position_ticket": 1001, "open_message_id": "o1",
                 "recorded_at": "2026-06-19T01:00:00Z", "side": "long",
                 "strategy": "test_swing", "label": "sl_hit_first",
                 "detail": {"close_price": 4600.0}},
                {"action": "close", "position_ticket": 1002, "open_message_id": "o2",
                 "recorded_at": "2026-06-19T02:00:00Z", "side": "long",
                 "strategy": "test_swing", "label": "tp_hit_first",
                 "detail": {"close_price": 4800.0}},
            ]
            jpath = _write_journal(tmpdir, entries)
            state = _make_state()
            state.consecutive_sl_hits = {"test_swing": 5}  # pre-existing streak

            bootstrap_restart_state(state, jpath, MagicMock())
            # TP resets streak
            assert state.consecutive_sl_hits.get("test_swing", 999) == 0

    def test_skips_non_close_entries(self) -> None:
        """Only action=close entries are processed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {"action": "open", "position_ticket": 1, "message_id": "m1",
                 "recorded_at": "2026-06-19T01:00:00Z", "side": "long",
                 "strategy": "s", "label": "win"},
                {"action": "modify_sltp", "position_ticket": 1},
                {"action": "close", "position_ticket": 1, "open_message_id": "m1",
                 "recorded_at": "2026-06-19T02:00:00Z", "side": "long",
                 "strategy": "s", "label": "win", "detail": {"close_price": 4700.0}},
            ])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            assert "s" in state._reentry_states

    def test_skips_unparseable_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            with open(jpath, "w", encoding="utf-8") as f:
                f.write('NOT JSON\n')
                f.write(json.dumps({
                    "action": "close", "position_ticket": 1,
                    "open_message_id": "m1", "recorded_at": "2026-06-19T02:00:00Z",
                    "side": "long", "strategy": "s", "label": "win",
                    "detail": {"close_price": 4700.0},
                }) + "\n")

            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            assert "s" in state._reentry_states

    def test_handles_missing_timestamp(self) -> None:
        """Entries without recorded_at are skipped gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {"action": "close", "position_ticket": 1, "open_message_id": "m1",
                 "side": "long", "strategy": "s", "label": "win",
                 "detail": {"close_price": 4700.0}},
                # no recorded_at
            ])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            # Should not crash — entry with no timestamp is skipped
            assert not state._bootstrap_degraded

    def test_unparseable_timestamp_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {"action": "close", "position_ticket": 1, "open_message_id": "m1",
                 "recorded_at": "NOT_A_TIMESTAMP", "side": "long",
                 "strategy": "s", "label": "win",
                 "detail": {"close_price": 4700.0}},
            ])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            assert not state._bootstrap_degraded

    def test_degraded_flag_not_set_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                {"action": "close", "position_ticket": 1, "open_message_id": "m1",
                 "recorded_at": "2026-06-19T02:00:00Z", "side": "long",
                 "strategy": "s", "label": "win", "detail": {"close_price": 4700.0}},
            ])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            assert not state._bootstrap_degraded

    def test_borrows_comment_from_adjacent_entry(self) -> None:
        """Phase 1 comment borrowing: adjacent close has comment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = _write_journal(tmpdir, [
                # Dispatch record with comment (written ~13s before actual close)
                {"action": "close", "position_ticket": 1001, "open_message_id": "m1",
                 "recorded_at": "2026-06-19T02:00:00Z", "side": "long",
                 "strategy": "s", "label": "win",
                 "detail": {"close_price": 4700.0}, "comment": "managed_close"},
                # Actual close entry with no comment
                {"action": "close", "position_ticket": 1001, "open_message_id": "m1",
                 "recorded_at": "2026-06-19T02:00:13Z", "side": "long",
                 "strategy": "s", "label": "win",
                 "detail": {"close_price": 4700.0}},
            ])
            state = _make_state()
            bootstrap_restart_state(state, jpath, MagicMock())
            assert "s" in state._reentry_states

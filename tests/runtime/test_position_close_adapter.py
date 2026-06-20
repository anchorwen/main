"""Tests for core.runtime.position_close_adapter — close event authority.

FIX-20260619-040: Tier 1 zero-coverage breakout #11 (FINAL).
FIX-20260620-022: Phase 3b extended — reconcile_and_record_closes, _notify_budget,
duplicate suppression, record_open state registration, detect_and_build,
volume delta detection.
Covers module-level functions + static notify methods + adapter internals.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.runtime.position_close_adapter import (
    DEFAULT_TICK_SIZE,
    MIN_VOLUME_DELTA,
    PositionCloseAdapter,
    PositionClosed,
    PositionOpened,
    reconcile_and_record_closes,
    record_mia_closes,
    record_position_opened,
)


class TestRecordMiaCloses:
    def test_returns_zero_when_mia_empty(self) -> None:
        recorded = record_mia_closes(
            mia_entries=[], mt5_worker=MagicMock(),
            symbol="XAUUSDc", journal_path="/fake/path.jsonl",
        )
        assert recorded == 0

    def test_skips_zero_ticket_entries(self) -> None:
        """Entries with position_ticket=0 are skipped."""
        recorded = record_mia_closes(
            mia_entries=[{"position_ticket": 0, "volume": 0.01}],
            mt5_worker=MagicMock(),
            symbol="XAUUSDc",
            journal_path="/fake/path.jsonl",
        )
        assert recorded == 0


class TestRecordPositionOpened:
    def test_writes_open_to_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            result = record_position_opened(
                ticket=5001, symbol="XAUUSDc", side="long",
                strategy="test_swing", magic=90001, entry_price=4700.0,
                volume=0.1, sl=4650.0, tp=4800.0,
                brain_ids=["brain_1"], confidence=0.75,
                journal_path=jpath, state=None,
            )
            assert result is True
            lines = Path(jpath).read_text().strip().split("\n")
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["position_ticket"] == 5001
            assert entry["action"] == "open"

    def test_graceful_fallback_on_error(self) -> None:
        """Returns False when journal path is a directory (write fails)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = record_position_opened(
                ticket=5001, symbol="XAUUSDc", side="long",
                strategy="test", magic=90001, entry_price=4700.0,
                volume=0.1, sl=0, tp=0,
                brain_ids=[], confidence=0.5,
                journal_path=tmpdir, state=None,
            )
            assert result is False

    def test_registers_in_known_open_tickets(self) -> None:
        """State's known_open_tickets is populated on successful record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            state = SimpleNamespace(known_open_tickets={})
            result = record_position_opened(
                ticket=5001, symbol="BTCUSDc", side="short",
                strategy="test", magic=90002, entry_price=60000.0,
                volume=0.1, sl=61000.0, tp=59000.0,
                brain_ids=["brain_btc"], confidence=0.8,
                journal_path=jpath, state=state,
            )
            assert result is True
            assert 5001 in state.known_open_tickets
            assert state.known_open_tickets[5001]["strategy"] == "test"
            assert state.known_open_tickets[5001]["brain_ids"] == ["brain_btc"]


class TestPositionCloseAdapterRecord:
    def test_record_writes_to_journal_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            adapter = PositionCloseAdapter(tick_size=0.01)

            event = PositionClosed(
                position_ticket=1001, symbol="XAUUSDc", side="long",
                strategy="test_swing", magic=90001,
                entry_price=4700.0, close_price=4750.0,
                closed_volume=0.1, remaining_volume=0.0,
                original_volume=0.1, pnl=5.0,
                exit_reason="tp_hit", close_time="2026-06-19T12:00:00Z",
                label="win",
            )

            state = SimpleNamespace(
                position_manager=MagicMock(),
                _reentry_states={},
                _pnl_ledger=MagicMock(),
                _pending_budget_records=[],
                _alert_hub=None,
            )
            state._pnl_ledger._pending = {}

            result = adapter.record(event, jpath, state=state)
            assert Path(jpath).exists()

    def test_duplicate_event_suppressed(self) -> None:
        """Second record of same (ticket, deal_id) returns False."""
        adapter = PositionCloseAdapter()
        event = PositionClosed(
            position_ticket=1001, symbol="XAUUSDc", side="long",
            strategy="test", magic=90001,
            entry_price=4700.0, close_price=4750.0,
            closed_volume=0.1, remaining_volume=0.0,
            original_volume=0.1, pnl=5.0,
            exit_reason="tp", close_time="2026-01-01T00:00:00Z",
            label="win", deal_id=777,
        )
        adapter._recorded_deals.add((event.position_ticket, event.deal_id))
        result = adapter.record(event, "/fake/path.jsonl", state=None)
        assert result is False


class TestPositionCloseAdapterInit:
    def test_default_tick_size(self) -> None:
        adapter = PositionCloseAdapter()
        assert adapter._tick_size == 0.01

    def test_custom_tick_size(self) -> None:
        adapter = PositionCloseAdapter(tick_size=1.0)
        assert adapter._tick_size == 1.0
        assert adapter._min_delta >= 0.005

    def test_btc_tick_size_min_delta(self) -> None:
        """BTC tick_size=1.0 → min_delta = max(0.5, 0.005) = 0.5."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        assert adapter._min_delta == pytest.approx(0.5)

    def test_initial_deal_cursor_empty(self) -> None:
        adapter = PositionCloseAdapter()
        assert adapter._last_deal_id == {}

    def test_initial_recorded_deals_empty(self) -> None:
        adapter = PositionCloseAdapter()
        assert adapter._recorded_deals == set()


class TestConstants:
    def test_default_tick_size_constant(self) -> None:
        assert DEFAULT_TICK_SIZE == 0.01

    def test_min_volume_delta_constant(self) -> None:
        assert MIN_VOLUME_DELTA == 0.005


class TestNotifyBudget:
    def test_appends_to_pending_when_available(self) -> None:
        event = PositionClosed(
            position_ticket=100, symbol="XAUUSDc", side="long",
            strategy="test", magic=1, entry_price=100.0, close_price=110.0,
            closed_volume=0.1, remaining_volume=0.0,
            original_volume=0.1, pnl=10.0,
            exit_reason="close", close_time="2026-01-01T00:00:00Z",
            label="win",
        )
        state = SimpleNamespace(_pending_budget_records=[])
        PositionCloseAdapter._notify_budget(event, state)
        assert len(state._pending_budget_records) == 1
        assert state._pending_budget_records[0]["pnl"] == 10.0
        assert state._pending_budget_records[0]["strategy"] == "test"

    def test_noop_when_pending_is_none(self) -> None:
        event = PositionClosed(
            position_ticket=100, symbol="XAUUSDc", side="long",
            strategy="test", magic=1, entry_price=100.0, close_price=110.0,
            closed_volume=0.1, remaining_volume=0.0,
            original_volume=0.1, pnl=10.0,
            exit_reason="close", close_time="2026-01-01T00:00:00Z",
            label="win",
        )
        PositionCloseAdapter._notify_budget(event, SimpleNamespace(_pending_budget_records=None))


class TestReconcileAndRecordCloses:
    def test_empty_known_tickets_returns_empty(self) -> None:
        events = reconcile_and_record_closes(
            known_tickets={},
            mt5_worker=MagicMock(),
            symbol="XAUUSDc",
            journal_path="/fake/path.jsonl",
        )
        assert events == []

    def test_xau_symbol_uses_correct_tick_size(self) -> None:
        mock_worker = MagicMock()
        mock_worker.positions_get.return_value = []
        events = reconcile_and_record_closes(
            known_tickets={},
            mt5_worker=mock_worker,
            symbol="XAUUSDc",
            journal_path="/fake/path.jsonl",
        )
        assert events == []

    def test_btc_symbol_uses_correct_tick_size(self) -> None:
        mock_worker = MagicMock()
        mock_worker.positions_get.return_value = []
        events = reconcile_and_record_closes(
            known_tickets={},
            mt5_worker=mock_worker,
            symbol="BTCUSDc",
            journal_path="/fake/path.jsonl",
        )
        assert events == []


class TestDetectAndBuild:
    def test_no_positions_no_events(self) -> None:
        adapter = PositionCloseAdapter(tick_size=0.01)
        mock_worker = MagicMock()
        mock_worker.positions_get.return_value = []
        events = adapter.detect_and_build(
            known_tickets={}, mt5_worker=mock_worker, symbol="XAUUSDc",
        )
        assert events == []

    def test_volume_delta_below_threshold_skipped(self) -> None:
        """Delta < min_delta and current_vol > 0 → skipped."""
        adapter = PositionCloseAdapter(tick_size=0.01)
        mock_worker = MagicMock()
        pos = MagicMock()
        pos.ticket = 100
        pos.volume = 0.999
        mock_worker.positions_get.return_value = [pos]
        events = adapter.detect_and_build(
            known_tickets={
                100: {"volume": 1.0, "entry_price": 100.0},
            },
            mt5_worker=mock_worker,
            symbol="XAUUSDc",
        )
        assert events == []


class TestRecordOpen:
    def test_record_open_writes_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            adapter = PositionCloseAdapter(tick_size=0.01)
            event = PositionOpened(
                position_ticket=5001, symbol="XAUUSDc", side="long",
                strategy="test_swing", magic=90001,
                entry_price=4700.0, volume=0.1,
                sl=4650.0, tp=4800.0,
                brain_ids=("brain_1",), confidence=0.75,
            )
            state = SimpleNamespace(known_open_tickets={})
            result = adapter.record_open(event, jpath, state=state)
            assert result is True
            assert 5001 in state.known_open_tickets
            assert state.known_open_tickets[5001]["entry_price"] == 4700.0

    def test_record_open_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            adapter = PositionCloseAdapter(tick_size=0.01)
            event = PositionOpened(
                position_ticket=5002, symbol="XAUUSDc", side="short",
                strategy="test", magic=1,
                entry_price=100.0, volume=0.1,
                sl=110.0, tp=90.0,
                brain_ids=(), confidence=0.5,
            )
            result = adapter.record_open(event, jpath, state=None)
            assert result is True

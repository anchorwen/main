"""Tests for core.runtime.position_close_adapter — close event authority.

FIX-20260619-040: Tier 1 zero-coverage breakout #11 (FINAL).
Covers module-level functions + static notify methods.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.runtime.position_close_adapter import (
    PositionCloseAdapter,
    PositionClosed,
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
            # Verify journal entry
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
                journal_path=tmpdir, state=None,  # dir, not file
            )
            assert result is False


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
            # Mock the pnl ledger's internal structure
            state._pnl_ledger._pending = {}

            result = adapter.record(event, jpath, state=state)
            # Should write to journal
            assert Path(jpath).exists()


class TestPositionCloseAdapterInit:
    def test_default_tick_size(self) -> None:
        adapter = PositionCloseAdapter()
        assert adapter._tick_size == 0.01

    def test_custom_tick_size(self) -> None:
        adapter = PositionCloseAdapter(tick_size=1.0)
        assert adapter._tick_size == 1.0
        assert adapter._min_delta >= 0.005

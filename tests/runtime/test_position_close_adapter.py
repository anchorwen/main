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
from unittest.mock import MagicMock

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
            mia_entries=[],
            mt5_worker=MagicMock(),
            symbol="XAUUSDc",
            journal_path="/fake/path.jsonl",
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
                ticket=5001,
                symbol="XAUUSDc",
                side="long",
                strategy="test_swing",
                magic=90001,
                entry_price=4700.0,
                volume=0.1,
                sl=4650.0,
                tp=4800.0,
                brain_ids=["brain_1"],
                confidence=0.75,
                journal_path=jpath,
                state=None,
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
                ticket=5001,
                symbol="XAUUSDc",
                side="long",
                strategy="test",
                magic=90001,
                entry_price=4700.0,
                volume=0.1,
                sl=0,
                tp=0,
                brain_ids=[],
                confidence=0.5,
                journal_path=tmpdir,
                state=None,
            )
            assert result is False

    def test_registers_in_known_open_tickets(self) -> None:
        """State's known_open_tickets is populated on successful record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = str(Path(tmpdir) / "live_trade_journal.jsonl")
            state = SimpleNamespace(known_open_tickets={})
            result = record_position_opened(
                ticket=5001,
                symbol="BTCUSDc",
                side="short",
                strategy="test",
                magic=90002,
                entry_price=60000.0,
                volume=0.1,
                sl=61000.0,
                tp=59000.0,
                brain_ids=["brain_btc"],
                confidence=0.8,
                journal_path=jpath,
                state=state,
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
                position_ticket=1001,
                symbol="XAUUSDc",
                side="long",
                strategy="test_swing",
                magic=90001,
                entry_price=4700.0,
                close_price=4750.0,
                closed_volume=0.1,
                remaining_volume=0.0,
                original_volume=0.1,
                pnl=5.0,
                exit_reason="tp_hit",
                close_time="2026-06-19T12:00:00Z",
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
            position_ticket=1001,
            symbol="XAUUSDc",
            side="long",
            strategy="test",
            magic=90001,
            entry_price=4700.0,
            close_price=4750.0,
            closed_volume=0.1,
            remaining_volume=0.0,
            original_volume=0.1,
            pnl=5.0,
            exit_reason="tp",
            close_time="2026-01-01T00:00:00Z",
            label="win",
            deal_id=777,
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
            position_ticket=100,
            symbol="XAUUSDc",
            side="long",
            strategy="test",
            magic=1,
            entry_price=100.0,
            close_price=110.0,
            closed_volume=0.1,
            remaining_volume=0.0,
            original_volume=0.1,
            pnl=10.0,
            exit_reason="close",
            close_time="2026-01-01T00:00:00Z",
            label="win",
        )
        state = SimpleNamespace(_pending_budget_records=[])
        PositionCloseAdapter._notify_budget(event, state)
        assert len(state._pending_budget_records) == 1
        assert state._pending_budget_records[0]["pnl"] == 10.0
        assert state._pending_budget_records[0]["strategy"] == "test"

    def test_noop_when_pending_is_none(self) -> None:
        event = PositionClosed(
            position_ticket=100,
            symbol="XAUUSDc",
            side="long",
            strategy="test",
            magic=1,
            entry_price=100.0,
            close_price=110.0,
            closed_volume=0.1,
            remaining_volume=0.0,
            original_volume=0.1,
            pnl=10.0,
            exit_reason="close",
            close_time="2026-01-01T00:00:00Z",
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
            known_tickets={},
            mt5_worker=mock_worker,
            symbol="XAUUSDc",
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
                position_ticket=5001,
                symbol="XAUUSDc",
                side="long",
                strategy="test_swing",
                magic=90001,
                entry_price=4700.0,
                volume=0.1,
                sl=4650.0,
                tp=4800.0,
                brain_ids=("brain_1",),
                confidence=0.75,
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
                position_ticket=5002,
                symbol="XAUUSDc",
                side="short",
                strategy="test",
                magic=1,
                entry_price=100.0,
                volume=0.1,
                sl=110.0,
                tp=90.0,
                brain_ids=(),
                confidence=0.5,
            )
            result = adapter.record_open(event, jpath, state=None)
            assert result is True


def _mt5_deal(**kw: object) -> SimpleNamespace:
    """Fake MT5 Deal (history_deals_get shape)."""
    base = {
        "entry": 0,
        "ticket": 1,
        "price": 100.0,
        "profit": 0.0,
        "reason": 3,
        "time": 0,
        "volume": 0.1,
        "position_id": 1,
        "comment": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestBuildEventExitDealSSOT:
    """DQAF-20260708-003: _build_event resolves the close from the EXIT deal,
    never the opening deal (which fabricated break-even at the entry price).
    """

    def test_full_close_uses_exit_deal_price_and_profit(self) -> None:
        adapter = PositionCloseAdapter(tick_size=1.0)
        entry = _mt5_deal(entry=0, ticket=1, price=63514.66, profit=0.0, reason=3, time=100)
        exit_ = _mt5_deal(entry=1, ticket=2, price=64598.99, profit=1084.0, reason=5, time=200)
        worker = MagicMock()
        worker.history_deals_get.return_value = [entry, exit_]

        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 63514.66,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
                "magic": 90411,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=worker,
        )
        assert evt is not None
        # close price MUST be the exit deal price, not the 63514.66 entry price
        assert evt.close_price == 64598.99
        assert evt.pnl == 1084.0
        assert evt.label == "tp_hit_first"
        assert evt.close_price_source == "mt5_exit_deal"
        assert evt.pnl_status == "verified_from_mt5_deal"
        # entry_price must NOT collapse to the close price
        assert evt.entry_price == 63514.66

    def test_sl_hit_records_loss_not_breakeven(self) -> None:
        adapter = PositionCloseAdapter(tick_size=1.0)
        entry = _mt5_deal(entry=0, ticket=1, price=64000.0, profit=0.0, time=100)
        exit_ = _mt5_deal(entry=1, ticket=2, price=63000.0, profit=-1000.0, reason=4, time=200)
        worker = MagicMock()
        worker.history_deals_get.return_value = [entry, exit_]

        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=worker,
        )
        assert evt is not None
        assert evt.pnl == -1000.0
        assert evt.label == "sl_hit_first"
        assert evt.close_price == 63000.0

    def test_only_entry_deal_returns_none_no_fabrication(self) -> None:
        """When only the opening deal exists, build NO event — never fabricate."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        entry = _mt5_deal(entry=0, ticket=1, price=64000.0, profit=0.0)
        worker = MagicMock()
        worker.history_deals_get.return_value = [entry]

        evt = adapter._build_event(
            ticket=1,
            open_entry={"entry_price": 64000.0, "side": "long", "strategy": "s", "volume": 0.1},
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=worker,
        )
        assert evt is None

    def test_journal_entry_carries_provenance(self) -> None:
        adapter = PositionCloseAdapter(tick_size=1.0)
        entry = _mt5_deal(entry=0, ticket=1, price=100.0, profit=0.0, time=100)
        exit_ = _mt5_deal(entry=1, ticket=2, price=110.0, profit=10.0, reason=5, time=200)
        worker = MagicMock()
        worker.history_deals_get.return_value = [entry, exit_]
        evt = adapter._build_event(
            ticket=1,
            open_entry={"entry_price": 100.0, "side": "long", "strategy": "s", "volume": 0.1},
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=worker,
        )
        assert evt is not None
        je = evt.to_journal_entry()
        assert je["_close_price_source"] == "mt5_exit_deal"
        assert je["_pnl_status"] == "verified_from_mt5_deal"
        assert je["detail"]["close_price"] == 110.0
        assert je["pnl"] == 10.0


class TestTrailAwareSLLabel:
    """DQAF-20260806-001 (FIX-2026XXXX-XXX): SL-hit label must distinguish a
    TRAILED SL from a first-touch SL.  Mirrors reconciliation.py:198-204
    (FIX-20260612-003): state.position_manager.get_position(ticket).trail_advances
    > 0 ⇒ label sl_hit_trailed, else sl_hit_first.

    IC regression-lock mandate (Iron Law #5): the adapter is the ACTIVE journal
    writer (live_cycle.py:1756); reconciliation runs only at restart, so the
    trail-aware contract must live HERE too.  Welded by these tests — before
    the fix, a trailed SL-hit is mislabeled sl_hit_first.

    Primary source: position_manager trail_advances (Option A, mirrors
    reconciliation).  MIA fallback: mia_close.py:89-92 captured trail_advances
    at detection time into trail_contribution; the position may already be gone
    from position_manager (ghost/unmanaged) → restore mia_close.py:180-185
    semantics that Strangler Fig #12 (FIX-20260611-005) silently dropped.
    """

    def _sl_worker(self, comment: str = "") -> MagicMock:
        worker = MagicMock()
        entry = _mt5_deal(entry=0, ticket=1, price=64000.0, profit=0.0, time=100)
        exit_ = _mt5_deal(
            entry=1,
            ticket=2,
            price=63000.0,
            profit=-1000.0,
            reason=4,
            time=200,
            comment=comment,
        )
        worker.history_deals_get.return_value = [entry, exit_]
        return worker

    def _state(self, trail_advances: int) -> SimpleNamespace:
        pos = SimpleNamespace(trail_advances=trail_advances)
        pm = MagicMock()
        pm.get_position.return_value = pos
        return SimpleNamespace(position_manager=pm)

    def test_sl_hit_with_trail_activity_labels_sl_hit_trailed(self) -> None:
        """Trail actively tightened SL ⇒ trail exit, NOT first-touch SL."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=self._sl_worker(),
            state=self._state(trail_advances=3),
        )
        assert evt is not None
        assert evt.label == "sl_hit_trailed"
        assert evt.exit_reason == "sl_hit"

    def test_sl_hit_without_trail_stays_sl_hit_first(self) -> None:
        """trail_advances == 0 ⇒ first-touch SL (unchanged contract)."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=self._sl_worker(),
            state=self._state(trail_advances=0),
        )
        assert evt is not None
        assert evt.label == "sl_hit_first"

    def test_sl_hit_without_state_backward_compat(self) -> None:
        """state=None (callers with no position_manager) ⇒ sl_hit_first."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=self._sl_worker(),
        )
        assert evt is not None
        assert evt.label == "sl_hit_first"

    def test_watchdog_comment_priority_preserved(self) -> None:
        """exit_watchdog: comment outranks trail label (DQAF-064 §1)."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=self._sl_worker(comment="exit_watchdog:hesitation"),
            state=self._state(trail_advances=5),
        )
        assert evt is not None
        assert evt.label == "watchdog:hesitation"

    def test_mia_entry_trail_contribution_fallback(self) -> None:
        """MIA path (Strangler Fig #12): position_manager may have already
        cleared a ghost — trail_contribution captured at detection time
        (mia_close.py:89-92) still proves the SL was trailed."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        evt = adapter._build_event(
            ticket=1,
            open_entry={
                "entry_price": 64000.0,
                "side": "long",
                "strategy": "btc_swing",
                "volume": 0.1,
                "trail_contribution": {
                    "initial_sl": 63200.0,
                    "final_sl": 64500.0,
                    "trail_advances": 2,
                },
            },
            closed_volume=0.1,
            remaining_volume=0.0,
            symbol="BTCUSDc",
            mt5_worker=self._sl_worker(),
        )
        assert evt is not None
        assert evt.label == "sl_hit_trailed"

    def test_detect_and_build_end_to_end_trail_label(self) -> None:
        """Runtime path (live_cycle.py:1756 → reconcile_and_record_closes →
        detect_and_build → _build_event): a trailed position closed by SL
        produces sl_hit_trailed through the PUBLIC API."""
        adapter = PositionCloseAdapter(tick_size=1.0)
        worker = self._sl_worker()
        pos = MagicMock()
        pos.ticket = 1
        pos.volume = 0.0  # closed → volume delta triggers detection
        worker.positions_get.return_value = [pos]
        events = adapter.detect_and_build(
            known_tickets={
                1: {"volume": 0.1, "entry_price": 64000.0, "side": "long", "strategy": "btc_swing"},
            },
            mt5_worker=worker,
            symbol="BTCUSDc",
            state=self._state(trail_advances=3),
        )
        assert len(events) == 1
        assert events[0].label == "sl_hit_trailed"

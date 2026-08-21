"""Cross-producer convergence tests — TECH_DEBT-007 (P6 / DQAF-20260821-001).

Iron Law #5 mandate: the same (deal_reason, deal_comment, trail_active) input
MUST produce byte-identical labels across every deal-informed producer:

    position_close_adapter  (active journal writer)
    reconciliation          (restart-only reconciliation)
    mia_close               (external intervention enrichment)
    settlement_queue        (verified-PnL settlement writer)

Before P6 the four paths disagreed on SL-trail, broker fallback, watchdog
short-code and None-reason handling.  These tests weld the convergence so no
producer can silently drift from the SSOT again (ReB:
CLOSE_LABEL_MULTI_PRODUCER_DIVERGENCE).

FIX-20260821-002.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.runtime.close_label import resolve_close_label
from core.runtime.deal_selection import resolve_exit_deal
from core.runtime.mia_close import build_mia_close_entry, enrich_mia_from_deals
from core.runtime.position_close_adapter import PositionCloseAdapter
from core.runtime.reconciliation import reconcile_closed_positions
from core.runtime.settlement_queue import SettlementEntry, SettlementQueue


def _mt5_deal(**kw: object) -> SimpleNamespace:
    """Fake MT5 Deal (history_deals_get shape)."""
    base = {
        "entry": 0,
        "ticket": 1,
        "price": 100.0,
        "profit": 0.0,
        "reason": 3,
        "time": 100,
        "volume": 0.1,
        "position_id": 1,
        "comment": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _deals(reason: int, comment: str) -> list[SimpleNamespace]:
    """Opening + closing deal for a given exit reason/comment."""
    return [
        _mt5_deal(entry=0, ticket=1, price=100.0, profit=0.0, reason=3, time=100),
        _mt5_deal(
            entry=1,
            ticket=2,
            price=90.0,
            profit=-10.0,
            reason=reason,
            time=200,
            comment=comment,
        ),
    ]


def _open_entry() -> dict[str, object]:
    return {
        "entry_price": 100.0,
        "side": "long",
        "strategy": "btc_swing",
        "volume": 0.1,
        "magic": 90411,
        "brain_ids": ["b1"],
        "message_id": "open_1",
        "recorded_at": "2026-08-21T00:00:00",
    }


def _trail_state(trail_active: bool) -> SimpleNamespace:
    pos = SimpleNamespace(trail_advances=3 if trail_active else 0)
    pm = MagicMock()
    pm.get_position.return_value = pos
    return SimpleNamespace(position_manager=pm, _reentry_states={})


def _adapter_label(reason: int, comment: str, trail_active: bool) -> str:
    adapter = PositionCloseAdapter(tick_size=1.0)
    worker = MagicMock()
    worker.history_deals_get.return_value = _deals(reason, comment)
    evt = adapter._build_event(
        ticket=1,
        open_entry={"entry_price": 100.0, "side": "long", "strategy": "btc_swing", "volume": 0.1},
        closed_volume=0.1,
        remaining_volume=0.0,
        symbol="BTCUSDc",
        mt5_worker=worker,
        state=_trail_state(trail_active),
    )
    assert evt is not None
    return evt.label


def _reconcil_label(reason: int, comment: str, trail_active: bool, tmp_path) -> str:
    worker = MagicMock()
    worker.positions_get.return_value = []  # ticket gone from MT5
    worker.history_deals_get.return_value = _deals(reason, comment)
    entries = reconcile_closed_positions(
        mt5_worker=worker,
        symbol="BTCUSDc",
        journal_path=str(tmp_path / "live_trade_journal.jsonl"),
        known_tickets={1: _open_entry()},
        state=_trail_state(trail_active),
    )
    assert len(entries) == 1
    return entries[0]["label"]


def _mia_label(reason: int, comment: str, trail_active: bool) -> str:
    pos = SimpleNamespace(
        ticket=1,
        side="long",
        entry_price=100.0,
        volume=0.1,
        initial_sl=95.0,
        initial_tp=110.0,
        current_sl=95.0,
        trail_advances=3 if trail_active else 0,
        strategy_name="btc_swing",
    )
    entry = build_mia_close_entry(pos, _open_entry())
    enrich_mia_from_deals(entry, _deals(reason, comment))
    return entry["label"]


def _settlement_label(reason: int, comment: str, trail_active: bool, tmp_path) -> str:
    entry = SettlementEntry(
        ticket=1,
        symbol="BTCUSDc",
        side="long",
        entry_price=100.0,
        volume=0.1,
        strategy="btc_swing",
        magic=0,
        trail_advances=3 if trail_active else 0,
    )
    resolution = resolve_exit_deal(_deals(reason, comment), cursor=0)
    assert resolution is not None and resolution.has_exit
    result = SettlementQueue()._handle_settled(
        entry,
        resolution,
        str(tmp_path / "j.jsonl"),
        None,
        None,
    )
    return result["event"].label


# ── The convergence matrix: every deal-informed row the producers must agree on ──
_CONVERGENCE_ROWS = [
    # (reason, comment, trail_active, expected)
    (4, "", False, "sl_hit_first"),
    (4, "", True, "sl_hit_trailed"),
    (5, "", True, "tp_hit_first"),
    (3, "bleed_stop_r-0.5", False, "managed:bleed_stop_r-0.5"),
    (3, "", False, "broker:signal_close"),
    (0, "", False, "broker:client_close"),
    (6, "", False, "broker:stop_out"),
    (7, "", False, "broker:risk_out"),
    (4, "exit_watchdog:hesitation_18c_no_breakeven", True, "watchdog:hesitation_18c"),
]


class TestAllDealInformedProducersConverge:
    @pytest.mark.parametrize(("reason", "comment", "trail_active", "expected"), _CONVERGENCE_ROWS)
    def test_same_deal_same_label_everywhere(
        self,
        reason: int,
        comment: str,
        trail_active: bool,
        expected: str,
        tmp_path,
    ) -> None:
        produced = {
            "adapter": _adapter_label(reason, comment, trail_active),
            "reconciliation": _reconcil_label(reason, comment, trail_active, tmp_path),
            "mia": _mia_label(reason, comment, trail_active),
            "settlement": _settlement_label(reason, comment, trail_active, tmp_path),
        }
        # Every producer must agree with each other AND with the SSOT mouth.
        assert set(produced.values()) == {expected}
        assert resolve_close_label(reason, comment, trail_active) == expected


class TestNoDealHonestFallback:
    def test_reconciliation_none_reason_is_unknown_close(self, tmp_path) -> None:
        """P6: no exit deal → close_reason None → honest unknown_close.

        Pre-P6 reconciliation fabricated ``broker:client_close`` for an
        unknown reason — a lie that mislabeled every orphaned close.
        """
        worker = MagicMock()
        worker.positions_get.return_value = []
        # Only an opening deal → resolve_exit_deal returns no_exit_deal.
        worker.history_deals_get.return_value = [
            _mt5_deal(entry=0, ticket=1, price=100.0, profit=0.0, reason=3, time=100)
        ]
        entries = reconcile_closed_positions(
            mt5_worker=worker,
            symbol="BTCUSDc",
            journal_path=str(tmp_path / "live_trade_journal.jsonl"),
            known_tickets={1: _open_entry()},
            state=None,
        )
        assert len(entries) == 1
        assert entries[0]["label"] == "unknown_close"
        assert entries[0]["detail"]["reason"] == "unknown_close"

    def test_mia_keeps_pnl_provisional_when_no_deal(self) -> None:
        """Documented divergence: no deal reason known → PnL is the only
        signal (provisional pre-deal semantics).  Out of the convergence
        contract — no deal exists to converge on."""
        pos = SimpleNamespace(
            ticket=1,
            side="long",
            entry_price=100.0,
            volume=0.1,
            initial_sl=95.0,
            initial_tp=110.0,
            current_sl=95.0,
            trail_advances=0,
            strategy_name="btc_swing",
        )
        entry = build_mia_close_entry(pos, _open_entry())
        # No exit deal → enrich keeps the PnL provisional estimate (loss).
        enrich_mia_from_deals(entry, [_mt5_deal(entry=0, ticket=1, price=100.0, profit=0.0)])
        assert entry["label"] == "loss"
        # resolve_exit_deal still resolves a no-exit resolution — provenance is
        # CLOSE_SRC_NO_EXIT_DEAL (only a literal empty list yields no_deals).
        assert entry["_close_price_source"] == "no_exit_deal"

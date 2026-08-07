"""Regression lock — DQAF-20260807-001 Zombie 逃逸原子修补.

Contract under test: every AGED zombie must enqueue a settlement entry EVEN
when ``known_open_tickets`` lacks the ticket (bridge-direct journal writes
escaped tracking) — so every position that leaves MT5 leaves a PnL corpse
(verified deal or settlement-timeout record).

08-06 production replay: m30 ticket 4448694178 opened 11:20Z, detected
zombie at 13:09:55Z, settlement queue EMPTY afterwards → PnL permanently
unaccounted.  Root cause: the pre-mgmt zombie clear gated the enqueue on
``_z_open is not None``.  Fixed via ``_zombie_settlement_fields`` fallback
+ unconditional enqueue (guarded only by queue presence).
"""

from __future__ import annotations

from types import SimpleNamespace

from core.runtime.live_cycle import _zombie_settlement_fields


def _pm_snapshot(**overrides: object) -> SimpleNamespace:
    """Minimal ActivePosition snapshot as returned by position_manager."""
    fields: dict[str, object] = dict(
        ticket=4448694178,
        side="long",
        entry_price=4266.116,
        volume=0.02,
        strategy_name="m30_swing",
        supporting_brain_ids=["brain_a", "brain_b"],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_z_open_present_uses_known_open_fields():
    """When known_open_tickets has the ticket, its fields win (no fallback)."""
    z_open = {
        "side": "short",
        "entry_price": 100.0,
        "volume": 0.05,
        "strategy": "h1_swing",
        "magic": 93200,
        "brain_ids": ["x"],
    }
    side, entry, vol, strat, magic, bids = _zombie_settlement_fields(z_open, _pm_snapshot())
    assert (side, entry, vol, strat, magic, bids) == (
        "short",
        100.0,
        0.05,
        "h1_swing",
        93200,
        ["x"],
    )


def test_z_open_none_falls_back_to_position_manager():
    """m30 4448694178 replay: known_open_tickets MISSING the ticket but the
    position_manager snapshot holds it → fields fall back to the snapshot.
    This is the exact escape that previously produced NO settlement corpse.
    """
    side, entry, vol, strat, magic, bids = _zombie_settlement_fields(None, _pm_snapshot())
    assert side == "long"
    assert entry == 4266.116
    assert vol == 0.02
    assert strat == "m30_swing"
    assert magic == 0  # magic not recoverable from PM snapshot → 0 (unknown)
    assert bids == ["brain_a", "brain_b"]


def test_z_open_none_pm_missing_returns_empty_fields():
    """Both sources missing → empty/zero fields (corpse still writable with
    unknowns — never a crash, never a skipped enqueue)."""
    side, entry, vol, strat, magic, bids = _zombie_settlement_fields({}, None)
    assert side == ""
    assert entry == 0.0
    assert vol == 0.0
    assert strat == ""
    assert magic == 0
    assert bids == []


def test_enqueue_contract_with_fallback_fields():
    """SettlementQueue accepts a fallback-built entry for an escaped ticket —
    the enqueue must succeed and leave the ticket pending (the PnL corpse
    will be written on settlement poll / timeout)."""
    from core.runtime.settlement_queue import SettlementQueue

    sq = SettlementQueue()
    _side, _entry, _vol, _strat, _magic, _bids = _zombie_settlement_fields(None, _pm_snapshot())
    sq.enqueue(
        ticket=4448694178,
        symbol="XAUUSDc",
        side=_side,
        entry_price=_entry,
        volume=_vol,
        strategy=_strat,
        magic=_magic,
        brain_ids=_bids,
        estimated_pnl=None,
        estimated_close_price=None,
        cycle=1,
    )
    assert sq.is_pending(4448694178)
    entry = sq.get(4448694178)
    assert entry is not None
    assert entry.strategy == "m30_swing"
    assert entry.entry_price == 4266.116
    assert entry.side == "long"

"""Regression tests for the broker-authoritative stale-position guard.

DQAF-20260709-002 / FIX exit-phase: ``_resolve_stale_position_action`` must
consult the broker (SSOT) before removing a position that is absent from
``known_open_tickets``.  A position that is STILL OPEN at the broker but has
transiently dropped out of the local tracker (e.g. across a market-closed
restart, where orphan re-adoption only runs at loop_iteration==1) must be
RE-ADOPTED, never stale-cleared — otherwise it ping-pongs between stale-clear
and restart-readopt and is left hedged and unmanaged (never exits).

Statistical scope: pure branch-table tests of the decision function; no live
data, no MT5 — the broker is mocked to return open / gone / timeout.
"""

from __future__ import annotations

import pytest

from core.runtime import fault_handler
from core.runtime.management_phase import _resolve_stale_position_action


class _Pos:
    """Minimal ActivePosition stand-in carrying the fields the guard reads."""

    def __init__(self, ticket: int) -> None:
        self.ticket = ticket
        self.side = "long"
        self.entry_price = 4039.122
        self.volume = 0.01
        self.current_sl = 4060.46
        self.current_tp = 4128.24
        self.initial_sl = 3978.58
        self.initial_tp = 4128.24
        self.strategy_name = "m30_swing"
        self.supporting_brain_ids = ["Swing_V9_M30_V4"]


class _BrokerPos:
    def __init__(self, ticket: int, magic: int) -> None:
        self.ticket = ticket
        self.magic = magic


class _State:
    def __init__(self, known: dict) -> None:
        self.known_open_tickets = known


class _Config:
    def __init__(self, no_mt5: bool = False) -> None:
        self.no_mt5 = no_mt5


class _MT5:
    """Broker mock — ``positions_get(ticket=...)`` returns a preset result."""

    def __init__(self, result) -> None:
        self._result = result
        self.calls: list[int] = []

    def positions_get(self, ticket=None):
        self.calls.append(ticket)
        return self._result


# The two real hedged XAU tickets from the incident.
SHORT = 4098792728
LONG = 4098917446


def test_tracked_when_present_in_known_open_tickets() -> None:
    """A tracked position needs no reconciliation and no broker probe."""
    pos = _Pos(LONG)
    state = _State({LONG: {"volume": 0.01}})
    mt5 = _MT5([_BrokerPos(LONG, 90001)])
    action, entry = _resolve_stale_position_action(pos, state, mt5, _Config())
    assert action == "tracked"
    assert entry is None
    assert mt5.calls == []  # no wasted probe on the hot path


def test_readopt_when_broker_confirms_still_open() -> None:
    """Untracked BUT open at broker → re-adopt (the incident's fix path)."""
    pos = _Pos(LONG)
    # LONG dropped from the tracker; SHORT still tracked (mirrors the incident).
    state = _State({SHORT: {"volume": 0.01}})
    mt5 = _MT5([_BrokerPos(LONG, 90001)])
    action, entry = _resolve_stale_position_action(pos, state, mt5, _Config())
    assert action == "readopt"
    assert entry is not None
    assert entry["side"] == "long"
    assert entry["strategy"] == "m30_swing"
    assert entry["magic"] == 90001  # taken from broker-authoritative record
    assert entry["volume"] == 0.01
    assert entry["sl"] == 4060.46
    assert entry["source"] == "management_readopt"
    assert entry["brain_ids"] == ["Swing_V9_M30_V4"]


def test_clear_when_broker_confirms_gone() -> None:
    """Untracked AND absent at broker → genuine stale close, clear."""
    pos = _Pos(LONG)
    state = _State({SHORT: {"volume": 0.01}})
    mt5 = _MT5([])  # broker returns nothing → really closed
    action, entry = _resolve_stale_position_action(pos, state, mt5, _Config())
    assert action == "clear"
    assert entry is None


def test_clear_when_no_mt5_preserves_legacy_semantics() -> None:
    """Backtest / no_mt5 has no broker SSOT → preserve legacy clear behaviour."""
    pos = _Pos(LONG)
    state = _State({SHORT: {"volume": 0.01}})
    action, entry = _resolve_stale_position_action(pos, state, None, _Config(no_mt5=True))
    assert action == "clear"
    assert entry is None


def test_retain_on_probe_timeout_never_infers_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inconclusive probe (MT5 timeout) must NOT be read as 'closed'."""
    pos = _Pos(LONG)
    state = _State({SHORT: {"volume": 0.01}})
    monkeypatch.setattr(
        fault_handler,
        "mt5_call_with_timeout",
        lambda *a, **k: fault_handler._MT5_TIMEOUT_SENTINEL,
    )
    action, entry = _resolve_stale_position_action(pos, state, _MT5([]), _Config())
    assert action == "retain"
    assert entry is None


def test_empty_known_open_tickets_is_tracked_not_cleared() -> None:
    """An empty tracker (fresh start) must not clear — guard only fires when the
    tracker is populated yet missing THIS ticket."""
    pos = _Pos(LONG)
    state = _State({})
    mt5 = _MT5([])
    action, entry = _resolve_stale_position_action(pos, state, mt5, _Config())
    assert action == "tracked"
    assert entry is None
    assert mt5.calls == []

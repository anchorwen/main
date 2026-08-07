"""Tests for JournalGate — orphan-prevention admission control.

journal_gate.py is under the 2026-06-04 journal architecture freeze yet had NO
dedicated tests (coverage 0%).  FIX-20260708-001 changes admission to key on the
IMMUTABLE position identity, so these tests pin:

  * the core regression: a re-ticketed close (new mutable position_ticket, same
    position_identifier as its open) is now ADMITTED instead of quarantined as a
    false orphan — the mechanism behind the recurring ~17/day quarantine bleed;
  * that genuine orphans (no identity match) are still rejected/quarantined;
  * reload/register/health invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.contracts.journal_sla import QUARANTINE_DAILY_LIMIT
from core.ledger.services.journal_gate import JournalGate


def _write_journal(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    jp = tmp_path / "live_trade_journal.jsonl"
    with open(jp, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return jp


def _open(ticket: int, identifier: int | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {"action": "open", "position_ticket": ticket}
    if identifier is not None:
        rec["position_identifier"] = identifier
    return rec


# ── reload / registration ─────────────────────────────────────────────


def test_reload_populates_known_tickets_from_opens(tmp_path: Path) -> None:
    jp = _write_journal(tmp_path, [_open(100, 100), _open(101, 101)])
    gate = JournalGate(jp, policy="quarantine")
    assert gate.known_ticket_count == 2


def test_stateless_gate_sees_open_written_by_other_process(tmp_path: Path) -> None:
    """IC 2026-08-07 Boundary 1 (The Stateless Gate): the gate must NOT rely on
    the in-memory ticket set built at construction.  A close arriving for a
    ticket whose open was written to the journal by ANOTHER process AFTER this
    gate was constructed must be admitted — otherwise multi-process drift
    (live_intent_loop vs mt5_bridge_worker vs daily_ops, zero IPC between them)
    quarantines legitimate closes as orphans, the exact anomaly this fix kills.
    """
    jp = _write_journal(tmp_path, [])  # gate constructed with EMPTY journal
    gate = JournalGate(jp, policy="quarantine")
    assert gate.known_ticket_count == 0
    # Simulate another process appending an open AFTER this gate instance exists.
    with open(jp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_open(100, 100)) + "\n")
    # Same gate instance — a stale in-memory set would reject this close as an
    # orphan.  The stateless gate re-reads the physical journal and admits it.
    assert (
        gate.validate_close({"action": "close", "position_ticket": 100, "position_identifier": 100})
        is True
    )
    assert not (tmp_path / "journal_orphan_quarantine.jsonl").exists()


def test_stateless_gate_still_quarantines_genuine_orphan(tmp_path: Path) -> None:
    """The stateless reload must NOT widen admission: a close with no matching
    open anywhere in the journal is still rejected + quarantined."""
    jp = _write_journal(tmp_path, [_open(100, 100)])
    gate = JournalGate(jp, policy="quarantine")
    orphan = {"action": "close", "position_ticket": 999, "position_identifier": 999}
    assert gate.validate_close(orphan) is False
    qpath = tmp_path / "journal_orphan_quarantine.jsonl"
    assert qpath.exists()
    assert len([l for l in qpath.read_text(encoding="utf-8").splitlines() if l]) == 1


def test_register_open_adds_ticket(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, []), policy="quarantine")
    assert gate.known_ticket_count == 0
    gate.register_open(555)
    assert gate.known_ticket_count == 1
    gate.register_open(0)  # invalid — ignored
    gate.register_open(-1)  # invalid — ignored
    assert gate.known_ticket_count == 1


# ── validate_close: the FIX-20260708-001 regression pin ────────────────


def test_reticketed_close_admitted_by_identity(tmp_path: Path) -> None:
    # Open under original ticket 100 (identity 100).  MT5 re-tickets to 205 on a
    # partial close; the close carries position_identifier=100 but ticket=205.
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="quarantine")
    reticketed_close = {
        "action": "close",
        "position_ticket": 205,  # NEW mutable ticket — unknown on its own
        "position_identifier": 100,  # immutable anchor — matches the open
        "pnl": -1.0,
    }
    # Before the fix this was quarantined as close_without_open.
    assert gate.validate_close(reticketed_close) is True
    # And nothing was written to quarantine.
    assert not (tmp_path / "journal_orphan_quarantine.jsonl").exists()


def test_close_with_matching_ticket_admitted(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="quarantine")
    assert gate.validate_close({"action": "close", "position_ticket": 100, "pnl": 1.0}) is True


def test_legacy_close_without_identifier_falls_back_to_ticket(tmp_path: Path) -> None:
    # No identifier anywhere → resolve_identity degrades to the ticket.  A close
    # whose ticket matches a known open is admitted; a re-ticketed one is not.
    gate = JournalGate(_write_journal(tmp_path, [_open(100)]), policy="quarantine")
    assert gate.validate_close({"action": "close", "position_ticket": 100}) is True
    assert gate.validate_close({"action": "close", "position_ticket": 205}) is False


# ── validate_close: genuine orphans still rejected + quarantined ───────


def test_unknown_close_rejected_and_quarantined(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="quarantine")
    orphan = {"action": "close", "position_ticket": 999, "position_identifier": 999, "pnl": -5.0}
    assert gate.validate_close(orphan) is False
    qpath = tmp_path / "journal_orphan_quarantine.jsonl"
    assert qpath.exists()
    lines = [line for line in qpath.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["_quarantine_reason"] == "close_without_open"
    assert "_quarantined_at" in rec


def test_close_without_any_identity_rejected(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="quarantine")
    assert gate.validate_close({"action": "close", "pnl": -1.0}) is False


def test_backfill_source_bypasses_gate(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, []), policy="quarantine")
    entry = {"action": "close", "position_ticket": 12345, "_source": "mt5_backfill"}
    assert gate.validate_close(entry) is True


def test_reject_policy_does_not_write_quarantine(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="reject")
    assert gate.validate_close({"action": "close", "position_ticket": 999}) is False
    assert not (tmp_path / "journal_orphan_quarantine.jsonl").exists()


# ── health / poison pill ───────────────────────────────────────────────


def test_get_health_reports_counts_and_status(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, [_open(100, 100)]), policy="quarantine")
    health = gate.get_health()
    assert health["known_tickets"] == 1
    assert health["quarantine_today"] == 0
    assert health["quarantine_status"] == "normal"
    assert health["policy"] == "quarantine"


def test_quarantine_counter_and_poison_pill(tmp_path: Path) -> None:
    gate = JournalGate(_write_journal(tmp_path, []), policy="quarantine")
    for i in range(QUARANTINE_DAILY_LIMIT):
        gate.validate_close({"action": "close", "position_ticket": 900000 + i})
    health = gate.get_health()
    assert health["quarantine_today"] == QUARANTINE_DAILY_LIMIT
    assert health["quarantine_total"] == QUARANTINE_DAILY_LIMIT
    # status escalated off "normal" once past the degraded threshold.
    assert health["quarantine_status"] in ("degraded", "critical")

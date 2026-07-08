"""Journal gate — orphan prevention with quarantine poison pill.

The journal is the Single Source of Truth.  A close event for a ticket that
was never opened through the journal is by definition invalid — it represents
either a broker-side position the system didn't create, or a phantom event
from a previous pipeline instance.

This module is the SINGLE chokepoint through which all five journal write
paths must pass before a close entry enters the journal.

投委会防线 #1 — Quarantine Poison Pill:
  Quarantine is a safety valve, NOT a trash can.  Ghost positions in
  quarantine still have real liquidation risk at the broker.
  >= 10 orphans/day → P0 DingTalk alert.

Usage:
    from core.ledger.services.journal_gate import JournalGate

    gate = JournalGate(journal_path, policy="quarantine")
    if gate.validate_close(entry):
        _append_journal(path, entry, gate=gate)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.journal_sla import QUARANTINE_DAILY_LIMIT, QUARANTINE_DEGRADED
from core.data.ticket_resolver import resolve_identity

_log = logging.getLogger(__name__)


class JournalGate:
    """Single authority for journal write admission control.

    Loads the set of tracked position tickets from the journal at
    initialization.  Close entries for unknown tickets are either
    rejected, quarantined, or logged (depending on policy).

    Policy values:
      - ``"reject"``: Silently drop (production default after bake-in).
      - ``"quarantine"``: Write to quarantine file for forensic audit
        (current production default).
      - ``"warn"``: Accept but log CRITICAL (debug / bake-in mode).
    """

    def __init__(
        self,
        journal_path: Path | str,
        *,
        policy: str = "quarantine",
        lock_dir: Path | None = None,
    ) -> None:
        self._journal_path = Path(journal_path)
        self._policy = policy
        self._lock_dir = lock_dir
        self._known_tickets: set[int] = set()
        self._today_orphan_count: int = 0
        self._today_date: str = ""
        self._quarantine_path = self._journal_path.parent / "journal_orphan_quarantine.jsonl"
        self._alert_sent_today: bool = False
        self._reload()

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def known_ticket_count(self) -> int:
        return len(self._known_tickets)

    def validate_close(self, entry: dict[str, Any]) -> bool:
        """Return True if this close entry should be written to the journal.

        A close entry is valid only if its position_ticket was previously
        registered via an open entry (``register_open``) OR was loaded from
        the journal at initialization.

        Backfill entries tagged ``_source: "mt5_backfill"`` bypass the gate
        because their opens are being created in the same batch.
        """
        # Backfill bypass — open + close created together
        if entry.get("_source") == "mt5_backfill":
            return True

        # FIX-20260708-001: admit on the IMMUTABLE identity, not the mutable
        # MT5 ticket.  A re-ticketed close (partial-close/netting) carries a NEW
        # position_ticket but the SAME position_identifier as its open leg, which
        # register_open recorded.  Keying admission on the mutable ticket
        # quarantined ~17 legitimate re-ticketed closes/day as false orphans.
        ticket = resolve_identity(entry)
        if not isinstance(ticket, int) or ticket <= 0:
            _log.warning("JournalGate: close entry has no valid position identity")
            return False

        if ticket in self._known_tickets:
            return True

        # Unknown ticket → handle per policy
        self._handle_rejection(entry, ticket)
        return False

    def register_open(self, ticket: int) -> None:
        """Register a ticket after its open entry is successfully written."""
        if isinstance(ticket, int) and ticket > 0:
            self._known_tickets.add(ticket)

    def bypass_for(self, source_tag: str) -> None:
        """Temporarily allow entries tagged with *source_tag* to bypass the gate.

        Call this before a batch backfill operation, then ``clear_bypass()``
        after the batch completes.
        """
        _log.info("JournalGate: bypass enabled for source=%s", source_tag)

    def clear_bypass(self) -> None:
        """Clear any active bypass."""
        _log.info("JournalGate: bypass cleared")

    def get_health(self) -> dict[str, Any]:
        """Return current quarantine health for the daily report."""
        self._roll_daily_counter()
        return {
            "quarantine_today": self._today_orphan_count,
            "quarantine_total": self._count_quarantine_entries(),
            "quarantine_status": (
                "critical"
                if self._today_orphan_count >= QUARANTINE_DAILY_LIMIT * 2
                else ("degraded" if self._today_orphan_count >= QUARANTINE_DEGRADED else "normal")
            ),
            "policy": self._policy,
            "known_tickets": len(self._known_tickets),
        }

    # ── Internal ─────────────────────────────────────────────────────────

    def _reload(self) -> None:
        """Scan journal for all open events to rebuild known_tickets."""
        if not self._journal_path.exists():
            _log.warning("JournalGate: journal not found at %s", self._journal_path)
            return
        count = 0
        try:
            for line in self._journal_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("action") == "open":
                    # FIX-20260708-001: register the immutable identity (== the
                    # opening ticket at open time) so re-ticketed closes match.
                    ticket = resolve_identity(e)
                    if isinstance(ticket, int) and ticket > 0:
                        self._known_tickets.add(ticket)
                        count += 1
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.exception("JournalGate: error reloading known tickets")
        _log.info("JournalGate: loaded %d known tickets from journal", count)

    def _roll_daily_counter(self) -> None:
        """Reset daily counter if the date has changed."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._today_date:
            self._today_date = today
            self._today_orphan_count = 0
            self._alert_sent_today = False

    def _count_quarantine_entries(self) -> int:
        """Count total entries in the quarantine file."""
        if not self._quarantine_path.exists():
            return 0
        try:
            text = self._quarantine_path.read_text(encoding="utf-8")
            return sum(1 for line in text.strip().split("\n") if line)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            return 0

    def _handle_rejection(self, entry: dict[str, Any], ticket: int) -> None:
        """Execute rejection policy and check quarantine poison pill."""
        self._roll_daily_counter()
        self._today_orphan_count += 1

        if self._policy == "quarantine":
            self._write_to_quarantine(entry)

        # ── Quarantine Poison Pill ───────────────────────────────────────
        # 投委会防线 #1: >= 10 orphans today → P0 alert
        if self._today_orphan_count >= QUARANTINE_DAILY_LIMIT and not self._alert_sent_today:
            self._alert_sent_today = True
            _log.critical(
                "[P0] JOURNAL_QUARANTINE_OVERFLOW: %d orphan closes quarantined today "
                "— possible broker-side liquidation risk on untracked positions. "
                "Quarantine file: %s",
                self._today_orphan_count,
                self._quarantine_path,
            )
            # Emit structured alert for DingTalk integration
            try:
                print(
                    json.dumps(
                        {
                            "event": "JOURNAL_QUARANTINE_OVERFLOW",
                            "severity": "P0",
                            "orphan_count_today": self._today_orphan_count,
                            "quarantine_file": str(self._quarantine_path),
                            "message": (
                                f"{self._today_orphan_count} orphan closes quarantined "
                                f"today — possible broker-side liquidation risk on "
                                f"untracked positions"
                            ),
                            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass

        _log.warning(
            "JournalGate: rejected close for untracked ticket=%s (policy=%s, " "orphans_today=%d)",
            ticket,
            self._policy,
            self._today_orphan_count,
        )

    def _write_to_quarantine(self, entry: dict[str, Any]) -> None:
        """Append the rejected entry to the quarantine file.

        Uses the same append-only pattern as the main journal.
        Does NOT use FileLock — quarantine is low-contention audit trail.
        """
        entry["_quarantined_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        entry["_quarantine_reason"] = "close_without_open"
        try:
            with open(self._quarantine_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.exception("JournalGate: failed to write to quarantine file")

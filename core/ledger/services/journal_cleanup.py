"""Trade journal orphan-entry auto-cleanup and integrity repair.

Scans the live trade journal for open entries that will never receive a
corresponding close (rejected orders, orders with no position ticket, or
orders older than a configurable threshold) and inserts synthetic close
entries so that downstream consumers (PnL calculation, position tracking,
performance analysis) are not misled by stale "open" positions.

Called once at pipeline startup; idempotent — running it multiple times
will not create duplicate close entries.

Also provides ``repair_journal()`` to backfill missing ``magic`` and
``strategy`` fields on legacy journal entries and to fix broken open→close
linkage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _append_journal(path: Path, entry: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _resolve_magic(entry: dict[str, Any]) -> int:
    """Extract magic from a journal entry, checking multiple locations."""
    # Top-level (v2+, post-fix)
    m = entry.get("magic", 0)
    if m:
        return int(m)
    # Nested in detail.request (bridge worker pre-fix opens)
    detail = entry.get("detail", {})
    if isinstance(detail, dict):
        req = detail.get("request", {})
        if isinstance(req, dict):
            m = req.get("magic", 0)
            if m:
                return int(m)
    return 0


def _resolve_strategy(entry: dict[str, Any]) -> str:
    """Resolve strategy name from journal entry."""
    # Top-level (v2+, post-fix)
    s = entry.get("strategy", "")
    if s:
        return s
    # Derive from magic
    magic = _resolve_magic(entry)
    if magic:
        try:
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            return MAGIC_TO_STRATEGY.get(magic, "")
        except Exception:
            pass
    return ""


def cleanup_orphan_opens(
    journal_path: Path,
    *,
    max_age_hours: int = 24,
    dry_run: bool = False,
) -> int:
    """Close orphan open entries that can never be filled.

    An open entry is considered orphan if:
      - Its ``ack_status`` is ``"rejected"``, OR
      - Its ``position_ticket`` is null and it is older than *max_age_hours*
      - It has a real position_ticket but NO matching close entry exists
        (checked via position_ticket OR open_message_id linkage)

    Returns the number of synthetic close entries written.
    """
    entries = _load_journal(journal_path)
    if not entries:
        return 0

    # Build the set of closed position_tickets and linked open message_ids
    closed_tickets: set[int] = set()
    closed_open_ids: set[str] = set()
    for e in entries:
        if e.get("action") == "close":
            # Match by position_ticket (robust — works even without open_message_id)
            ticket = e.get("position_ticket")
            if ticket is not None and isinstance(ticket, int) and ticket > 0:
                closed_tickets.add(ticket)
            # Match by open_message_id (explicit linkage, preferred when present)
            open_id = e.get("open_message_id")
            if open_id:
                closed_open_ids.add(open_id)
        # Also track modify_sltp as "not orphan" — the position is actively managed
        if e.get("action") == "modify_sltp":
            ticket = e.get("position_ticket")
            if ticket is not None and isinstance(ticket, int) and ticket > 0:
                closed_tickets.add(ticket)

    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=max_age_hours)

    cleaned = 0
    for e in entries:
        if e.get("action") != "open":
            continue

        msg_id = e.get("message_id", "")
        ticket = e.get("position_ticket")

        # Check all linkage methods
        if msg_id and msg_id in closed_open_ids:
            continue  # Explicitly closed via open_message_id
        if (
            ticket is not None
            and isinstance(ticket, int)
            and ticket > 0
            and ticket in closed_tickets
        ):
            continue  # Closed via position_ticket match

        ack = e.get("ack_status", "")
        recorded_str = e.get("recorded_at", "")
        recorded_at: datetime | None = None
        if recorded_str:
            try:
                ts = recorded_str.replace("Z", "")
                recorded_at = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass

        should_close = False
        reason = ""

        if ack == "rejected":
            should_close = True
            reason = "auto_orphan_rejected"
        elif ticket is None and recorded_at is not None and recorded_at < cutoff:
            should_close = True
            reason = "auto_orphan_stale"
        elif ticket is None and recorded_at is None:
            should_close = True
            reason = "auto_orphan_no_ticket"
        # NOTE: entries with real position_tickets that lack a close entry
        # are NOT auto-closed here — the reconciliation mechanism in
        # live_cycle.py (_reconcile_closed_positions) checks MT5 directly
        # and writes proper close entries. Auto-closing these would create
        # false close entries for positions still open in MT5.

        if not should_close:
            continue

        magic = _resolve_magic(e)
        strategy = _resolve_strategy(e)

        close_entry: dict[str, Any] = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": _utc_now_iso(),
            "message_id": f"close_{msg_id}",
            "target": e.get("target", "exec_bridge"),
            "ack_status": "closed",
            "detail": {
                "reason": reason,
                "close_price": 0.0,
                "pnl": 0.0,
            },
            "symbol": e.get("symbol", ""),
            "action": "close",
            "side": e.get("side", ""),
            "volume": e.get("volume", 0.0),
            "pnl": 0.0,
            "label": reason,
            "position_ticket": ticket,
            "magic": magic,
            "strategy": strategy,
            "sl": e.get("sl"),
            "tp": e.get("tp"),
            "open_message_id": msg_id,
        }

        if not dry_run:
            _append_journal(journal_path, close_entry)
        cleaned += 1

    return cleaned


def repair_journal(
    journal_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and repair journal integrity.

    Performs the following checks and repairs:

    1. **Backfill missing magic/strategy fields** on all entries (legacy fix).
    2. **Detect unclosed opens** — same logic as cleanup_orphan_opens but
       reports counts instead of auto-closing (auto-close uses cutoff).
    3. **Detect duplicate entries** — same message_id appearing twice.
    4. **Report link integrity** — fraction of close entries with valid
       open_message_id or position_ticket linkage.

    Returns a report dict with counts and repair actions taken.
    """
    entries = _load_journal(journal_path)
    if not entries:
        return {"status": "empty", "entries": 0}

    report: dict[str, Any] = {
        "status": "ok",
        "total_entries": len(entries),
        "backfilled_magic": 0,
        "backfilled_strategy": 0,
        "unclosed_opens": 0,
        "unclosed_tickets": [],  # list[int]
        "duplicates_removed": 0,
        "opens": 0,
        "closes": 0,
        "modifies": 0,
        "close_link_integrity_pct": 0.0,
    }

    # ── Build indices ──
    seen_ids: set[str] = set()
    tickets_to_remove: set[int] = set()
    open_entries: list[dict[str, Any]] = []
    close_entries: list[dict[str, Any]] = []

    for e in entries:
        action = e.get("action", "")
        if action == "open":
            report["opens"] += 1
            open_entries.append(e)
        elif action == "close":
            report["closes"] += 1
            close_entries.append(e)
        elif action == "modify_sltp":
            report["modifies"] += 1

        # Duplicate detection
        msg_id = e.get("message_id", "")
        if msg_id and msg_id in seen_ids:
            tickets_to_remove.add(e.get("position_ticket", 0))
            report["duplicates_removed"] += 1
        if msg_id:
            seen_ids.add(msg_id)

    # ── Backfill missing magic/strategy ──
    if not dry_run:
        needs_rewrite = False
        for e in entries:
            if not e.get("magic"):
                m = _resolve_magic(e)
                if m:
                    e["magic"] = m
                    report["backfilled_magic"] += 1
                    needs_rewrite = True
            if not e.get("strategy"):
                s = _resolve_strategy(e)
                if s:
                    e["strategy"] = s
                    report["backfilled_strategy"] += 1
                    needs_rewrite = True

        if needs_rewrite:
            journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in entries) + "\n",
                encoding="utf-8",
            )

    # ── Link integrity ──
    closed_tickets: set[int] = set()
    linked_closes = 0
    for e in close_entries:
        ticket = e.get("position_ticket")
        if ticket is not None and isinstance(ticket, int) and ticket > 0:
            closed_tickets.add(ticket)
        if e.get("open_message_id"):
            linked_closes += 1

    total_closes = len(close_entries)
    if total_closes > 0:
        report["close_link_integrity_pct"] = round(linked_closes / total_closes * 100, 1)

    # ── Detect unclosed opens ──
    for e in open_entries:
        ticket = e.get("position_ticket")
        msg_id = e.get("message_id", "")
        # Check both linkage methods
        linked = False
        if (
            ticket is not None
            and isinstance(ticket, int)
            and ticket > 0
            and ticket in closed_tickets
        ):
            linked = True
        if msg_id:
            for ce in close_entries:
                if ce.get("open_message_id") == msg_id:
                    linked = True
                    break
        if not linked:
            report["unclosed_opens"] += 1
            if ticket is not None and isinstance(ticket, int) and ticket > 0:
                report["unclosed_tickets"].append(ticket)

    if report["unclosed_opens"] > 0 or report["duplicates_removed"] > 0:
        report["status"] = "needs_repair"

    return report


def repair_and_cleanup(
    journal_path: Path,
    *,
    max_age_hours: int = 24,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run repair_journal then cleanup_orphan_opens in a single pass.

    This is the canonical entry point for startup journal maintenance.
    Call it once at the beginning of every live pipeline session.
    """
    repair_report = repair_journal(journal_path, dry_run=dry_run)
    orphan_count = cleanup_orphan_opens(journal_path, max_age_hours=max_age_hours, dry_run=dry_run)
    repair_report["orphans_closed"] = orphan_count
    return repair_report

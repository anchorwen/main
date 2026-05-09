"""Trade journal orphan-entry auto-cleanup.

Scans the live trade journal for open entries that will never receive a
corresponding close (rejected orders, orders with no position ticket, or
orders older than a configurable threshold) and inserts synthetic close
entries so that downstream consumers (PnL calculation, position tracking,
performance analysis) are not misled by stale "open" positions.

Called once at pipeline startup; idempotent — running it multiple times
will not create duplicate close entries.
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

    Entries with a real position_ticket and accepted status are left alone
    (they represent genuine MT5 positions that will be closed by the bridge).

    Returns the number of synthetic close entries written.
    """
    entries = _load_journal(journal_path)
    if not entries:
        return 0

    # Build the set of message_ids that already have a close entry
    closed_ids: set[str] = set()
    for e in entries:
        if e.get("action") == "close":
            open_id = e.get("open_message_id")
            if open_id:
                closed_ids.add(open_id)

    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=max_age_hours)

    cleaned = 0
    for e in entries:
        if e.get("action") != "open":
            continue

        msg_id = e.get("message_id", "")
        if not msg_id:
            continue
        if msg_id in closed_ids:
            continue

        ack = e.get("ack_status", "")
        ticket = e.get("position_ticket")
        recorded_str = e.get("recorded_at", "")
        recorded_at: datetime | None = None
        if recorded_str:
            try:
                # Handle both "2026-05-06T12:00:00" and "2026-05-06T12:00:00Z"
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

        if not should_close:
            continue

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
            "sl": e.get("sl"),
            "tp": e.get("tp"),
            "open_message_id": msg_id,
        }

        if not dry_run:
            _append_journal(journal_path, close_entry)
        cleaned += 1

    return cleaned

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
    parse_errors = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").strip().split("\n"), start=1):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors += 1
                # FIX-20260601-043: log corrupted lines for monitoring.
                # repair_journal() will drop these when it rewrites.
                try:  # noqa: SIM105
                    print(
                        json.dumps(
                            {
                                "event": "journal_parse_error",
                                "file": str(path),
                                "line": lineno,
                                "preview": line[:120],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
    if parse_errors:
        try:  # noqa: SIM105
            print(
                json.dumps(
                    {
                        "event": "journal_parse_error_summary",
                        "file": str(path),
                        "total_errors": parse_errors,
                        "total_lines": lineno,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    return entries


def _read_tail_lines(path: Path, n: int = 1000) -> list[str]:
    """Read last N lines using reverse seek — O(lines_wanted), not O(file_size).

    Avoids loading the entire journal file into memory (critical for XAU where
    journal can exceed 8 MB / 10k lines).  Reads in 4 KiB chunks from EOF
    backwards until N+1 newlines are found.

    FIX-20260620-023: extracted from _append_journal to enable lock→scan→write
    atomicity without paying the cost of readlines() on every call.
    """
    try:
        with open(path, "rb") as _f:
            _f.seek(0, 2)  # EOF
            _size = _f.tell()
            if _size == 0:
                return []
            _buf = b""
            _chunk_sz = 4096
            while _buf.count(b"\n") <= n and _size > 0:
                _read_sz = min(_chunk_sz, _size)
                _size -= _read_sz
                _f.seek(_size)
                _chunk = _f.read(_read_sz)
                _buf = _chunk + _buf
            _raw_lines = _buf.split(b"\n")
            # Last N+1 to account for possible partial leading line, skip empty
            return [
                _l.decode("utf-8", errors="replace")
                for _l in _raw_lines[-n - 1 :]
                if _l
            ]
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
        return []  # Best-effort — if read fails, write anyway


def _append_journal(
    path: Path, entry: dict[str, Any], *, lock_dir: Path | None = None
) -> None:
    """Append one JSON line to the journal with advisory lock and dedup.

    FIX-20260601-043: lock_dir enables FileLock serialisation with the MT5
    bridge worker and live_cycle reconciliation paths.

    FIX-20260611-005: dedup close entries by position_ticket.

    FIX-20260620-023 (Ω L3): dedup scans moved INSIDE FileLock to close the
    TOCTOU race that caused 2-5 duplicate close entries per affected ticket.
    Previously both scans ran outside the lock → two concurrent _append_journal
    calls could both pass dedup, both acquire the lock sequentially, and both
    write.  Now: lock → scan → write is atomic.  For performance (user's
    directive), the scan uses _read_tail_lines (byte-level reverse seek)
    instead of readlines() to avoid loading the full file.
    """
    _action = entry.get("action", "")
    _ticket = entry.get("position_ticket")
    _msg_id = entry.get("message_id", "")

    # ── Inline helpers ──────────────────────────────────────────────────

    def _scan_for_duplicate() -> bool:
        """Return True if a duplicate was found → skip this write.

        Must be called INSIDE the FileLock to prevent TOCTOU.
        Scans last 1000 lines (covers DQAF-20260620-001 extended window).
        """
        if not path.exists():
            return False

        _tail_lines = _read_tail_lines(path, n=1000)

        for _line in _tail_lines:
            try:
                _existing = json.loads(_line)
            except json.JSONDecodeError:
                continue

            # ── Message-ID dedup (FIX-20260611-017) ──
            # Cross-ticket message_id reuse = execution-queue double-flush bug.
            if _msg_id and _action in ("open", "close"):
                if _existing.get("message_id") == _msg_id:
                    _existing_ticket = _existing.get("position_ticket", 0) or 0
                    if _existing_ticket and _existing_ticket != (_ticket or 0):
                        import logging as _dedup_log

                        _dedup_log.getLogger(__name__).warning(
                            "[CRITICAL] Journal dedup: message_id=%s reused "
                            "across tickets %s vs %s — skipping duplicate write. "
                            "This indicates an execution queue double-flush bug.",
                            _msg_id,
                            _existing_ticket,
                            _ticket,
                        )
                    return True  # Already recorded — skip

            # ── Same-ticket close dedup (FIX-20260611-005) ──
            if _action == "close" and _ticket is not None:
                if (
                    _existing.get("action") == "close"
                    and _existing.get("position_ticket") == _ticket
                ):
                    return True  # Already recorded — skip duplicate

        return False

    def _do_write() -> None:
        with open(path, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Lock → scan → write (atomic) ────────────────────────────────────
    if lock_dir is not None:
        from core.infrastructure.distributed_lock import FileLock

        _lock = FileLock(
            "live_trade_journal", lock_dir=str(lock_dir), ttl_seconds=10
        )
        _acquired = _lock.acquire(blocking=True, timeout_seconds=5)
        if _acquired.acquired:
            try:
                if not _scan_for_duplicate():
                    _do_write()
            finally:
                _lock.release()
        else:
            # Lock denied — scan+write anyway (best-effort, journal is advisory).
            # TOCTOU risk is reduced here because lock contention implies fewer
            # concurrent writers.  Primary defense is the position_close_adapter
            # fix (FIX-023 L2) which prevents re-detection of already-recorded closes.
            if not _scan_for_duplicate():
                _do_write()
    else:
        if not _scan_for_duplicate():
            _do_write()


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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    return ""


def cleanup_orphan_opens(
    journal_path: Path,
    *,
    max_age_hours: int = 24,
    dry_run: bool = False,
    lock_dir: Path | None = None,  # FIX-20260601-043
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
            _append_journal(journal_path, close_entry, lock_dir=lock_dir)
        cleaned += 1

    return cleaned


def repair_journal(
    journal_path: Path,
    *,
    dry_run: bool = False,
    lock_dir: Path | None = None,  # FIX-20260601-043: serialise with bridge
) -> dict[str, Any]:
    """Validate and repair journal integrity.

    Performs the following checks and repairs:

    1. **Backfill missing magic/strategy fields** on all entries (legacy fix).
    2. **Detect unclosed opens** — same logic as cleanup_orphan_opens but
       reports counts instead of auto-closing (auto-close uses cutoff).
    3. **Detect duplicate entries** — same message_id appearing twice.
    4. **Report link integrity** — fraction of close entries with valid
       open_message_id or position_ticket linkage.

    FIX-20260601-043: When lock_dir is provided, acquires the live_trade_journal
    FileLock before the full-file rewrite (step 1 backfill).  This prevents the
    truncation-in-progress corruption that occurs when the bridge appends while
    repair rewrites.

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

        # Duplicate detection — message_id
        msg_id = e.get("message_id", "")
        if msg_id and msg_id in seen_ids:
            tickets_to_remove.add(e.get("position_ticket", 0))
            e["_duplicate"] = True
            report["duplicates_removed"] += 1
        if msg_id:
            seen_ids.add(msg_id)

    # ── FIX-20260612-023: position_ticket duplicate detection ──
    # Bridge close + reconciliation close produce two entries for the
    # same ticket with different message_ids.  Keep the best one.
    _close_entries = [(i, e) for i, e in enumerate(entries) if e.get("action") == "close"]
    _ticket_groups: dict = {}
    for _i, _e in _close_entries:
        _t = _e.get("position_ticket")
        if _t and isinstance(_t, int) and _t > 0:
            _ticket_groups.setdefault(_t, []).append((_i, _e))
    for _ticket, _group in _ticket_groups.items():
        if len(_group) > 1:
            # Keep best: closed > accepted > rejected, prefer larger abs(PnL)
            def _sort_key(item):
                _e = item[1]
                _ack = {"closed": 0, "accepted": 1, "rejected": 2}
                _has_cp = 1 if (_e.get("detail", {}).get("close_price") or 0) > 0 else 0
                return (_ack.get(_e.get("ack_status", ""), 99), -_has_cp, -abs(_e.get("pnl") or 0))
            _group.sort(key=_sort_key)
            for _i, _e in _group[1:]:
                if not entries[_i].get("_duplicate"):
                    entries[_i]["_duplicate"] = True
                    report["duplicates_removed"] += 1

    # ── Remove detected duplicates ──
    _dup_count = report["duplicates_removed"]
    if _dup_count > 0:
        entries = [e for e in entries if not e.get("_duplicate")]
        for e in entries:
            e.pop("_duplicate", None)

    # ── Backfill missing magic/strategy ──
    if not dry_run:
        needs_rewrite = _dup_count > 0  # FIX-20260612-010: also rewrite for dup removal
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
            # FIX-20260612-014: Use temp-file + atomic swap pattern (same as
            # compact_journal) instead of write_text() overwrite.
            # Lock is acquired BEFORE re-reading, so bridge writes are
            # blocked during the entire operation — no stale-snapshot window.
            # This eliminates the FIX-008 lock-then-reread workaround.
            import os as _os
            _temp_path = journal_path.with_suffix(".jsonl.repair_tmp")

            if lock_dir is not None:
                from core.infrastructure.distributed_lock import FileLock
                _lock = FileLock("live_trade_journal", lock_dir=str(lock_dir), ttl_seconds=10)
                _acquired = _lock.acquire(blocking=True, timeout_seconds=5)
                if not _acquired.acquired:
                    return report  # lock denied — skip
                _locked = True
            else:
                _locked = False

            try:
                # Re-read under lock to capture all entries
                _final_entries = _load_journal(journal_path)
                # Apply backfill to the fresh snapshot
                _backfill_map: dict[str, dict[str, Any]] = {}
                for e in entries:
                    _mid = e.get("message_id", "")
                    if _mid and (not e.get("magic") or not e.get("strategy")):
                        _backfill_map[_mid] = {
                            "magic": e.get("magic") or _resolve_magic(e),
                            "strategy": e.get("strategy") or _resolve_strategy(e),
                        }
                for _fe in _final_entries:
                    _fid = _fe.get("message_id", "")
                    if _fid in _backfill_map:
                        _bf = _backfill_map[_fid]
                        if not _fe.get("magic") and _bf["magic"]:
                            _fe["magic"] = _bf["magic"]
                        if not _fe.get("strategy") and _bf["strategy"]:
                            _fe["strategy"] = _bf["strategy"]
                # ── DQAF-20260620-001: two-pass dedup ──────────────────────
                # Pass 1: message_id dedup (catches same-message retries).
                _seen: set[str] = set()
                _deduped: list[dict[str, Any]] = []
                for _e in _final_entries:
                    _mid = _e.get("message_id", "")
                    if _mid and _mid in _seen:
                        continue
                    if _mid:
                        _seen.add(_mid)
                    _deduped.append(_e)
                _deduped_msg_count = len(_final_entries) - len(_deduped)

                # ── Pass 2: position_ticket dedup for close entries ──
                # FIX-20260612-023 detected but the rewrite path was
                # only applying message_id dedup — ticket-based duplicates
                # (same ticket, different message_ids from bridge vs
                # execution queue) survived every repair attempt.
                _close_indices: dict[int, list[int]] = {}
                for _i, _e in enumerate(_deduped):
                    if _e.get("action") != "close":
                        continue
                    _t = _e.get("position_ticket")
                    if _t and isinstance(_t, int) and _t > 0:
                        _close_indices.setdefault(_t, []).append(_i)

                _drop_indices: set[int] = set()
                for _ticket, _group in _close_indices.items():
                    if len(_group) <= 1:
                        continue
                    # Keep best: closed > accepted > rejected, prefer larger abs(PnL)
                    _ack_map = {"closed": 0, "accepted": 1, "rejected": 2}
                    _group.sort(key=lambda _i: (
                        _ack_map.get(_deduped[_i].get("ack_status", ""), 99),
                        -(1 if (_deduped[_i].get("detail", {}).get("close_price") or 0) > 0 else 0),
                        -abs(_deduped[_i].get("pnl") or 0),
                    ))
                    for _idx in _group[1:]:
                        _drop_indices.add(_idx)

                if _drop_indices:
                    _deduped = [_e for _i, _e in enumerate(_deduped) if _i not in _drop_indices]

                report["duplicates_removed"] += _deduped_msg_count + len(_drop_indices)

                # Write to temp file, then atomic swap
                _temp_path.write_text(
                    "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in _deduped) + "\n",
                    encoding="utf-8",
                )
                _os.replace(_temp_path, journal_path)
            finally:
                if _locked:
                    _lock.release()
                if _temp_path.exists():
                    import contextlib
                    with contextlib.suppress(OSError):
                        _temp_path.unlink()

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
    lock_dir: Path | None = None,  # FIX-20260601-043
) -> dict[str, Any]:
    """Run repair_journal then cleanup_orphan_opens in a single pass.

    This is the canonical entry point for startup journal maintenance.
    Call it once at the beginning of every live pipeline session.
    """
    repair_report = repair_journal(journal_path, dry_run=dry_run, lock_dir=lock_dir)
    orphan_count = cleanup_orphan_opens(journal_path, max_age_hours=max_age_hours, dry_run=dry_run, lock_dir=lock_dir)
    repair_report["orphans_closed"] = orphan_count
    return repair_report


def compact_journal(
    journal_path: Path,
    *,
    retention_days: int = 30,
    dry_run: bool = False,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Remove old rejected entries from the journal with atomic swap.

    FIX-20260607-144: Journal compaction for rejected retry entries.
    Rejected entries (ack_status="rejected") are ephemeral noise — failed
    dispatch attempts that carry no durable trade outcome.  They are retained
    for *retention_days* for post-mortem analysis, then pruned.

    Uses atomic ``os.replace()`` so a crash during compaction cannot corrupt
    the journal.  Acquires the same ``FileLock`` as ``_append_journal()``
    to prevent bridge/live_cycle concurrent writes during the rewrite.

    Safety confirmed (Phase 1):
      - ``restart_state.py`` reverse-scans journal and deduplicates by
        ``open_message_id``.  Only the last close per position matters.
        Removing old rejected entries does not affect restart behaviour.
      - Journal writes use ``open(path, "a")`` (lazy-writer pattern).
        No persistent file handle exists — ``os.replace()`` is safe.

    Args:
        journal_path: Path to live_trade_journal.jsonl.
        retention_days: How many days to keep rejected entries (default 30).
        dry_run: If True, report counts but do not modify the journal.
        lock_dir: Directory for FileLock (same as append path).

    Returns:
        Dict with ``retained``, ``removed``, ``dry_run`` counts.
    """
    import logging
    import os as _os
    import time as _time

    _log = logging.getLogger(__name__)

    if not journal_path.exists():
        return {"status": "empty", "retained": 0, "removed": 0}

    cutoff = _time.time() - (retention_days * 24 * 3600)
    temp_path = journal_path.with_suffix(".jsonl.tmp")
    retained = 0
    removed = 0
    corrupted = 0

    # ── Acquire FileLock (same lock as _append_journal) ──
    _lock_acquired = False
    _lock = None
    if lock_dir is not None:
        from core.infrastructure.distributed_lock import FileLock

        _lock = FileLock("live_trade_journal", lock_dir=str(lock_dir), ttl_seconds=10)
        _acquired = _lock.acquire(blocking=True, timeout_seconds=5)
        _lock_acquired = _acquired.acquired if _acquired else False

    try:
        # ── Pass 1: filter → temp file ──
        with open(journal_path, encoding="utf-8") as f_in, \
             open(temp_path, "w", encoding="utf-8") as f_out:
            for line in f_in:
                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    # Corrupted line — keep it (conservative, human review)
                    f_out.write(line)
                    corrupted += 1
                    continue

                ack = record.get("ack_status", "")
                if ack != "rejected":
                    # Always retain non-rejected entries
                    f_out.write(line)
                    retained += 1
                    continue

                # Rejected entry — check age
                ts_str = record.get("recorded_at", "")
                record_ts = 0.0
                if ts_str:
                    try:
                        from datetime import datetime
                        record_ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).timestamp()
                    except (ValueError, TypeError):
                        pass

                if record_ts >= cutoff:
                    # Within retention window — keep
                    f_out.write(line)
                    retained += 1
                else:
                    # Outside retention window — prune
                    removed += 1

        # ── Pass 2: atomic swap ──
        if not dry_run and removed > 0:
            _os.replace(temp_path, journal_path)
            _log.info(
                "Journal compaction complete: retained=%d removed=%d (old rejected, >%dd)",
                retained, removed, retention_days,
            )
        elif dry_run and removed > 0:
            _log.info(
                "Journal compaction DRY-RUN: would retain=%d remove=%d (old rejected, >%dd)",
                retained, removed, retention_days,
            )
        else:
            _log.debug("Journal compaction: nothing to remove")

    except Exception as exc:  # BLE001:FOG
        _log.error("Journal compaction failed: %s", exc, exc_info=True)
        # Clean up temp file on failure
        import contextlib
        with contextlib.suppress(OSError):
            if temp_path.exists():
                temp_path.unlink()
        return {"status": "error", "error": str(exc)[:200]}
    finally:
        if _lock_acquired and _lock is not None:
            _lock.release()
        # Clean up temp file if still present (e.g. dry run or no-op)
        import contextlib
        with contextlib.suppress(OSError):
            if temp_path.exists() and (dry_run or removed == 0):
                temp_path.unlink()

    return {
        "status": "ok",
        "retained": retained,
        "removed": removed,
        "corrupted_lines": corrupted,
        "dry_run": dry_run,
    }

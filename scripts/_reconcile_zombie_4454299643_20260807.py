#!/usr/bin/env python3
"""One-shot legal reconciliation — resurrect ticket 4454299643's quarantined
PnL corpse back into the SSOT main ledger.

DQAF-20260807-003 (IC 裁决 Step 3 — The Zombie Resurrection): a legitimate
XAUUSDc m30_swing short (open 2026-08-07T06:10Z, vol=0.02) closed at
08:34:15Z (sl_hit_first, PnL=−66.30, verified_from_mt5_deal), but its close
was FALSE-quarantined by the pre-fix STATE-FUL JournalGate (multi-process
``_known_tickets`` drift → ``close_without_open``).  The close never entered
the SSOT ledger — downstream audit (DQAF-20260807-002 memo) misread it as
"still open" because the main journal only carries the open leg.

This script performs the LEGAL reconciliation the IC ordered:
  1. Read the corpse from journal_orphan_quarantine.jsonl (verified PnL).
  2. Verify the open leg exists in the main journal (position_identifier).
  3. Verify NO close leg exists yet in the main journal (idempotent).
  4. Rebuild the close record with the PHYSICAL dispatch volume (0.02 —
     IC 裁决 2a: 记账必须以物理派发结果为唯一真值; the corpse's 0.0 is
     the ghost-volume fingerprint; PnL −66.30 self-proves 0.02 lots:
     32.807pts × $2/pt ≈ −66.3).
  5. Append via the OFFICIAL write path (_append_journal + Stateless Gate
     + FileLock) — the same chokepoint settlement_queue uses.
  6. Append an audit marker to quarantine (append-only chain).

Iron Law #11: stdout is the only legal evidence source.  Idempotent — safe
to re-run; a close leg already present ⇒ no-op.

Usage:
    python scripts/_reconcile_zombie_4454299643_20260807.py --dry-run
    python scripts/_reconcile_zombie_4454299643_20260807.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TICKET = 4454299643
OPEN_MESSAGE_ID = "eq_m30_swing_c35f96182aa6"
PHYSICAL_VOLUME = 0.02  # from open leg vol=0.02 (IC 裁决 2a physical dispatch truth)
DATA_DIR = Path("data")
JOURNAL_PATH = DATA_DIR / "live_trade_journal.jsonl"
QUARANTINE_PATH = DATA_DIR / "journal_orphan_quarantine.jsonl"
LOCK_DIR = DATA_DIR / "locks"


def _load_corpse() -> dict | None:
    """Return the quarantined settlement_verified record for TICKET."""
    if not QUARANTINE_PATH.exists():
        return None
    for line in QUARANTINE_PATH.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("position_identifier") == TICKET or rec.get("position_ticket") == TICKET:
            if str(rec.get("message_id", "")).startswith("settlement_verified"):
                return rec
    return None


def _open_legs() -> list[dict]:
    """Return all open/close legs for TICKET in the main journal."""
    if not JOURNAL_PATH.exists():
        return []
    legs = []
    for line in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("position_identifier") == TICKET or rec.get("position_ticket") == TICKET:
            legs.append(rec)
    return legs


def _main() -> int:
    parser = argparse.ArgumentParser(description="Legal reconciliation backfill for 4454299643")
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not write")
    parser.add_argument(
        "--no-quarantine-marker", action="store_true", help="Skip quarantine audit marker"
    )
    args = parser.parse_args()

    report: dict = {"ticket": TICKET, "action": "dry_run" if args.dry_run else "write"}

    # ── 1. corpse exists? ─────────────────────────────────────────────
    corpse = _load_corpse()
    if corpse is None:
        print(json.dumps({**report, "status": "no_corpse"}, ensure_ascii=False))
        return 0
    report["corpse_found"] = True
    report["corpse_pnl"] = corpse.get("pnl")
    report["corpse_volume"] = corpse.get("volume")
    report["corpse_close_time"] = corpse.get("close_time")
    report["corpse_quarantine_reason"] = corpse.get("_quarantine_reason")

    # ── 2. open leg in main journal? ─────────────────────────────────
    legs = _open_legs()
    opens = [l for l in legs if l.get("action") == "open"]
    closes = [l for l in legs if l.get("action") == "close"]
    report["main_journal_opens"] = len(opens)
    report["main_journal_closes"] = len(closes)
    report["open_volume"] = opens[0].get("volume") if opens else None

    if not opens:
        print(json.dumps({**report, "status": "no_open_leg"}, ensure_ascii=False))
        return 1  # safety: never resurrect a close without its open

    if closes:
        print(json.dumps({**report, "status": "already_settled"}, ensure_ascii=False))
        return 0  # idempotent no-op

    # ── 3. rebuild close with physical volume ─────────────────────────
    # Remove quarantine-only markers; stamp reconciliation provenance.
    rebuilt = {k: v for k, v in corpse.items() if not k.startswith("_quarantine")}
    rebuilt["volume"] = PHYSICAL_VOLUME  # IC 2a: physical dispatch truth
    rebuilt["_source"] = "zombie_reconcile_backfill"
    rebuilt["_backfilled_at"] = corpse.get("recorded_at", "")
    rebuilt["_backfill_of"] = "journal_orphan_quarantine"
    rebuilt.pop("_quarantined_at", None)
    rebuilt.pop("_quarantine_reason", None)

    report["rebuilt_close"] = {
        "action": rebuilt.get("action"),
        "message_id": rebuilt.get("message_id"),
        "volume": rebuilt.get("volume"),
        "pnl": rebuilt.get("pnl"),
        "_pnl_status": rebuilt.get("_pnl_status"),
        "label": rebuilt.get("label"),
        "close_time": rebuilt.get("close_time"),
        "entry_price": rebuilt.get("entry_price"),
        "exit_price": rebuilt.get("exit_price"),
        "position_identifier": rebuilt.get("position_identifier"),
    }

    if args.dry_run:
        print(json.dumps({**report, "status": "ready_to_write"}, ensure_ascii=False))
        return 0

    # ── 4. official write path (Stateless Gate + FileLock + dedup) ────
    from core.ledger.services.journal_cleanup import _append_journal
    from core.ledger.services.journal_gate import JournalGate

    gate = JournalGate(JOURNAL_PATH, policy="quarantine")
    if not gate.validate_close(rebuilt):
        print(json.dumps({**report, "status": "gate_rejected"}, ensure_ascii=False))
        return 1  # gate must admit now (stateless: open leg is on disk)

    try:
        _append_journal(JOURNAL_PATH, rebuilt, lock_dir=LOCK_DIR, gate=gate)
    except Exception as exc:  # noqa: BLE001 — one-shot reconcile must report
        print(
            json.dumps(
                {**report, "status": "write_failed", "error": str(exc)[:200]}, ensure_ascii=False
            )
        )
        return 1

    # ── 5. verify landed ──────────────────────────────────────────────
    legs_after = _open_legs()
    closes_after = [l for l in legs_after if l.get("action") == "close"]
    landed = any(
        c.get("message_id") == f"settlement_verified_{TICKET}"
        and c.get("_source") == "zombie_reconcile_backfill"
        for c in closes_after
    )
    report["verified_close_legs"] = len(closes_after)
    report["status"] = "landed" if landed else "verify_mismatch"

    # ── 6. quarantine audit marker (append-only) ──────────────────────
    if not args.no_quarantine_marker and landed:
        from datetime import UTC, datetime

        marker = {
            "event": "zombie_reconciled",
            "ticket": TICKET,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "reconciled_to": str(JOURNAL_PATH),
            "pnl": corpse.get("pnl"),
            "volume": PHYSICAL_VOLUME,
            "backfill_source": "zombie_reconcile_backfill",
            "dqaf": "DQAF-20260807-003",
        }
        with open(QUARANTINE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(marker, ensure_ascii=False) + "\n")
        report["quarantine_marker"] = "appended"

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

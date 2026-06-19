# type: ignore
#!/usr/bin/env python
"""Backfill null PnL in live_trade_journal.jsonl.

Two strategies, applied in order:

  Strategy A — From close_price (``closed`` entries):
    PnL = (close_price - entry_price) × volume × contract_size
    Entry price resolved from matching open journal entry.

  Strategy B — From MT5 deal history (``close_accepted`` entries):
    Queries ``mt5.history_deals_get(position=ticket)`` to extract
    actual fill profit from the broker.

Usage::

    # Dry-run (report only, no changes)
    python scripts/backfill_journal_pnl.py --base-dir data_btc --dry-run

    # Live run
    python scripts/backfill_journal_pnl.py --base-dir data_btc

Requires MT5 terminal to be running for Strategy B.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_journal(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def _find_open_for_ticket(
    entries: list[dict[str, Any]], ticket: int
) -> dict[str, Any] | None:
    """Find the open journal entry for a position ticket."""
    for e in entries:
        if e.get("action") == "open" and e.get("position_ticket") == ticket:
            return e
    return None


def _resolve_entry_price(open_entry: dict[str, Any] | None) -> float | None:
    """Extract entry price from an open journal entry."""
    if open_entry is None:
        return None
    detail = open_entry.get("detail", {})
    if not isinstance(detail, dict):
        return None
    req = detail.get("request", {})
    if isinstance(req, dict) and req.get("price"):
        return float(req["price"])
    # Fallback: use SL to estimate entry (SL is set relative to entry)
    sl = open_entry.get("sl")
    tp = open_entry.get("tp")
    side = open_entry.get("side", "")
    if sl and tp and side:
        sl_val = float(sl)
        tp_val = float(tp)
        if side == "long":
            return sl_val  # SL below entry — rough lower bound
        else:
            return sl_val  # SL above entry — rough upper bound
    return None


def _backfill_from_close_price(
    entries: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Strategy A: calculate PnL from close_price for closed entries."""
    report = {"strategy": "A_close_price", "fixed": 0, "skipped": 0, "details": []}

    for e in entries:
        if e.get("action") != "close":
            continue
        if e.get("pnl") is not None:
            continue
        if e.get("ack_status") != "closed":
            continue

        ticket = e.get("position_ticket")
        detail = e.get("detail", {})
        close_price = detail.get("close_price") if isinstance(detail, dict) else None
        if close_price is None:
            report["skipped"] += 1
            report["details"].append(
                {"ticket": ticket, "reason": "no_close_price", "ack": e.get("ack_status")}
            )
            continue

        open_entry = _find_open_for_ticket(entries, ticket)
        entry_price = _resolve_entry_price(open_entry)
        side = e.get("side", "")
        # Prefer open entry volume — close entries often have vol=0.0 for full closes
        _close_vol = float(e.get("volume", 0) or 0)
        _open_vol = float(open_entry.get("volume", 0) or 0) if open_entry else 0.0
        volume = _open_vol if _open_vol > 0 else (_close_vol if _close_vol > 0 else 0.01)

        cp = float(close_price)
        if entry_price and side:
            ep = float(entry_price)
            if side == "short":
                pnl = round((ep - cp) * volume, 2)
            else:
                pnl = round((cp - ep) * volume, 2)

            if not dry_run:
                e["pnl"] = pnl
                if isinstance(e.get("detail"), dict):
                    e["detail"]["profit"] = pnl

            report["fixed"] += 1
            report["details"].append(
                {
                    "ticket": ticket,
                    "side": side,
                    "entry": ep,
                    "close": cp,
                    "pnl": pnl,
                    "method": "close_price_calc",
                }
            )
        else:
            report["skipped"] += 1
            report["details"].append(
                {
                    "ticket": ticket,
                    "reason": "no_entry_price" if not entry_price else "no_side",
                    "close_price": cp,
                }
            )

    return report


def _backfill_from_mt5(
    entries: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Strategy B: query MT5 deal history for close_accepted entries."""
    report = {"strategy": "B_mt5_history", "fixed": 0, "skipped": 0, "mt5_available": False, "details": []}

    # Try to connect to MT5
    mt5 = None
    try:
        import MetaTrader5 as _mt5_module

        if mt5_terminal_path:
            if not _mt5_module.initialize(path=mt5_terminal_path):
                report["reason"] = f"mt5_init_failed: {_mt5_module.last_error()}"
                return report
        else:
            if not _mt5_module.initialize():
                report["reason"] = f"mt5_init_failed: {_mt5_module.last_error()}"
                return report
        mt5 = _mt5_module
        report["mt5_available"] = True
    except ImportError:
        report["reason"] = "MetaTrader5 module not installed"
        return report
    except Exception as exc:
        report["reason"] = f"mt5_init_exception: {exc}"
        return report

    try:
        for e in entries:
            if e.get("action") != "close":
                continue
            if e.get("pnl") is not None:
                continue

            ticket = e.get("position_ticket")
            if ticket is None:
                continue

            # Query deal history
            deals = mt5.history_deals_get(position=int(ticket))
            if not deals or len(deals) == 0:
                report["skipped"] += 1
                report["details"].append(
                    {"ticket": ticket, "reason": "no_deals_found"}
                )
                continue

            # Find exit deal(s) — entry=1 means exit (out)
            exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
            if not exit_deals:
                report["skipped"] += 1
                report["details"].append(
                    {"ticket": ticket, "reason": "no_exit_deals", "total_deals": len(deals)}
                )
                continue

            last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
            profit = getattr(last_exit, "profit", None)
            price = getattr(last_exit, "price", None)
            volume = getattr(last_exit, "volume", None)

            if profit is not None:
                pnl = round(float(profit), 2)
                if not dry_run:
                    e["pnl"] = pnl
                    if isinstance(e.get("detail"), dict):
                        e["detail"]["profit"] = float(profit)
                    if price is not None and float(price) > 0:
                        e["detail"]["close_price"] = float(price)
                    if volume is not None:
                        e["detail"]["fill_volume"] = float(volume)

                report["fixed"] += 1
                report["details"].append(
                    {
                        "ticket": ticket,
                        "pnl": pnl,
                        "close_price": float(price) if price else None,
                        "method": "mt5_deal_history",
                    }
                )
            else:
                report["skipped"] += 1
                report["details"].append(
                    {"ticket": ticket, "reason": "no_profit_in_deal"}
                )
    finally:
        if mt5:
            mt5.shutdown()

    return report


def _rewrite_journal(
    journal_path: Path, entries: list[dict[str, Any]], *, lock_dir: Path | None = None
) -> bool:
    """Atomically rewrite the journal with backfilled entries."""
    if lock_dir is not None:
        from core.infrastructure.distributed_lock import FileLock

        lock = FileLock("live_trade_journal", lock_dir=str(lock_dir), ttl_seconds=10)
        acquired = lock.acquire(blocking=True, timeout_seconds=5)
        if not acquired.acquired:
            print(f"  [SKIP] Could not acquire journal lock: {acquired.error}")
            return False
        try:
            journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in entries) + "\n",
                encoding="utf-8",
            )
        finally:
            lock.release()
    else:
        journal_path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in entries) + "\n",
            encoding="utf-8",
        )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill null PnL in trade journal")
    parser.add_argument("--base-dir", default="data_btc", help="Data directory")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no changes")
    parser.add_argument(
        "--mt5-terminal-path",
        default=r"D:\MetaTrader 5\terminal64.exe",
        help="MT5 terminal path for Strategy B",
    )
    parser.add_argument("--skip-mt5", action="store_true", help="Skip MT5 deal history query")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir)
    journal_path = base_dir / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print(f"Journal not found: {journal_path}")
        return 1

    entries = _load_journal(journal_path)
    null_count = sum(1 for e in entries if e.get("action") == "close" and e.get("pnl") is None)
    print(f"Journal: {len(entries)} entries, {null_count} close entries with null PnL")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    # Strategy A: close_price calculation
    report_a = _backfill_from_close_price(entries, dry_run=args.dry_run)
    print(f"Strategy A (close_price calc): fixed={report_a['fixed']} skipped={report_a['skipped']}")
    for d in report_a.get("details", []):
        if d.get("method"):
            print(f"  OK ticket={d['ticket']} {d.get('side','')} entry={d.get('entry')} close={d.get('close')} pnl={d.get('pnl')}")
        else:
            print(f"  SKIP ticket={d.get('ticket')} reason={d.get('reason')}")

    # Strategy B: MT5 deal history
    if not args.skip_mt5:
        report_b = _backfill_from_mt5(
            entries,
            dry_run=args.dry_run,
            mt5_terminal_path=args.mt5_terminal_path,
        )
        print(f"\nStrategy B (MT5 history): fixed={report_b['fixed']} skipped={report_b['skipped']} mt5={report_b['mt5_available']}")
        if not report_b["mt5_available"]:
            print(f"  ⚠️  MT5 not available: {report_b.get('reason', 'unknown')}")
        for d in report_b.get("details", []):
            if d.get("method"):
                print(f"  [FIX] ticket={d['ticket']} pnl={d.get('pnl')} close={d.get('close_price')}")
            else:
                print(f"  SKIP ticket={d.get('ticket')} reason={d.get('reason')}")
    else:
        report_b = {"fixed": 0}

    total_fixed = report_a["fixed"] + report_b["fixed"]
    remaining = null_count - total_fixed

    print(f"\n{'=' * 50}")
    print(f"  Total fixed: {total_fixed}/{null_count}")
    print(f"  Remaining null: {remaining}")
    print(f"  Mode: {'DRY RUN — no changes' if args.dry_run else 'LIVE'}")

    if total_fixed > 0 and not args.dry_run:
        lock_dir = base_dir / "locks"
        ok = _rewrite_journal(journal_path, entries, lock_dir=lock_dir)
        if ok:
            print(f"  Journal rewritten: {journal_path}")
        else:
            print("  [FAIL] Could not rewrite journal")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

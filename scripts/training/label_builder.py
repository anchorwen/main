"""Build P&L labels from live_trade_journal.jsonl for training data closed-loop.

Matches open/close actions by position_ticket, extracts entry/exit prices from
journal detail fields, and computes P&L labels.

Output schema: training_label.v1 — one JSONL record per completed trade.

Usage:
  python scripts/training/label_builder.py --journal data/live_trade_journal.jsonl
  python scripts/training/label_builder.py --journal data/live_trade_journal.jsonl --date 2026-05-04
  python scripts/training/label_builder.py --journal data/live_trade_journal.jsonl --output data/labels/training_labels.jsonl
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "training_label.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_journal_entries(
    journal_path: Path, *, date_filter: str | None = None
) -> list[dict[str, Any]]:
    """Parse .jsonl journal file, optionally filtered by date."""
    entries: list[dict[str, Any]] = []
    if not journal_path.exists():
        return entries
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_filter:
            recorded = str(rec.get("recorded_at", ""))
            if not recorded.startswith(date_filter):
                continue
        entries.append(rec)
    return entries


def _extract_entry_price(detail: dict[str, Any] | None) -> float | None:
    """Extract entry/fill price from journal detail block."""
    if not detail or not isinstance(detail, dict):
        return None
    order = detail.get("order")
    if isinstance(order, dict):
        price = order.get("price") or order.get("price_open")
        if price is not None:
            return float(price)
    return None


def _extract_exit_price(detail: dict[str, Any] | None) -> float | None:
    """Extract exit/close price from journal detail (for close actions)."""
    if not detail or not isinstance(detail, dict):
        return None
    order = detail.get("order")
    if isinstance(order, dict):
        price = order.get("price") or order.get("price_close") or order.get("price_current")
        if price is not None:
            return float(price)
    return None


def _compute_pnl(
    side: str,
    entry_price: float | None,
    exit_price: float | None,
    volume: float | None,
) -> float | None:
    """Compute P&L in price units (before volume scaling)."""
    if entry_price is None or exit_price is None:
        return None
    pnl = exit_price - entry_price if side == "long" else entry_price - exit_price
    if volume is not None and volume > 0:
        pnl *= volume
    return round(pnl, 6)


def _classify_label(pnl: float | None) -> str:
    """Classify P&L into training label category."""
    if pnl is None:
        return "unlabeled"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def build_trade_records(
    journal_path: Path,
    *,
    date_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Build trade records from journal entries by matching open/close pairs."""
    entries = _read_journal_entries(journal_path, date_filter=date_filter)
    if not entries:
        return []

    # Group by position_ticket
    by_ticket: dict[int, list[dict[str, Any]]] = {}
    unlinked: list[dict[str, Any]] = []

    for rec in entries:
        ticket = rec.get("position_ticket")
        if ticket is not None and isinstance(ticket, int) and ticket > 0:
            by_ticket.setdefault(ticket, []).append(rec)
        else:
            unlinked.append(rec)

    trades: list[dict[str, Any]] = []

    for ticket, recs in by_ticket.items():
        opens = [r for r in recs if r.get("action") == "open"]
        closes = [r for r in recs if r.get("action") in ("close", "modify")]

        for i, open_rec in enumerate(opens):
            close_rec = closes[i] if i < len(closes) else None

            side = str(open_rec.get("side", ""))
            entry_price = _extract_entry_price(open_rec.get("detail"))
            exit_price = _extract_exit_price(close_rec.get("detail")) if close_rec else None
            volume = open_rec.get("effective_volume_hint") or open_rec.get("volume")

            pnl = _compute_pnl(side, entry_price, exit_price, volume)

            trade: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "label_id": f"label_ticket_{ticket}_{i}",
                "position_ticket": ticket,
                "symbol": open_rec.get("symbol", ""),
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "label": _classify_label(pnl),
                "volume": volume,
                "open_message_id": open_rec.get("message_id"),
                "open_recorded_at": open_rec.get("recorded_at"),
                "close_message_id": close_rec.get("message_id") if close_rec else None,
                "close_recorded_at": close_rec.get("recorded_at") if close_rec else None,
                "is_closed": close_rec is not None,
                "open_ack_status": open_rec.get("ack_status"),
                "sl": open_rec.get("sl"),
                "tp": open_rec.get("tp"),
            }
            trades.append(trade)

    # Add unlinked records as unlabeled
    for rec in unlinked:
        if rec.get("action") != "open":
            continue
        side = str(rec.get("side", ""))
        entry_price = _extract_entry_price(rec.get("detail"))
        volume = rec.get("effective_volume_hint") or rec.get("volume")

        trade = {
            "schema_version": SCHEMA_VERSION,
            "label_id": f"label_unlinked_{rec.get('message_id', 'unknown')[:20]}",
            "position_ticket": None,
            "symbol": rec.get("symbol", ""),
            "side": side,
            "entry_price": entry_price,
            "exit_price": None,
            "pnl": None,
            "label": "unlabeled",
            "volume": volume,
            "open_message_id": rec.get("message_id"),
            "open_recorded_at": rec.get("recorded_at"),
            "close_message_id": None,
            "close_recorded_at": None,
            "is_closed": False,
            "open_ack_status": rec.get("ack_status"),
            "sl": rec.get("sl"),
            "tp": rec.get("tp"),
        }
        trades.append(trade)

    return trades


def build_basic_stats_report(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build aggregate label statistics."""
    if not records:
        return {
            "schema_version": "label_stats.v1",
            "generated_at": _utc_now_iso(),
            "total_records": 0,
        }

    wins = sum(1 for r in records if r["label"] == "win")
    losses = sum(1 for r in records if r["label"] == "loss")
    breakeven = sum(1 for r in records if r["label"] == "breakeven")
    unlabeled = sum(1 for r in records if r["label"] == "unlabeled")
    closed = sum(1 for r in records if r["is_closed"])
    pnls = [r["pnl"] for r in records if r["pnl"] is not None]

    return {
        "schema_version": "label_stats.v1",
        "generated_at": _utc_now_iso(),
        "total_records": len(records),
        "closed_trades": closed,
        "open_trades": len(records) - closed,
        "labels": {
            "win": wins,
            "loss": losses,
            "breakeven": breakeven,
            "unlabeled": unlabeled,
        },
        "pnl_summary": {
            "total_pnl": round(sum(pnls), 6) if pnls else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else None,
            "max_pnl": round(max(pnls), 6) if pnls else None,
            "min_pnl": round(min(pnls), 6) if pnls else None,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="label_builder")
    p.add_argument(
        "--journal",
        type=Path,
        required=True,
        help="Path to live_trade_journal.jsonl",
    )
    p.add_argument(
        "--date",
        default=None,
        help="ISO date filter (UTC), e.g. 2026-05-04",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write JSONL labels to file (default: stdout)",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print summary statistics only",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = build_trade_records(Path(args.journal), date_filter=args.date)

    if args.stats:
        stats = build_basic_stats_report(records)
        print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
        return 0

    lines = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(lines + "\n", encoding="utf-8")
        print(f"[label_builder] Wrote {len(records)} labels to {out}")
    else:
        print(lines)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

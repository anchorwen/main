"""Build quality report from live trade journal."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trade_quality_report")
    parser.add_argument("--journal-path", default="data/live_trade_journal.jsonl")
    parser.add_argument(
        "--date", default=None, help="UTC date key like 2026-04-28; default=today UTC"
    )
    parser.add_argument("--symbol", default=None, help="Optional symbol filter for journal rows")
    parser.add_argument("--output", default=None)
    return parser


def _today_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _iter_records(journal_path: Path):
    if not journal_path.exists():
        return
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def consecutive_rejected_suffix(records: list[dict]) -> int:
    """Count trailing rejected ack_status entries from the end of the day list."""
    n = 0
    for r in reversed(records):
        if str(r.get("ack_status", "")).lower() == "rejected":
            n += 1
        else:
            break
    return n


def build_report(
    *, journal_path: str, date_key: str | None = None, symbol: str | None = None
) -> dict:
    date_key = date_key or _today_key()
    records = []
    for rec in _iter_records(Path(journal_path)) or []:
        if not str(rec.get("recorded_at", "")).startswith(date_key):
            continue
        if symbol is not None:
            sym = rec.get("symbol") or (rec.get("detail") or {}).get("symbol")
            if str(sym or "").upper() != symbol.upper():
                continue
        records.append(rec)
    counts = Counter(str(r.get("ack_status", "other")).lower() for r in records)
    total = len(records)
    rejected_reasons: Counter[str] = Counter()
    for r in records:
        if str(r.get("ack_status", "")).lower() == "rejected":
            detail = r.get("detail", {}) or {}
            rejected_reasons[str(detail.get("reason", "unknown"))] += 1
    acceptance_rate = (counts.get("accepted", 0) / total) if total else 0.0
    rejection_rate = (counts.get("rejected", 0) / total) if total else 0.0
    return {
        "schema_version": "trade_quality_report.v1",
        "generated_at": datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "date_key": date_key,
        "journal_path": str(journal_path),
        "total": total,
        "counts": {
            "accepted": counts.get("accepted", 0),
            "acknowledged": counts.get("acknowledged", 0),
            "rejected": counts.get("rejected", 0),
            "other": total
            - counts.get("accepted", 0)
            - counts.get("acknowledged", 0)
            - counts.get("rejected", 0),
        },
        "acceptance_rate": round(acceptance_rate, 6),
        "rejection_rate": round(rejection_rate, 6),
        "rejected_reasons": dict(rejected_reasons),
        "live_consecutive_rejected_tail": consecutive_rejected_suffix(records),
        "latest_records": records[-10:],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(journal_path=args.journal_path, date_key=args.date, symbol=args.symbol)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

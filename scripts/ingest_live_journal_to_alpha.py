"""Ingest live trade journal quality report into AlphaPerformanceStore."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.alpha.performance_store import AlphaPerformanceStore
from core.runtime.schema_versions import SCHEMA_ALPHA_LIVE_BRIDGE_INGESTION
from scripts.trade_quality_report import build_report


def _load_store(path: Path) -> AlphaPerformanceStore:
    store = AlphaPerformanceStore()
    if not path.exists():
        return store
    payload = json.loads(path.read_text(encoding="utf-8"))
    for summary in payload.get("summaries", []):
        for snapshot in summary.get("history", []):
            store.record_snapshot(
                snapshot["alpha_id"],
                snapshot.get("metrics", {}),
                source=snapshot.get("source", "file"),
                window=snapshot.get("window", "latest"),
            )
    return store


def _save_store(path: Path, store: AlphaPerformanceStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = store.to_dict()
    payload["summaries"] = [
        {
            **store.summarize(alpha_id),
            "history": [snap.to_dict() for snap in store.history(alpha_id)],
        }
        for alpha_id in sorted(store._snapshots)
    ]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingest_live_journal_to_alpha")
    parser.add_argument(
        "--base-dir", default="data", help="Directory containing alpha_performance.json"
    )
    parser.add_argument(
        "--journal-path",
        default=None,
        help="Journal JSONL path; default <base-dir>/live_trade_journal.jsonl",
    )
    parser.add_argument(
        "--date", default=None, help="UTC date key like 2026-04-28; default=today UTC"
    )
    parser.add_argument("--alpha-id", required=True)
    parser.add_argument(
        "--symbol", default=None, help="Optional symbol filter passed to build_report"
    )
    parser.add_argument(
        "--output", default=None, help="Optional path to write ingestion result JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    journal_path = args.journal_path or str(base / "live_trade_journal.jsonl")
    report = build_report(journal_path=journal_path, date_key=args.date, symbol=args.symbol)
    perf_path = base / "alpha_performance.json"
    store = _load_store(perf_path)
    snapshot = store.ingest_live_bridge_report(
        args.alpha_id,
        report,
        journal_source_path=str(Path(journal_path).resolve()),
        symbol_filter=args.symbol,
    )
    _save_store(perf_path, store)
    payload = {
        "schema_version": SCHEMA_ALPHA_LIVE_BRIDGE_INGESTION,
        "alpha_id": args.alpha_id,
        "performance_file": str(perf_path.resolve()),
        "snapshot": snapshot.to_dict(),
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    print(rendered)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

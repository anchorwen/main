"""Read-only Shadow vs Live comparison report (observation / audit only; no promotion)."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.trade_quality_report import build_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow_live_compare_report")
    parser.add_argument("--date", required=True, help="UTC date key YYYY-MM-DD")
    parser.add_argument("--symbol", required=True, help="Instrument symbol e.g. XAUUSDc")
    parser.add_argument(
        "--journal-path",
        default=None,
        help="Live journal JSONL; default <base-dir>/live_trade_journal.jsonl",
    )
    parser.add_argument("--base-dir", default="data")
    parser.add_argument(
        "--shadow-baseline-json",
        default=None,
        help="Optional V9 regression baseline JSON (results[] entries with symbol field)",
    )
    parser.add_argument("--output", default=None)
    return parser


def _load_shadow_baseline(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _summarize_shadow_payload(payload: dict | None, symbol: str) -> dict | None:
    if not payload:
        return None
    results = payload.get("results") or []
    sym_u = symbol.upper()
    rows = [r for r in results if str(r.get("symbol", "")).upper() == sym_u]
    if not rows:
        return {
            "matched_rows": 0,
            "total_results_in_file": len(results),
            "by_action": {},
            "by_dispatch_status": {},
            "note": "no_matching_symbol_in_baseline_results",
        }
    actions = Counter(str(r.get("action", "")) for r in rows)
    disp = Counter(str(r.get("dispatch_status", "")) for r in rows)
    risks = Counter(str(r.get("risk_status", "")) for r in rows)
    return {
        "matched_rows": len(rows),
        "total_results_in_file": len(results),
        "by_action": dict(actions),
        "by_dispatch_status": dict(disp),
        "by_risk_status": dict(risks),
        "scenarios": sorted({str(r.get("scenario", "")) for r in rows}),
    }


def _parity_notes(shadow: dict | None, live: dict) -> list[str]:
    notes: list[str] = []
    if shadow is None:
        notes.append("shadow_baseline_missing_or_empty")
    elif shadow.get("matched_rows") == 0:
        notes.append("shadow_symbol_no_rows_compare_live_only")
    lr = live.get("rejection_rate")
    if lr is not None and lr > 0:
        notes.append("live_journal_reports_rejections_same_day")
    if not notes:
        notes.append("compare_summary_only_no_automatic_parity_claim")
    return notes


def _load_shadow_config(base_dir: Path) -> dict | None:
    config_path = base_dir.parent / "configs" / "live_shadow_config.json"
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _summarize_shadow_outbox(shadow_outbox_root: Path, date_key: str) -> dict[str, Any]:
    """Count shadow intent files written to mt5_shadow_outbox for a given date."""
    if not shadow_outbox_root.exists():
        return {"intent_count": 0, "intent_by_action": {}, "note": "no_shadow_outbox"}
    date_dir = shadow_outbox_root / date_key
    patterns = [
        "shadow_bridge/*.mt5.json",
        "**/*.mt5.json",
    ]
    intents: list[dict] = []
    for pattern in patterns:
        for p in date_dir.glob(pattern) if date_dir.exists() else []:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                intents.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    actions: Counter = Counter()
    for intent in intents:
        env = intent.get("envelope", {})
        payload = env.get("payload", {})
        actions[str(payload.get("action", "unknown"))] += 1
    return {
        "intent_count": len(intents),
        "intent_by_action": dict(actions),
        "path": str(shadow_outbox_root.resolve()),
        "date_key": date_key,
    }


def _compare_parity(
    live_full: dict,
    shadow_summary: dict | None,
    shadow_outbox_summary: dict,
    shadow_config: dict | None = None,
) -> str:
    """Determine parity status between live and shadow signals."""
    live_total = live_full.get("total", 0)
    live_accepted = live_full.get("counts", {}).get("accepted", 0)
    shadow_intents = shadow_outbox_summary.get("intent_count", 0)
    shadow_outbox_summary.get("intent_by_action", {}).get(
        "open_long", 0
    ) + shadow_outbox_summary.get("intent_by_action", {}).get("open_short", 0)

    if not shadow_config or not shadow_config.get("shadow", {}).get("enabled", False):
        return "shadow_disabled"
    if shadow_intents == 0 and live_total == 0:
        return "both_silent"
    if shadow_intents > 0 and live_total == 0:
        return "shadow_active_live_silent"
    if live_accepted > 0 and shadow_intents > 0:
        return "both_active"
    if live_accepted > 0 and shadow_intents == 0:
        return "live_active_shadow_silent"
    return "mixed_unknown"


def build_report_payload(
    *,
    date_key: str,
    symbol: str,
    journal_path: str,
    shadow_baseline_json: str | None,
    base_dir: Path | None = None,
) -> dict:
    live_full = build_report(journal_path=journal_path, date_key=date_key, symbol=symbol)
    shadow_payload = (
        _load_shadow_baseline(Path(shadow_baseline_json)) if shadow_baseline_json else None
    )
    shadow_summary = _summarize_shadow_payload(shadow_payload, symbol)
    live_execution_summary = {
        "date_key": live_full.get("date_key"),
        "symbol_filter": symbol,
        "total": live_full.get("total"),
        "counts": live_full.get("counts"),
        "acceptance_rate": live_full.get("acceptance_rate"),
        "rejection_rate": live_full.get("rejection_rate"),
        "rejected_reasons": live_full.get("rejected_reasons"),
        "live_consecutive_rejected_tail": live_full.get("live_consecutive_rejected_tail"),
    }

    # Shadow outbox & config
    shadow_outbox_summary: dict[str, Any] = {"intent_count": 0, "intent_by_action": {}}
    shadow_config = None
    if base_dir:
        shadow_config = _load_shadow_config(base_dir)
        if shadow_config:
            shadow_outbox_root = base_dir / "mt5_shadow_outbox"
            shadow_outbox_summary = _summarize_shadow_outbox(shadow_outbox_root, date_key)

    parity_status = _compare_parity(live_full, shadow_summary, shadow_outbox_summary, shadow_config)

    return {
        "schema_version": "shadow_live_compare_report.v2",
        "date_key": date_key,
        "symbol": symbol,
        "journal_path": journal_path,
        "shadow_baseline_path": shadow_baseline_json,
        "shadow_config_loaded": shadow_config is not None,
        "shadow_signal_summary": shadow_summary,
        "shadow_outbox_summary": shadow_outbox_summary,
        "live_execution_summary": live_execution_summary,
        "parity_status": parity_status,
        "parity_notes": _parity_notes(shadow_summary, live_full),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    journal_path = args.journal_path or str(base / "live_trade_journal.jsonl")
    payload = build_report_payload(
        date_key=args.date,
        symbol=args.symbol,
        journal_path=journal_path,
        shadow_baseline_json=args.shadow_baseline_json,
        base_dir=base,
    )
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

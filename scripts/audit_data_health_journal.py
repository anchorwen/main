"""Audit trade journal data health: duplicates, close_price gap, trail gap, PnL null.
# type: ignore  # FIX-20260620-076: Sev 4 audit script, suppressed

Iron Law #11: All statistics below are the sole source of truth.
Target: live_trade_journal.jsonl (NOT ledger_events.jsonl which is Brain PnL)
Usage: python scripts/audit_data_health_journal.py [--data-dir data_btc] [--data-dir data]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _parse_ts(ts) -> datetime | None:
    """Parse various timestamp formats to timezone-aware UTC datetime."""
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            ts_str = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        elif isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=UTC)
    except (ValueError, TypeError, OSError):
        pass
    return None


def audit_trade_journal(data_dir: str = "data_btc") -> dict:
    """Run full trade journal health audit."""
    base = Path(data_dir)
    journal_path = base / "live_trade_journal.jsonl"

    results: dict = {
        "symbol": base.name,
        "journal_path": str(journal_path),
        "journal_exists": journal_path.exists(),
    }

    if not journal_path.exists():
        results["error"] = "journal not found"
        return results

    # ── Load journal ──
    raw = journal_path.read_text(encoding="utf-8")
    entries = []
    parse_errors = 0
    for line in raw.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors += 1

    results["total_entries"] = len(entries)
    results["parse_errors"] = parse_errors

    # ── Action distribution ──
    actions = Counter(e.get("action", "unknown") for e in entries)
    results["actions"] = dict(actions.most_common())

    # ── Ack status distribution ──
    ack_statuses = Counter(e.get("ack_status", "unknown") for e in entries)
    results["ack_statuses"] = dict(ack_statuses.most_common())

    # ── Duplicate detection (same message_id) ──
    msg_ids = Counter(e.get("message_id", "") for e in entries if e.get("message_id"))
    dup_msg_ids = {mid: count for mid, count in msg_ids.items() if count > 1}
    results["duplicate_message_ids"] = len(dup_msg_ids)
    results["duplicate_entries"] = sum(count - 1 for count in dup_msg_ids.values())
    results["duplicate_samples"] = sorted(dup_msg_ids.items(), key=lambda x: -x[1])[:5]

    # ── Close entries analysis ──
    close_entries = [e for e in entries if e.get("action") == "close"]
    results["close_entries_total"] = len(close_entries)

    # close_price in detail
    with_price = []
    without_price = []
    for e in close_entries:
        detail = e.get("detail", {})
        if isinstance(detail, dict):
            cp = detail.get("close_price")
            if cp is not None and (isinstance(cp, (int, float)) and cp > 0):
                with_price.append(e)
            else:
                without_price.append(e)
        else:
            without_price.append(e)

    results["close_price_present"] = len(with_price)
    results["close_price_missing"] = len(without_price)
    results["close_price_rate"] = round(len(with_price) / max(len(close_entries), 1), 4)

    # PnL null rate (pnl field on close entries)
    pnl_null = [e for e in close_entries if e.get("pnl") is None]
    results["pnl_null_count"] = len(pnl_null)
    results["pnl_null_rate"] = round(len(pnl_null) / max(len(close_entries), 1), 4)

    # PnL from detail for null cases
    detail_pnl_populated = 0
    for e in pnl_null:
        detail = e.get("detail", {})
        if isinstance(detail, dict) and detail.get("pnl") is not None:
            detail_pnl_populated += 1
    results["pnl_null_but_in_detail"] = detail_pnl_populated

    # ── Modify SL/TP entries (trail) ──
    modify_entries = [e for e in entries if e.get("action") == "modify_sltp"]
    results["modify_sltp_total"] = len(modify_entries)
    results["trail_rate"] = round(len(modify_entries) / max(len(close_entries), 1), 4)

    # ── Time range ──
    timestamps = []
    for e in entries:
        ts = e.get("recorded_at")
        dt = _parse_ts(ts)
        if dt:
            timestamps.append(dt)

    if timestamps:
        results["time_min"] = min(timestamps).isoformat()
        results["time_max"] = max(timestamps).isoformat()
        results["time_span_days"] = round(
            (max(timestamps) - min(timestamps)).total_seconds() / 86400, 2
        )

    # ── Missing close_price samples (detail view) ──
    results["missing_close_price_samples"] = [
        {
            "message_id": e.get("message_id", "?")[-20:],
            "recorded_at": e.get("recorded_at", "?"),
            "strategy": e.get("strategy", "?"),
            "detail_keys": list(e.get("detail", {}).keys())
            if isinstance(e.get("detail"), dict)
            else str(type(e.get("detail"))),
            "pnl": e.get("pnl"),
        }
        for e in without_price[:10]
    ]

    # ── close_price missing by strategy ──
    missing_by_strategy: Counter[str] = Counter()
    for e in without_price:
        missing_by_strategy[e.get("strategy", "unknown")] += 1
    results["close_price_missing_by_strategy"] = dict(missing_by_strategy.most_common(10))

    # ── PnL null by strategy ──
    pnl_null_by_strategy: Counter[str] = Counter()
    for e in pnl_null:
        pnl_null_by_strategy[e.get("strategy", "unknown")] += 1
    results["pnl_null_by_strategy"] = dict(pnl_null_by_strategy.most_common(10))

    return results


# ── Main ──
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    args = ap.parse_args()

    result = audit_trade_journal(args.data_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

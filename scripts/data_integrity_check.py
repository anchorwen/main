#!/usr/bin/env python3
"""
数据完整性验证脚本 v2 (Iron Law #11 compliant)
验证实盘数据收集的完整性和正确性。

检查维度:
  1. 文件级 — JSONL 可解析、无损坏行
  2. 时间连续性 — 无异常时间跳跃、日期覆盖完整
  3. 跨文件一致性 — journal ↔ live_labels ↔ meta_exit ↔ position_snapshots
  4. 必填字段 — schema 关键字段非空
  5. 事件链完整性 — open → modify → close 闭环
  6. 重复检测 — 相同 ID 重复记录
  7. golden_master 决策周期完整性

Usage:
  PYTHONIOENCODING=utf-8 python scripts/data_integrity_check.py --data-dir data_btc
"""

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────
# UTILITY
# ──────────────────────────────────────────────


def load_jsonl(path: str) -> tuple[list[dict], list[str]]:
    """Load JSONL, tracking line parse errors."""
    rows: list[dict] = []
    errors: list[str] = []
    if not os.path.exists(path):
        return rows, [f"FILE_NOT_FOUND: {path}"]
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                errors.append(f"L{i}: {e}")
    return rows, errors


def parse_ts(val: Any) -> datetime | None:
    """Parse a timestamp from string or unix float."""
    if val is None:
        return None
    if isinstance(val, int | float):
        return datetime.fromtimestamp(val, tz=UTC)
    if isinstance(val, str):
        try:
            v = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            pass
    return None


def pct(a: int, b: int) -> str:
    if b == 0:
        return "N/A"
    return f"{a/b*100:.1f}%"


# ──────────────────────────────────────────────
# CHECKS
# ──────────────────────────────────────────────


def check_file_integrity(name: str, path: str, errors: list[str]) -> dict:
    rows, parse_errs = load_jsonl(path)
    errors.extend(parse_errs)
    return {
        "file": name,
        "path": path,
        "rows": len(rows),
        "parse_errors": len(parse_errs),
        "exists": os.path.exists(path),
    }


def check_timeline(
    name: str,
    rows: list[dict],
    ts_extractor: Callable[[dict[Any, Any]], datetime | None],
    errors: list[str],
    csv_mode: bool = False,
) -> dict:
    """Check time continuity."""
    if not rows:
        return {
            "file": name,
            "first": None,
            "last": None,
            "date_span_days": 0,
            "backward_jumps": 0,
            "days_with_data": 0,
        }

    timestamps = []
    backward_jumps = 0
    prev_dt = None
    for i, r in enumerate(rows):
        dt = ts_extractor(r)
        if dt:
            timestamps.append(dt)
            if prev_dt and dt < prev_dt:
                backward_jumps += 1
                if backward_jumps <= 3:
                    errors.append(
                        f"{name} L{i}: backward jump {prev_dt.isoformat()} -> {dt.isoformat()}"
                    )
            prev_dt = dt or prev_dt

    if not timestamps:
        return {
            "file": name,
            "first": None,
            "last": None,
            "date_span_days": 0,
            "backward_jumps": 0,
            "days_with_data": 0,
        }

    first, last = min(timestamps), max(timestamps)
    days = (last - first).days
    daily = Counter(dt.strftime("%Y-%m-%d") for dt in timestamps)

    return {
        "file": name,
        "first": first.isoformat(),
        "last": last.isoformat(),
        "date_span_days": days,
        "backward_jumps": backward_jumps,
        "days_with_data": len(daily),
        "daily_counts": dict(sorted(daily.items())),
    }


def check_missing_fields(
    rows: list[dict], required: list[str], name: str, errors: list[str]
) -> dict:
    missing: Counter[str] = Counter()
    for i, r in enumerate(rows):
        for field in required:
            if r.get(field) is None:
                missing[field] += 1
                if missing[field] <= 3:
                    errors.append(f"{name} L{i+1}: missing '{field}'")
    return dict(missing)


def check_duplicates(rows: list[dict], id_field: str, name: str, errors: list[str]) -> dict:
    seen: dict[Any, int] = {}
    dupes = 0
    for i, r in enumerate(rows):
        rid = r.get(id_field)
        if rid is None:
            continue
        if rid in seen:
            dupes += 1
            if dupes <= 5:
                errors.append(f"{name}: duplicate {id_field}={rid} (L{seen[rid]} vs L{i+1})")
        else:
            seen[rid] = i + 1
    return {"total": len(rows), "duplicates": dupes, "unique_ids": len(seen)}


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Data Integrity Check v2")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    DATA = Path(args.data_dir)
    errors: list[str] = []

    print("=" * 72)
    print("  DATA INTEGRITY VERIFICATION REPORT v2")
    print("  Iron Law #11 - Script-Generated Statistics Only")
    print(f"  Data dir: {DATA.resolve()}")
    print(f"  Check time: {datetime.now(UTC).isoformat()}")
    print("=" * 72)

    # ── 1. FILE INTEGRITY ──────────────────────
    print("\n[1] FILE INTEGRITY (JSONL Parse)")
    print("-" * 50)

    file_specs = {
        "ledger_events": DATA / "ledger_events.jsonl",
        "live_trade_journal": DATA / "live_trade_journal.jsonl",
        "live_trade_journal.augmented": DATA / "live_trade_journal.augmented.jsonl",
        "golden_master": DATA / "golden_master.jsonl",
        "position_snapshots": DATA / "position_snapshots.jsonl",
        "meta_exit_snapshots": DATA / "meta_exit_snapshots.jsonl",
        "regime_snapshots": DATA / "regime_snapshots.jsonl",
        "bridge_processed_wal": DATA / "bridge_processed_wal.jsonl",
        "reports/live_labels": DATA / "reports" / "live_labels.jsonl",
        "reports/labels_with_regime": DATA / "reports" / "labels_with_regime.jsonl",
        "reports/exit_watchdog_alerts": DATA / "reports" / "exit_watchdog_alerts.jsonl",
        "logs/phase_telemetry": DATA / "logs" / "phase_telemetry.jsonl",
        "logs/alert_audit": DATA / "logs" / "alert_audit.jsonl",
        "logs/alert_undelivered": DATA / "logs" / "alert_undelivered.jsonl",
    }

    file_results = {}
    all_data: dict[str, list[dict]] = {}
    for fname, fpath in file_specs.items():
        fr = check_file_integrity(fname, str(fpath), errors)
        file_results[fname] = fr
        all_data[fname] = load_jsonl(str(fpath))[0]
        warn = " !!PARSE_ERRORS" if fr["parse_errors"] > 0 else ""
        status = "NOT FOUND" if not fr["exists"] else f"{fr['rows']:>6d} rows"
        print(f"  {fname:35s} {status}{warn}")

    # ── 2. TIMELINE CONTINUITY ──────────────────
    print("\n[2] TIMELINE CONTINUITY")
    print("-" * 50)

    # Extractors keyed to file names
    timeline_specs = {
        "ledger_events": lambda r: parse_ts(r.get("timestamp")),
        "live_trade_journal": lambda r: parse_ts(r.get("recorded_at")),
        "golden_master": lambda r: parse_ts(r.get("timestamp_utc")),
        "position_snapshots": lambda r: parse_ts(r.get("time")),
        "meta_exit_snapshots": lambda r: parse_ts(r.get("timestamp_utc")),
        "regime_snapshots": lambda r: parse_ts(r.get("timestamp_utc")),
        "logs/phase_telemetry": lambda r: parse_ts(r.get("time")),
    }

    for fname, extractor in timeline_specs.items():
        rows = all_data.get(fname, [])
        tl = check_timeline(fname, rows, extractor, errors)
        if tl["first"]:
            bw = f" bw_jumps={tl['backward_jumps']}" if tl["backward_jumps"] > 0 else ""
            print(
                f"  {fname:35s} {tl['first'][:19]} .. {tl['last'][:19]}  "
                f"span={tl['date_span_days']:>3d}d  days={tl['days_with_data']}{bw}"
            )
        else:
            print(f"  {fname:35s} NO TIMESTAMPS")

    # ── 3. REQUIRED FIELDS ─────────────────────
    print("\n[3] REQUIRED FIELDS CHECK")
    print("-" * 50)

    journal = all_data.get("live_trade_journal", [])
    labels = all_data.get("reports/live_labels", [])
    pos_snaps = all_data.get("position_snapshots", [])
    meta_exit = all_data.get("meta_exit_snapshots", [])
    gm = all_data.get("golden_master", [])
    regime = all_data.get("regime_snapshots", [])
    telemetry = all_data.get("logs/phase_telemetry", [])

    field_checks = [
        (
            "live_trade_journal",
            journal,
            ["schema_version", "recorded_at", "action", "ack_status", "symbol", "position_ticket"],
        ),
        ("live_labels", labels, ["position_ticket", "label", "pnl", "entry_price", "exit_price"]),
        ("position_snapshots", pos_snaps, ["ticket", "time", "bars_held"]),
        ("meta_exit_snapshots", meta_exit, ["ticket", "timestamp_utc", "meta_exit"]),
        ("golden_master", gm, ["cycle", "timestamp_utc", "inputs", "outputs", "summary"]),
        ("regime_snapshots", regime, ["timestamp_utc", "detected_regime", "current_atr"]),
        ("phase_telemetry", telemetry, ["event", "phase", "time", "cycle"]),
    ]

    for name, rows, req_fields in field_checks:
        missing = check_missing_fields(rows, req_fields, name, errors)
        if missing:
            print(f"  {name:35s} MISSING: {missing}")
        else:
            print(f"  {name:35s} all required fields OK")

    # ── 4. DUPLICATE CHECK ─────────────────────
    print("\n[4] DUPLICATE DETECTION")
    print("-" * 50)

    dup_specs = [
        ("ledger_events", "event_id"),
        ("live_trade_journal", "message_id"),
        ("live_labels", "label_id"),
        ("phase_telemetry", "event"),
    ]
    for name, id_field in dup_specs:
        rows = all_data.get(name, [])
        result = check_duplicates(rows, id_field, name, errors)
        warn = " !!DUPLICATES" if result["duplicates"] > 0 else " OK"
        print(
            f"  {name:35s} {result['total']} rows, {result['unique_ids']} unique {id_field}s, "
            f"dupes={result['duplicates']}{warn}"
        )

    # ── 5. CROSS-FILE CONSISTENCY ──────────────
    print("\n[5] CROSS-FILE CONSISTENCY (Ticket-based)")
    print("-" * 50)

    # Build ticket sets from each source
    def _tickets(rows: list[dict], key: str = "position_ticket") -> set:
        result = set()
        for r in rows:
            v = r.get(key)
            if v is not None:
                result.add(v)
        return result

    j_tickets = _tickets(journal)
    j_open_tickets = _tickets(
        [r for r in journal if r.get("action") == "open" and r.get("ack_status") == "accepted"]
    )
    j_close_tickets = _tickets([r for r in journal if r.get("action") == "close"])
    j_labeled_tickets = _tickets([r for r in journal if r.get("label") is not None])
    j_orphan = sum(1 for r in journal if r.get("position_ticket") is None)

    lb_tickets = _tickets(labels)
    ps_tickets = _tickets(pos_snaps, "ticket")
    me_tickets = _tickets(meta_exit, "ticket")

    print(f"  Journal entries:        {len(journal):>6d}")
    print(f"  Journal unique tickets: {len(j_tickets):>6d}")
    print(f"  Open tickets (ack'd):   {len(j_open_tickets):>6d}")
    print(f"  Close events:           {len(j_close_tickets):>6d}")
    print(f"  Orphan (no ticket):     {j_orphan:>6d}")
    print(f"  Open w/o close:         {len(j_open_tickets - j_close_tickets):>6d}")
    still_open = j_open_tickets - j_close_tickets
    if still_open:
        print(f"    Currently open:       {sorted(still_open)}")

    print(
        f"\n  Live Labels:            {len(labels):>6d} entries, {len(lb_tickets)} unique tickets"
    )
    print(
        f"  Position Snapshots:     {len(pos_snaps):>6d} entries, {len(ps_tickets)} unique tickets"
    )
    print(
        f"  MetaExit Snapshots:     {len(meta_exit):>6d} entries, {len(me_tickets)} unique tickets"
    )

    # Cross-file overlaps
    print(f"\n  Tickets in Journal AND Labels:  {len(j_tickets & lb_tickets):>6d}")
    print(f"  Tickets in Journal NOT Labels:  {len(j_tickets - lb_tickets):>6d}")
    print(f"  Tickets in Labels NOT Journal:  {len(lb_tickets - j_tickets):>6d}")
    print(f"  Tickets in Journal AND PSnaps:  {len(j_tickets & ps_tickets):>6d}")
    print(f"  Tickets in Journal AND MetaEx:  {len(j_tickets & me_tickets):>6d}")

    # Closed tickets with/without MetaExit
    closed_with_me = j_close_tickets & me_tickets
    closed_without_me = j_close_tickets - me_tickets
    print(f"\n  Closed tickets WITH MetaExit:   {len(closed_with_me):>6d}")
    print(f"  Closed tickets WITHOUT MetaEx:  {len(closed_without_me):>6d}")
    if closed_without_me:
        print(f"    sample: {sorted(closed_without_me)[:10]}")

    # ── 6. SCHEMA VERSION DISTRIBUTION ─────────
    print("\n[6] SCHEMA VERSION DISTRIBUTION")
    print("-" * 50)

    for name in ["live_trade_journal", "live_labels", "live_trade_journal.augmented"]:
        rows = all_data.get(name, [])
        versions = Counter(r.get("schema_version", "MISSING") for r in rows)
        print(f"  {name:35s} {dict(versions)}")

    # ── 7. JOURNAL RECEIPT CONSISTENCY ─────────
    print("\n[7] RECEIPT PATH CONSISTENCY (Journal)")
    print("-" * 50)

    missing_rp = sum(1 for r in journal if not r.get("receipt_path"))
    out_arch_mismatch = 0
    for r in journal:
        ob = r.get("outbox_path", "")
        ar = r.get("archive_path", "")
        if ob and ar and os.path.basename(ob) != os.path.basename(ar):
            out_arch_mismatch += 1
    print(f"  Missing receipt_path:      {missing_rp}")
    print(f"  Outbox/Archive mismatch:   {out_arch_mismatch}")

    # ── 8. BRAIN VOTES & GATE AUDIT COVERAGE ───
    print("\n[8] BRAIN VOTES & GATE AUDIT COVERAGE")
    print("-" * 50)

    votes_dir = DATA / "brain_votes"
    if votes_dir.is_dir():
        daily_counts = {}
        for fname in sorted(os.listdir(votes_dir)):
            if fname.endswith(".jsonl"):
                path = votes_dir / fname
                rows, _ = load_jsonl(str(path))
                daily_counts[fname.replace(".jsonl", "")] = len(rows)
        total_votes = sum(daily_counts.values())

        j_dates = set()
        for r in journal:
            dt = parse_ts(r.get("recorded_at"))
            if dt:
                j_dates.add(dt.strftime("%Y-%m-%d"))
        vote_dates = set(daily_counts.keys())
        missing_dates = j_dates - vote_dates
        extra_dates = vote_dates - j_dates

        print(f"  Brain votes total:        {total_votes:>6d}")
        print(f"  Daily files:              {len(daily_counts):>6d}")
        print(f"  Journal trading days:     {len(j_dates):>6d}")
        if missing_dates:
            print(f"  !! Trading days WITHOUT votes: {sorted(missing_dates)}")
        else:
            print("  All trading days have brain votes OK")
        if extra_dates:
            print(f"  Vote days w/o trades:     {sorted(extra_dates)}")
    else:
        print("  brain_votes/ directory not found")

    gate_dir = DATA / "gate_audit"
    if gate_dir.is_dir():
        g_counts = {}
        for fname in sorted(os.listdir(gate_dir)):
            if fname.endswith(".jsonl"):
                path = gate_dir / fname
                rows, _ = load_jsonl(str(path))
                g_counts[fname.replace(".jsonl", "")] = len(rows)
        # Check gate audit for today
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        today_gate = g_counts.get(today, 0)
        warn = "" if today_gate > 0 else " !!NO GATE AUDIT TODAY"
        print(f"  Gate audit total:         {sum(g_counts.values()):>6d}")
        print(f"  Daily files:              {len(g_counts):>6d}")
        print(f"  Today ({today}):      {today_gate:>6d} records{warn}")

    # ── 9. GOLDEN MASTER CYCLE ANALYSIS ────────
    print("\n[9] GOLDEN MASTER CYCLE ANALYSIS")
    print("-" * 50)

    if gm:
        cycles = [c for r in gm if (c := r.get("cycle")) is not None]
        gm_strategy_counts: Counter[str] = Counter()
        gm_decision_counts = 0
        for r in gm:
            outputs = r.get("outputs", {})
            for strat_name in outputs:
                if isinstance(outputs[strat_name], dict):
                    if outputs[strat_name].get("should_trade"):
                        gm_decision_counts += 1
                        gm_strategy_counts[strat_name] += 1

        print(f"  Total cycles:             {len(gm):>6d}")
        print(f"  Cycle range:              {min(cycles)} - {max(cycles)}")
        print(f"  Trade decisions:          {gm_decision_counts:>6d}")
        if gm_strategy_counts:
            print(f"  Strategy decisions:       {dict(gm_strategy_counts)}")

        # Check cycle continuity
        cycles_sorted = sorted(set(cycles))
        gaps = []
        for i in range(1, len(cycles_sorted)):
            if cycles_sorted[i] - cycles_sorted[i - 1] > 1:
                gaps.append((cycles_sorted[i - 1], cycles_sorted[i]))
        if gaps:
            print(f"  !! Cycle gaps:            {len(gaps)} gaps found")
            for g in gaps[:5]:
                print(f"      {g[0]} -> {g[1]} (gap of {g[1]-g[0]-1})")
        else:
            print("  Cycle continuity: OK (no gaps)")
    else:
        print("  No golden_master data")

    # ── 10. PHASE TELEMETRY ────────────────────
    print("\n[10] PHASE TELEMETRY")
    print("-" * 50)

    if telemetry:
        phases = Counter(r.get("phase") for r in telemetry)
        events = Counter(r.get("event") for r in telemetry)
        t_cycles = Counter(r.get("cycle") for r in telemetry)
        print(f"  Total events:             {len(telemetry):>6d}")
        print(f"  Phase distribution:       {dict(phases.most_common(10))}")
        print(f"  Event type distribution:  {dict(events.most_common(10))}")
        print(f"  Unique cycles:            {len(t_cycles):>6d}")

        # Check today's telemetry
        today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
        today_events = 0
        for r in telemetry:
            dt = parse_ts(r.get("time"))
            if dt is not None and dt.strftime("%Y-%m-%d") == today_utc:
                today_events += 1
        print(f"  Today ({today_utc}):  {today_events:>6d} events")
    else:
        print("  No telemetry data")

    # ── 11. ALERT DELIVERY ─────────────────────
    print("\n[11] ALERT DELIVERY STATUS")
    print("-" * 50)

    alert_audit = all_data.get("logs/alert_audit", [])
    alert_undelivered = all_data.get("logs/alert_undelivered", [])
    statuses = Counter(r.get("status") for r in alert_audit)

    print(f"  Alert audit total:        {len(alert_audit):>6d}")
    print(f"  Undelivered alerts:       {len(alert_undelivered):>6d}")
    if statuses:
        print(f"  Audit status dist:        {dict(statuses)}")

    # ── 12. DATA GAPS INDEX ────────────────────
    print("\n[12] KNOWN DATA GAPS (from deferred_data_collection_gaps.md)")
    print("-" * 50)
    print("  P2: DecisionRecord only fires on dispatch (not all cycles)")
    print("  P3: DecisionScorer dormant (not in live_cycle)")
    print("  P3: OnlineFeedbackHook dormant (not triggered)")
    print("  P3: ParamOptimizer dormant (OU param online optimization not running)")
    print("  (All 4 items deferred per 2026-05-17 audit, re-eval date: 2026-06-17)")

    # ── SUMMARY ────────────────────────────────
    print("\n" + "=" * 72)
    print("  VERIFICATION SUMMARY")
    print("=" * 72)

    total_errors = len(errors)
    total_rows = sum(fr["rows"] for fr in file_results.values())
    total_files = len(file_results)

    if total_errors == 0:
        print("  ALL CHECKS PASSED - No data integrity issues detected")
    else:
        print(f"  {total_errors} DATA INTEGRITY ISSUES FOUND:")
        print()
        for e in errors[:40]:
            print(f"  * {e}")
        if len(errors) > 40:
            print(f"  ... and {len(errors) - 40} more")

    print(f"\n  Files checked: {total_files}")
    print(f"  Total JSONL rows: {total_rows:,}")
    print(f"  Errors: {total_errors}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

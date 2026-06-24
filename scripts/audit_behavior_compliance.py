#!/usr/bin/env python3
"""Behavior Compliance Audit — FIX-20260610-009 post-deployment verification.

Iron Law #11: Script-stdout is the ONLY admissible evidence.
This script audits system BEHAVIOR (not code syntax) against declared
business rules.  It answers:

  Q1. Are there more concurrent open positions than max_positions allows?
  Q2. Are inter-trade intervals below the reentry cooldown minimum?
  Q3. Does Phase 10 consensus dispatch go through the same safety gates
      as the main strategy evaluation path?
  Q4. Where exactly is max_positions enforced — and where is it bypassed?

Usage:
  python scripts/audit_behavior_compliance.py --data-dir data_btc [--symbol BTCUSDc]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc


# ── Q1: Concurrent position count vs max_positions ──────────────────────


def audit_concurrent_positions(data_dir: Path, max_allowed: int) -> dict:
    """Scan intent log for open/close events; compute peak concurrency."""
    log_path = data_dir / "logs"
    intent_logs = sorted(log_path.glob("intent_*.log"))
    if not intent_logs:
        return {"error": "no intent logs found", "passed": False}

    latest_log = intent_logs[-1]

    opens: list[dict] = []  # {ticket, time, side, volume}
    closes: list[dict] = []  # {ticket, time}
    trail_moves: list[dict] = []

    with open(latest_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("event", "")

            if etype == "position_registered_for_mgmt":
                opens.append(
                    {
                        "ticket": evt.get("ticket"),
                        "time": evt.get("time", ""),
                        "side": evt.get("side", ""),
                        "entry_price": evt.get("entry_price", 0),
                    }
                )

            elif etype in ("intent_dispatched",):
                # Only count OPEN actions
                side = evt.get("side")
                if side in ("long", "short"):
                    opens.append(
                        {
                            "ticket": None,  # ticket assigned after MT5 ack
                            "time": evt.get("time", ""),
                            "side": side,
                            "entry_price": evt.get("reference_used", 0),
                            "dispatch_intent_id": evt.get("dispatch", {}).get("intent_id", ""),
                        }
                    )

            elif etype == "trail_stop_moved":
                trail_moves.append(
                    {
                        "ticket": evt.get("ticket"),
                        "time": evt.get("time", ""),
                        "old_sl": evt.get("old_sl", 0),
                        "new_sl": evt.get("new_sl", 0),
                    }
                )

    # ── Build timeline of open/close events ──
    # Use management_phase_diag to track per-ticket state
    tickets: dict[int, dict] = {}
    with open(latest_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            if evt.get("event") == "management_phase_diag":
                ticket = evt.get("ticket")
                if ticket:
                    tickets[ticket] = {
                        "side": evt.get("side"),
                        "entry": evt.get("entry"),
                        "cycles_held": evt.get("cycles_held", 0),
                        "current_sl": evt.get("current_sl"),
                        "current_tp": evt.get("current_tp"),
                        "lowest_low": evt.get("lowest_low"),
                        "highest_high": evt.get("highest_high"),
                        "breakeven_fired": evt.get("breakeven_fired", False),
                        "time": evt.get("time"),
                    }

    # Active = tickets seen in management_phase_diag (not yet closed)
    peak_concurrent = len(tickets)
    unique_tickets = sorted(tickets.keys())

    return {
        "passed": peak_concurrent <= max_allowed,
        "peak_concurrent": peak_concurrent,
        "max_allowed": max_allowed,
        "active_tickets": unique_tickets,
        "ticket_details": {t: tickets[t] for t in unique_tickets},
    }


# ── Q2: Inter-trade interval vs reentry cooldown ────────────────────────


def audit_reentry_intervals(data_dir: Path, min_cooldown_s: float = 300) -> dict:
    """Measure time between consecutive same-direction opens."""
    log_path = data_dir / "logs"
    intent_logs = sorted(log_path.glob("intent_*.log"))
    if not intent_logs:
        return {"error": "no intent logs", "passed": False}

    latest_log = intent_logs[-1]

    all_opens: list[dict] = []
    with open(latest_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("event") == "position_registered_for_mgmt":
                all_opens.append(evt)

    violations = []
    intervals = []
    for i in range(1, len(all_opens)):
        prev = all_opens[i - 1]
        curr = all_opens[i]
        if prev.get("side") == curr.get("side"):
            try:
                t_prev = datetime.fromisoformat(prev["time"].replace("Z", "+00:00"))
                t_curr = datetime.fromisoformat(curr["time"].replace("Z", "+00:00"))
                delta_s = (t_curr - t_prev).total_seconds()
                intervals.append(delta_s)
                if delta_s < min_cooldown_s:
                    violations.append(
                        {
                            "prev_ticket": prev.get("ticket"),
                            "curr_ticket": curr.get("ticket"),
                            "prev_time": prev["time"],
                            "curr_time": curr["time"],
                            "interval_s": delta_s,
                            "min_required_s": min_cooldown_s,
                        }
                    )
            except (ValueError, KeyError):
                pass

    return {
        "passed": len(violations) == 0,
        "total_same_direction_pairs": len(intervals),
        "intervals_s": intervals,
        "min_interval_s": min(intervals) if intervals else None,
        "max_interval_s": max(intervals) if intervals else None,
        "violations": violations,
        "threshold_s": min_cooldown_s,
    }


# ── Q3: Phase 10 gate path vs main eval gate path ───────────────────────


def audit_dispatch_gate_paths(data_dir: Path) -> dict:
    """Compare safety gate decisions between main eval and Phase 10 dispatch.

    For each cycle where Phase 10 dispatched an order, check if the main
    eval path reached a different conclusion (blocked vs allowed).
    """
    log_path = data_dir / "logs"
    intent_logs = sorted(log_path.glob("intent_*.log"))
    if not intent_logs:
        return {"error": "no intent logs", "passed": False}

    latest_log = intent_logs[-1]

    cycle_evals: dict[str, dict] = {}  # cycle_time → eval result
    cycle_dispatches: dict[str, list] = {}  # cycle_time → dispatch events

    with open(latest_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("event", "")
            evt_time = evt.get("time", "")[:16]  # minute precision

            if etype == "multi_strategy_eval":
                for s in evt.get("strategies", []):
                    if s.get("strategy") == "btc_swing":
                        cycle_evals[evt_time] = {
                            "should_trade": s.get("should_trade"),
                            "direction": s.get("direction"),
                            "confidence": s.get("confidence"),
                            "reason": s.get("reason"),
                        }
            elif etype == "reentry_blocked":
                cycle_evals.setdefault(evt_time, {})["reentry_blocked"] = {
                    "direction": evt.get("direction"),
                    "confidence": evt.get("confidence"),
                    "reason": evt.get("reason"),
                }
            elif etype == "intent_dispatched":
                cycle_dispatches.setdefault(evt_time, []).append(
                    {
                        "side": evt.get("side"),
                        "confidence": evt.get("confidence"),
                        "intent_id": evt.get("dispatch", {}).get("intent_id", ""),
                    }
                )

    # Find cycles where dispatch happened but main eval said should_trade=False
    gate_bypass_events = []
    for ct, eval_result in cycle_evals.items():
        dispatches = cycle_dispatches.get(ct, [])
        if dispatches and not eval_result.get("should_trade", False):
            gate_bypass_events.append(
                {
                    "cycle_time": ct,
                    "main_eval": eval_result,
                    "phase10_dispatches": dispatches,
                }
            )

    return {
        "passed": len(gate_bypass_events) == 0,
        "total_cycles_with_eval": len(cycle_evals),
        "total_cycles_with_dispatch": len(cycle_dispatches),
        "gate_bypass_count": len(gate_bypass_events),
        "gate_bypass_details": gate_bypass_events[:5],
    }


# ── Q4: Code-level max_positions enforcement audit ─────────────────────


def audit_max_positions_code() -> dict:
    """Static analysis: trace where max_positions is read and where dispatch
    bypasses it."""
    import subprocess

    root = Path(__file__).resolve().parent.parent

    # Find all references to max_positions (Python-native, no grep dependency)
    refs = []
    for py_file in sorted(root.glob("core/**/*.py")):
        try:
            with open(py_file, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if "max_positions" in line:
                        refs.append(f"{py_file}:{i}: {line.rstrip()}")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    for py_file in sorted(root.glob("scripts/**/*.py")):
        try:
            with open(py_file, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if "max_positions" in line:
                        refs.append(f"{py_file}:{i}: {line.rstrip()}")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    # Find Phase 10 dispatch path
    phase10_lines = []
    with open(root / "core" / "runtime" / "live_cycle.py", encoding="utf-8") as f:
        lines = f.readlines()
        in_phase10 = False
        for i, line in enumerate(lines, 1):
            if "Phase 10" in line or "raw_proposals = []" in line:
                in_phase10 = True
            if in_phase10:
                phase10_lines.append(f"L{i}: {line.rstrip()}")
            if in_phase10 and "_compute_contract_group_consensus" in line:
                # Read until end of consensus block
                for j in range(i, min(i + 15, len(lines))):
                    phase10_lines.append(f"L{j+1}: {lines[j].rstrip()}")
                break

    return {
        "max_positions_references": [r for r in refs if "__pycache__" not in r],
        "phase10_dispatch_path": phase10_lines[:40],
    }


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Behavior Compliance Audit")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--max-positions", type=int, default=2)
    parser.add_argument("--min-cooldown-s", type=float, default=300)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"FATAL: data dir {data_dir} not found")
        return 1

    all_passed = True

    # ── Q1 ──
    print("=" * 60)
    print("Q1: Concurrent Position Limit")
    print(f"    Rule: max_positions = {args.max_positions}")
    print("=" * 60)
    q1 = audit_concurrent_positions(data_dir, args.max_positions)
    if "error" in q1:
        print(f"  ERROR: {q1['error']}")
    else:
        status = "PASS" if q1["passed"] else "FAIL"
        print(
            f"  [{status}] Peak concurrent: {q1['peak_concurrent']} "
            f"(limit: {q1['max_allowed']})"
        )
        print(f"  Active tickets: {q1['active_tickets']}")
        for t, d in q1["ticket_details"].items():
            print(
                f"    ticket={t} side={d.get('side')} entry={d.get('entry')} "
                f"cycles={d.get('cycles_held')} breakeven={d.get('breakeven_fired')} "
                f"highest={d.get('highest_high')} lowest={d.get('lowest_low')}"
            )
        if not q1["passed"]:
            all_passed = False

    # ── Q2 ──
    print()
    print("=" * 60)
    print("Q2: Reentry Cooldown Intervals")
    print(f"    Rule: same-direction opens >= {args.min_cooldown_s}s apart")
    print("=" * 60)
    q2 = audit_reentry_intervals(data_dir, args.min_cooldown_s)
    if "error" in q2:
        print(f"  ERROR: {q2['error']}")
    else:
        status = "PASS" if q2["passed"] else "FAIL"
        print(f"  [{status}] Same-direction pairs: {q2['total_same_direction_pairs']}")
        print(f"  Intervals (s): {q2['intervals_s']}")
        print(f"  Min interval: {q2['min_interval_s']}s")
        if q2["violations"]:
            for v in q2["violations"]:
                print(
                    f"  VIOLATION: ticket {v['prev_ticket']} → {v['curr_ticket']} "
                    f"interval={v['interval_s']:.0f}s < {v['min_required_s']}s"
                )
        if not q2["passed"]:
            all_passed = False

    # ── Q3 ──
    print()
    print("=" * 60)
    print("Q3: Gate Path Consistency")
    print("    Rule: Phase 10 dispatch should not bypass main eval gates")
    print("=" * 60)
    q3 = audit_dispatch_gate_paths(data_dir)
    if "error" in q3:
        print(f"  ERROR: {q3['error']}")
    else:
        status = "PASS" if q3["passed"] else "FAIL"
        print(f"  [{status}] Cycles with eval: {q3['total_cycles_with_eval']}")
        print(f"  Cycles with dispatch: {q3['total_cycles_with_dispatch']}")
        print(f"  Gate bypass events: {q3['gate_bypass_count']}")
        if q3["gate_bypass_details"]:
            for gb in q3["gate_bypass_details"]:
                print(f"  BYPASS at {gb['cycle_time']}:")
                print(
                    f"    Main eval: should_trade={gb['main_eval'].get('should_trade')} "
                    f"reason={gb['main_eval'].get('reason','')}"
                )
                if gb["main_eval"].get("reentry_blocked"):
                    print(f"    Reentry blocked: {gb['main_eval']['reentry_blocked']}")
                for d in gb["phase10_dispatches"]:
                    print(f"    Phase10 dispatch: {d['side']} conf={d['confidence']}")
        if not q3["passed"]:
            all_passed = False

    # ── Q4 ──
    print()
    print("=" * 60)
    print("Q4: max_positions Code Enforcement Path")
    print("=" * 60)
    q4 = audit_max_positions_code()
    print("  References to max_positions:")
    for r in q4["max_positions_references"]:
        print(f"    {r}")
    print()
    print("  Phase 10 dispatch path (first 40 lines):")
    for line in q4["phase10_dispatch_path"]:
        print(f"    {line}")

    # ── Final ──
    print()
    print("=" * 60)
    print(f"OVERALL: {'PASS' if all_passed else 'FAIL'}")
    print("=" * 60)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

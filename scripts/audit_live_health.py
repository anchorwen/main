#!/usr/bin/env python3
"""Full-stack live trading health audit — BTC + XAU (Iron Law #11).

Usage:
  python scripts/audit_live_health.py [--data-dir-btc data_btc] [--data-dir-xau data]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def load_latest_log(data_dir: Path) -> list[dict]:
    logs = sorted((data_dir / "logs").glob("intent_*.log"))
    if not logs:
        return []
    events = []
    with open(logs[-1], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def audit_symbol(name: str, data_dir: Path) -> dict:
    events = load_latest_log(data_dir)
    if not events:
        return {"symbol": name, "error": "no log data"}

    # ── Basic stats ──
    cycles = sum(1 for e in events if e.get("event") == "cycle_end")
    first_cycle = None
    last_cycle = None
    for e in events:
        if e.get("event") == "cycle_end":
            t = e.get("time", "")
            if first_cycle is None:
                first_cycle = t
            last_cycle = t

    # ── Trading activity ──
    dispatches = [e for e in events if e.get("event") == "intent_dispatched"]
    positions = [e for e in events if e.get("event") == "position_registered_for_mgmt"]
    closes = [e for e in events if "close" in e.get("event", "").lower()]

    # ── Eval results ──
    eval_events = [e for e in events if e.get("event") == "multi_strategy_eval"]
    rejection_reasons: Counter = Counter()
    strategy_directions: dict[str, Counter] = defaultdict(Counter)
    strategy_should_trade: dict[str, int] = Counter()

    for ev in eval_events:
        for s in ev.get("strategies", []):
            name_s = s.get("strategy", "unknown")
            reason = s.get("reason", "no_reason")
            if not s.get("should_trade"):
                rejection_reasons[reason] += 1
            else:
                strategy_should_trade[name_s] += 1
            strategy_directions[name_s][s.get("direction", "neutral")] += 1

    # ── Gate bypass (Phase 10) ──
    gate_bypasses = [e for e in events if e.get("event") == "consensus_blocked_by_main_eval"]
    low_conf_skips = [e for e in events if e.get("event") == "low_confidence_skip"]

    # ── Brain health ──
    brain_alerts = [e for e in events if e.get("event") == "brain_alert"]
    alert_types = Counter(e.get("alert_type", "?") for e in brain_alerts)

    # ── Position management ──
    mgmt_diags = [e for e in events if e.get("event") == "management_phase_diag"]
    grace_skips = [e for e in events if e.get("event") == "grace_period_skip"]
    trail_moves = [e for e in events if e.get("event") == "trail_stop_moved"]

    # ── Consensus ──
    consensus_reasons = []
    for e in low_conf_skips:
        alloc = e.get("allocation", {})
        consensus_reasons.append(alloc.get("reason", "?"))

    # ── Startup integrity ──
    startup = [e for e in events if "startup" in e.get("event", "")]
    integrity = [e for e in events if "integrity" in e.get("event", "")]

    return {
        "symbol": name,
        "cycles": cycles,
        "first_cycle": first_cycle,
        "last_cycle": last_cycle,
        "dispatches": len(dispatches),
        "positions_opened": len(positions),
        "eval_cycles": len(eval_events),
        "rejection_reasons": dict(rejection_reasons.most_common(10)),
        "approved_trades": dict(strategy_should_trade),
        "strategy_directions": {k: dict(v) for k, v in strategy_directions.items()},
        "gate_bypasses": len(gate_bypasses),
        "consensus_reasons": Counter(consensus_reasons).most_common(5),
        "brain_alerts": len(brain_alerts),
        "alert_types": dict(alert_types),
        "active_positions": len(set(e.get("ticket") for e in mgmt_diags)),
        "grace_periods": len(grace_skips),
        "trail_moves": len(trail_moves),
        "startup_issues": len([e for e in integrity if "error" in e.get("event","")]),
    }


def main() -> int:
    import io as _io

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # FIX-20260611-022
    parser = argparse.ArgumentParser(description="Live health audit")
    parser.add_argument("--data-dir-btc", default="data_btc")
    parser.add_argument("--data-dir-xau", default="data")
    args = parser.parse_args()

    all_passed = True

    for label, data_dir in [("BTC", args.data_dir_btc), ("XAU", args.data_dir_xau)]:
        r = audit_symbol(label, Path(data_dir))
        print(f"\n{'='*60}")
        print(f"  {label} — Live Trading Health Audit")
        print(f"{'='*60}")

        if "error" in r:
            print(f"  ERROR: {r['error']}")
            all_passed = False
            continue

        print(f"  Cycles: {r['cycles']} ({r['first_cycle']} → {r['last_cycle']})")
        print(f"  Dispatches: {r['dispatches']} | Positions: {r['positions_opened']}")
        print(f"  Eval cycles: {r['eval_cycles']}")
        print(f"  Active positions: {r['active_positions']}")
        print(f"  Gate bypasses: {r['gate_bypasses']} | Grace periods: {r['grace_periods']}")
        print(f"  Trail moves: {r['trail_moves']}")
        print(f"  Brain alerts: {r['brain_alerts']}")
        if r["alert_types"]:
            print(f"  Alert types: {r['alert_types']}")

        # ── Key diagnostics ──
        print("\n  ── Trade Decisions ──")
        if r["approved_trades"]:
            for strat, count in r["approved_trades"].items():
                print(f"    ✅ {strat}: {count} approved")
        if r["rejection_reasons"]:
            print("    ❌ Rejection reasons:")
            for reason, count in r["rejection_reasons"].items():
                print(f"       [{count:3d}] {reason[:80]}")
        else:
            print("    ⚠️  NO eval data — strategy evaluation not running!")

        if r["strategy_directions"]:
            print("\n  ── Strategy Directions ──")
            for strat, dirs in r["strategy_directions"].items():
                print(f"    {strat}: {dirs}")

        if r["consensus_reasons"]:
            print("\n  ── Consensus Phase ──")
            for reason, count in r["consensus_reasons"]:
                print(f"    [{count:3d}] {reason}")

        # ── Health flags ──
        flags = []
        if r["cycles"] == 0:
            flags.append("NO_CYCLES")
        if r["eval_cycles"] == 0:
            flags.append("NO_EVAL_DATA")
        if r["gate_bypasses"] > 0:
            flags.append(f"GATE_BYPASS_x{r['gate_bypasses']}")
        if r["brain_alerts"] > 0:
            flags.append(f"BRAIN_ALERTS_x{r['brain_alerts']}")
        if r["dispatches"] == 0 and r["cycles"] > 3:
            flags.append("NO_TRADES")
        if r["startup_issues"] > 0:
            flags.append("STARTUP_ERRORS")

        if flags:
            print(f"\n  🚩 FLAGS: {', '.join(flags)}")
            all_passed = False
        else:
            print("\n  ✅ HEALTHY")

    print(f"\n{'='*60}")
    print(f"OVERALL: {'PASS' if all_passed else 'NEEDS REVIEW'}")
    print(f"{'='*60}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

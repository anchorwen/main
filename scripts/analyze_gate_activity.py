"""Analyze gate activity from golden_master.jsonl — dead gate detection.

Iron Law #11 compliant: all statistics from script stdout, zero LLM inference.

Usage:
    python scripts/analyze_gate_activity.py --data-dir data_btc
    python scripts/analyze_gate_activity.py --data-dir data_btc --top-n 20 --dead-threshold-pct 0.05
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


def _classify_reason(reason: str) -> str:
    """Classify a gate reason string into a canonical gate name.

    Strips dynamic values (confidence floats, timestamps, durations)
    to produce stable gate category labels.
    """
    if not reason or not isinstance(reason, str):
        return "UNKNOWN"

    # Strip trailing float values (confidence, p_win, etc.)
    # e.g. "time_expired_confidence_decayed_0.627" → "time_expired_confidence_decayed"
    # e.g. "p_win_below_dynamic_floor_0.400_lt_0.550" → "p_win_below_dynamic_floor"
    cleaned = re.sub(r"[_\.]\d+\.?\d*(?=_|$)", "", reason)
    # Strip trailing "remaining_Ns_gap_Ns" for family_spacing
    cleaned = re.sub(r"_remaining_\d+s_gap_\d+s$", "", cleaned)
    # Strip trailing colon values
    cleaned = re.sub(r":[^:]+$", "", cleaned)

    return cleaned


def analyze_golden_master(
    data_dir: str,
    *,
    dead_threshold_pct: float = 0.05,
    top_n: int = 20,
) -> dict:
    """Analyze golden_master.jsonl for gate activity patterns.

    Returns a dict with gate_stats, summary, and dead_gates.
    """
    gm_path = Path(data_dir) / "golden_master.jsonl"
    if not gm_path.exists():
        print(f"ERROR: {gm_path} not found", file=sys.stderr)
        sys.exit(1)

    total_cycles = 0
    total_outputs = 0
    gate_counts: Counter = Counter()
    gate_strategy: defaultdict[str, Counter] = defaultdict(Counter)
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}

    with open(gm_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            total_cycles += 1
            ts = rec.get("timestamp_utc", "")
            outputs = rec.get("outputs", {})

            for strategy, output in outputs.items():
                if not isinstance(output, dict):
                    continue
                total_outputs += 1
                reason = output.get("reason", "MISSING")
                gate = _classify_reason(reason)

                gate_counts[gate] += 1
                gate_strategy[gate][strategy] += 1

                if gate not in first_seen:
                    first_seen[gate] = ts
                last_seen[gate] = ts

    # Compute metrics
    gate_stats = []
    for gate, count in gate_counts.most_common():
        pct = (count / total_outputs * 100) if total_outputs > 0 else 0.0
        strategies = gate_strategy[gate]
        top_strategy = strategies.most_common(1)[0] if strategies else ("?", 0)
        gate_stats.append(
            {
                "gate": gate,
                "blocks": count,
                "block_rate_pct": round(pct, 2),
                "top_strategy": top_strategy[0],
                "top_strategy_blocks": top_strategy[1],
                "unique_strategies": len(strategies),
                "first_seen": first_seen.get(gate, ""),
                "last_seen": last_seen.get(gate, ""),
            }
        )

    # Dead gates: block_rate below threshold
    dead_gates = [g for g in gate_stats if g["block_rate_pct"] < dead_threshold_pct]
    active_gates = [g for g in gate_stats if g["block_rate_pct"] >= dead_threshold_pct]

    return {
        "total_cycles": total_cycles,
        "total_outputs": total_outputs,
        "unique_gates": len(gate_counts),
        "gate_stats": gate_stats[:top_n],
        "dead_gate_count": len(dead_gates),
        "dead_gates": dead_gates,
        "active_gate_count": len(active_gates),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze gate activity from golden_master.jsonl — dead gate detection"
    )
    parser.add_argument(
        "--data-dir",
        default="data_btc",
        help="Data directory containing golden_master.jsonl (default: data_btc)",
    )
    parser.add_argument("--top-n", type=int, default=20, help="Show top N gates (default: 20)")
    parser.add_argument(
        "--dead-threshold-pct",
        type=float,
        default=0.05,
        help="Block rate below which a gate is considered dead (default: 0.05%%)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON instead of formatted table"
    )
    args = parser.parse_args()

    result = analyze_golden_master(
        args.data_dir,
        dead_threshold_pct=args.dead_threshold_pct,
        top_n=args.top_n,
    )

    if args.json:
        json.dump(result, sys.stdout, indent=2, default=str)
        return

    # ── Formatted output ──
    print(f"=== Gate Activity Analysis ===")
    print(f"Data dir:     {args.data_dir}")
    print(f"Total cycles: {result['total_cycles']}")
    print(f"Total outputs:{result['total_outputs']}")
    print(f"Unique gates: {result['unique_gates']}")
    print(f"Dead gates:   {result['dead_gate_count']} (block rate < {args.dead_threshold_pct}%)")
    print(f"Active gates: {result['active_gate_count']}")
    print()

    # Top gates table
    print(f"{'Gate':<55} {'Blocks':>7} {'Rate%':>7} {'Status':>8} {'Top Strategy':<25}")
    print("-" * 110)
    for g in result["gate_stats"]:
        status = "DEAD" if g["block_rate_pct"] < args.dead_threshold_pct else "ACTIVE"
        if g["gate"].startswith("approved"):
            status = "PASS"
        print(
            f"{g['gate']:<55} {g['blocks']:>7} {g['block_rate_pct']:>6.2f}% "
            f"{status:>8} {g['top_strategy']:<25}"
        )

    # Dead gates detail
    if result["dead_gates"]:
        print()
        print(f"--- Dead Gates (block rate < {args.dead_threshold_pct}%) ---")
        for g in result["dead_gates"]:
            if g["gate"].startswith("approved"):
                continue
            print(f"  {g['gate']:<55} {g['blocks']:>7} blocks " f"({g['block_rate_pct']:.3f}%)")

    print()
    print("[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Window 3 - G4: Snapshot Coverage Gap + G6/G7: XAU Governance
=============================================================
G4: Why is snapshot coverage low (BTC 34% / XAU 12.6%)?
G6: XAU Leaderboard 0 brains (FIX-132 only covered BTC)
G7: XAU training_readiness.json missing

Output: self-contained closing report for G4+G6+G7.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    records.append(json.loads(line))
    return records


def check_snapshot_coverage(label: str, data_dir: Path) -> dict[str, Any]:
    """G4: Why is snapshot coverage low?"""
    journal = load_jsonl(data_dir / "live_trade_journal.jsonl")
    snaps = load_jsonl(data_dir / "position_snapshots.jsonl")

    opens = {r.get("position_ticket"): r for r in journal if r.get("action") == "open" and isinstance(r.get("position_ticket"), int)}
    closes = {r.get("position_ticket"): r for r in journal if r.get("action") == "close" and isinstance(r.get("position_ticket"), int)}
    matched = set(opens.keys()) & set(closes.keys())

    snap_tickets: set[int] = set()
    for s in snaps:
        t = s.get("ticket") or s.get("position_ticket")
        if isinstance(t, int):
            snap_tickets.add(t)

    with_snap = matched & snap_tickets
    no_snap = matched - snap_tickets
    coverage = len(with_snap) / max(len(matched), 1) * 100

    # Label distribution for no-snapshot trades
    no_snap_labels: Counter[str] = Counter()
    for ticket in no_snap:
        c = closes.get(ticket)
        if c:
            lbl = c.get("label", "no_label")
            no_snap_labels[str(lbl)] += 1

    # Snapshot count per trade distribution
    snap_per_trade: Counter[int] = Counter()
    for ticket in with_snap:
        count = sum(1 for s in snaps if (s.get("ticket") or s.get("position_ticket")) == ticket)
        snap_per_trade[count] += 1

    return {
        "label": label,
        "matched_trades": len(matched),
        "snaps_total": len(snaps),
        "with_snapshots": len(with_snap),
        "without_snapshots": len(no_snap),
        "coverage_pct": round(coverage, 1),
        "no_snap_top_labels": no_snap_labels.most_common(5),
        "snap_per_trade_dist": dict(snap_per_trade.most_common(10)),
    }


def check_xau_governance(data_dir: Path) -> dict[str, Any]:
    """G6+G7: XAU governance gaps."""
    result: dict[str, Any] = {}

    # G6: Leaderboard
    lb_path = data_dir / "leaderboard.json"
    if lb_path.exists():
        lb = load_json(lb_path)
        brains = lb.get("brains", lb.get("entries", []))
        if isinstance(brains, list):
            result["leaderboard_brains"] = len(brains)
            result["leaderboard_brain_ids"] = [
                b.get("brain_id", b.get("id", "?")) for b in brains[:20]
            ]
        elif isinstance(brains, dict):
            result["leaderboard_brains"] = len(brains)
            result["leaderboard_brain_ids"] = list(brains.keys())[:20]
        else:
            result["leaderboard_brains"] = 0
            result["leaderboard_brain_ids"] = []
    else:
        result["leaderboard_brains"] = -1
        result["leaderboard_brain_ids"] = []
        result["leaderboard_missing"] = True

    # G7: training_readiness.json
    tr_path = data_dir / "training_readiness.json"
    if tr_path.exists():
        tr = load_json(tr_path)
        result["training_readiness_exists"] = True
        result["ready_to_train"] = tr.get("ready", tr.get("ready_to_train"))
        result["pending_brains"] = tr.get("pending", tr.get("brains_pending", []))
        result["blocked_brains"] = tr.get("blocked", [])
        result["last_checked"] = tr.get("last_checked", tr.get("updated_at", "N/A"))
    else:
        result["training_readiness_exists"] = False
        result["ready_to_train"] = None
        result["pending_brains"] = []
        result["blocked_brains"] = []

    # Governance state for XAU
    gov_path = data_dir / "governance_state.json"
    if gov_path.exists():
        gov = load_json(gov_path)
        brain_states = gov.get("brain_states", gov.get("brains", {}))
        if isinstance(brain_states, dict):
            result["gov_brains"] = len(brain_states)
            statuses = Counter(
                b.get("status", "unknown") for b in brain_states.values()
            )
            result["gov_status_dist"] = dict(statuses)
        elif isinstance(brain_states, list):
            result["gov_brains"] = len(brain_states)
        else:
            result["gov_brains"] = 0
    else:
        result["gov_brains"] = -1

    return result


def main():
    print("=" * 70)
    print("  WINDOW 3: G4 (Snapshot) + G6/G7 (XAU Governance)")
    print("=" * 70)

    # --- G4: Snapshot Coverage ---
    print(f"\n{'─' * 70}")
    print("  G4: Snapshot Coverage Gap")
    print(f"{'─' * 70}")

    for label, data_dir in [("BTC", Path("data_btc")), ("XAU", Path("data"))]:
        s = check_snapshot_coverage(label, data_dir)
        print(f"\n  {label}:")
        print(f"    Matched trades:       {s['matched_trades']}")
        print(f"    Total snapshots:      {s['snaps_total']}")
        print(f"    With snapshots:       {s['with_snapshots']} ({s['coverage_pct']}%)")
        print(f"    Without snapshots:    {s['without_snapshots']}")
        print(f"    No-snap top labels:   {s['no_snap_top_labels']}")
        print(f"    Snap/trade dist:      {s['snap_per_trade_dist']}")

    # --- G6+G7: XAU Governance ---
    print(f"\n{'─' * 70}")
    print("  G6/G7: XAU Governance Gaps")
    print(f"{'─' * 70}")

    xau = check_xau_governance(Path("data"))
    print(f"\n  XAU Leaderboard:        {xau['leaderboard_brains']} brains"
          f"{' (MISSING FILE!)' if xau.get('leaderboard_missing') else ''}")
    if xau["leaderboard_brains"] > 0:
        print(f"    Brain IDs:            {xau['leaderboard_brain_ids'][:10]}")

    print(f"  XAU Governance:         {xau['gov_brains']} brains")
    if xau.get("gov_status_dist"):
        print(f"    Status dist:          {xau['gov_status_dist']}")

    tr = "EXISTS" if xau["training_readiness_exists"] else "MISSING"
    print(f"  training_readiness.json: {tr}")
    if xau["training_readiness_exists"]:
        print(f"    Ready to train:       {xau['ready_to_train']}")
        print(f"    Pending brains:       {len(xau.get('pending_brains', []))}")
        print(f"    Last checked:         {xau.get('last_checked', 'N/A')}")

    # --- Verdicts ---
    print(f"\n{'=' * 70}")
    print("  G4+G6+G7 VERDICT")
    print(f"{'=' * 70}")

    gaps_4 = []
    for label, data_dir in [("BTC", Path("data_btc")), ("XAU", Path("data"))]:
        s = check_snapshot_coverage(label, data_dir)
        if s["coverage_pct"] < 50:
            gaps_4.append(f"G4 {label}: snapshot coverage {s['coverage_pct']}% < 50%")

    gaps_67 = []
    if xau["leaderboard_brains"] <= 0:
        gaps_67.append(f"G6: XAU leaderboard has {xau['leaderboard_brains']} brains (need >=1)")
    if not xau["training_readiness_exists"]:
        gaps_67.append("G7: XAU training_readiness.json MISSING")
    elif not xau.get("ready_to_train"):
        gaps_67.append("G7: training_readiness exists but ready=False")

    for g in gaps_4 + gaps_67:
        print(f"  [!] {g}")

    total_gaps = len(gaps_4) + len(gaps_67)
    if total_gaps > 0:
        print(f"\n  G4/G6/G7 STATUS: OPEN ({total_gaps} gap(s))")
    else:
        print("\n  G4/G6/G7 STATUS: ALL CLOSED")

    print("\n[DONE] Window 3 complete.")


if __name__ == "__main__":
    main()

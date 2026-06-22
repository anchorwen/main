#!/usr/bin/env python
"""Window 2 - G3: Alpha Signal Pipeline Vacuum
=============================================
Checks:
  A. alpha_allocation.json state (any brains registered?)
  B. How many brains have valid alpha signals in recent journal
  C. Is the AlphaAllocation framework (FIX-004) connected to data flow?
  D. Brain registration vs alpha feed gap

Output: self-contained closing report for G3.
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


def check_alpha_pipeline(label: str, data_dir: Path) -> dict[str, Any]:
    """Full alpha pipeline audit for one symbol."""
    result: dict[str, Any] = {"label": label}

    # A. alpha_allocation.json
    # DQAF-053: alpha_allocation.json lives in reports/ subdirectory
    alpha_path = data_dir / "reports" / "alpha_allocation.json"
    if not alpha_path.exists():
        alpha_path = data_dir / "alpha_allocation.json"  # legacy fallback
    if alpha_path.exists():
        alpha = load_json(alpha_path)
        result["alpha_state_exists"] = True
        allocations = alpha.get("allocations", alpha.get("brain_allocations", {}))
        result["registered_brains"] = len(allocations) if isinstance(allocations, dict) else 0
        result["brain_list"] = (
            list(allocations.keys())[:20] if isinstance(allocations, dict) else []
        )
        result["total_alpha"] = (
            sum(a.get("weight", 0) for a in allocations.values())
            if isinstance(allocations, dict)
            else 0
        )
    else:
        result["alpha_state_exists"] = False
        result["registered_brains"] = 0
        result["brain_list"] = []
        result["total_alpha"] = 0

    # B. Journal: which brains are producing signals?
    journal = load_jsonl(data_dir / "live_trade_journal.jsonl")
    opens = [r for r in journal if r.get("action") == "open"]
    brain_counter: Counter[str] = Counter()
    for o in opens:
        bids = o.get("brain_ids") or ["unknown"]
        if isinstance(bids, str):
            bids = [bids]
        for b in bids:
            if b:
                brain_counter[b] += 1
    brain_counter.pop("unknown", None)
    result["journal_brains_active"] = len(brain_counter)
    result["journal_brain_trade_counts"] = brain_counter.most_common(20)

    # C. Cross-reference: alpha brains vs journal brains
    alpha_brains = set(result.get("brain_list", []))
    journal_brains = set(brain_counter.keys())
    result["alpha_not_in_journal"] = sorted(alpha_brains - journal_brains)
    result["journal_not_in_alpha"] = sorted(journal_brains - alpha_brains)
    result["both"] = sorted(alpha_brains & journal_brains)

    # D. Check execution_state for alpha feed wiring
    # DQAF-053: execution_state.json may live in reports/ subdirectory
    exec_path = data_dir / "reports" / "execution_state.json"
    if not exec_path.exists():
        exec_path = data_dir / "execution_state.json"  # legacy fallback
    if exec_path.exists():
        es = load_json(exec_path)
        result["exec_state_exists"] = True
        result["alpha_feed_active"] = es.get("alpha_feed_active", es.get("alpha_enabled"))
    else:
        result["exec_state_exists"] = False
        result["alpha_feed_active"] = None

    return result


def main():
    targets = [
        ("BTC", Path("data_btc")),
        ("XAU", Path("data")),
    ]

    print("=" * 70)
    print("  WINDOW 2: G3 - Alpha Signal Pipeline Vacuum")
    print("=" * 70)

    all_results = {}
    for label, data_dir in targets:
        print(f"\n--- {label} ({data_dir}) ---")
        r = check_alpha_pipeline(label, data_dir)
        all_results[label] = r
        print(f"  alpha_allocation.json:     {'EXISTS' if r['alpha_state_exists'] else 'MISSING'}")
        print(f"  Registered alpha brains:   {r['registered_brains']}")
        if r["registered_brains"] > 0:
            print(f"  Brain list:                {r['brain_list'][:10]}")
            print(f"  Total alpha weight:        {r['total_alpha']:.4f}")
        print(f"  Journal active brains:     {r['journal_brains_active']}")
        print(f"  Top journal brains:        {r['journal_brain_trade_counts'][:5]}")
        print(f"  In BOTH (alpha+journal):   {len(r['both'])}")
        print(f"  Alpha-only (no journal):   {len(r['alpha_not_in_journal'])}")
        print(f"  Journal-only (no alpha):   {len(r['journal_not_in_alpha'])}")
        print(f"  Alpha feed active:         {r.get('alpha_feed_active')}")

    # GAP assessment
    print(f"\n{'=' * 70}")
    print("  G3 VERDICT")
    print(f"{'=' * 70}")
    gaps = []
    for label, r in all_results.items():
        if r["registered_brains"] == 0:
            gaps.append(f"{label}: 0 brains registered in alpha_allocation.json")
        if not r.get("alpha_feed_active"):
            gaps.append(f"{label}: alpha_feed not enabled in execution_state")
        if r["journal_brains_active"] > 0 and r["registered_brains"] == 0:
            gaps.append(
                f"{label}: {r['journal_brains_active']} brains active in journal "
                f"but 0 registered for alpha allocation"
            )
    if gaps:
        for g in gaps:
            print(f"  [!] {g}")
        print(f"\n  G3 STATUS: OPEN ({len(gaps)} gap(s))")
    else:
        print("  G3 STATUS: CLOSED - Alpha pipeline wired and allocating")

    print("\n[DONE] Window 2 complete.")


if __name__ == "__main__":
    main()

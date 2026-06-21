"""
DQAF-033 P0: Hard Time-Series Correlation — close_accepted ↔ mt5_deal_reason_3
===============================================================================
IC Mandate amendment: asof merge with ±5s tolerance to determine how many
mt5_deal_reason_3 events are shadow records of close_accepted (H1: double-journaling)
vs genuinely external closes (H2: broker B-book sweep).

Key question: given |close_accepted|=51 and |MIA|=99, how many pairs can H1 explain?
Δ = 99 - 51 - matched = genuine external closes.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def analyze_temporal_coupling(data_dir: str) -> dict[str, Any]:
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {"error": f"Journal not found: {journal_path}"}

    # ── 1. Load all close events with their type ──
    close_accepted: list[dict] = []
    mia_events: list[dict] = []
    all_opens: dict[int, dict] = {}

    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("action") == "open":
                t = rec.get("position_ticket")
                if t:
                    all_opens[t] = rec
            elif rec.get("action") == "close":
                label = rec.get("label", "")
                detail = rec.get("detail", {})
                reason = detail.get("reason", "") if isinstance(detail, dict) else ""
                if label == "close_accepted":
                    close_accepted.append(rec)
                if reason == "mt5_deal_reason_3":
                    mia_events.append(rec)

    print(f"[DATA] close_accepted: {len(close_accepted)}, MIA: {len(mia_events)}, Opens: {len(all_opens)}")

    # ── 2. Build time-indexed lists ──
    ca_times: list[tuple[datetime, dict]] = []
    for rec in close_accepted:
        ts = parse_ts(rec.get("recorded_at", ""))
        if ts:
            ca_times.append((ts, rec))

    mia_times: list[tuple[datetime, dict]] = []
    for rec in mia_events:
        ts = parse_ts(rec.get("recorded_at", ""))
        if ts:
            mia_times.append((ts, rec))

    ca_times.sort(key=lambda x: x[0])
    mia_times.sort(key=lambda x: x[0])

    # ── 3. Asof merge: for each close_accepted, find nearest MIA within ±5s ──
    TOLERANCE = 5.0  # seconds
    matched_ca: set[int] = set()  # indices into ca_times
    matched_mia: set[int] = set()  # indices into mia_times
    pairs: list[dict] = []

    for i, (ca_ts, ca_rec) in enumerate(ca_times):
        best_j = -1
        best_gap = float("inf")
        for j, (mia_ts, mia_rec) in enumerate(mia_times):
            if j in matched_mia:
                continue
            gap = abs((ca_ts - mia_ts).total_seconds())
            if gap < TOLERANCE and gap < best_gap:
                best_gap = gap
                best_j = j

        if best_j >= 0:
            matched_ca.add(i)
            matched_mia.add(best_j)
            pairs.append({
                "ca_index": i,
                "mia_index": best_j,
                "ca_ts": ca_ts.isoformat(),
                "mia_ts": mia_times[best_j][0].isoformat(),
                "gap_seconds": round(best_gap, 2),
                "ca_ticket": ca_rec.get("position_ticket"),
                "mia_ticket": mia_times[best_j][1].get("position_ticket"),
                "same_ticket": ca_rec.get("position_ticket") == mia_times[best_j][1].get("position_ticket"),
            })

    # ── 4. Categorize unmatched ──
    unmatched_ca = [ca_times[i] for i in range(len(ca_times)) if i not in matched_ca]
    unmatched_mia = [mia_times[j] for j in range(len(mia_times)) if j not in matched_mia]

    # ── 5. For unmatched MIA, check if they have open events ──
    unmatched_mia_with_open = []
    unmatched_mia_no_open = []
    for ts, rec in unmatched_mia:
        ticket = rec.get("position_ticket")
        if ticket and ticket in all_opens:
            unmatched_mia_with_open.append((ts, rec))
        else:
            unmatched_mia_no_open.append((ts, rec))

    # ── 6. Check POSITION_IDENTIFIER usage ──
    # In close_accepted: look for identifier/position_id fields
    ca_identifiers = Counter()
    for _, rec in ca_times:
        detail = rec.get("detail", {})
        if isinstance(detail, dict):
            for k in ["identifier", "position_id", "pos_identifier", "pos_id"]:
                if k in detail:
                    ca_identifiers["has_identifier"] += 1
                    break
            else:
                ca_identifiers["no_identifier"] += 1

    mia_identifiers = Counter()
    for _, rec in mia_times:
        detail = rec.get("detail", {})
        if isinstance(detail, dict):
            for k in ["identifier", "position_id", "pos_identifier", "pos_id"]:
                if k in detail:
                    mia_identifiers["has_identifier"] += 1
                    break
            else:
                mia_identifiers["no_identifier"] += 1

    # ── 7. For matched pairs where same_ticket=False, check if they share
    # any open event (same position, different recording)
    matched_same_position = 0
    for pair in pairs:
        if pair["same_ticket"]:
            matched_same_position += 1

    # ── 8. Unmatched close_accepted analysis ──
    # These are bridge-worker closes that PCA didn't detect — possible PCA blind spot
    ca_no_match_detail = []
    for ts, rec in unmatched_ca:
        detail = rec.get("detail", {})
        ca_no_match_detail.append({
            "ticket": rec.get("position_ticket"),
            "ts": ts.isoformat(),
            "pnl": rec.get("pnl"),
            "has_profit": "profit" in detail if isinstance(detail, dict) else False,
            "has_fill_vol": "fill_volume" in detail if isinstance(detail, dict) else False,
        })

    # ── 9. Compute Δ = genuine external closes ──
    # Theory: at most min(|C|, |M|) can be double-journaling
    # Excess MIA (>|C|) are genuine externals regardless of matching
    max_explainable = min(len(close_accepted), len(mia_events))
    matched_count = len(pairs)
    min_genuine_external = len(mia_events) - max_explainable  # cardinality argument
    actual_genuine_external = len(mia_events) - matched_count

    return {
        "close_accepted_count": len(close_accepted),
        "mia_count": len(mia_events),
        "tolerance_seconds": TOLERANCE,
        "matched_pairs": len(pairs),
        "matched_same_ticket": matched_same_position,
        "matched_diff_ticket": len(pairs) - matched_same_position,
        "unmatched_ca_count": len(unmatched_ca),
        "unmatched_mia_count": len(unmatched_mia),
        "unmatched_mia_with_open": len(unmatched_mia_with_open),
        "unmatched_mia_no_open": len(unmatched_mia_no_open),
        "cardinality_gap": len(mia_events) - len(close_accepted),
        "min_genuine_external_by_math": min_genuine_external,
        "actual_genuine_external": actual_genuine_external,
        "explanation_coverage_pct": round(matched_count / max(len(mia_events), 1) * 100, 1),
        "pairs": pairs[:20],  # first 20 for inspection
        "unmatched_ca_sample": ca_no_match_detail[:10],
        "identifier_usage": {
            "close_accepted": dict(ca_identifiers),
            "mia": dict(mia_identifiers),
        },
        "h1_max_explanation_pct": round(max_explainable / max(len(mia_events), 1) * 100, 1),
        "h2_min_explanation_pct": round(min_genuine_external / max(len(mia_events), 1) * 100, 1),
    }


def print_report(results: dict) -> None:
    if "error" in results:
        print(f"ERROR: {results['error']}")
        return

    print("=" * 90)
    print("  DQAF-033 P0: 硬核时间序列关联分析")
    print(f"  close_accepted ↔ mt5_deal_reason_3  asof merge (±{results['tolerance_seconds']}s)")
    print("=" * 90)

    print(f"\n  close_accepted: {results['close_accepted_count']}")
    print(f"  MIA (reason=3):  {results['mia_count']}")
    print(f"  基数差 Δ:       {results['cardinality_gap']} (MIA - close_accepted)")

    print(f"\n  ── MATCHING RESULTS ──")
    print(f"  Matched pairs:       {results['matched_pairs']}")
    print(f"    Same ticket:       {results['matched_same_ticket']}")
    print(f"    Different ticket:  {results['matched_diff_ticket']}")
    print(f"  Unmatched close_accepted: {results['unmatched_ca_count']}")
    print(f"  Unmatched MIA:            {results['unmatched_mia_count']}")
    print(f"    With open event:   {results['unmatched_mia_with_open']}")
    print(f"    No open event:     {results['unmatched_mia_no_open']}")

    print(f"\n  ── ROOT CAUSE ATTRIBUTION ──")
    print(f"  H1 (double-journaling) max coverage: {results['h1_max_explanation_pct']}%")
    print(f"  H2 (genuine external) min share:     {results['h2_min_explanation_pct']}%")
    print(f"  Matched within ±{results['tolerance_seconds']}s:            {results['matched_pairs']}/{results['mia_count']} ({results['explanation_coverage_pct']}%)")
    print(f"  → Actual H2 (genuine external):      {results['actual_genuine_external']}/{results['mia_count']}")

    print(f"\n  ── POSITION IDENTIFIER USAGE ──")
    print(f"  close_accepted: {results['identifier_usage']['close_accepted']}")
    print(f"  MIA:            {results['identifier_usage']['mia']}")

    if results.get("pairs"):
        print(f"\n  ── SAMPLE MATCHED PAIRS (first 10) ──")
        print(f"  {'CA Time':<22} {'MIA Time':<22} {'Gap':>6} {'SameTkt':>8}")
        for p in results["pairs"][:10]:
            print(f"  {p['ca_ts'][:19]:<22} {p['mia_ts'][:19]:<22} {p['gap_seconds']:>5.1f}s {str(p['same_ticket']):>8}")

    if results.get("unmatched_ca_sample"):
        print(f"\n  ── SAMPLE UNMATCHED close_accepted ──")
        for u in results["unmatched_ca_sample"][:5]:
            print(f"    ticket={u['ticket']} ts={u['ts'][:19]} pnl={u['pnl']} profit={u['has_profit']} fill_vol={u['has_fill_vol']}")

    # ── VERDICT ──
    print(f"\n  ── VERDICT ──")
    verdict_parts = []
    if results["matched_same_ticket"] > 0:
        verdict_parts.append(f"H1 confirmed: {results['matched_same_ticket']} double-journaling pairs (same ticket)")
    if results["matched_diff_ticket"] > 0:
        verdict_parts.append(f"H1b confirmed: {results['matched_diff_ticket']} pairs with DIFFERENT tickets → ticket mapping bug")
    if results["actual_genuine_external"] > 0:
        verdict_parts.append(f"H2 confirmed: {results['actual_genuine_external']} genuinely external closes ({results['actual_genuine_external']/max(results['mia_count'],1)*100:.0f}%)")
    if results["unmatched_mia_no_open"] > 0:
        verdict_parts.append(f"CRITICAL: {results['unmatched_mia_no_open']} MIA have NO open event → ghost positions")
    for vp in verdict_parts:
        print(f"  {vp}")

    print("\n" + "=" * 90)
    print("  [DONE] All statistics above are the sole source of truth (Iron Law #11).")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--tolerance", type=float, default=5.0)
    args = parser.parse_args()
    results = analyze_temporal_coupling(args.data_dir)
    print_report(results)


if __name__ == "__main__":
    main()

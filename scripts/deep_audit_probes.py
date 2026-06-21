#!/usr/bin/env python
"""Deep Dive: Targeted investigation of critical anomalies found in deep audit.
DQAF-20260621-043 — Phase 2: Root cause probes.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


def probe_close_only_gap(data_dir: Path, label: str):
    """Investigate WHY so many close records have no matching open.

    Hypotheses:
    A. Close records use different ticket IDs than opens (MT5 ticket format mismatch)
    B. Open records exist but in a different time window (journal rotation)
    C. Orphan entries from MT5 bridge — positions opened before system start
    D. MIA (missing-in-action) close events without corresponding opens
    """
    print(f"\n{'='*72}")
    print(f"  PROBE 1: Close-Only Gap Root Cause — {label}")
    print(f"{'='*72}")

    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)

    opens = [r for r in records if r.get("action") == "open"]
    closes = [r for r in records if r.get("action") == "close"]

    open_tickets = {r.get("position_ticket") for r in opens if r.get("position_ticket")}
    close_tickets = {r.get("position_ticket") for r in closes if r.get("position_ticket")}

    close_only_tickets = close_tickets - open_tickets
    open_only_tickets = open_tickets - close_tickets

    print(f"  Open tickets:  {len(open_tickets)}")
    print(f"  Close tickets: {len(close_tickets)}")
    print(f"  Close-only:    {len(close_only_tickets)}")
    print(f"  Open-only:     {len(open_only_tickets)}")

    # Sample close-only records to understand their structure
    close_only_records = [r for r in closes if r.get("position_ticket") in close_only_tickets]

    print("\n  -- Close-only ticket value ranges --")
    if close_only_tickets:
        tickets_list = sorted(close_only_tickets)
        print(f"  Min ticket: {tickets_list[0]}")
        print(f"  Max ticket: {tickets_list[-1]}")
        print(f"  Sample tickets: {tickets_list[:10]}")

    # Check MT5 ticket format: MT5 uses much larger ticket numbers
    # System-generated tickets typically start from 1
    mt5_style = [t for t in close_only_tickets if t > 1_000_000_000]
    system_style = [t for t in close_only_tickets if t < 1_000_000_000]
    print(f"\n  MT5-style tickets (>1B): {len(mt5_style)}")
    print(f"  System-style tickets (<1B): {len(system_style)}")

    # Check labels on close-only records
    close_only_labels = Counter(r.get("label", "no_label") for r in close_only_records)
    print("\n  -- Close-only label distribution --")
    for lbl, cnt in close_only_labels.most_common(10):
        lbl_str = str(lbl) if lbl is not None else "None"
        print(f"  {lbl_str:30s} -> {cnt}")

    # Check if close-only records have detail/reason
    close_only_with_detail = [r for r in close_only_records if r.get("detail")]
    print(f"\n  Close-only with detail: {len(close_only_with_detail)}")
    if close_only_with_detail:
        details = Counter(
            str(r.get("detail", {}).get("reason", r.get("detail")))
            for r in close_only_with_detail
        )
        for d, cnt in details.most_common(5):
            print(f"    reason='{d}': {cnt}")

    # Check deal_reason on close-only records
    deal_reasons = Counter()
    for r in close_only_records:
        dr = r.get("deal_reason") or r.get("mt5_deal_reason") or (r.get("detail", {}).get("deal_reason") if isinstance(r.get("detail"), dict) else None)
        if dr:
            deal_reasons[str(dr)] += 1
    if deal_reasons:
        print("\n  -- Close-only deal_reason distribution --")
        for dr, cnt in deal_reasons.most_common(10):
            print(f"  {dr}: {cnt}")

    # Time analysis: are close-only records older or newer?
    close_only_times = [r.get("recorded_at", "") for r in close_only_records if r.get("recorded_at")]
    matched_close_times = [r.get("recorded_at", "") for r in closes if r.get("position_ticket") in open_tickets and r.get("recorded_at")]

    if close_only_times:
        close_only_times.sort()
        print(f"\n  Close-only time range: {close_only_times[0]} -> {close_only_times[-1]}")
    if matched_close_times:
        matched_close_times.sort()
        print(f"  Matched close time range: {matched_close_times[0]} -> {matched_close_times[-1]}")


def probe_unknown_brain_trades(data_dir: Path, label: str):
    """Investigate trades assigned to 'unknown' brain."""
    print(f"\n{'='*72}")
    print(f"  PROBE 2: Unknown Brain Trades — {label}")
    print(f"{'='*72}")

    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)

    opens = [r for r in records if r.get("action") == "open"]
    closes = [r for r in records if r.get("action") == "close"]

    close_by_ticket = {r.get("position_ticket"): r for r in closes if r.get("position_ticket")}
    open_by_ticket = {r.get("position_ticket"): r for r in opens if r.get("position_ticket")}

    unknown_trades = []
    for ticket in set(open_by_ticket.keys()) & set(close_by_ticket.keys()):
        o = open_by_ticket[ticket]
        brain_ids = o.get("brain_ids") or ["unknown"]
        if isinstance(brain_ids, str):
            brain_ids = [brain_ids]
        if "unknown" in brain_ids or not brain_ids or brain_ids == [None]:
            c = close_by_ticket[ticket]
            pnl = c.get("pnl", 0) or 0
            unknown_trades.append({
                "ticket": ticket,
                "pnl": pnl,
                "label": c.get("label", ""),
                "side": o.get("side", ""),
                "open_time": o.get("recorded_at", ""),
                "close_time": c.get("recorded_at", ""),
                "brain_ids": brain_ids,
            })

    print(f"  Unknown-brain trades: {len(unknown_trades)}")
    if unknown_trades:
        total_pnl = sum(t["pnl"] for t in unknown_trades)
        print(f"  Total PnL: {total_pnl:+.2f}R")

        # Side distribution
        sides = Counter(t["side"] for t in unknown_trades)
        print(f"  Sides: {dict(sides)}")

        # Label distribution
        labels = Counter(t["label"] for t in unknown_trades)
        print(f"  Labels: {dict(labels)}")

        # Show sample trades
        print("\n  -- Sample unknown trades --")
        for t in unknown_trades[:5]:
            print(f"  ticket={t['ticket']}, side={t['side']}, pnl={t['pnl']:+.2f}R, label={t['label']}, time={t['open_time']}")

        # Check if these are from specific time periods
        if unknown_trades:
            times = [t["open_time"] for t in unknown_trades if t["open_time"]]
            times.sort()
            print(f"\n  Time range: {times[0]} -> {times[-1]}" if times else "  No timestamps")


def probe_snapshot_coverage_gap(data_dir: Path, label: str):
    """Investigate why snapshot coverage is low."""
    print(f"\n{'='*72}")
    print(f"  PROBE 3: Snapshot Coverage Gap — {label}")
    print(f"{'='*72}")

    journal_path = data_dir / "live_trade_journal.jsonl"
    snap_path = data_dir / "position_snapshots.jsonl"

    journal = load_jsonl(journal_path)
    snaps = load_jsonl(snap_path)

    # Get all completed trade tickets (matched open+close)
    opens = {r.get("position_ticket"): r for r in journal if r.get("action") == "open" and r.get("position_ticket")}
    closes = {r.get("position_ticket"): r for r in journal if r.get("action") == "close" and r.get("position_ticket")}
    matched = set(opens.keys()) & set(closes.keys())

    # Get all snapshot tickets
    snap_tickets = set()
    for s in snaps:
        ticket = s.get("ticket") or s.get("position_ticket")
        if ticket:
            snap_tickets.add(ticket)

    no_snap = matched - snap_tickets
    with_snap = matched & snap_tickets

    print(f"  Matched trades:        {len(matched)}")
    print(f"  With snapshots:        {len(with_snap)} ({len(with_snap)/max(len(matched),1)*100:.1f}%)")
    print(f"  Without snapshots:     {len(no_snap)} ({len(no_snap)/max(len(matched),1)*100:.1f}%)")

    # Check if no-snapshot trades share characteristics
    no_snap_trades = []
    for ticket in no_snap:
        o = opens.get(ticket)
        c = closes.get(ticket)
        if o and c:
            no_snap_trades.append({
                "ticket": ticket,
                "pnl": c.get("pnl", 0) or 0,
                "label": c.get("label", ""),
                "open_time": o.get("recorded_at", ""),
                "close_time": c.get("recorded_at", ""),
            })

    if no_snap_trades:
        # Are they breakeven/MIA trades?
        labels = Counter(t["label"] for t in no_snap_trades)
        print("\n  -- No-snapshot trade labels --")
        for lbl, cnt in labels.most_common(10):
            print(f"  {lbl}: {cnt}")

        # Time distribution
        times = [t["open_time"] for t in no_snap_trades if t["open_time"]]
        if times:
            times.sort()
            print(f"\n  Time range: {times[0]} -> {times[-1]}")


def probe_journal_vs_governance_brain_count(data_dir: Path, label: str):
    """Why does governance have so few brains vs journal?"""
    print(f"\n{'='*72}")
    print(f"  PROBE 4: Journal vs Governance Brain Mismatch — {label}")
    print(f"{'='*72}")

    # Journal brains
    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)
    opens = [r for r in records if r.get("action") == "open"]

    journal_brain_set = set()
    for o in opens:
        bids = o.get("brain_ids") or ["unknown"]
        if isinstance(bids, str):
            bids = [bids]
        for b in bids:
            if b:
                journal_brain_set.add(b)
    journal_brain_set.discard(None)
    journal_brain_set.discard("unknown")

    print(f"  Journal brains (named): {len(journal_brain_set)}")
    print(f"  Journal brains: {sorted(journal_brain_set)[:20]}...")

    # Governance brains
    gov_path = data_dir / "governance_state.json"
    with open(gov_path, encoding="utf-8") as f:
        gov = json.load(f)

    brain_states = gov.get("brain_states", gov.get("brains", {}))
    if isinstance(brain_states, dict):
        gov_brains = set(brain_states.keys())
    elif isinstance(brain_states, list):
        gov_brains = set(b.get("brain_id", "") for b in brain_states)
    else:
        gov_brains = set()

    print(f"  Governance brains (current): {len(gov_brains)}")

    # Transition log brains
    trans_log = gov.get("transition_log", [])
    trans_brains = set(t.get("brain_id", "") for t in trans_log)
    trans_brains.discard("")
    print(f"  Transition log brains (ever): {len(trans_brains)}")

    # Which journal brains are NOT in governance?
    missing_from_gov = journal_brain_set - gov_brains
    print(f"\n  Journal brains MISSING from governance: {len(missing_from_gov)}")
    for b in sorted(missing_from_gov)[:15]:
        # Check if it was ever in transition log
        in_trans = "YES" if b in trans_brains else "NO"
        print(f"    {b:45s} in_transition_log={in_trans}")

    # Which governance brains have ZERO journal trades?
    gov_only = gov_brains - journal_brain_set
    if gov_only:
        print(f"\n  Governance brains with ZERO journal trades: {len(gov_only)}")
        for b in sorted(gov_only):
            if isinstance(brain_states, dict) and b in brain_states:
                perf = brain_states[b].get("performance_metrics", {})
                trades = perf.get("total_trades", "?") if isinstance(perf, dict) else "?"
                status = brain_states[b].get("status", "?")
                print(f"    {b:45s} status={status}, gov_trades={trades}")


def probe_close_no_label(data_dir: Path, label: str):
    """Investigate close records without labels."""
    print(f"\n{'='*72}")
    print(f"  PROBE 5: Close Records Without Labels — {label}")
    print(f"{'='*72}")

    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)
    closes = [r for r in records if r.get("action") == "close"]

    no_label = [c for c in closes if not c.get("label")]
    print(f"  Total closes: {len(closes)}")
    print(f"  Without label: {len(no_label)}")

    if no_label:
        # Show structure
        print("\n  -- Sample no-label close record --")
        sample = no_label[0]
        for k, v in sample.items():
            print(f"    {k}: {str(v)[:200]}")

        # Check if they have detail.reason
        with_reason = [c for c in no_label if c.get("detail") and isinstance(c.get("detail"), dict) and c["detail"].get("reason")]
        print(f"\n  With detail.reason: {len(with_reason)}")
        if with_reason:
            reasons = Counter(c["detail"]["reason"] for c in with_reason)
            for r, cnt in reasons.most_common():
                print(f"    {r}: {cnt}")


def main():
    targets = [
        (Path("data_btc"), "BTC"),
        (Path("data"), "XAU"),
    ]

    for data_dir, label in targets:
        print(f"\n{'#'*72}")
        print(f"  DEEP DIVE: {label} ({data_dir})")
        print(f"{'#'*72}")

        probe_close_only_gap(data_dir, label)
        probe_unknown_brain_trades(data_dir, label)
        probe_snapshot_coverage_gap(data_dir, label)
        probe_journal_vs_governance_brain_count(data_dir, label)
        probe_close_no_label(data_dir, label)

    print(f"\n{'='*72}")
    print("  DEEP DIVE COMPLETE")
    print(f"{'='*72}")
    print("\n[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()

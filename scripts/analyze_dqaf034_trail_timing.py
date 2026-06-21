#!/usr/bin/env python3
"""DQAF-034: Trailing SL lock/timing audit — Iron Law #11 analysis script.

Analysis dimensions:
  1. Per-ticket snapshot count distribution (0, 1, 2-5, 6-10, 11+)
  2. Trail distance delta: how many tickets have trail_sl_distance NEVER change?
  3. Time delta from position open to first snapshot
  4. Correlation: snapshot count vs trail advances vs PnL outcome
  5. Restart gap analysis: trail vacuum periods

Usage:
  python scripts/analyze_dqaf034_trail_timing.py --data-dir data_btc
  python scripts/analyze_dqaf034_trail_timing.py --data-dir data_btc --journal-path data_btc/live_trade_journal.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

UTC = timezone.utc


def load_journal(path: str) -> list[dict]:
    """Load trade journal, return sorted list of records."""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_snapshots(path: str) -> list[dict]:
    """Load position snapshots, return sorted list."""
    snaps = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snaps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return snaps


def parse_iso(ts: str) -> datetime | None:
    """Parse ISO timestamp to datetime (always timezone-aware)."""
    try:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="DQAF-034 Trail Timing Audit")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--journal-path", default=None)
    parser.add_argument("--snapshots-path", default=None)
    args = parser.parse_args()

    data_dir = args.data_dir
    journal_path = args.journal_path or f"{data_dir}/live_trade_journal.jsonl"
    snapshots_path = args.snapshots_path or f"{data_dir}/position_snapshots.jsonl"

    print("=" * 70)
    print("DQAF-034: Trailing SL Lock/Timing Audit")
    print(f"  Journal: {journal_path}")
    print(f"  Snapshots: {snapshots_path}")
    print("=" * 70)

    # ── Load data ──
    journal = load_journal(journal_path)
    snaps = load_snapshots(snapshots_path)

    # ── Index journal by position_ticket ──
    opens: dict[int, dict] = {}
    closes: dict[int, dict] = {}
    for rec in journal:
        ticket = rec.get("position_ticket")
        if ticket is None:
            continue
        action = rec.get("action", "")
        if action == "open":
            if ticket not in opens or (rec.get("recorded_at", "") > opens[ticket].get("recorded_at", "")):
                opens[ticket] = rec
        elif action == "close":
            closes[ticket] = rec  # last close wins

    # ── Index snapshots by ticket ──
    snap_by_ticket: dict[int, list[dict]] = defaultdict(list)
    for s in snaps:
        ticket = s.get("ticket")
        if ticket is not None:
            snap_by_ticket[int(ticket)].append(s)

    # Sort snapshots by time
    for ticket in snap_by_ticket:
        snap_by_ticket[ticket].sort(key=lambda s: s.get("time", ""))

    # ── Merge: open time + snapshots + close ──
    all_tickets = set(list(opens.keys()) + list(closes.keys()))
    traded_tickets = [t for t in all_tickets if t in opens]

    print(f"\n📊 总体统计:")
    print(f"  Journal records: {len(journal)}")
    print(f"  Open events: {len(opens)}")
    print(f"  Close events: {len(closes)}")
    print(f"  Position snapshots total: {len(snaps)}")
    print(f"  Unique tickets in snapshots: {len(snap_by_ticket)}")

    # ── Dimension 1: Snapshot count per ticket ──
    print(f"\n{'='*70}")
    print("DIMENSION 1: Snapshot Count Distribution (traded positions only)")
    print(f"{'='*70}")

    snap_count_dist: dict[str, list[int]] = defaultdict(list)
    no_snap_tickets = []
    for ticket in traded_tickets:
        count = len(snap_by_ticket.get(ticket, []))
        if count == 0:
            band = "0"
            no_snap_tickets.append(ticket)
        elif count == 1:
            band = "1"
        elif count <= 5:
            band = "2-5"
        elif count <= 10:
            band = "6-10"
        elif count <= 20:
            band = "11-20"
        else:
            band = "21+"
        snap_count_dist[band].append(ticket)

    for band in ["0", "1", "2-5", "6-10", "11-20", "21+"]:
        tickets_in_band = snap_count_dist.get(band, [])
        pct = len(tickets_in_band) / len(traded_tickets) * 100 if traded_tickets else 0
        print(f"  {band:>5s} snapshots: {len(tickets_in_band):>4d} positions ({pct:5.1f}%)")

    # ── Dimension 2: Trail distance delta ──
    print(f"\n{'='*70}")
    print("DIMENSION 2: Trail Distance Movement (did SL ever move?)")
    print(f"{'='*70}")

    trail_never_moved = []
    trail_moved = []
    no_trail_data = []

    for ticket in traded_tickets:
        ticket_snaps = snap_by_ticket.get(ticket, [])
        if not ticket_snaps:
            no_trail_data.append(ticket)
            continue

        distances = [s.get("trailing_sl_distance", 0) for s in ticket_snaps]
        unique_distances = set(distances)
        initial = distances[0] if distances else 0
        final = distances[-1] if distances else 0
        delta = final - initial

        if len(unique_distances) == 1:
            trail_never_moved.append((ticket, initial, len(ticket_snaps)))
        else:
            trail_moved.append((ticket, initial, final, delta, len(ticket_snaps)))

    print(f"  Trail NEVER moved: {len(trail_never_moved)} positions")
    print(f"  Trail MOVED:       {len(trail_moved)} positions")
    print(f"  No snapshot data:   {len(no_trail_data)} positions")

    if trail_never_moved:
        # Distribution of initial trail distances for never-moved
        init_dists = [d for _, d, _ in trail_never_moved]
        print(f"\n  Never-moved initial trail distance distribution:")
        dist_counter = Counter(init_dists)
        for dist, count in dist_counter.most_common(10):
            print(f"    initial={dist:>6.0f} pts: {count} positions")

        # Snap count vs trail movement
        low_snap_no_move = sum(1 for _, _, c in trail_never_moved if c <= 1)
        print(f"\n  Never-moved positions with ≤1 snapshot: {low_snap_no_move}/{len(trail_never_moved)}")
        # Show sample
        print(f"  Sample never-moved tickets (first 5):")
        for ticket, dist, count in trail_never_moved[:5]:
            open_rec = opens.get(ticket, {})
            print(f"    ticket={ticket}, snap_count={count}, trail_dist={dist}, "
                  f"side={open_rec.get('side','?')}, strategy={open_rec.get('strategy','?')}")

    if trail_moved:
        deltas = [d for _, _, _, d, _ in trail_moved]
        advances = [d for d in deltas if d > 0]
        retreats = [d for d in deltas if d < 0]
        print(f"\n  Trail moved positions: {len(trail_moved)}")
        print(f"    Trail advanced (SL tightened): {len(advances)}")
        print(f"    Trail retreated (SL loosened): {len(retreats)}")
        if advances:
            print(f"    Mean advance: {sum(advances)/len(advances):.1f} pts")
        print(f"  Sample moved tickets (first 5):")
        for ticket, init, final, delta, count in trail_moved[:5]:
            print(f"    ticket={ticket}, snaps={count}, {init:.0f}→{final:.0f} (Δ={delta:+.0f})")

    # ── Dimension 3: Time delta from open to first snapshot ──
    print(f"\n{'='*70}")
    print("DIMENSION 3: Time Delta — Position Open → First Snapshot")
    print(f"{'='*70}")

    time_deltas: list[float] = []
    for ticket in traded_tickets:
        open_rec = opens.get(ticket)
        ticket_snaps = snap_by_ticket.get(ticket, [])
        if not open_rec or not ticket_snaps:
            continue
        open_time = parse_iso(open_rec.get("recorded_at", ""))
        first_snap_time = parse_iso(ticket_snaps[0].get("time", ""))
        if open_time and first_snap_time:
            delta_s = (first_snap_time - open_time).total_seconds()
            time_deltas.append((ticket, delta_s))

    if time_deltas:
        deltas_only = [d for _, d in time_deltas]
        deltas_only.sort()
        n = len(deltas_only)
        print(f"  Positions with both open + snapshot: {n}")
        print(f"  Min time delta:  {deltas_only[0]:.0f}s")
        print(f"  P25 time delta:  {deltas_only[n//4]:.0f}s")
        print(f"  Median delta:    {deltas_only[n//2]:.0f}s")
        print(f"  P75 time delta:  {deltas_only[3*n//4]:.0f}s")
        print(f"  Max time delta:  {deltas_only[-1]:.0f}s")

        # Negative deltas = snapshot BEFORE position open (the race condition!)
        negative = [(t, d) for t, d in time_deltas if d < 0]
        zero_to_30s = [(t, d) for t, d in time_deltas if 0 <= d <= 30]
        thirty_to_60s = [(t, d) for t, d in time_deltas if 30 < d <= 60]
        one_to_5min = [(t, d) for t, d in time_deltas if 60 < d <= 300]
        over_5min = [(t, d) for t, d in time_deltas if d > 300]

        print(f"\n  Time bucket distribution:")
        print(f"    Negative (snap before open): {len(negative):>4d} ({len(negative)/n*100:5.1f}%) ⚠️ RACE")
        print(f"    0-30s:                        {len(zero_to_30s):>4d} ({len(zero_to_30s)/n*100:5.1f}%)")
        print(f"    30-60s:                       {len(thirty_to_60s):>4d} ({len(thirty_to_60s)/n*100:5.1f}%)")
        print(f"    1-5min:                       {len(one_to_5min):>4d} ({len(one_to_5min)/n*100:5.1f}%)")
        print(f"    >5min:                        {len(over_5min):>4d} ({len(over_5min)/n*100:5.1f}%)")

    # ── Dimension 4: PnL correlation ──
    print(f"\n{'='*70}")
    print("DIMENSION 4: Trail Activation vs PnL Outcome")
    print(f"{'='*70}")

    trail_active_pnl: list[float] = []
    trail_inactive_pnl: list[float] = []
    trail_never_moved_set = {t for t, _, _ in trail_never_moved}
    trail_moved_set = {t for t, _, _, _, _ in trail_moved}

    for ticket in traded_tickets:
        close_rec = closes.get(ticket)
        if close_rec is None:
            continue
        pnl = close_rec.get("pnl")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (ValueError, TypeError):
            continue

        if ticket in trail_never_moved_set:
            trail_inactive_pnl.append(pnl)
        elif ticket in trail_moved_set:
            trail_active_pnl.append(pnl)

    if trail_active_pnl:
        wins = sum(1 for p in trail_active_pnl if p > 0)
        total = len(trail_active_pnl)
        avg = sum(trail_active_pnl) / total
        print(f"  Trail ACTIVE ({total} trades):")
        print(f"    Win rate: {wins}/{total} = {wins/total*100:.1f}%")
        print(f"    Avg PnL:  {avg:+.2f}R")
        print(f"    Total PnL: {sum(trail_active_pnl):+.2f}R")

    if trail_inactive_pnl:
        wins = sum(1 for p in trail_inactive_pnl if p > 0)
        total = len(trail_inactive_pnl)
        avg = sum(trail_inactive_pnl) / total if total else 0
        print(f"  Trail INACTIVE ({total} trades):")
        print(f"    Win rate: {wins}/{total} = {wins/total*100:.1f}%")
        print(f"    Avg PnL:  {avg:+.2f}R")
        print(f"    Total PnL: {sum(trail_inactive_pnl):+.2f}R")

    if trail_active_pnl and trail_inactive_pnl:
        active_total = sum(trail_active_pnl)
        inactive_total = sum(trail_inactive_pnl)
        print(f"\n  Δ PnL (active - inactive): {active_total - inactive_total:+.2f}R")
        print(f"  Win rate Δ: {(len([p for p in trail_active_pnl if p>0])/len(trail_active_pnl) - len([p for p in trail_inactive_pnl if p>0])/len(trail_inactive_pnl))*100:+.1f}%")

    # ── Dimension 5: Trail vacuum periods (restart gaps) ──
    print(f"\n{'='*70}")
    print("DIMENSION 5: Trail Vacuum — Temporal Gaps in Snapshot Coverage")
    print(f"{'='*70}")

    if snaps:
        all_times = sorted([parse_iso(s["time"]) for s in snaps if parse_iso(s["time"])])
        if len(all_times) >= 2:
            gaps = []
            for i in range(1, len(all_times)):
                gap_s = (all_times[i] - all_times[i-1]).total_seconds()
                if gap_s > 300:  # gaps > 5 minutes
                    gaps.append((all_times[i-1], all_times[i], gap_s))

            print(f"  Total snapshots: {len(all_times)}")
            print(f"  Time span: {all_times[0].isoformat()} → {all_times[-1].isoformat()}")
            print(f"  Gaps > 5min: {len(gaps)}")
            if gaps:
                print(f"  Top 10 largest gaps:")
                gaps.sort(key=lambda g: -g[2])
                for start, end, gap_s in gaps[:10]:
                    print(f"    {start.isoformat()[:19]} → {end.isoformat()[:19]} : {gap_s/60:.0f}min")

    # ── Summary ──
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    total_with_trail = len(trail_moved) + len(trail_never_moved)
    pct_never_moved = len(trail_never_moved) / total_with_trail * 100 if total_with_trail else 0
    # Low-snap = 0 or 1 snapshots
    low_snap_tickets = set(no_snap_tickets + [t for t, _, c in trail_never_moved if c <= 1] +
                           [t for t, _, _, _, c in trail_moved if c <= 1])
    # Merge
    low_snap_all = set()
    for t in traded_tickets:
        c = len(snap_by_ticket.get(t, []))
        if c <= 1:
            low_snap_all.add(t)

    print(f"  Traded positions: {len(traded_tickets)}")
    print(f"  Trail never moved: {len(trail_never_moved)}/{total_with_trail} ({pct_never_moved:.1f}%)")
    print(f"  Positions with ≤1 snapshot: {len(low_snap_all)}/{len(traded_tickets)} "
          f"({len(low_snap_all)/len(traded_tickets)*100:.1f}%)")

    # The critical finding: of never-moved, how many had 0-1 snaps?
    never_moved_low_snap = [t for t, _, c in trail_never_moved if c <= 1]
    print(f"  Never-moved AND ≤1 snap: {len(never_moved_low_snap)}/{len(trail_never_moved)} "
          f"({len(never_moved_low_snap)/len(trail_never_moved)*100 if trail_never_moved else 0:.1f}%)")
    print(f"  → These are the TRAIL_VACUUM positions: not enough snapshots to activate trail")
    print(f"\n[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()

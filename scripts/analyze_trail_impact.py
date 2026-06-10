#!/usr/bin/env python3
"""
Trail Stop Modification — Phase 2 Deep Dive
============================================
Focus: compare by CLOSE date, analyze modify_sltp SL migration,
and snapshot trail progression with sharper metrics.

Dedup logic: group by position_ticket, use close-action record for PnL.
Win = PnL > 0.  Breakeven = |PnL| < 0.01 (counts as non-winner).
"""

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
CUTOFF = datetime(2026, 6, 9, 15, 0, 0, tzinfo=CST)


def load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt
    except (ValueError, TypeError):
        return None


def is_before_cutoff(ts_str: str | None) -> bool:
    dt = parse_ts(ts_str)
    return dt is not None and dt < CUTOFF


def is_after_cutoff(ts_str: str | None) -> bool:
    dt = parse_ts(ts_str)
    return dt is not None and dt >= CUTOFF


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    data_dir = "data_btc"
    journal = load_jsonl(os.path.join(data_dir, "live_trade_journal.jsonl"))
    snapshots = load_jsonl(os.path.join(data_dir, "position_snapshots.jsonl"))

    # ── SECTION A: Analysis by CLOSE date ────────────────────────────────
    print("=" * 70)
    print("  SECTION A: TRADE OUTCOMES BY CLOSE DATE")
    print("  (comparing exits before vs after 2026-06-09 15:00 CST)")
    print("=" * 70)

    by_ticket: dict[int, list[dict]] = defaultdict(list)
    for r in journal:
        ticket = r.get("position_ticket")
        if ticket:
            by_ticket[ticket].append(r)

    FINAL_ACTIONS = {
        "close",
        "loss",
        "win",
        "close_accepted",
        "tp_hit_first",
        "sl_hit_first",
        "breakeven",
        "auto_orphan_rejected",
        "manual_close",
    }

    trades_before_close = []
    trades_after_close = []

    for ticket, recs in by_ticket.items():
        finals = [r for r in recs if r.get("action") in FINAL_ACTIONS]
        if not finals:
            continue

        final = max(finals, key=lambda r: r.get("recorded_at", "z"))
        pnl = final.get("pnl")
        close_time = final.get("recorded_at")
        exit_label = final.get("label")
        exit_action = final.get("action")

        # Get open record
        opens = [r for r in recs if r.get("action") == "open"]
        open_time = opens[0].get("recorded_at") if opens else None

        # Get strategy/side from any record
        strategy = next((r.get("strategy") for r in recs if r.get("strategy")), None)
        side = next((r.get("side") for r in recs if r.get("side")), None)

        # Count modify_sltp records
        mod_count = sum(1 for r in recs if r.get("action") == "modify_sltp")

        # SL at open
        sl_open = opens[0].get("sl") if opens else None

        trade = {
            "ticket": ticket,
            "open_time": open_time,
            "close_time": close_time,
            "pnl": pnl,
            "exit_label": exit_label,
            "exit_action": exit_action,
            "strategy": strategy,
            "side": side,
            "sl_open": sl_open,
            "mod_count": mod_count,
            "is_winner": pnl is not None and pnl > 0,
            "is_loser": pnl is not None and pnl < 0,
        }

        if is_after_cutoff(close_time):
            trades_after_close.append(trade)
        else:
            trades_before_close.append(trade)

    def report_trades(trades, label):
        closed = [t for t in trades if t["pnl"] is not None]
        winners = [t for t in closed if t["is_winner"]]
        losers = [t for t in closed if t["is_loser"]]
        if not closed:
            print(f"\n{label}: No closed trades")
            return

        total_pnl = sum(t["pnl"] for t in closed)
        wr = len(winners) / len(closed) * 100
        avg_pnl = total_pnl / len(closed)
        avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

        print(f"\n{label} ({len(closed)} trades):")
        print(
            f"  Win Rate: {wr:.1f}% | Total PnL: ${total_pnl:+.2f} | "
            f"Avg PnL: ${avg_pnl:+.2f} | Avg Win: ${avg_win:+.2f} | Avg Loss: ${avg_loss:+.2f}"
        )

        # Exit reasons
        cats = Counter(t["exit_label"] for t in closed)
        print("  Exit reasons:")
        for cat, cnt in cats.most_common():
            cat_pnl = sum(t["pnl"] for t in closed if t["exit_label"] == cat)
            cat_w = sum(1 for t in closed if t["exit_label"] == cat and t["is_winner"])
            print(f"    {cat}: {cnt} | PnL: ${cat_pnl:+.2f} | W: {cat_w}/{cnt}")

        # PnL distribution
        pnls = sorted(t["pnl"] for t in closed)
        print(f"  PnL range: ${pnls[0]:+.2f} to ${pnls[-1]:+.2f}")
        print(f"  Median PnL: ${pnls[len(pnls)//2]:+.2f}")

        # Per-ticket listing
        print("\n  Per-ticket detail:")
        for t in sorted(closed, key=lambda x: x.get("pnl", 0) or 0):
            ct = t.get("close_time", "?")[:16] if t.get("close_time") else "?"
            print(
                f"    #{t['ticket']} | close={ct} | PnL=${t['pnl']:+.2f} | "
                f"label={t['exit_label']} | side={t['side']} | mods={t['mod_count']}"
            )

        return closed

    closed_bc = report_trades(trades_before_close, "[BEFORE cutoff — closed before Jun 9 15:00]")
    closed_ac = report_trades(trades_after_close, "[AFTER cutoff — closed after Jun 9 15:00]")

    # ── SECTION B: modify_sltp SL migration analysis ──────────────────────
    print("\n\n" + "=" * 70)
    print("  SECTION B: modify_sltp — SL MIGRATION ANALYSIS")
    print("  (how did trail change the SL level before vs after?)")
    print("=" * 70)

    # Get all modify_sltp records grouped by ticket
    modify_records = [r for r in journal if r.get("action") == "modify_sltp"]
    modify_by_ticket: dict[int, list[dict]] = defaultdict(list)
    for r in modify_records:
        ticket = r.get("position_ticket")
        if ticket:
            modify_by_ticket[ticket].append(r)

    print(f"\nTotal modify_sltp records: {len(modify_records)}")
    print(f"Unique tickets with SL modifications: {len(modify_by_ticket)}")

    # For each ticket, analyze SL progression
    sl_migrations_before = []
    sl_migrations_after = []

    for ticket, mods in modify_by_ticket.items():
        mods_sorted = sorted(mods, key=lambda r: r.get("recorded_at", "z"))
        sl_values = [(m.get("recorded_at", "?"), m.get("sl")) for m in mods_sorted]
        sl_numeric = [(ts, sl) for ts, sl in sl_values if sl is not None and sl > 0]

        if len(sl_numeric) < 2:
            continue

        first_sl = sl_numeric[0][1]
        last_sl = sl_numeric[-1][1]
        sl_delta = last_sl - first_sl
        sl_pct_move = (sl_delta / first_sl * 100) if first_sl else 0

        # Classify as before/after based on first modify time
        is_after = is_after_cutoff(sl_numeric[0][0])

        # How many times did SL move up (tightening for long)?
        advances = sum(
            1 for i in range(1, len(sl_numeric)) if sl_numeric[i][1] > sl_numeric[i - 1][1] + 0.1
        )

        migration = {
            "ticket": ticket,
            "first_ts": sl_numeric[0][0],
            "num_modifies": len(mods),
            "first_sl": first_sl,
            "last_sl": last_sl,
            "sl_delta": sl_delta,
            "sl_pct_move": sl_pct_move,
            "advances": advances,
        }

        if is_after:
            sl_migrations_after.append(migration)
        else:
            sl_migrations_before.append(migration)

    def report_sl_migration(migrations, label):
        if not migrations:
            print(f"\n{label}: No SL migrations")
            return
        print(f"\n{label} ({len(migrations)} tickets):")
        deltas = [m["sl_delta"] for m in migrations]
        pcts = [m["sl_pct_move"] for m in migrations]
        advances = [m["advances"] for m in migrations]
        mods_count = [m["num_modifies"] for m in migrations]

        print(
            f"  SL delta: min={min(deltas):.1f}, max={max(deltas):.1f}, "
            f"avg={sum(deltas)/len(deltas):.1f}"
        )
        print(
            f"  SL % move: min={min(pcts):.2f}%, max={max(pcts):.2f}%, "
            f"avg={sum(pcts)/len(pcts):.2f}%"
        )
        print(
            f"  Advances per ticket: min={min(advances)}, max={max(advances)}, "
            f"avg={sum(advances)/len(advances):.1f}"
        )
        print(f"  Modifies per ticket: avg={sum(mods_count)/len(mods_count):.1f}")

        # Show tickets sorted by SL delta (largest tightening first)
        print("\n  Per-ticket details (sorted by SL delta):")
        for m in sorted(migrations, key=lambda x: x["sl_delta"], reverse=True):
            print(
                f"    #{m['ticket']} | {m['first_ts'][:16]} | "
                f"SL: {m['first_sl']:.1f}→{m['last_sl']:.1f} "
                f"(Δ{m['sl_delta']:+.1f}, {m['sl_pct_move']:+.2f}%) | "
                f"advances={m['advances']} | mods={m['num_modifies']}"
            )

    report_sl_migration(sl_migrations_before, "[BEFORE] SL migrations")
    report_sl_migration(sl_migrations_after, "[AFTER] SL migrations")

    # ── SECTION C: active position snapshot deep dive ─────────────────────
    print("\n\n" + "=" * 70)
    print("  SECTION C: SNAPSHOT TRAIL PROGRESSION — ACTIVE POSITIONS")
    print("  (positions with snapshots on or after Jun 9 15:00)")
    print("=" * 70)

    # Find positions that have snapshots spanning the cutoff
    snap_by_ticket: dict[int, list[dict]] = defaultdict(list)
    for s in snapshots:
        ticket = s.get("ticket")
        if ticket:
            snap_by_ticket[ticket].append(s)

    # Positions active after cutoff
    active_after = []
    for ticket, snaps in snap_by_ticket.items():
        snaps_sorted = sorted(snaps, key=lambda s: s.get("time", "z"))
        # Check if any snapshot is after cutoff
        has_after = any(is_after_cutoff(s.get("time")) for s in snaps_sorted)
        if not has_after:
            continue

        before_snaps = [s for s in snaps_sorted if is_before_cutoff(s.get("time"))]
        after_snaps = [s for s in snaps_sorted if is_after_cutoff(s.get("time"))]

        def snap_stats(snaps_list):
            if not snaps_list:
                return None
            dists = [s.get("trailing_sl_distance", 0) or 0 for s in snaps_list]
            rs = [s.get("unrealized_pnl_r", 0) or 0 for s in snaps_list]
            atrs = [s.get("current_atr", 0) or 0 for s in snaps_list]
            # Trail tightness: trail_dist / current_atr
            tightness = [d / a for d, a in zip(dists, atrs, strict=False) if a > 0 and d > 0]
            return {
                "count": len(snaps_list),
                "avg_dist": sum(dists) / len(dists) if dists else 0,
                "avg_r": sum(rs) / len(rs) if rs else 0,
                "avg_tightness": sum(tightness) / len(tightness) if tightness else 0,
                "max_r": max(rs) if rs else 0,
            }

        bs = snap_stats(before_snaps)
        as_ = snap_stats(after_snaps)

        active_after.append(
            {
                "ticket": ticket,
                "before": bs,
                "after": as_,
                "total_snaps": len(snaps_sorted),
            }
        )

    if active_after:
        print(f"\nPositions active across cutoff: {len(active_after)}")
        for a in sorted(active_after, key=lambda x: -(x["total_snaps"])):
            print(f"\n  Ticket #{a['ticket']} ({a['total_snaps']} snapshots):")
            if a["before"]:
                b = a["before"]
                print(
                    f"    BEFORE: snaps={b['count']}, avg_dist={b['avg_dist']:.1f}, "
                    f"avg_R={b['avg_r']:.2f}, max_R={b['max_r']:.2f}, "
                    f"tightness={b['avg_tightness']:.2f}"
                )
            if a["after"]:
                af = a["after"]
                print(
                    f"    AFTER:  snaps={af['count']}, avg_dist={af['avg_dist']:.1f}, "
                    f"avg_R={af['avg_r']:.2f}, max_R={af['max_r']:.2f}, "
                    f"tightness={af['avg_tightness']:.2f}"
                )
            # Compare tightness delta
            if a["before"] and a["after"]:
                t_delta = a["after"]["avg_tightness"] - a["before"]["avg_tightness"]
                r_delta = a["after"]["max_r"] - a["before"]["max_r"]
                print(f"    Δ: tightness {t_delta:+.2f}, max_R {r_delta:+.2f}")
                if t_delta < -0.3:
                    print("    ⚠ Tightness DECREASED significantly (trail got looser)")
                elif t_delta > 0.3:
                    print("    ⚠ Tightness INCREASED significantly (trail got tighter)")
    else:
        print("\nNo positions active across the cutoff")

    # ── SECTION D: SUMMARY ──────────────────────────────────────────────
    print("\n\n" + "=" * 70)
    print("  SECTION D: KEY OBSERVATIONS")
    print("=" * 70)

    # Calculate before vs after close stats
    if closed_bc and closed_ac:
        wr_b = sum(1 for t in closed_bc if t["is_winner"]) / len(closed_bc) * 100
        wr_a = sum(1 for t in closed_ac if t["is_winner"]) / len(closed_ac) * 100
        pnl_b = sum(t["pnl"] for t in closed_bc)
        pnl_a = sum(t["pnl"] for t in closed_ac)

        print(f"\n1. Win Rate (by close date): {wr_b:.1f}% → {wr_a:.1f}% (Δ {wr_a-wr_b:+.1f}pp)")
        print(
            f"2. Total PnL (by close date): ${pnl_b:+.2f} → ${pnl_a:+.2f} (Δ ${pnl_a-pnl_b:+.2f})"
        )

        # Avg mod_count
        mod_b_avg = sum(t["mod_count"] for t in closed_bc) / len(closed_bc)
        mod_a_avg = sum(t["mod_count"] for t in closed_ac) / len(closed_ac)
        print(f"3. Avg modify_sltp per trade: {mod_b_avg:.1f} → {mod_a_avg:.1f}")

        # Exit reason shifts
        print("\n4. Exit reason shift:")
        cats_b = Counter(t["exit_label"] for t in closed_bc)
        cats_a = Counter(t["exit_label"] for t in closed_ac)
        all_cats = set(list(cats_b.keys()) + list(cats_a.keys()))
        for cat in sorted(all_cats):
            cb = cats_b.get(cat, 0)
            ca = cats_a.get(cat, 0)
            if cb != ca:
                print(f"     {cat}: {cb} → {ca}")

    # Key observation: "trail" exit label never appears
    all_labels = set()
    for t in (closed_bc or []) + (closed_ac or []):
        if t["exit_label"]:
            all_labels.add(t["exit_label"])
    if "trail" not in all_labels:
        print("\n⚠ CRITICAL OBSERVATION: 'trail' exit label has NEVER been recorded.")
        print("   The trail mechanism modifies SL but the actual exit is always")
        print("   classified as 'sl_hit_first', 'loss', or other labels.")
        print("   This means changes to trail behavior can only be observed")
        print("   indirectly through SL hit rates and PnL distributions.")

    # Modifies before/after
    if sl_migrations_before and sl_migrations_after:
        avg_delta_b = sum(m["sl_delta"] for m in sl_migrations_before) / len(sl_migrations_before)
        avg_delta_a = sum(m["sl_delta"] for m in sl_migrations_after) / len(sl_migrations_after)
        print(
            f"\n5. Avg SL migration: {avg_delta_b:.1f} → {avg_delta_a:.1f} pts (more positive = tighter trail)"
        )
        avg_adv_b = sum(m["advances"] for m in sl_migrations_before) / len(sl_migrations_before)
        avg_adv_a = sum(m["advances"] for m in sl_migrations_after) / len(sl_migrations_after)
        print(f"6. Avg trail advances: {avg_adv_b:.1f} → {avg_adv_a:.1f}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()

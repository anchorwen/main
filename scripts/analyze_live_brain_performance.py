#!/usr/bin/env python
"""
Comprehensive Live/Shadow Brain Performance Audit Script
=========================================================
Iron Law #11 compliant — all statistics below are computed from structured data,
not from reading text snippets.

口径 (Definitions):
  - Dedup: one trade = one position_ticket, identified by the OPEN action.
            Close events matched by position_ticket. If multiple close events exist,
            the last one with pnl != null wins.
  - Win rate: trades with pnl > 0 / (total_trades - breakeven_trades).
    Breakeven = pnl == 0 or label == 'breakeven'.
  - PnL: always in R-units (pnl_r), sourced from close events' pnl field.
  - Direction: sourced from open event's side field (long/short).
  - Brain attribution: from the open event's brain_ids list.
  - Exit reason taxonomy:
      tp_hit        — tp_hit or tp_hit_first
      sl_hit        — sl_hit, sl_hit_first, sl_hit_trailed
      watchdog      — exit_watchdog:*
      mia_close     — mia_close or mt5_deal_reason_3
      unknown_close — unknown_close, client_close, position_not_found
      other         — everything else

Usage:
  python scripts/analyze_live_brain_performance.py --data-dir data_btc
  python scripts/analyze_live_brain_performance.py --data-dir data_btc --brain-id BTC_Swing_V1
  python scripts/analyze_live_brain_performance.py --data-dir data_btc --full-dump
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict]:
    """Load JSONL file, skipping empty/malformed lines."""
    records: list[dict] = []
    if not path.exists():
        print(f"[WARN] File not found: {path}")
        return records
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[WARN] JSON decode error at {path}:{i}")
    return records


def classify_exit(label: str | None, detail: dict | None) -> str:
    """Classify exit reason into taxonomy buckets."""
    if label in ("tp_hit_first", "tp_hit", "win"):
        return "tp_hit"
    if label in ("sl_hit_first", "sl_hit_trailed", "sl_hit", "loss"):
        return "sl_hit"
    if label and label.startswith("exit_watchdog:"):
        return "watchdog"
    if label == "breakeven":
        return "breakeven"
    # Check detail reason
    reason = ""
    if isinstance(detail, dict):
        reason = detail.get("reason", "")
    if reason in ("tp_hit",):
        return "tp_hit"
    if reason in ("sl_hit",):
        return "sl_hit"
    if reason in ("mia_close", "mt5_deal_reason_3"):
        return "mia_close"
    if reason in (
        "unknown_close",
        "client_close",
        "position_not_found",
        "auto_orphan_rejected",
        "auto_orphan_stale",
    ):
        return "unknown_close"
    # fallback: check label
    if label in ("win",):
        return "tp_hit"
    if label in ("loss",):
        return "sl_hit"
    return "other"


def compute_drawdown(pnl_series: list[float]) -> float:
    """Compute max drawdown from cumulative PnL."""
    if not pnl_series:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        cum += pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def compute_sharpe(pnl_series: list[float]) -> float:
    """Compute Sharpe-like ratio: mean(pnl) / std(pnl) * sqrt(N)."""
    if len(pnl_series) < 2:
        return 0.0
    import math

    mean_pnl = sum(pnl_series) / len(pnl_series)
    variance = sum((x - mean_pnl) ** 2 for x in pnl_series) / (len(pnl_series) - 1)
    if variance <= 0:
        return 0.0
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (mean_pnl / std) * math.sqrt(len(pnl_series))


def analyze_trade_journal(data_dir: Path):
    """Main analysis: reconstruct trades from live_trade_journal.jsonl."""
    journal = load_jsonl(data_dir / "live_trade_journal.jsonl")
    snapshots = load_jsonl(data_dir / "position_snapshots.jsonl")

    if not journal:
        print("[ERROR] No journal entries found. Aborting.")
        return

    # --- Phase 1: Build trade map ---
    # Key: position_ticket -> {open_event, close_events[], modify_events[]}
    trades: dict[int, dict] = defaultdict(lambda: {"open": None, "closes": [], "modifies": []})

    for rec in journal:
        ticket = rec.get("position_ticket")
        if ticket is None:
            continue
        action = rec.get("action", "")
        if action == "open":
            trades[ticket]["open"] = rec
        elif action == "close":
            trades[ticket]["closes"].append(rec)
        elif action == "modify_sltp":
            trades[ticket]["modifies"].append(rec)

    # --- Phase 2: Resolve each trade ---
    resolved = []
    for ticket, tdata in trades.items():
        open_rec = tdata["open"]
        if open_rec is None:
            # Orphan close (no matching open) — skip
            continue

        # Find the definitive close event (prefer one with non-null pnl)
        close_rec = None
        for c in tdata["closes"]:
            pnl = c.get("pnl")
            if pnl is not None:
                close_rec = c
                break
        if close_rec is None and tdata["closes"]:
            close_rec = tdata["closes"][-1]

        resolved.append(
            {
                "ticket": ticket,
                "open": open_rec,
                "close": close_rec,
                "modify_count": len(tdata["modifies"]),
            }
        )

    # --- Phase 3: Compute statistics ---
    total_trades = len(resolved)
    closed_trades = [t for t in resolved if t["close"] is not None]
    open_positions = [t for t in resolved if t["close"] is None]

    # Track per-brain stats
    brain_stats: dict[str, dict] = defaultdict(
        lambda: {
            "trades": [],
            "pnls": [],
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "long_trades": 0,
            "short_trades": 0,
            "long_wins": 0,
            "short_wins": 0,
            "exit_reasons": defaultdict(int),
            "strategies": defaultdict(int),
            "total_pnl_r": 0.0,
            "total_modifies": 0,
        }
    )

    # Track per-strategy stats
    strategy_stats: dict[str, dict] = defaultdict(
        lambda: {
            "trades": [],
            "pnls": [],
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "total_pnl_r": 0.0,
        }
    )

    # Track exit reason stats
    exit_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl_r_total": 0.0, "pnls": []})

    # Track time-series for cumulative PnL
    all_pnls = []
    date_pnls: dict[str, list[float]] = defaultdict(list)

    # Track watchdog exit details
    watchdog_exits: list[dict] = []

    for t in closed_trades:
        open_rec = t["open"]
        close_rec = t["close"]

        pnl_r = close_rec.get("pnl") or 0.0
        label = close_rec.get("label", "")
        side = open_rec.get("side", "unknown")
        strategy = open_rec.get("strategy", "unknown")
        brain_ids = open_rec.get("brain_ids") or ["unknown"]
        detail = close_rec.get("detail") or {}
        close_reason = detail.get("reason", "") if isinstance(detail, dict) else ""

        exit_cat = classify_exit(label, detail)
        recorded_at = close_rec.get("recorded_at", "")
        date_str = recorded_at[:10] if recorded_at else "unknown"

        # Track specific watchdog reasons
        if exit_cat == "watchdog":
            watchdog_exits.append(
                {
                    "ticket": t["ticket"],
                    "label": label,
                    "pnl_r": pnl_r,
                    "side": side,
                    "strategy": strategy,
                    "brain_ids": brain_ids,
                    "date": date_str,
                    "close_reason": close_reason,
                }
            )

        all_pnls.append(pnl_r)
        date_pnls[date_str].append(pnl_r)

        exit_stats[exit_cat]["count"] += 1
        exit_stats[exit_cat]["pnl_r_total"] += pnl_r
        exit_stats[exit_cat]["pnls"].append(pnl_r)

        for brain_id in brain_ids:
            bs = brain_stats[brain_id]
            bs["trades"].append(t)
            bs["pnls"].append(pnl_r)
            bs["total_pnl_r"] += pnl_r
            bs["total_modifies"] += t["modify_count"]
            bs["exit_reasons"][exit_cat] += 1
            bs["strategies"][strategy] += 1
            if side == "long":
                bs["long_trades"] += 1
                if pnl_r > 0:
                    bs["long_wins"] += 1
            elif side == "short":
                bs["short_trades"] += 1
                if pnl_r > 0:
                    bs["short_wins"] += 1
            if pnl_r > 0:
                bs["wins"] += 1
            elif pnl_r < 0:
                bs["losses"] += 1
            else:
                bs["breakeven"] += 1

        # Per-strategy
        ss = strategy_stats[strategy]
        ss["trades"].append(t)
        ss["pnls"].append(pnl_r)
        ss["total_pnl_r"] += pnl_r
        if pnl_r > 0:
            ss["wins"] += 1
        elif pnl_r < 0:
            ss["losses"] += 1
        else:
            ss["breakeven"] += 1

    # --- Phase 4: Shadow vs Live classification ---
    # Read governance state to get live/shadow classification
    gov_state = {}
    gov_path = data_dir / "governance_state.json"
    if gov_path.exists():
        with open(gov_path) as f:
            gov_state = json.load(f)

    brain_states = gov_state.get("brain_states", {})
    # Read leaderboard
    leaderboard = {}
    lb_path = data_dir / "reports" / "leaderboard.json"
    if lb_path.exists():
        with open(lb_path) as f:
            leaderboard = json.load(f)

    # --- Phase 5: Read brain_performance.json for tracker-level data ---
    bp_data = {}
    bp_path = data_dir / "brain_performance.json"
    if bp_path.exists():
        with open(bp_path) as f:
            bp_data = json.load(f)

    # --- Phase 6: Position Snapshot Analysis ---
    # Get trail behavior statistics
    snapshot_by_ticket: dict[int, list[dict]] = defaultdict(list)
    for s in snapshots:
        ticket = s.get("ticket")
        if ticket is not None:
            snapshot_by_ticket[ticket].append(s)

    # --- Report ---
    print("=" * 80)
    print("  BTC LIVE/SHADOW BRAIN PERFORMANCE AUDIT REPORT")
    print(f"  Generated: {datetime.now().isoformat()}")
    print(f"  Data Dir: {data_dir}")
    print("=" * 80)

    # A. Volume
    print("\n" + "─" * 80)
    print("  A. TRADE VOLUME OVERVIEW")
    print("─" * 80)
    print(f"  Total journal entries:          {len(journal):>6}")
    print(f"  Total position snapshots:       {len(snapshots):>6}")
    print(f"  Unique position tickets:        {len(trades):>6}")
    print(f"  Closed trades (with PnL):       {len(closed_trades):>6}")
    print(f"  Open positions (no close yet):  {len(open_positions):>6}")
    print(
        f"  Date range: {date_pnls and min(date_pnls.keys())} → {date_pnls and max(date_pnls.keys())}"
    )
    if open_positions:
        print(f"  Open tickets: {[t['ticket'] for t in open_positions]}")

    # B. Aggregate Performance
    print("\n" + "─" * 80)
    print("  B. AGGREGATE PERFORMANCE (All Closed Trades)")
    print("─" * 80)

    total_pnl = sum(all_pnls)
    wins = sum(1 for p in all_pnls if p > 0)
    losses = sum(1 for p in all_pnls if p < 0)
    breakevens = sum(1 for p in all_pnls if p == 0)
    n = len(all_pnls)
    wr = wins / max(n - breakevens, 1)
    avg_win = sum(p for p in all_pnls if p > 0) / max(wins, 1)
    avg_loss = sum(p for p in all_pnls if p < 0) / max(losses, 1)
    total_win_pnl = sum(p for p in all_pnls if p > 0)
    total_loss_pnl = abs(sum(p for p in all_pnls if p < 0))
    pf = total_win_pnl / max(total_loss_pnl, 0.01)
    max_dd = compute_drawdown(all_pnls)
    sharpe = compute_sharpe(all_pnls)

    # Direction breakdown
    long_pnls = []
    short_pnls = []
    for t in closed_trades:
        side = t["open"].get("side", "")
        pnl = t["close"].get("pnl") or 0.0
        if side == "long":
            long_pnls.append(pnl)
        elif side == "short":
            short_pnls.append(pnl)

    print(f"  Total closed trades:  {n}")
    print(f"  Wins:                 {wins} ({wins/max(n,1)*100:.1f}%)")
    print(f"  Losses:               {losses} ({losses/max(n,1)*100:.1f}%)")
    print(f"  Breakeven:            {breakevens} ({breakevens/max(n,1)*100:.1f}%)")
    print(f"  Win Rate (excl. BE):  {wr*100:.1f}%")
    print(f"  Total PnL (R):        {total_pnl:+.1f}R")
    print(f"  Avg Win:              {avg_win:+.1f}R")
    print(f"  Avg Loss:             {avg_loss:+.1f}R")
    print(f"  Avg Trade:            {total_pnl/max(n,1):+.1f}R")
    print(f"  Profit Factor:        {pf:.2f}")
    print(f"  Max Drawdown (R):     {max_dd:.1f}R")
    print(f"  Sharpe (approx):      {sharpe:+.2f}")
    print(f"  Risk/Reward Ratio:    {abs(avg_win/max(avg_loss, -0.01)):.2f}")

    print(
        f"\n  Long trades:  {len(long_pnls)}, PnL={sum(long_pnls):+.1f}R, WR={sum(1 for p in long_pnls if p>0)/max(len(long_pnls),1)*100:.1f}%"
    )
    print(
        f"  Short trades: {len(short_pnls)}, PnL={sum(short_pnls):+.1f}R, WR={sum(1 for p in short_pnls if p>0)/max(len(short_pnls),1)*100:.1f}%"
    )

    # C. Exit Reason Analysis
    print("\n" + "─" * 80)
    print("  C. EXIT REASON BREAKDOWN")
    print("─" * 80)
    exit_order = [
        "tp_hit",
        "sl_hit",
        "watchdog",
        "mia_close",
        "unknown_close",
        "breakeven",
        "other",
    ]
    print(
        f"  {'Exit Reason':<20} {'Count':>6} {'Share':>7} {'PnL(R)':>10} {'Avg PnL':>9} {'Win%':>7}"
    )
    print(f"  {'-'*20} {'-'*6} {'-'*7} {'-'*10} {'-'*9} {'-'*7}")
    for ecat in exit_order:
        es = exit_stats.get(ecat)
        if es and es["count"] > 0:
            pnls = es["pnls"]
            ec_wins = sum(1 for p in pnls if p > 0)
            ec_wr = ec_wins / max(len(pnls), 1)
            print(
                f"  {ecat:<20} {es['count']:>6} {es['count']/max(n,1)*100:>6.1f}% {es['pnl_r_total']:>+10.1f} {es['pnl_r_total']/es['count']:>+9.1f} {ec_wr*100:>6.1f}%"
            )

    # D. Watchdog detail
    if watchdog_exits:
        print("\n" + "─" * 80)
        print("  D. WATCHDOG EXIT DETAIL")
        print("─" * 80)
        wd_by_label: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "pnl_r_total": 0.0, "pnls": []}
        )
        for w in watchdog_exits:
            lbl = w["label"]
            wd_by_label[lbl]["count"] += 1
            wd_by_label[lbl]["pnl_r_total"] += w["pnl_r"]
            wd_by_label[lbl]["pnls"].append(w["pnl_r"])
        print(f"  {'Watchdog Label':<50} {'Count':>5} {'PnL(R)':>9} {'Avg PnL':>8}")
        print(f"  {'-'*50} {'-'*5} {'-'*9} {'-'*8}")
        for lbl in sorted(wd_by_label.keys()):
            wd = wd_by_label[lbl]
            print(
                f"  {lbl:<50} {wd['count']:>5} {wd['pnl_r_total']:>+9.1f} {wd['pnl_r_total']/wd['count']:>+8.1f}"
            )

    # E. Per-Brain Performance
    print("\n" + "─" * 80)
    print("  E. PER-BRAIN PERFORMANCE (sorted by Total PnL R)")
    print("─" * 80)

    brain_list = []
    for bid, bs in brain_stats.items():
        pnls = bs["pnls"]
        n_b = len(pnls)
        wins_b = bs["wins"]
        losses_b = bs["losses"]
        be_b = bs["breakeven"]
        wr_b = wins_b / max(n_b - be_b, 1)
        total_b = bs["total_pnl_r"]
        dd_b = compute_drawdown(pnls)
        sharpe_b = compute_sharpe(pnls)
        avg_w = sum(p for p in pnls if p > 0) / max(wins_b, 1)
        avg_l = sum(p for p in pnls if p < 0) / max(losses_b, 1)
        # Get status from governance or leaderboard
        status = "unknown"
        if bid in brain_states:
            status = brain_states[bid].get("status", "unknown")
        # From leaderboard
        lb_brains = leaderboard.get("brains", [])
        for lb in lb_brains:
            if lb["brain_id"] == bid:
                status = lb.get("status", status)
                break
        brain_list.append(
            {
                "brain_id": bid,
                "status": status,
                "trades": n_b,
                "wins": wins_b,
                "losses": losses_b,
                "be": be_b,
                "wr": wr_b,
                "total_pnl_r": total_b,
                "avg_win": avg_w,
                "avg_loss": avg_l,
                "max_dd": dd_b,
                "sharpe": sharpe_b,
                "long_trades": bs["long_trades"],
                "short_trades": bs["short_trades"],
                "long_wins": bs["long_wins"],
                "short_wins": bs["short_wins"],
                "modifies": bs["total_modifies"],
                "exit_reasons": dict(bs["exit_reasons"]),
                "strategies": dict(bs["strategies"]),
            }
        )

    brain_list.sort(key=lambda x: x["total_pnl_r"], reverse=True)

    print(
        f"  {'Brain ID':<35} {'Status':<10} {'Trades':>6} {'Win%':>6} {'PnL(R)':>9} {'AvgW':>7} {'AvgL':>7} {'MaxDD':>7} {'Sharpe':>7}"
    )
    print(f"  {'-'*35} {'-'*10} {'-'*6} {'-'*6} {'-'*9} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
    for b in brain_list:
        print(
            f"  {b['brain_id']:<35} {b['status']:<10} {b['trades']:>6} {b['wr']*100:>5.1f}% {b['total_pnl_r']:>+9.1f} {b['avg_win']:>+7.1f} {b['avg_loss']:>+7.1f} {b['max_dd']:>7.1f} {b['sharpe']:>+7.2f}"
        )

    # F. Per-Strategy Performance
    print("\n" + "─" * 80)
    print("  F. PER-STRATEGY PERFORMANCE")
    print("─" * 80)
    strat_list = []
    for sid, ss in strategy_stats.items():
        pnls = ss["pnls"]
        n_s = len(pnls)
        wr_s = ss["wins"] / max(n_s - ss["breakeven"], 1)
        strat_list.append(
            {
                "strategy": sid,
                "trades": n_s,
                "wins": ss["wins"],
                "losses": ss["losses"],
                "be": ss["breakeven"],
                "wr": wr_s,
                "total_pnl_r": ss["total_pnl_r"],
            }
        )
    strat_list.sort(key=lambda x: x["total_pnl_r"], reverse=True)
    print(f"  {'Strategy':<20} {'Trades':>6} {'Win%':>6} {'PnL(R)':>9} {'Avg Trade':>9}")
    print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*9} {'-'*9}")
    for s in strat_list:
        avg_tr = s["total_pnl_r"] / max(s["trades"], 1)
        print(
            f"  {s['strategy']:<20} {s['trades']:>6} {s['wr']*100:>5.1f}% {s['total_pnl_r']:>+9.1f} {avg_tr:>+9.1f}"
        )

    # G. Daily PnL
    print("\n" + "─" * 80)
    print("  G. DAILY PnL BREAKDOWN")
    print("─" * 80)
    daily = []
    for d, pnls in sorted(date_pnls.items()):
        daily.append((d, len(pnls), sum(pnls)))
    print(f"  {'Date':<12} {'Trades':>7} {'PnL(R)':>10} {'Cum PnL':>10}")
    print(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*10}")
    cum = 0.0
    for d, cnt, pnl in daily:
        cum += pnl
        print(f"  {d:<12} {cnt:>7} {pnl:>+10.1f} {cum:>+10.1f}")

    # H. Brain Direction Bias
    print("\n" + "─" * 80)
    print("  H. BRAIN DIRECTION BIAS (Long vs Short)")
    print("─" * 80)
    print(
        f"  {'Brain ID':<35} {'Long T':>7} {'Long WR':>7} {'Short T':>7} {'Short WR':>7} {'Bias':>8}"
    )
    print(f"  {'-'*35} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*8}")
    for b in brain_list:
        lt = b["long_trades"]
        st = b["short_trades"]
        lwr = b["long_wins"] / max(lt, 1) * 100
        swr = b["short_wins"] / max(st, 1) * 100
        total_dir = lt + st
        if total_dir > 0:
            bias = f"{lt/total_dir*100:.0f}% long" if lt >= st else f"{st/total_dir*100:.0f}% short"
        else:
            bias = "N/A"
        if lt + st > 0:
            print(f"  {b['brain_id']:<35} {lt:>7} {lwr:>6.1f}% {st:>7} {swr:>6.1f}% {bias:>8}")

    # I. Trailing SL Statistics
    print("\n" + "─" * 80)
    print("  I. TRAILING SL BEHAVIOR (from position_snapshots)")
    print("─" * 80)
    trail_counts: defaultdict[str, int] = defaultdict(int)
    trail_pnls: dict[str, list[float]] = defaultdict(list)
    for t in closed_trades:
        ticket = t["ticket"]
        sns = snapshot_by_ticket.get(ticket, [])
        trail_count = len(sns)
        if trail_count <= 1:
            trail_counts["0-1 (no trail)"] += 1
        elif trail_count <= 3:
            trail_counts["2-3"] += 1
        elif trail_count <= 6:
            trail_counts["4-6"] += 1
        elif trail_count <= 10:
            trail_counts["7-10"] += 1
        else:
            trail_counts["11+"] += 1
        pnl = t["close"].get("pnl") or 0.0
        bucket = (
            "0-1"
            if trail_count <= 1
            else "2-3"
            if trail_count <= 3
            else "4-6"
            if trail_count <= 6
            else "7-10"
            if trail_count <= 10
            else "11+"
        )
        trail_pnls[bucket].append(pnl)
    print(f"  {'Snapshot Bucket':<20} {'Count':>6} {'Avg PnL(R)':>11} {'Win%':>7}")
    print(f"  {'-'*20} {'-'*6} {'-'*11} {'-'*7}")
    for bucket in ["0-1 (no trail)", "2-3", "4-6", "7-10", "11+"]:
        cnt = trail_counts[bucket]
        pnls_b = trail_pnls.get(bucket, [])
        avg_p = sum(pnls_b) / max(len(pnls_b), 1)
        wr_b = sum(1 for p in pnls_b if p > 0) / max(len(pnls_b), 1) * 100
        print(f"  {bucket:<20} {cnt:>6} {avg_p:>+11.1f} {wr_b:>6.1f}%")

    # J. Brain Governance Status Summary
    print("\n" + "─" * 80)
    print("  J. GOVERNANCE & LEADERBOARD CROSS-REFERENCE")
    print("─" * 80)
    lb_brains = leaderboard.get("brains", [])
    print(f"  Leaderboard brains: {len(lb_brains)}")
    print(f"  Governance brain_states: {len(brain_states)}")
    print(f"  Brains with actual trades in journal: {len(brain_list)}")

    # Find brains that exist but have no trades
    all_registered = set()
    for lb in lb_brains:
        all_registered.add(lb["brain_id"])
    for bid in brain_states:
        all_registered.add(bid)
    traded_brains = {b["brain_id"] for b in brain_list}
    no_trades = all_registered - traded_brains
    if no_trades:
        print(f"\n  Brains with NO trades (registered but idle):")
        for bid in sorted(no_trades):
            status = "unknown"
            if bid in brain_states:
                status = brain_states[bid].get("status", "unknown")
            for lb in lb_brains:
                if lb["brain_id"] == bid:
                    status = lb.get("status", status)
                    break
            print(f"    {bid}: status={status}")

    # K. Key Findings Summary
    print("\n" + "─" * 80)
    print("  K. KEY FINDINGS & RECOMMENDATIONS PREVIEW")
    print("─" * 80)

    # Identify worst brains
    losing_brains = [b for b in brain_list if b["total_pnl_r"] < 0 and b["trades"] >= 10]
    profitable_brains = [b for b in brain_list if b["total_pnl_r"] > 0 and b["trades"] >= 5]

    print(f"\n  Losing brains (≥10 trades, PnL<0): {len(losing_brains)}")
    for b in losing_brains:
        print(
            f"    {b['brain_id']}: {b['total_pnl_r']:+.1f}R, WR={b['wr']*100:.1f}%, {b['trades']} trades, status={b['status']}"
        )

    print(f"\n  Profitable brains (≥5 trades, PnL>0): {len(profitable_brains)}")
    for b in profitable_brains:
        print(
            f"    {b['brain_id']}: {b['total_pnl_r']:+.1f}R, WR={b['wr']*100:.1f}%, {b['trades']} trades, status={b['status']}"
        )

    # PnL concentration
    top_loss_brains = sorted(
        [b for b in brain_list if b["total_pnl_r"] < 0], key=lambda x: x["total_pnl_r"]
    )[:5]
    print(f"\n  Top 5 PnL destroyers:")
    for b in top_loss_brains:
        print(
            f"    {b['brain_id']}: {b['total_pnl_r']:+.1f}R, {b['trades']} trades, WR={b['wr']*100:.1f}%"
        )

    # Best exit channel
    for ecat in exit_order:
        es = exit_stats.get(ecat)
        if es and es["count"] > 0:
            if es["pnl_r_total"] > 0:
                print(
                    f"\n  Best exit channel: {ecat} — {es['pnl_r_total']:+.1f}R across {es['count']} trades"
                )
                break

    print("\n[DONE] All statistics above are the sole source of truth.")
    print("=" * 80)

    # --- Optional full dump ---
    return {
        "brain_list": brain_list,
        "strategy_list": strat_list,
        "exit_stats": exit_stats,
        "daily": daily,
        "total_pnl": total_pnl,
        "total_trades": n,
        "all_pnls": all_pnls,
        "closed_trades": closed_trades,
        "watchdog_exits": watchdog_exits,
    }


def main():
    parser = argparse.ArgumentParser(description="Comprehensive Live Brain Performance Audit")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    parser.add_argument("--brain-id", help="Filter to a specific brain ID")
    parser.add_argument("--full-dump", action="store_true", help="Output full trade-by-trade dump")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"[ERROR] Data directory not found: {data_dir}")
        sys.exit(1)

    result = analyze_trade_journal(data_dir)

    if args.full_dump and result:
        print("\n" + "=" * 80)
        print("  FULL TRADE-BY-TRADE DUMP")
        print("=" * 80)
        for t in result["closed_trades"]:
            open_rec = t["open"]
            close_rec = t["close"]
            ticket = t["ticket"]
            pnl = close_rec.get("pnl") or 0.0
            side = open_rec.get("side", "?")
            brains = open_rec.get("brain_ids") or ["?"]
            label = close_rec.get("label", "")
            detail = close_rec.get("detail") or {}
            reason = detail.get("reason", "") if isinstance(detail, dict) else ""
            entry_time = open_rec.get("recorded_at", "")[:19]
            close_time = close_rec.get("recorded_at", "")[:19]
            entry_price = open_rec.get("detail", {}).get("request", {}).get("price", "?")
            sl = open_rec.get("sl", "?")
            tp = open_rec.get("tp", "?")
            volume = open_rec.get("volume", "?")

            if args.brain_id and args.brain_id not in brains:
                continue

            print(f"  ticket={ticket} | {entry_time} → {close_time} | {side} | {volume} lot")
            print(f"    brains={brains} | entry={entry_price} | sl={sl} | tp={tp}")
            print(
                f"    pnl={pnl:+.1f}R | label={label} | reason={reason} | exit={classify_exit(label, detail)}"
            )
            print(f"    modifies={t['modify_count']}")
            print()


if __name__ == "__main__":
    main()

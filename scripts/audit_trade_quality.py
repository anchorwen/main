# type: ignore
#!/usr/bin/env python3
"""Full trade audit — BTC + XAU (Iron Law #11).

Script-based only.  No raw-log reading for conclusions.
Audits: trade quality, position integrity, exit quality, data gaps.

Usage:
  python scripts/audit_trade_quality.py [--data-dir-btc data_btc] [--data-dir-xau data]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def load_journal(data_dir: str) -> list[dict]:
    path = Path(data_dir) / "live_trade_journal.jsonl"
    if not path.exists():
        return []
    trades = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return trades


def load_snapshots(data_dir: str) -> list[dict]:
    path = Path(data_dir) / "position_snapshots.jsonl"
    if not path.exists():
        return []
    snaps = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snaps.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return snaps


def load_intent_events(data_dir: str) -> list[dict]:
    log_dir = Path(data_dir) / "logs"
    intent_logs = sorted(log_dir.glob("intent_*.log"))
    if not intent_logs:
        return []
    events = []
    # Read the latest log
    with open(intent_logs[-1], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def audit_symbol(name: str, data_dir: str, label: str) -> dict:
    print(f"\n{'='*70}")
    print(f"  {label} — Trade Quality Audit")
    print(f"{'='*70}")

    journal = load_journal(data_dir)
    snaps = load_snapshots(data_dir)
    events = load_intent_events(data_dir)

    # ── Q1: Data Integrity ──
    print("\n  ── Q1: Data Integrity ──")
    pnl_null = sum(1 for t in journal if t.get("pnl") is None and t.get("action") == "close")
    total_close = sum(1 for t in journal if t.get("action") == "close")
    missing_entry = sum(1 for t in journal if t.get("entry_price") is None)
    missing_exit = sum(1 for t in journal if t.get("exit_price") is None and t.get("action") == "close")

    print(f"    Total journal entries: {len(journal)}")
    print(f"    Close entries: {total_close}")
    print(f"    PnL null (close): {pnl_null}/{total_close} ({pnl_null/total_close*100:.1f}%)" if total_close else "    PnL null: N/A")
    print(f"    Missing entry_price: {missing_entry}")
    print(f"    Missing exit_price: {missing_exit}")

    # ── Q2: Trade Quality ──
    print("\n  ── Q2: Trade Quality (btc_swing / swing only) ──")
    swing_trades = [t for t in journal if "swing" in str(t.get("strategy", "")).lower()]
    if not swing_trades:
        swing_trades = [t for t in journal if t.get("action") == "close"]

    # Dedup by position_ticket
    seen_tickets = set()
    unique_trades = []
    for t in reversed(swing_trades):
        ticket = t.get("position_ticket") or t.get("ticket") or id(t)
        if ticket not in seen_tickets:
            seen_tickets.add(ticket)
            unique_trades.append(t)

    pnls = [t.get("pnl") for t in unique_trades if t.get("pnl") is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakevens = sum(1 for p in pnls if p == 0)
    total_pnl = sum(pnls)
    labels = Counter(str(t.get("label", "?")) for t in unique_trades)

    print(f"    Unique closed trades: {len(unique_trades)}")
    print(f"    Wins: {wins} | Losses: {losses} | Breakevens: {breakevens}")
    if pnls:
        print(f"    Win rate: {wins/len(pnls)*100:.1f}%")
    print(f"    Total PnL: ${total_pnl:.2f}")
    print(f"    Avg PnL per trade: ${total_pnl/len(pnls):.2f}" if pnls else "")
    print(f"    Labels: {dict(labels.most_common(8))}")

    # Exit reasons
    exit_reasons: dict[str, int] = Counter()
    for t in unique_trades:
        reason = str(t.get("exit_reason", t.get("label", "?")))
        exit_reasons[reason[:50]] += 1
    print(f"    Exit reasons (top 8): {dict(exit_reasons.most_common(8))}")

    # ── Q3: Recent trades (last 10) ──
    print("\n  ── Q3: Last 10 closed trades ──")
    sorted_trades = sorted(
        unique_trades,
        key=lambda t: str(t.get("close_time", "") or t.get("open_time", "")),
        reverse=True,
    )
    for t in sorted_trades[:10]:
        ticket = t.get("position_ticket", "?")
        label = str(t.get("label", "?"))[:25]
        pnl = t.get("pnl")
        pnl_str = f"${pnl:.2f}" if pnl is not None else "$NULL"
        entry = t.get("entry_price", "?")
        exit_p = t.get("exit_price", "?")
        close_t = str(t.get("close_time", "?"))[:19]
        exit_r = str(t.get("exit_reason", "?"))[:40]
        strategy = t.get("strategy", "?")
        print(f"    {strategy:20s} ticket={str(ticket):>15s} pnl={pnl_str:>8s} "
              f"label={label:25s} entry={str(entry):>10s} exit={str(exit_p):>10s} "
              f"close={close_t} reason={exit_r}")

    # ── Q4: Position integrity from snapshots ──
    print("\n  ── Q4: Position Snapshots ──")
    if snaps:
        # Group by ticket, get last snapshot each
        by_ticket: dict[int, dict] = {}
        for s in snaps:
            ticket = s.get("ticket", 0)
            if ticket and (ticket not in by_ticket or
               str(s.get("snapshot_time", "")) > str(by_ticket[ticket].get("snapshot_time", ""))):
                by_ticket[ticket] = s

        active = {t: s for t, s in by_ticket.items() if not s.get("close_time")}
        closed = {t: s for t, s in by_ticket.items() if s.get("close_time")}

        print(f"    Total snapshots: {len(snaps)}")
        print(f"    Unique tickets: {len(by_ticket)}")
        print(f"    Active positions (no close_time): {len(active)}")
        print(f"    Closed positions: {len(closed)}")

        if active:
            print("    Active:")
            for t, s in active.items():
                pnl = s.get("current_pnl", 0)
                entry = s.get("entry_price", 0)
                sl = s.get("sl", 0)
                tp = s.get("tp", 0)
                side = s.get("side", "?")
                open_t = str(s.get("open_time", "?"))[:19]
                print(f"      ticket={t} {side} entry={entry} sl={sl} tp={tp} "
                      f"pnl=${pnl:.2f} open={open_t}")
    else:
        print("    No snapshot data")

    # ── Q5: Cycle activity health ──
    print("\n  ── Q5: Cycle Activity ──")
    cycles = [e for e in events if e.get("event") == "cycle_end"]
    evals = [e for e in events if e.get("event") == "multi_strategy_eval"]
    dispatches = [e for e in events if e.get("event") == "intent_dispatched"]
    reentries = [e for e in events if e.get("event") == "reentry_blocked"]
    brain_alerts = [e for e in events if e.get("event") == "brain_alert"]
    consensus_blocked = [e for e in events if e.get("event") == "consensus_blocked_by_main_eval"]

    print(f"    Cycles: {len(cycles)}")
    print(f"    Eval events: {len(evals)}")
    print(f"    Dispatches: {len(dispatches)}")
    print(f"    Reentry blocks: {len(reentries)}")
    print(f"    Brain alerts: {len(brain_alerts)}")
    print(f"    Consensus blocked: {len(consensus_blocked)}")

    if evals:
        eval_reasons: dict[str, int] = Counter()
        for e in evals:
            for s in e.get("strategies", []):
                if not s.get("should_trade"):
                    eval_reasons[s.get("reason", "?")[:60]] += 1
        print("    Top rejection reasons:")
        for reason, count in eval_reasons.most_common(5):
            print(f"      [{count:3d}] {reason}")

    # ── Flags ──
    flags = []
    if pnl_null > total_close * 0.2:
        flags.append(f"PNL_NULL_{pnl_null}/{total_close}")
    if missing_entry > len(journal) * 0.1:
        flags.append("MISSING_ENTRY_PRICE")
    if len(cycles) == 0:
        flags.append("NO_CYCLES")
    if len(brain_alerts) > 10:
        flags.append(f"BRAIN_ALERTS_{len(brain_alerts)}")

    if flags:
        print(f"\n  🚩 FLAGS: {', '.join(flags)}")
    else:
        print("\n  ✅ No critical flags")

    return {
        "symbol": label,
        "total_journal": len(journal),
        "total_close": total_close,
        "pnl_null_rate": pnl_null / total_close if total_close else 0,
        "unique_trades": len(unique_trades),
        "win_rate": wins / len(pnls) if pnls else 0,
        "total_pnl": total_pnl,
        "cycles": len(cycles),
        "dispatches": len(dispatches),
        "brain_alerts": len(brain_alerts),
        "consensus_blocked": len(consensus_blocked),
        "flags": flags,
    }


def main() -> int:
    import io as _io

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # FIX-20260611-022
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir-btc", default="data_btc")
    parser.add_argument("--data-dir-xau", default="data")
    args = parser.parse_args()

    btc = audit_symbol("btc", args.data_dir_btc, "BTC")
    xau = audit_symbol("xau", args.data_dir_xau, "XAU")

    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Symbol':8s} {'Trades':>8s} {'WR':>8s} {'PnL':>10s} {'Alerts':>8s} {'Flags'}")
    for r in [btc, xau]:
        flags_str = ",".join(r["flags"]) if r["flags"] else "OK"
        print(f"  {r['symbol']:8s} {r['unique_trades']:>8d} "
              f"{r['win_rate']*100:>7.1f}% {r['total_pnl']:>9.2f} "
              f"{r['brain_alerts']:>8d} {flags_str}")

    all_ok = not btc["flags"] and not xau["flags"]
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Audit live brain performance from the ONLY authoritative source: live_trade_journal.jsonl.

Iron Law #11: Script stdout is the sole source of truth.  No manual counting, no
snippet-reading, no "I saw in the log" — every number in the conclusion MUST
appear in this script's output.

De-duplication: group by position_ticket, keep the LAST close entry per ticket.
Win definition: pnl > 0 = win, pnl < 0 = loss, pnl == 0 = breakeven.
PnL currency: raw USD PnL from journal (NOT R-units).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main(data_dir: str) -> int:
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print(f"FATAL: {journal_path} not found")
        return 1

    # ── Read all journal lines ──
    raw_entries: list[dict] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw_entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    print("=== Journal Raw Stats ===")
    print(f"Total raw lines: {len(raw_entries)}")

    # ── Separate opens and closes ──
    opens: dict[str, dict] = {}  # message_id -> open entry
    closes: list[dict] = []

    for e in raw_entries:
        ack = e.get("ack_status", "")
        if ack == "accepted":
            mid = e.get("message_id", "")
            if mid:
                opens[mid] = e
        elif ack == "closed":
            closes.append(e)

    print(f"Accepted (opens): {len(opens)}")
    print(f"Closed: {len(closes)}")

    # ── Dedup: per position_ticket, keep LAST close ──
    ticket_closes: dict[int, dict] = {}
    dupes = 0
    for c in closes:
        ticket = c.get("position_ticket")
        if ticket is None:
            continue
        ticket = int(ticket)
        if ticket in ticket_closes:
            dupes += 1
            # Keep the one with later recorded_at
            existing_ts = ticket_closes[ticket].get("recorded_at", "")
            new_ts = c.get("recorded_at", "")
            if new_ts > existing_ts:
                ticket_closes[ticket] = c
        else:
            ticket_closes[ticket] = c

    print(f"Deduped closes (by ticket): {len(ticket_closes)} (dupes removed: {dupes})")

    # ── JOIN close → open via open_message_id ──
    linked = 0
    unlinked = 0
    for ticket, close in ticket_closes.items():
        open_mid = close.get("open_message_id", "")
        if open_mid and open_mid in opens:
            linked += 1
        else:
            unlinked += 1

    print(f"Linked (close→open): {linked}")
    print(f"Unlinked (no open found): {unlinked}")

    # ── Compute stats per strategy ──
    strat_stats: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
        "total_pnl": 0.0, "pnl_list": [],
        "brains": defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "total_pnl": 0.0}),
        "directions": defaultdict(int),
    })

    for ticket, close in ticket_closes.items():
        strategy = close.get("strategy", "") or "unknown"
        pnl = close.get("pnl")
        if pnl is None:
            continue
        pnl = float(pnl)

        s = strat_stats[strategy]
        s["trades"] += 1
        s["total_pnl"] += pnl
        s["pnl_list"].append(pnl)

        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1
        else:
            s["breakeven"] += 1

        direction = close.get("side", "unknown")
        s["directions"][direction] += 1

        # Per-brain stats from close entry
        brain_ids = close.get("brain_ids")
        if isinstance(brain_ids, list):
            for bid in brain_ids:
                bs = s["brains"][str(bid)]
                bs["trades"] += 1
                bs["total_pnl"] += pnl
                if pnl > 0:
                    bs["wins"] += 1
                elif pnl < 0:
                    bs["losses"] += 1
                else:
                    bs["breakeven"] += 1
        else:
            # Single brain close
            bid = close.get("brain_id") or close.get("strategy") or "unknown"
            bs = s["brains"][str(bid)]
            bs["trades"] += 1
            bs["total_pnl"] += pnl
            if pnl > 0:
                bs["wins"] += 1
            elif pnl < 0:
                bs["losses"] += 1
            else:
                bs["breakeven"] += 1

    # ── OUTPUT: Per-strategy summary ──
    print(f"\n{'='*80}")
    print("=== Per-Strategy Performance ===")
    print(f"{'='*80}")
    for sname in sorted(strat_stats.keys()):
        s = strat_stats[sname]
        t = s["trades"]
        w = s["wins"]
        l_ = s["losses"]
        be = s["breakeven"]
        wr = w / (w + l_) * 100 if (w + l_) > 0 else 0.0
        total = s["total_pnl"]
        avg_win = sum(p for p in s["pnl_list"] if p > 0) / max(w, 1)
        avg_loss = abs(sum(p for p in s["pnl_list"] if p < 0)) / max(l_, 1)
        pf = (sum(p for p in s["pnl_list"] if p > 0) / avg_loss) if avg_loss > 0 else 0.0
        dirs = dict(s["directions"])

        print(f"\n  Strategy: {sname}")
        print(f"    Trades: {t}  Wins: {w}  Losses: {l_}  BE: {be}")
        print(f"    Win Rate: {wr:.1f}% (excl BE)")
        print(f"    Total PnL: ${total:,.2f}")
        print(f"    Avg Win: ${avg_win:,.2f}  Avg Loss: ${avg_loss:,.2f}")
        print(f"    Profit Factor: {pf:.2f}")
        print(f"    Directions: {dirs}")

        # Per-brain within strategy
        brains = s["brains"]
        if brains:
            print("    --- Per-Brain Breakdown ---")
            for bid in sorted(brains.keys(), key=lambda b: brains[b]["trades"], reverse=True):
                bs = brains[bid]
                bt = bs["trades"]
                if bt == 0:
                    continue
                bw = bs["wins"]
                bl = bs["losses"]
                bwr = bw / (bw + bl) * 100 if (bw + bl) > 0 else 0.0
                bpf = (sum(1 for _ in [1]) / 1)  # placeholder
                print(f"      {bid}: trades={bt} wins={bw} losses={bl} wr={bwr:.1f}% pnl=${bs['total_pnl']:,.2f}")

    # ── OUTPUT: Overall summary ──
    all_trades = sum(s["trades"] for s in strat_stats.values())
    all_wins = sum(s["wins"] for s in strat_stats.values())
    all_losses = sum(s["losses"] for s in strat_stats.values())
    all_be = sum(s["breakeven"] for s in strat_stats.values())
    all_pnl = sum(s["total_pnl"] for s in strat_stats.values())
    all_wr = all_wins / (all_wins + all_losses) * 100 if (all_wins + all_losses) > 0 else 0.0

    print(f"\n{'='*80}")
    print("=== OVERALL ===")
    print(f"  Total Trades: {all_trades}  Wins: {all_wins}  Losses: {all_losses}  BE: {all_be}")
    print(f"  Win Rate: {all_wr:.1f}%")
    print(f"  Total PnL: ${all_pnl:,.2f}")
    print(f"  Strategies: {len(strat_stats)}")
    print(f"{'='*80}")

    # ── Label distribution ──
    label_counts: dict[str, int] = defaultdict(int)
    for close in ticket_closes.values():
        lbl = close.get("label", "unknown")
        label_counts[str(lbl)] += 1
    print("\n  Label Distribution:")
    for lbl, cnt in sorted(label_counts.items(), key=lambda x: -x[1]):
        print(f"    {lbl}: {cnt}")

    # ── Time range ──
    timestamps = sorted(
        c.get("recorded_at", "") for c in ticket_closes.values() if c.get("recorded_at")
    )
    if timestamps:
        print(f"\n  Time Range: {timestamps[0][:19]} → {timestamps[-1][:19]}")

    print("\n[DONE] All statistics above are the sole source of truth. (Iron Law #11)")
    return 0


if __name__ == "__main__":
    data_dir = "data_btc"
    args = sys.argv[1:]
    if "--data-dir" in args:
        idx = args.index("--data-dir")
        if idx + 1 < len(args):
            data_dir = args[idx + 1]
    elif args and not args[0].startswith("--"):
        data_dir = args[0]
    sys.exit(main(data_dir))

"""Analyze recent consecutive losses in live trade journal.

Iron Law #11 compliant — all statistics from stdout of this script.
Deduplication: by position_ticket, preferring ack_status="closed" over "accepted",
REJECTING all ack_status="rejected" entries.
Win definition: PnL > 0 = win, PnL < 0 = loss, PnL == 0 (or None with breakeven label) = breakeven.

Usage:
  python scripts/analyze_recent_losses.py --data-dir data_btc [--last-n 30]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

STATUS_PRIORITY = {"closed": 3, "accepted": 2, "rejected": 0}


def analyze(data_dir: str, last_n: int) -> dict[str, Any]:
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print(f"ERROR: journal not found at {journal_path}")
        sys.exit(1)

    # ── Load all entries ──
    entries: list[dict[str, Any]] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))

    # ── Filter: EXCLUDE ack_status="rejected" ──
    clean = [e for e in entries if e.get("ack_status") != "rejected"]
    rejected_count = sum(1 for e in entries if e.get("ack_status") == "rejected")
    print(f"  原始条目: {len(entries)}  |  排除 rejected: {rejected_count}  |  有效: {len(clean)}")

    # ── Group by position_ticket ──
    ticket_entries: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in clean:
        t = e.get("position_ticket")
        if t:
            ticket_entries[t].append(e)

    # ── Build per-trade summary ──
    trades: list[dict[str, Any]] = []

    for ticket, group in sorted(ticket_entries.items()):
        # Separate by action
        opens = [e for e in group if e.get("action") == "open"]
        closes = [e for e in group if e.get("action") == "close"]
        modifies = [e for e in group if e.get("action") == "modify_sltp"]

        if not closes:
            continue  # still open

        # Pick best close: prefer "closed" ack_status, then "accepted", highest status first
        closes_sorted = sorted(closes, key=lambda e: STATUS_PRIORITY.get(e.get("ack_status", ""), 0), reverse=True)
        best_close = closes_sorted[0]

        # If the best close has no PnL, try the next one
        pnl = best_close.get("pnl")
        if pnl is None and len(closes_sorted) > 1:
            for alt in closes_sorted[1:]:
                if alt.get("pnl") is not None:
                    pnl = alt.get("pnl")
                    best_close = alt
                    break

        label = best_close.get("label", "")
        side = best_close.get("side", "")
        recorded_at = best_close.get("recorded_at", "")
        ack_status = best_close.get("ack_status", "")

        # Open context
        first_open = opens[0] if opens else None
        entry_price = None
        sl = None
        tp = None
        brain_ids: list[str] = []
        direction = side

        if first_open:
            direction = first_open.get("side", side)
            brain_ids = first_open.get("brain_ids", [])
            detail = first_open.get("detail", {})
            if isinstance(detail, dict):
                req = detail.get("request", {})
                if isinstance(req, dict):
                    sl = req.get("sl")
                    tp = req.get("tp")
                    entry_price = req.get("price")

        trades.append({
            "ticket": ticket,
            "side": direction,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "pnl": pnl,
            "label": label,
            "recorded_at": recorded_at,
            "ack_status": ack_status,
            "brain_ids": brain_ids,
            "modify_count": len(modifies),
            "close_attempts": len(closes),
        })

    # ── Sort by recorded_at ──
    trades.sort(key=lambda t: t["recorded_at"] or "")

    # ── Statistics ──
    trades_with_pnl = [t for t in trades if t["pnl"] is not None]
    wins = [t for t in trades_with_pnl if t["pnl"] > 0]
    losses = [t for t in trades_with_pnl if t["pnl"] < 0]
    breakevens = [
        t for t in trades
        if (t["pnl"] == 0 or (t["pnl"] is None and "breakeven" in str(t.get("label", "")).lower()))
    ]

    total_pnl = sum(t["pnl"] for t in trades_with_pnl if t["pnl"] is not None)
    total_wins = len(wins)
    total_losses = len(losses)
    total_be = len(breakevens)
    total_closed = total_wins + total_losses + total_be

    win_rate = total_wins / (total_wins + total_losses) if (total_wins + total_losses) > 0 else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    profit_factor = (
        abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses))
        if losses and sum(t["pnl"] for t in losses) != 0
        else float("inf")
    )

    # ── Label distribution ──
    label_counter: Counter = Counter()
    label_pnl: dict[str, float] = defaultdict(float)
    for t in trades_with_pnl:
        label_counter[t["label"]] += 1
        label_pnl[t["label"]] += t["pnl"] or 0
    for t in breakevens:
        label_counter[t["label"]] += 1
        label_pnl[t["label"]] += t["pnl"] or 0

    # ── Recent N trades ──
    all_settled = trades_with_pnl + [b for b in breakevens if b not in trades_with_pnl]
    all_settled.sort(key=lambda t: t["recorded_at"] or "")
    recent = all_settled[-last_n:] if last_n > 0 else all_settled

    # ── Consecutive loss streak at tail ──
    loss_streak = 0
    for t in reversed(all_settled):
        pnl_val = t.get("pnl")
        if pnl_val is not None and pnl_val < 0:
            loss_streak += 1
        elif pnl_val is not None and pnl_val > 0:
            break
        elif pnl_val is None:
            # breakeven — doesn't break the streak but doesn't count as loss either
            pass
        else:
            break

    # ── PnL sequence ──
    recent_pnl_sequence = []
    for t in recent:
        pnl_val = t.get("pnl")
        if pnl_val is not None:
            if pnl_val > 0:
                recent_pnl_sequence.append("W")
            elif pnl_val < 0:
                recent_pnl_sequence.append("L")
            else:
                recent_pnl_sequence.append("B")
        else:
            recent_pnl_sequence.append("?")

    # ══════════════════════════════════════════════════════════════════════
    # OUTPUT
    # ══════════════════════════════════════════════════════════════════════
    sep = "=" * 90
    print(f"\n{sep}")
    print(f"  实盘交易审计 — 最近 {last_n} 笔")
    print(f"  数据源: {journal_path}")
    print("  去重口径: 按 position_ticket，优先 ack_status=closed > accepted，排除 rejected")
    print("  胜率口径: PnL>0=win / PnL<0=loss / PnL≈0=be（不含 be 的胜率）")
    print(f"{sep}")

    print("\n── 全量概览 ──")
    print(f"  已平仓:      {total_closed} 笔")
    print(f"  盈利:        {total_wins} 笔  平均 +${avg_win:.2f}")
    print(f"  亏损:        {total_losses} 笔  平均 -${abs(avg_loss):.2f}")
    print(f"  保本:        {total_be} 笔")
    print(f"  胜率 (不含be): {win_rate:.1%} ({total_wins}/{total_wins + total_losses})")
    print(f"  盈亏比:      {profit_factor:.2f}")
    print(f"  累计 PnL:    ${total_pnl:+.2f}")
    print(f"  被拒平仓记录: {rejected_count} 条 (已排除)")
    print(f"  未平仓:      {len(ticket_entries) - total_closed} 笔")

    print("\n── 出场标签分布 ──")
    total_for_pct = total_closed if total_closed > 0 else 1
    for label, count in label_counter.most_common(20):
        pct = count / total_for_pct * 100
        lp = label_pnl.get(label, 0)
        print(f"  {label:55s} {count:>4}笔 ({pct:5.1f}%)  PnL=${lp:+.2f}")

    print(f"\n── 最近 {last_n} 笔交易明细 ──")
    hdr = f"  {'时间':<22s} {'Ticket':<14s} {'Side':<6s} {'Entry':>10s} {'SL':>8s} {'TP':>8s} {'PnL':>8s} {'Ack':<9s} {'Label':<42s} {'Brains'}"
    print(hdr)
    print(f"  {'-'*22} {'-'*14} {'-'*6} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*42} {'-'*20}")

    for t in recent:
        pnl_val = t["pnl"]
        pnl_str = f"${pnl_val:+.2f}" if pnl_val is not None else "$   ?"
        entry_str = f"${t['entry_price']:.1f}" if t["entry_price"] else "?"
        sl_str = f"${t['sl']:.1f}" if t["sl"] else "?"
        tp_str = f"${t['tp']:.1f}" if t["tp"] else "?"
        brain_str = ",".join(t["brain_ids"][:3]) if t["brain_ids"] else "?"
        ack = t.get("ack_status", "")[:9]
        print(f"  {t['recorded_at'] or '?':22s} {t['ticket']:<14} {t['side'] or '?':6s} {entry_str:>10s} {sl_str:>8s} {tp_str:>8s} {pnl_str:>8s} {ack:<9s} {t['label'] or '?':42s} {brain_str}")

    # ── Streak analysis ──
    print("\n── 亏损连续性分析 ──")
    print(f"  当前连续亏损: {loss_streak} 笔")
    print(f"  最近 {last_n} 笔 PnL 序列: {''.join(recent_pnl_sequence)}")

    # Show last N in sequence with ticket numbers
    print(f"\n  逐笔序列 (最近 {min(last_n, len(recent))} 笔):")
    for i, t in enumerate(recent):
        pnl_val = t["pnl"]
        if pnl_val is not None:
            flag = "W" if pnl_val > 0 else ("L" if pnl_val < 0 else "B")
        else:
            flag = "?"
        print(f"    [{flag}] ticket={t['ticket']}  PnL={pnl_val}  label={t['label']}  time={t['recorded_at']}")

    # If there IS a loss streak, show details
    if loss_streak > 0:
        print(f"\n── 当前连续亏损详情 (最近 {loss_streak} 笔) ──")
        streak_losses: list[dict] = []
        for t in reversed(all_settled):
            if t["pnl"] is not None and t["pnl"] < 0 and len(streak_losses) < loss_streak:
                streak_losses.append(t)
        for t in reversed(streak_losses):
            brain_str = ",".join(t["brain_ids"]) if t["brain_ids"] else "?"
            sl_str = f"${t['sl']:.1f}" if t["sl"] else "?"
            tp_str = f"${t['tp']:.1f}" if t["tp"] else "?"
            entry_str = f"${t['entry_price']:.1f}" if t["entry_price"] else "?"
            print(f"  ticket={t['ticket']}  time={t['recorded_at']}  side={t['side']}  entry={entry_str}  SL={sl_str}  TP={tp_str}  PnL=${t['pnl']:.2f}  label={t['label']}  brains=[{brain_str}]")

    # ── Direction analysis ──
    side_pnl: dict[str, list[float]] = defaultdict(list)
    side_trades: dict[str, list[dict]] = defaultdict(list)
    for t in trades_with_pnl:
        if t["side"] and t["pnl"] is not None:
            side_pnl[t["side"]].append(t["pnl"])
            side_trades[t["side"]].append(t)

    print("\n── 方向分析 ──")
    for side, pnls in sorted(side_pnl.items()):
        total = sum(pnls)
        wins_s = sum(1 for p in pnls if p > 0)
        losses_s = sum(1 for p in pnls if p < 0)
        wr = wins_s / (wins_s + losses_s) if (wins_s + losses_s) > 0 else 0
        avg_pnl = total / len(pnls) if pnls else 0
        print(f"  {side:6s}: {len(pnls)}笔  胜率={wr:.1%}  avg=${avg_pnl:+.2f}  PnL=${total:+.2f}")

    # ── Direction switch analysis ──
    print("\n── 最近方向切换 ──")
    prev_side = None
    switches = []
    for t in trades:
        if t["side"] and t["side"] != prev_side:
            if prev_side is not None:
                switches.append((prev_side, t["side"], t["recorded_at"]))
            prev_side = t["side"]
    for old, new, ts in switches[-5:]:
        print(f"  {ts}  {old} → {new}")

    # ── Brain-level PnL contribution ──
    brain_pnl: dict[str, list[float]] = defaultdict(list)
    for t in trades_with_pnl:
        if t["pnl"] is not None and t["brain_ids"]:
            for b in t["brain_ids"]:
                brain_pnl[b].append(t["pnl"])

    print("\n── Brain PnL 贡献 ──")
    for brain, pnls in sorted(brain_pnl.items(), key=lambda x: sum(x[1])):
        total = sum(pnls)
        wins_b = sum(1 for p in pnls if p > 0)
        losses_b = sum(1 for p in pnls if p < 0)
        wr = wins_b / (wins_b + losses_b) if (wins_b + losses_b) > 0 else 0
        print(f"  {brain:40s}: {len(pnls):>3}笔  胜率={wr:.1%}  PnL=${total:+.2f}")

    return {
        "total_closed": total_closed,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "loss_streak": loss_streak,
        "recent_sequence": "".join(recent_pnl_sequence),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Analyze recent losses in live trade journal")
    p.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    p.add_argument("--last-n", type=int, default=30, help="Number of recent trades to show (default: 30)")
    args = p.parse_args()
    analyze(args.data_dir, args.last_n)

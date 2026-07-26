#!/usr/bin/env python3
"""
M15 Swing XAU 实盘表现审计 — Iron Law #11 合规

统计口径:
  - SSOT: data/live_trade_journal.jsonl (XAU)
  - 去重: position_ticket
  - 配对: open + close 动作按 position_ticket join
  - 胜率: close.pnl > 0 / settled_count
  - PnL 口径: close.pnl (美元净额)
  - 方向: open.side

用法: python scripts/_analyze_m15_swing_now.py
"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _pnl(close_row: dict) -> float:
    try:
        return float(close_row.get("pnl", 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_ts(ts: str):
    """Parse ISO timestamp string to UTC-aware datetime."""
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError, OSError):
        return None


def main():
    journal_path = Path("data/live_trade_journal.jsonl")

    # ── Load journal, filter M15 ──
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    modifies: list[dict] = []

    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            if row.get("strategy") != "m15_swing":
                continue

            action = row.get("action", "")
            ticket = str(row.get("position_ticket", ""))

            if action == "open":
                opens[ticket] = row
            elif action == "close":
                closes[ticket] = row
            elif action == "modify_sltp":
                modifies.append(row)

    # ── Pair open→close ──
    settled: list[tuple[dict, dict]] = []
    unmatched_opens: list[dict] = []
    unmatched_closes: list[dict] = []

    for ticket, open_row in opens.items():
        if ticket in closes:
            settled.append((open_row, closes[ticket]))
        else:
            unmatched_opens.append(open_row)

    for ticket, close_row in closes.items():
        if ticket not in opens:
            unmatched_closes.append(close_row)

    n_total = len(settled)
    print("=== M15 Swing XAU 实盘审计 ===")
    print(
        f"Journal M15 entries: open={len(opens)}, close={len(closes)}, modify_sltp={len(modifies)}"
    )
    print(f"配对结算: {n_total} trades")
    print(f"未匹配: open={len(unmatched_opens)} (持仓中), close={len(unmatched_closes)} (孤儿平仓)")

    if n_total == 0:
        print("\n[No settled trades to analyze]")
        return

    # ── Classify ──
    win_trades = [(o, c) for o, c in settled if _pnl(c) > 0]
    loss_trades = [(o, c) for o, c in settled if _pnl(c) <= 0]

    n = n_total
    n_wins = len(win_trades)
    n_losses = len(loss_trades)
    wr = n_wins / n * 100

    total_pnl = sum(_pnl(c) for _, c in settled)
    gross_profit = sum(_pnl(c) for _, c in win_trades)
    gross_loss = abs(sum(_pnl(c) for _, c in loss_trades))
    pf = gross_profit / gross_loss if gross_loss else float("inf")

    avg_win = gross_profit / n_wins if n_wins else 0
    avg_loss = gross_loss / n_losses if n_losses else 0

    print(f"""
{'='*60}
总体表现
{'='*60}
结算交易:   {n}
胜率:       {wr:.1f}% ({n_wins}W / {n_losses}L)
总盈亏:     ${total_pnl:+.2f}
Profit Factor: {pf:.2f}
平均盈利:   ${avg_win:+.2f}
平均亏损:   ${avg_loss:+.2f}
盈亏比:     {avg_win / avg_loss if avg_loss else 0:.2f}
""")

    # ── Direction ──
    print(f"{'='*60}")
    print("方向分析")
    print(f"{'='*60}")
    dir_stats: dict[str, dict] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
    )
    for o, c in settled:
        side = (o.get("side") or "unknown").upper()
        p = _pnl(c)
        dir_stats[side]["count"] += 1
        dir_stats[side]["pnl"] += p
        if p > 0:
            dir_stats[side]["wins"] += 1
        else:
            dir_stats[side]["losses"] += 1

    for side in ["LONG", "SHORT"]:
        ds = dir_stats.get(side, {})
        cnt = ds.get("count", 0)
        if cnt == 0:
            print(f"  {side:6s}: 0 trades")
            continue
        w = ds.get("wins", 0)
        pnl_d = ds.get("pnl", 0.0)
        wr_d = w / cnt * 100
        print(f"  {side:6s}: {cnt:3d}t | WR={wr_d:5.1f}% | PnL=${pnl_d:+8.2f}")

    # ── Weekly ──
    print(f"\n{'='*60}")
    print("按周聚类")
    print(f"{'='*60}")
    weekly: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for o, c in settled:
        dt = _parse_ts(c.get("recorded_at") or o.get("recorded_at", ""))
        if dt:
            weekly[dt.strftime("%Y-W%W")].append((o, c))

    for wk in sorted(weekly.keys()):
        trades = weekly[wk]
        nw = len(trades)
        w_wk = sum(1 for _, c in trades if _pnl(c) > 0)
        pnl_wk = sum(_pnl(c) for _, c in trades)
        gp = sum(_pnl(c) for _, c in trades if _pnl(c) > 0)
        gl = abs(sum(_pnl(c) for _, c in trades if _pnl(c) <= 0))
        pf_wk = gp / gl if gl else float("inf")
        wr_wk = w_wk / nw * 100 if nw else 0
        longs = sum(1 for o, _ in trades if (o.get("side") or "").upper() == "LONG")
        shorts = sum(1 for o, _ in trades if (o.get("side") or "").upper() == "SHORT")
        print(
            f"  {wk}: {nw:3d}t | WR={wr_wk:5.1f}% | PnL=${pnl_wk:+7.2f} | PF={pf_wk:.2f} | L/S={longs}/{shorts}"
        )

    # ── Recent trend ──
    print(f"\n{'='*60}")
    print("近期趋势")
    print(f"{'='*60}")
    now = datetime.now(UTC)
    for days in [7, 14, 30, 60]:
        cutoff = now - timedelta(days=days)
        recent = [
            (o, c)
            for o, c in settled
            if (dt := _parse_ts(c.get("recorded_at") or "")) and dt >= cutoff
        ]
        nr = len(recent)
        if nr:
            w_r = sum(1 for _, c in recent if _pnl(c) > 0)
            pnl_r = sum(_pnl(c) for _, c in recent)
            gp = sum(_pnl(c) for _, c in recent if _pnl(c) > 0)
            gl = abs(sum(_pnl(c) for _, c in recent if _pnl(c) <= 0))
            pf_r = gp / gl if gl else float("inf")
            wr_r = w_r / nr * 100
            print(f"  {days:2d}d: {nr:3d}t | WR={wr_r:5.1f}% | PnL=${pnl_r:+7.2f} | PF={pf_r:.2f}")
        else:
            print(f"  {days:2d}d: 0 trades")

    # ── Monthly ──
    print(f"\n{'='*60}")
    print("按月聚类")
    print(f"{'='*60}")
    monthly: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for o, c in settled:
        dt = _parse_ts(c.get("recorded_at") or o.get("recorded_at", ""))
        if dt:
            monthly[dt.strftime("%Y-%m")].append((o, c))

    for mk in sorted(monthly.keys()):
        trades = monthly[mk]
        nm = len(trades)
        w_m = sum(1 for _, c in trades if _pnl(c) > 0)
        pnl_m = sum(_pnl(c) for _, c in trades)
        gp = sum(_pnl(c) for _, c in trades if _pnl(c) > 0)
        gl = abs(sum(_pnl(c) for _, c in trades if _pnl(c) <= 0))
        pf_m = gp / gl if gl else float("inf")
        wr_m = w_m / nm * 100 if nm else 0
        print(f"  {mk}: {nm:3d}t | WR={wr_m:5.1f}% | PnL=${pnl_m:+7.2f} | PF={pf_m:.2f}")

    # ── Brain contribution ──
    print(f"\n{'='*60}")
    print("Brain 贡献 (close.brain_ids)")
    print(f"{'='*60}")
    brain_pnls: dict[str, list[float]] = defaultdict(list)
    for _, c in settled:
        brain_ids = c.get("brain_ids") or c.get("brain_id") or ["unknown"]
        if isinstance(brain_ids, str):
            brain_ids = [brain_ids]
        p = _pnl(c)
        for bid in brain_ids:
            brain_pnls[bid].append(p)

    for bid in sorted(brain_pnls.keys()):
        pnls = brain_pnls[bid]
        nb = len(pnls)
        w_b = sum(1 for p in pnls if p > 0)
        pnl_b = sum(pnls)
        gp = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf_b = gp / gl if gl else float("inf")
        wr_b = w_b / nb * 100 if nb else 0
        print(f"  {bid:45s}: {nb:3d}t | WR={wr_b:5.1f}% | PnL=${pnl_b:+7.2f} | PF={pf_b:.2f}")

    # ── Exit labels ──
    print(f"\n{'='*60}")
    print("出场标签 (close.label)")
    print(f"{'='*60}")
    label_counts: dict[str, int] = defaultdict(int)
    label_pnls: dict[str, float] = defaultdict(float)
    for _, c in settled:
        lb = c.get("label") or "unknown"
        if isinstance(lb, dict):
            lb = lb.get("reason", "dict_no_reason")
        label_counts[lb] += 1
        label_pnls[lb] += _pnl(c)

    for lb in sorted(label_counts.keys(), key=lambda k: -label_counts[k]):
        cnt = label_counts[lb]
        pnl_l = label_pnls[lb]
        print(f"  {lb:45s}: {cnt:3d}t | PnL=${pnl_l:+7.2f}")

    # ── SL/TP spread analysis ──
    print(f"\n{'='*60}")
    print("SL/TP 参数分布 (open entry)")
    print(f"{'='*60}")
    sl_vals: dict[float, int] = defaultdict(int)
    tp_vals: dict[float, int] = defaultdict(int)
    for o, _ in settled:
        sl = o.get("sl")
        tp = o.get("tp")
        # compute SL/TP distances relative to entry
        entry_price = None
        detail = o.get("detail", {})
        if isinstance(detail, dict):
            entry_price = detail.get("request", {}).get("price") or detail.get("price")
        if sl and tp and entry_price:
            try:
                sl_dist = abs(float(entry_price) - float(sl))
                tp_dist = abs(float(tp) - float(entry_price))
                # round to nearest 0.5
                sl_r = round(sl_dist * 2) / 2
                tp_r = round(tp_dist * 2) / 2
                sl_vals[sl_r] += 1
                tp_vals[tp_r] += 1
            except (ValueError, TypeError):
                pass

    if sl_vals:
        print("  SL distances (points from entry):")
        for dist in sorted(sl_vals.keys()):
            print(f"    {dist:6.1f}: {sl_vals[dist]:3d} trades")
    if tp_vals:
        print("  TP distances (points from entry):")
        for dist in sorted(tp_vals.keys()):
            print(f"    {dist:6.1f}: {tp_vals[dist]:3d} trades")

    # ── Open positions ──
    if unmatched_opens:
        print(f"\n{'='*60}")
        print(f"当前持仓 (未匹配 open): {len(unmatched_opens)}")
        print(f"{'='*60}")
        for op in unmatched_opens[:5]:
            ticket = op.get("position_ticket", "?")
            side = op.get("side", "?")
            ts = op.get("recorded_at", "?")
            sl = op.get("sl", "?")
            tp = op.get("tp", "?")
            print(f"  Ticket={ticket} | {side} | opened={ts} | SL={sl} | TP={tp}")

    print(f"\n[analysis complete — {n_total} settled trades]")


if __name__ == "__main__":
    main()

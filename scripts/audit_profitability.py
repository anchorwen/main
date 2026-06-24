"""Profitability Audit -- Iron Law #11 compliant.
Analyzes live trading performance for a specific time window.
Usage: python scripts/audit_profitability.py [--hours N] [--symbol btc|xau|both]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def parse_ts(val):
    if not val:
        return None
    try:
        s = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def analyze_journal(data_dir: Path, window_start: datetime, window_end: datetime, label: str):
    """Analyze live_trade_journal for profitability."""
    journal = load_jsonl(data_dir / "live_trade_journal.jsonl")

    # Group entries by position_ticket
    trades: dict[int, list] = defaultdict(list)
    for entry in journal:
        ticket = entry.get("position_ticket")
        if ticket is None:
            continue
        trades[ticket].append(entry)

    # Analyze each trade
    trade_records = []
    for ticket, entries in trades.items():
        # Find open and close
        open_entry = None
        close_entry = None
        trail_entries = []
        for e in entries:
            action = e.get("action", "")
            if action in ("order_send", "open_long", "open_short"):
                open_entry = e
            if e.get("pnl") is not None:
                close_entry = e
            if action == "modify_sltp" and e.get("ack_status") == "accepted":
                trail_entries.append(e)

        if open_entry is None:
            continue

        open_ts = parse_ts(open_entry.get("recorded_at"))
        if open_ts is None:
            continue
        if open_ts < window_start or open_ts > window_end:
            continue

        close_ts = parse_ts(close_entry.get("recorded_at")) if close_entry else None
        pnl = close_entry.get("pnl") if close_entry else None
        label = close_entry.get("label") if close_entry else "open"
        strategy = open_entry.get("strategy", "?")
        side = open_entry.get("side", "?")
        volume = open_entry.get("volume", 0)
        magic = open_entry.get("magic", 0)
        p_win = open_entry.get("p_win")

        # Get entry price from detail
        detail = open_entry.get("detail", {})
        req = detail.get("request", {}) if isinstance(detail, dict) else {}
        entry_price = req.get("price")

        # Count trail modifications
        trail_count = len(trail_entries)

        trade_records.append(
            {
                "ticket": ticket,
                "strategy": strategy,
                "side": side,
                "entry_price": entry_price,
                "volume": volume,
                "magic": magic,
                "p_win": p_win,
                "pnl": pnl,
                "label": label,
                "open_time": open_ts,
                "close_time": close_ts,
                "trail_count": trail_count,
                "duration_min": (close_ts - open_ts).total_seconds() / 60 if close_ts else None,
            }
        )

    # Also check labels file for additional trade info
    labels = load_jsonl(data_dir / "reports" / "live_labels.jsonl")
    label_by_ticket = {}
    for lb in labels:
        ticket = lb.get("position_ticket")
        if ticket:
            label_by_ticket[ticket] = lb

    # Merge label info
    for tr in trade_records:
        lb = label_by_ticket.get(tr["ticket"])
        if lb:
            if tr["pnl"] is None and lb.get("pnl") is not None:
                tr["pnl"] = lb["pnl"]
            if tr["label"] == "open" and lb.get("label"):
                tr["label"] = lb["label"]
            if lb.get("exit_price"):
                tr["exit_price"] = lb["exit_price"]
            if lb.get("close_recorded_at"):
                ct = parse_ts(lb["close_recorded_at"])
                if ct and tr["close_time"] is None:
                    tr["close_time"] = ct
                    if tr["open_time"]:
                        tr["duration_min"] = (ct - tr["open_time"]).total_seconds() / 60

    # Sort by open time
    trade_records.sort(key=lambda x: x["open_time"])

    return trade_records


def print_report(trades: list, label: str, window_start: datetime, window_end: datetime):
    """Print formatted profitability report."""
    print(f"\n{'=' * 90}")
    print(f"  {label} PROFITABILITY AUDIT")
    print(f"  Window: {window_start.isoformat()[:19]}Z to {window_end.isoformat()[:19]}Z")
    print(f"{'=' * 90}")

    if not trades:
        print("  No trades found in this window.")
        return

    # Summary stats
    total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    closed_trades = [t for t in trades if t["pnl"] is not None]
    open_trades = [t for t in trades if t["pnl"] is None]
    wins = [t for t in closed_trades if (t["pnl"] or 0) > 0]
    losses = [t for t in closed_trades if (t["pnl"] or 0) < 0]
    breakeven = [t for t in closed_trades if (t["pnl"] or 0) == 0]

    print("\n  [SUMMARY]")
    print(f"    Total trades opened: {len(trades)}")
    print(f"    Closed: {len(closed_trades)} | Still open: {len(open_trades)}")
    if closed_trades:
        print(f"    Wins: {len(wins)} | Losses: {len(losses)} | Breakeven: {len(breakeven)}")
        win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0
        print(f"    Win rate: {win_rate:.1f}%")
        print(f"    Total PnL: {total_pnl:+.2f}R")
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"    Avg win: {avg_win:+.2f}R | Avg loss: {avg_loss:+.2f}R")
        if avg_loss != 0:
            print(
                f"    Reward/Risk: {abs(avg_win/avg_loss):.2f}"
                if avg_loss != 0
                else "    Reward/Risk: INF"
            )

    # Per strategy breakdown
    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)

    print("\n  [BY STRATEGY]")
    print(
        f"    {'Strategy':30s} | {'Trades':>5s} | {'Wins':>5s} | {'Losses':>6s} | {'Open':>4s} | {'PnL(R)':>9s} | {'WR%':>6s} | {'AvgDur':>7s}"
    )
    print(f"    {'-'*30} | {'-'*5} | {'-'*5} | {'-'*6} | {'-'*4} | {'-'*9} | {'-'*6} | {'-'*7}")

    for sname in sorted(by_strategy.keys()):
        st = by_strategy[sname]
        st_closed = [t for t in st if t["pnl"] is not None]
        st_open = [t for t in st if t["pnl"] is None]
        st_wins = [t for t in st_closed if (t["pnl"] or 0) > 0]
        st_pnl = sum(t.get("pnl", 0) or 0 for t in st)
        st_wr = len(st_wins) / len(st_closed) * 100 if st_closed else 0
        durations = [t["duration_min"] for t in st_closed if t["duration_min"] is not None]
        avg_dur = sum(durations) / len(durations) if durations else 0
        print(
            f"    {sname:30s} | {len(st):>5d} | {len(st_wins):>5d} | {len(st_closed)-len(st_wins):>6d} | {len(st_open):>4d} | {st_pnl:>+9.2f} | {st_wr:>5.1f}% | {avg_dur:>6.0f}m"
        )

    # Individual trades
    print("\n  [INDIVIDUAL TRADES]")
    print(
        f"    {'Ticket':>12s} | {'Open Time':19s} | {'Close Time':19s} | {'Strategy':25s} | {'Side':5s} | {'Entry':>10s} | {'PnL(R)':>8s} | {'Label':20s} | {'Dur(min)':>8s} | {'Trails':>6s}"
    )
    print(
        f"    {'-'*12} | {'-'*19} | {'-'*19} | {'-'*25} | {'-'*5} | {'-'*10} | {'-'*8} | {'-'*20} | {'-'*8} | {'-'*6}"
    )

    for t in trades:
        ot = t["open_time"].strftime("%Y-%m-%d %H:%M:%S") if t["open_time"] else "?"
        ct = t["close_time"].strftime("%Y-%m-%d %H:%M:%S") if t["close_time"] else "(open)"
        entry = f"{t['entry_price']:.2f}" if t["entry_price"] else "?"
        pnl_str = f"{t['pnl']:+.2f}" if t["pnl"] is not None else "OPEN"
        dur = f"{t['duration_min']:.0f}" if t["duration_min"] else "-"
        print(
            f"    {t['ticket']:>12d} | {ot:19s} | {ct:19s} | {t['strategy']:25s} | {t['side']:5s} | {entry:>10s} | {pnl_str:>8s} | {t['label']:20s} | {dur:>8s} | {t['trail_count']:>6d}"
        )

    # Exit reason breakdown
    label_counts = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in trades:
        if t["pnl"] is not None:
            lb = t["label"]
            label_counts[lb]["count"] += 1
            label_counts[lb]["pnl"] += t["pnl"]

    if label_counts:
        print("\n  [EXIT REASONS]")
        print(f"    {'Label':25s} | {'Count':>5s} | {'PnL(R)':>9s} | {'Avg PnL':>9s}")
        print(f"    {'-'*25} | {'-'*5} | {'-'*9} | {'-'*9}")
        for lb in sorted(label_counts.keys(), key=lambda x: -label_counts[x]["count"]):
            lc = label_counts[lb]
            avg = lc["pnl"] / lc["count"] if lc["count"] > 0 else 0
            print(f"    {lb:25s} | {lc['count']:>5d} | {lc['pnl']:>+9.2f} | {avg:>+9.2f}")


def analyze_golden_master_decisions(data_dir: Path, window_start: datetime, window_end: datetime):
    """Extract strategy decisions from golden_master for the window."""
    gm = load_jsonl(data_dir / "golden_master.jsonl")
    decisions = []

    for entry in gm:
        ts = parse_ts(entry.get("timestamp_utc") or entry.get("time"))
        if ts is None or ts < window_start or ts > window_end:
            continue

        summary = entry.get("summary", {})
        strats = summary.get("strategy_results", [])
        if not isinstance(strats, list):
            continue

        for s in strats:
            sname = s.get("strategy", s.get("strategy_name", "?"))
            should_trade = s.get("should_trade", s.get("should_open"))
            reason = s.get("reason", "")
            p_win = s.get("p_win")
            direction = s.get("direction", "?")
            confidence = s.get("confidence", 0)
            volume = s.get("volume", 0)

            decisions.append(
                {
                    "time": ts,
                    "strategy": sname,
                    "should_trade": should_trade,
                    "reason": str(reason),
                    "p_win": p_win,
                    "direction": direction,
                    "confidence": confidence,
                    "volume": volume,
                }
            )

    return decisions


def print_decision_summary(decisions: list, label: str):
    """Print decision rejection reasons."""
    if not decisions:
        return

    approved = [d for d in decisions if d["should_trade"]]
    rejected = [d for d in decisions if not d["should_trade"]]

    print(f"\n  [DECISIONS: {label}]")
    print(f"    Total cycles with decisions: {len(set(d['time'] for d in decisions))}")
    print(f"    Approved: {len(approved)} | Rejected: {len(rejected)}")

    # Rejection reason breakdown
    reason_counts = defaultdict(list)
    for d in rejected:
        reason_counts[d["reason"]].append(d)

    if reason_counts:
        print("\n  [REJECTION REASONS]")
        for reason in sorted(reason_counts.keys(), key=lambda x: -len(reason_counts[x])):
            items = reason_counts[reason]
            strategies = set(d["strategy"] for d in items)
            print(
                f"    {reason[:90]:90s} | count={len(items):>3d} | strategies={sorted(strategies)}"
            )

    # Approved decisions
    if approved:
        print("\n  [APPROVED DECISIONS]")
        for d in approved:
            print(
                f"    {d['time'].strftime('%H:%M:%S')} | {d['strategy']:30s} | dir={d['direction']:6s} | p_win={d['p_win']} | conf={d['confidence']:.4f} | vol={d['volume']}"
            )


def main():
    hours_back = 36
    if len(sys.argv) > 1:
        try:
            hours_back = int(sys.argv[1])
        except ValueError:
            pass

    symbol = "both"
    if len(sys.argv) > 2:
        symbol = sys.argv[2].lower()

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=hours_back)

    print("=" * 90)
    print("  LIVE PROFITABILITY AUDIT -- Iron Law #11 Compliance")
    print(f"  Generated: {window_end.isoformat()[:19]}Z")
    print(f"  Window: {window_start.isoformat()[:19]}Z to {window_end.isoformat()[:19]}Z")
    print(f"  Duration: {hours_back}h")
    print("=" * 90)

    # Analysis parameters
    for sym_label, data_dir in [("BTC", DATA_BTC), ("XAU", DATA_XAU)]:
        if symbol not in ("both", sym_label.lower()):
            continue

        print(f"\n{'#' * 90}")
        print(f"#  {sym_label} ({data_dir})")
        print(f"{'#' * 90}")

        # Trade analysis
        trades = analyze_journal(data_dir, window_start, window_end, sym_label)
        print_report(trades, sym_label, window_start, window_end)

        # Decision analysis
        decisions = analyze_golden_master_decisions(data_dir, window_start, window_end)
        print_decision_summary(decisions, sym_label)

    # Cross-symbol summary
    print(f"\n{'=' * 90}")
    print("  AUDIT COMPLETE -- the above is the sole source of truth")
    print("=" * 90)


DATA_BTC = PROJECT_ROOT / "data_btc"
DATA_XAU = PROJECT_ROOT / "data"

if __name__ == "__main__":
    main()

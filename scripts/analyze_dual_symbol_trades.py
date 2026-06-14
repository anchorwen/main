"""
Dual-Symbol Trade Audit Script (Iron Law #11 compliant)
========================================================
- Reads live_labels.jsonl from both data/ (XAU) and data_btc/ (BTC)
- Dedup by position_ticket
- Outputs daily PnL, win rate, direction distribution, label distribution
- All conclusions MUST be based on this script's stdout only.

Usage:
  python scripts/analyze_dual_symbol_trades.py --xau-dir data --btc-dir data_btc
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_labels(data_dir: str) -> list[dict]:
    labels_path = Path(data_dir) / "reports" / "live_labels.jsonl"
    if not labels_path.exists():
        print(f"ERROR: {labels_path} not found", file=sys.stderr)
        return []
    labels = []
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                labels.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return labels


def dedup_by_ticket(labels: list[dict]) -> list[dict]:
    """Keep last occurrence of each position_ticket. Unlinked (null ticket) kept as-is."""
    seen = {}
    unlinked = []
    for lb in labels:
        ticket = lb.get("position_ticket")
        if ticket is None:
            unlinked.append(lb)
        else:
            seen[ticket] = lb
    result = list(seen.values()) + unlinked
    return result


def analyze(symbol: str, labels: list[dict]):
    labels = dedup_by_ticket(labels)

    # Filter by symbol
    own = [lb for lb in labels if lb.get("symbol") == symbol]

    # Daily aggregates
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_trades: dict[str, int] = defaultdict(int)
    daily_wins: dict[str, int] = defaultdict(int)
    daily_losses: dict[str, int] = defaultdict(int)

    label_counts: dict[str, int] = defaultdict(int)
    side_counts: dict[str, int] = defaultdict(int)
    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_breakeven = 0

    # Paper vs Real
    paper_count = 0
    real_count = 0

    for lb in own:
        is_paper = lb.get("open_message_id", "").startswith("paper_record")
        if is_paper:
            paper_count += 1
        else:
            real_count += 1

        label = lb.get("label", "unknown")
        label_counts[label] += 1
        side = lb.get("side", "unknown")
        side_counts[side] += 1

        pnl = lb.get("pnl")
        date = (lb.get("open_recorded_at") or lb.get("close_recorded_at") or "")[:10]
        if not date:
            continue

        daily_trades[date] += 1

        if pnl is not None:
            total_trades += 1
            total_pnl += pnl
            daily_pnl[date] += pnl
            if pnl > 0:
                total_wins += 1
                daily_wins[date] += 1
            elif pnl < 0:
                total_losses += 1
                daily_losses[date] += 1
            else:
                total_breakeven += 1

    # Print report
    print(f"\n{'='*70}")
    print(f"  {symbol} Trade Audit Report")
    print(f"{'='*70}")
    print(f"  Total labels (raw):           {len(own)}")
    print(f"  Paper records (no MT5 ticket): {paper_count}")
    print(f"  Real MT5 positions:            {real_count}")
    print(f"  Settled trades (with PnL):     {total_trades}")
    print(f"  Wins:   {total_wins} ({total_wins/max(total_trades,1)*100:.1f}%)")
    print(f"  Losses: {total_losses} ({total_losses/max(total_trades,1)*100:.1f}%)")
    print(f"  BE:     {total_breakeven}")
    print(f"  Win Rate (excl BE): {total_wins/max(total_wins+total_losses,1)*100:.1f}%")
    print(f"  Total PnL: {total_pnl:+.2f}")
    print(f"  Avg PnL/trade: {total_pnl/max(total_trades,1):+.4f}")
    print(f"  Label distribution: {dict(label_counts)}")
    print(f"  Side distribution:  {dict(side_counts)}")

    # Daily table (last 30 days)
    print(f"\n  {'─'*60}")
    print("  Daily Breakdown (last 30 days with activity)")
    print(f"  {'─'*60}")
    print(f"  {'Date':<12} {'Trades':>7} {'Wins':>5} {'Losses':>7} {'Win%':>6} {'PnL':>10}")
    print(f"  {'─'*60}")
    sorted_dates = sorted(daily_trades.keys())
    recent = sorted_dates[-30:]
    for date in recent:
        t = daily_trades[date]
        w = daily_wins[date]
        l = daily_losses[date]
        wr = w / max(w + l, 1) * 100
        pnl = daily_pnl[date]
        # Only show dates with trades
        marker = " ←" if t <= 2 else ""
        print(f"  {date:<12} {t:>7} {w:>5} {l:>7} {wr:>5.0f}% {pnl:>+10.2f}{marker}")

    # Weekly aggregates
    print(f"\n  {'─'*60}")
    print("  Weekly Aggregates")
    print(f"  {'─'*60}")
    weekly: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for date in sorted_dates:
        # ISO week
        week = date  # simplify: group by date ranges
        # Actually use simple 7-day buckets
        pass

    # Monthly aggregates
    monthly: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for date in sorted_dates:
        month_key = date[:7]
        monthly[month_key]["trades"] += daily_trades[date]
        monthly[month_key]["wins"] += daily_wins[date]
        monthly[month_key]["losses"] += daily_losses[date]
        monthly[month_key]["pnl"] += daily_pnl[date]

    print(f"  {'Month':<9} {'Trades':>7} {'Wins':>5} {'Losses':>7} {'Win%':>6} {'PnL':>10}")
    print(f"  {'─'*50}")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / max(m["wins"] + m["losses"], 1) * 100
        print(f"  {month:<9} {m['trades']:>7} {m['wins']:>5} {m['losses']:>7} {wr:>5.0f}% {m['pnl']:>+10.2f}")

    return {
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_pnl": total_pnl,
        "daily": dict(daily_pnl),
        "daily_trades": dict(daily_trades),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xau-dir", default="data")
    parser.add_argument("--btc-dir", default="data_btc")
    args = parser.parse_args()

    xau_labels = load_labels(args.xau_dir)
    btc_labels = load_labels(args.btc_dir)

    print(f"Loaded {len(xau_labels)} XAU labels from {args.xau_dir}")
    print(f"Loaded {len(btc_labels)} BTC labels from {args.btc_dir}")

    xau_stats = analyze("XAUUSDc", xau_labels)
    btc_stats = analyze("BTCUSDc", btc_labels)

    # Cross-symbol comparison
    print(f"\n{'='*70}")
    print("  Cross-Symbol Comparison")
    print(f"{'='*70}")
    print(f"  {'Metric':<25} {'XAUUSDc':>15} {'BTCUSDc':>15}")
    print(f"  {'─'*55}")
    print(f"  {'Settled Trades':<25} {xau_stats['total_trades']:>15} {btc_stats['total_trades']:>15}")
    print(f"  {'Win Rate':<25} {xau_stats['total_wins']/max(xau_stats['total_wins']+xau_stats['total_losses'],1)*100:>14.1f}% {btc_stats['total_wins']/max(btc_stats['total_wins']+btc_stats['total_losses'],1)*100:>14.1f}%")
    print(f"  {'Total PnL':<25} {xau_stats['total_pnl']:>+14.2f} {btc_stats['total_pnl']:>+14.2f}")
    # Active days
    xau_days = len(xau_stats['daily_trades'])
    btc_days = len(btc_stats['daily_trades'])
    print(f"  {'Active Trading Days':<25} {xau_days:>15} {btc_days:>15}")
    # Avg trades/day (on active days)
    xau_avg = xau_stats['total_trades'] / max(xau_days, 1)
    btc_avg = btc_stats['total_trades'] / max(btc_days, 1)
    print(f"  {'Avg Trades/Day (active)':<25} {xau_avg:>14.1f} {btc_avg:>14.1f}")

    # Recent trend: last 5 active days each
    print("\n  Recent Trend (last 5 active days):")
    print(f"  {'XAU':<45} {'BTC':<45}")
    print(f"  {'─'*90}")
    xau_recent = sorted(xau_stats['daily_trades'].keys())[-5:]
    btc_recent = sorted(btc_stats['daily_trades'].keys())[-5:]
    for i in range(max(len(xau_recent), len(btc_recent))):
        xau_str = ""
        btc_str = ""
        if i < len(xau_recent):
            d = xau_recent[i]
            xau_str = f"{d}: {xau_stats['daily_trades'][d]} trades, PnL={xau_stats['daily'].get(d,0):+.2f}"
        if i < len(btc_recent):
            d = btc_recent[i]
            btc_str = f"{d}: {btc_stats['daily_trades'][d]} trades, PnL={btc_stats['daily'].get(d,0):+.2f}"
        print(f"  {xau_str:<45} {btc_str:<45}")

    # Anomaly detection
    print(f"\n{'='*70}")
    print("  Anomaly Flags")
    print(f"{'='*70}")
    flags = []

    # Check XAU trade count collapse
    xau_daily_sorted = sorted(xau_stats['daily_trades'].items())
    if len(xau_daily_sorted) >= 10:
        early_avg = sum(v for _, v in xau_daily_sorted[:10]) / 10
        recent_10 = xau_daily_sorted[-10:]
        recent_avg = sum(v for _, v in recent_10) / 10
        if early_avg > 0 and recent_avg < early_avg * 0.3:
            flags.append(f"XAU trade count collapsed: early avg={early_avg:.1f}/day -> recent avg={recent_avg:.1f}/day ({recent_avg/early_avg*100:.0f}%)")

    # Check BTC
    btc_daily_sorted = sorted(btc_stats['daily_trades'].items())
    if len(btc_daily_sorted) >= 10:
        early_avg = sum(v for _, v in btc_daily_sorted[:10]) / 10
        recent_10 = btc_daily_sorted[-10:]
        recent_avg = sum(v for _, v in recent_10) / 10
        if early_avg > 0 and recent_avg < early_avg * 0.3:
            flags.append(f"BTC trade count collapsed: early avg={early_avg:.1f}/day -> recent avg={recent_avg:.1f}/day ({recent_avg/early_avg*100:.0f}%)")

    # Check unusual win rate
    if xau_stats['total_wins'] + xau_stats['total_losses'] > 20:
        xau_wr = xau_stats['total_wins'] / (xau_stats['total_wins'] + xau_stats['total_losses'])
        if xau_wr < 0.35:
            flags.append(f"XAU win rate critically low: {xau_wr*100:.1f}%")

    if btc_stats['total_wins'] + btc_stats['total_losses'] > 20:
        btc_wr = btc_stats['total_wins'] / (btc_stats['total_wins'] + btc_stats['total_losses'])
        if btc_wr < 0.35:
            flags.append(f"BTC win rate critically low: {btc_wr*100:.1f}%")

    if not flags:
        print("  No critical anomalies detected.")
    else:
        for f in flags:
            print(f"  [!] {f}")

    print("\n[DONE] Script completed. All statistics above are the sole source of truth.")
    print("        Do not supplement or modify these numbers in any diagnostic report.")


if __name__ == "__main__":
    main()

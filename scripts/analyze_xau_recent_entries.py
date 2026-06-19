# type: ignore
from __future__ import annotations

#!/usr/bin/env python3
"""XAU Recent Entry Quality Audit — Iron Law #11 compliant.

Statistical methodology (declared upfront):
  - Dedup: by position_ticket (first open per ticket wins)
  - Win rate: PnL > 0 = win, PnL < 0 = loss, PnL == 0 = breakeven
  - WR includes breakeven in denominator; "pure WR" excludes breakeven
  - PnL: raw PnL from journal close records, summed per ticket
  - Timeframe: default 2026-06-07 to 2026-06-09 (recent 3 days)
  - Comparison period: 2026-06-01 to 2026-06-06 (prior week)

Usage:
  python scripts/analyze_xau_recent_entries.py --data-dir data
  python scripts/analyze_xau_recent_entries.py --data-dir data --days 3
"""

import json
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path


def load_journal(data_dir: str) -> list[dict]:
    """Load all journal records from data_dir/live_trade_journal.jsonl."""
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    records = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def build_trade_ledger(records: list[dict]) -> list[dict]:
    """Build per-ticket trade ledger with strict dedup.

    Each trade = one open + (optionally) one or more closes/modifies.
    Dedup: group by position_ticket, take first open.
    """
    # Index opens by ticket
    opens_by_ticket: dict[int, dict] = {}
    closes_by_ticket: dict[int, list[dict]] = defaultdict(list)

    for rec in records:
        action = rec.get("action", "")
        ticket = rec.get("position_ticket")
        if ticket is None:
            continue
        try:
            ticket = int(ticket)
        except (ValueError, TypeError):
            continue

        if action == "open":
            if ticket not in opens_by_ticket:
                opens_by_ticket[ticket] = rec
        elif action == "close":
            closes_by_ticket[ticket].append(rec)

    # Build trades
    trades = []
    for ticket, open_rec in opens_by_ticket.items():
        closes = closes_by_ticket.get(ticket, [])
        # Sort closes by timestamp
        closes.sort(key=lambda r: r.get("recorded_at", ""))

        total_pnl = 0.0
        exit_reasons = []
        for c in closes:
            pnl = c.get("pnl")
            if pnl is not None:
                try:
                    total_pnl += float(pnl)
                except (ValueError, TypeError):
                    pass
            comment = c.get("comment", "") or ""
            label = c.get("label", "") or ""
            detail = c.get("detail", {}) or {}
            reason = detail.get("reason", "") if isinstance(detail, dict) else ""
            exit_reasons.append(
                {
                    "ts": c.get("recorded_at", ""),
                    "pnl": pnl,
                    "label": label,
                    "comment": comment,
                    "reason": reason,
                }
            )

        # Extract entry context
        entry_ctx = open_rec.get("entry_context", {}) or {}
        brain_preds = entry_ctx.get("brain_predictions", []) if isinstance(entry_ctx, dict) else []

        trade = {
            "ticket": ticket,
            "open_ts": open_rec.get("recorded_at", ""),
            "strategy": open_rec.get("strategy", ""),
            "side": open_rec.get("side", ""),
            "confidence": open_rec.get("confidence"),
            "p_win": open_rec.get("p_win"),
            "volume": open_rec.get("volume"),
            "entry_price": open_rec.get("detail", {}).get("entry_price")
            if isinstance(open_rec.get("detail"), dict)
            else None,
            "sl": open_rec.get("sl"),
            "tp": open_rec.get("tp"),
            "entry_spread": entry_ctx.get("entry_spread") if isinstance(entry_ctx, dict) else None,
            "atr": entry_ctx.get("atr") if isinstance(entry_ctx, dict) else None,
            "regime": entry_ctx.get("regime") if isinstance(entry_ctx, dict) else None,
            "trend_direction": entry_ctx.get("trend_direction")
            if isinstance(entry_ctx, dict)
            else None,
            "brain_predictions": brain_preds,
            "exit_reasons": exit_reasons,
            "total_pnl": total_pnl,
            "is_closed": len(closes) > 0,
        }
        trades.append(trade)

    trades.sort(key=lambda t: t["open_ts"])
    return trades


def classify_outcome(trade: dict) -> str:
    """Classify trade outcome: win, loss, breakeven, or open."""
    if not trade["is_closed"]:
        return "open"
    pnl = trade["total_pnl"]
    if pnl > 0:
        return "win"
    elif pnl < 0:
        return "loss"
    return "breakeven"


def filter_by_date(trades: list[dict], start: str, end: str) -> list[dict]:
    """Filter trades by open_ts date range (inclusive)."""
    return [t for t in trades if start <= t["open_ts"][:10] <= end]


def summarize(trades: list[dict], label: str) -> dict:
    """Compute summary statistics for a set of trades."""
    closed = [t for t in trades if t["is_closed"]]
    wins = [t for t in closed if t["total_pnl"] > 0]
    losses = [t for t in closed if t["total_pnl"] < 0]
    breakevens = [t for t in closed if t["total_pnl"] == 0]
    still_open = [t for t in trades if not t["is_closed"]]

    total_pnl = sum(t["total_pnl"] for t in closed)
    total_wins = len(wins)
    total_closed = len(closed)

    wr = total_wins / total_closed * 100 if total_closed else 0
    avg_win = sum(t["total_pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["total_pnl"] for t in losses) / len(losses) if losses else 0
    pf = (
        abs(sum(t["total_pnl"] for t in wins) / sum(t["total_pnl"] for t in losses))
        if losses and sum(t["total_pnl"] for t in losses) != 0
        else float("inf")
        if wins
        else 0
    )

    # Strategy breakdown
    by_strategy: dict[str, int] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in closed:
        s = t["strategy"] or "unknown"
        by_strategy[s]["count"] += 1
        by_strategy[s]["pnl"] += t["total_pnl"]
        if t["total_pnl"] > 0:
            by_strategy[s]["wins"] += 1
        elif t["total_pnl"] < 0:
            by_strategy[s]["losses"] += 1

    # Exit reason breakdown
    exit_reason_counter: dict[str, int] = Counter()
    for t in closed:
        for er in t["exit_reasons"]:
            label = er["label"] or er["reason"] or "unknown"
            exit_reason_counter[label] += 1

    # Direction breakdown
    by_direction = Counter(t["side"] for t in trades)

    # Confidence distribution
    confidences = [t["confidence"] for t in trades if t.get("confidence") is not None]

    # P_win distribution
    p_wins = [t["p_win"] for t in trades if t.get("p_win") is not None]

    # Timing: hour of day distribution
    hour_dist: dict[int, int] = Counter()
    for t in trades:
        ts = t["open_ts"]
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hour_dist[dt.hour] += 1
            except Exception:  # BLE001:REVIEWED
                pass

    # Regime at entry
    regime_dist = Counter(t.get("regime", "unknown") for t in trades)

    # Trend direction at entry vs trade direction
    trend_mismatches = 0
    trend_matches = 0
    for t in trades:
        trend = t.get("trend_direction", "")
        side = t.get("side", "")
        if trend and side:
            if (trend == "up" and side == "long") or (trend == "down" and side == "short"):
                trend_matches += 1
            elif (trend == "up" and side == "short") or (trend == "down" and side == "long"):
                trend_mismatches += 1

    return {
        "label": label,
        "total_opens": len(trades),
        "total_closed": total_closed,
        "still_open": len(still_open),
        "wins": total_wins,
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate": wr,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": pf,
        "by_strategy": dict(by_strategy),
        "by_direction": dict(by_direction),
        "exit_reasons": dict(exit_reason_counter.most_common(10)),
        "avg_confidence": sum(confidences) / len(confidences) if confidences else 0,
        "min_confidence": min(confidences) if confidences else 0,
        "max_confidence": max(confidences) if confidences else 0,
        "avg_p_win": sum(p_wins) / len(p_wins) if p_wins else 0,
        "min_p_win": min(p_wins) if p_wins else 0,
        "max_p_win": max(p_wins) if p_wins else 0,
        "hour_distribution": dict(sorted(hour_dist.items())),
        "regime_distribution": dict(regime_dist),
        "trend_matches": trend_matches,
        "trend_mismatches": trend_mismatches,
    }


def analyze_brain_predictions(trades: list[dict]) -> dict:
    """Analyze brain prediction quality at entry time."""
    brain_stats: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "correct_direction": 0,
            "wrong_direction": 0,
            "avg_confidence": 0.0,
            "confidences": [],
        }
    )

    for t in trades:
        side = t.get("side", "")
        expected = "up" if side == "long" else "down" if side == "short" else None
        if expected is None:
            continue

        for bp in t.get("brain_predictions", []):
            brain_id = bp.get("brain_id", "unknown")
            up_prob = bp.get("up_prob", 0)
            down_prob = bp.get("down_prob", 0)
            confidence = bp.get("confidence", 0)

            brain_stats[brain_id]["count"] += 1
            brain_stats[brain_id]["confidences"].append(confidence)

            # Check if brain agreed with trade direction
            if expected == "up" and up_prob > down_prob:
                brain_stats[brain_id]["correct_direction"] += 1
            elif expected == "down" and down_prob > up_prob:
                brain_stats[brain_id]["correct_direction"] += 1
            else:
                brain_stats[brain_id]["wrong_direction"] += 1

    # Compute averages
    for brain_id, stats in brain_stats.items():
        if stats["confidences"]:
            stats["avg_confidence"] = sum(stats["confidences"]) / len(stats["confidences"])
        stats["direction_agreement"] = (
            stats["correct_direction"] / stats["count"] * 100 if stats["count"] else 0
        )
        del stats["confidences"]  # cleanup

    return dict(brain_stats)


def print_summary(stats: dict) -> None:
    """Print formatted summary."""
    print(f"\n{'='*70}")
    print(f"  {stats['label']}")
    print(f"{'='*70}")
    print(f"  Total opens:       {stats['total_opens']:>6}")
    print(f"  Closed:            {stats['total_closed']:>6}")
    print(f"  Still open:        {stats['still_open']:>6}")
    print(f"  Wins:              {stats['wins']:>6}")
    print(f"  Losses:            {stats['losses']:>6}")
    print(f"  Breakevens:        {stats['breakevens']:>6}")
    print(f"  Win Rate:          {stats['win_rate']:>6.1f}%")
    print(f"  Total PnL:         {stats['total_pnl']:>+8.2f}")
    print(f"  Avg Win:           {stats['avg_win']:>+8.2f}")
    print(f"  Avg Loss:          {stats['avg_loss']:>+8.2f}")
    print(f"  Profit Factor:     {stats['profit_factor']:>8.2f}")
    print(f"  Avg Confidence:    {stats['avg_confidence']:>8.4f}")
    print(f"  Confidence range:  [{stats['min_confidence']:.4f}, {stats['max_confidence']:.4f}]")
    print(f"  Avg p_win:         {stats['avg_p_win']:>8.4f}")
    print(f"  p_win range:       [{stats['min_p_win']:.4f}, {stats['max_p_win']:.4f}]")
    print(f"  Trend matches:     {stats['trend_matches']:>6}")
    print(f"  Trend mismatches:  {stats['trend_mismatches']:>6}")
    print()
    print("  Strategy breakdown:")
    for sname, sinfo in sorted(stats["by_strategy"].items()):
        wr = sinfo["wins"] / sinfo["count"] * 100 if sinfo["count"] else 0
        print(
            f"    {sname:25s}: {sinfo['count']:3d} trades, {sinfo['wins']}W/{sinfo['losses']}L, "
            f"WR={wr:.1f}%, PnL={sinfo['pnl']:+.2f}"
        )
    print()
    print("  Direction:")
    for d, c in stats["by_direction"].items():
        print(f"    {d}: {c}")
    print()
    print("  Hour distribution (UTC):")
    for h, c in stats["hour_distribution"].items():
        bar = "#" * c
        print(f"    {h:02d}:00 — {c:3d} {bar}")
    print()
    print("  Regime at entry:")
    for r, c in stats["regime_distribution"].items():
        print(f"    {r}: {c}")
    print()
    print("  Top exit reasons:")
    for reason, c in stats["exit_reasons"].items():
        print(f"    {reason:30s}: {c}")


def print_trade_details(trades: list[dict], max_show: int = 30) -> None:
    """Print per-trade details."""
    print(f"\n{'='*120}")
    print("  Individual Trade Details (most recent first)")
    print(f"{'='*120}")
    print(
        f"  {'Open Time':<20} {'Strat':<18} {'Side':<6} {'Conf':<8} {'p_win':<8} {'PnL':>8} {'Outcome':<10} {'Exit Reasons'}"
    )
    print(f"  {'-'*118}")

    for t in reversed(trades[-max_show:]):
        outcome = classify_outcome(t)
        exit_str = (
            ", ".join(f"{er['label'] or er['reason'] or '?'}" for er in t["exit_reasons"])
            or "(still open)"
        )
        print(
            f"  {t['open_ts'][:19]:<20} "
            f"{t['strategy']:<18} "
            f"{t['side']:<6} "
            f"{t['confidence'] or '?':<8} "
            f"{t['p_win'] or '?':<8} "
            f"{t['total_pnl']:>+8.2f} "
            f"{outcome:<10} "
            f"{exit_str[:50]}"
        )


def main() -> None:
    parser = ArgumentParser(description="XAU Recent Entry Quality Audit")
    parser.add_argument("--data-dir", default="data", help="Data directory (default: data)")
    parser.add_argument("--days", type=int, default=3, help="Recent days to analyze (default: 3)")
    args = parser.parse_args()

    data_dir = args.data_dir
    days = args.days

    # Date ranges
    today = datetime.now(UTC)
    recent_start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    recent_end = today.strftime("%Y-%m-%d")
    compare_start = (today - timedelta(days=days + 6)).strftime("%Y-%m-%d")
    compare_end = (today - timedelta(days=days + 1)).strftime("%Y-%m-%d")

    print("XAU Recent Entry Quality Audit")
    print(f"  Data dir: {data_dir}")
    print(f"  Recent period: {recent_start} to {recent_end} ({days} days)")
    print(f"  Comparison period: {compare_start} to {compare_end}")

    records = load_journal(data_dir)
    print(f"  Total journal records: {len(records)}")

    trades = build_trade_ledger(records)
    print(f"  Total trades (deduped by ticket): {len(trades)}")

    # Recent period
    recent_trades = filter_by_date(trades, recent_start, recent_end)
    recent_stats = summarize(recent_trades, f"Recent ({recent_start} → {recent_end})")
    print_summary(recent_stats)

    # Comparison period
    compare_trades = filter_by_date(trades, compare_start, compare_end)
    compare_stats = summarize(compare_trades, f"Comparison ({compare_start} → {compare_end})")
    print_summary(compare_stats)

    # Delta
    print(f"\n{'='*70}")
    print("  Period-over-Period Delta")
    print(f"{'='*70}")
    for key in [
        "total_opens",
        "total_closed",
        "wins",
        "losses",
        "total_pnl",
        "win_rate",
        "avg_confidence",
        "avg_p_win",
        "profit_factor",
    ]:
        rv = recent_stats.get(key, 0)
        cv = compare_stats.get(key, 0)
        if isinstance(rv, (int, float)) and isinstance(cv, (int, float)):
            delta = rv - cv
            if key == "win_rate":
                print(f"  {key:<25s}: {rv:>8.1f}% vs {cv:>8.1f}%  (Δ={delta:+.1f}%)")
            elif key == "total_pnl":
                print(f"  {key:<25s}: {rv:>+8.2f}  vs {cv:>+8.2f}  (Δ={delta:+.2f})")
            else:
                print(f"  {key:<25s}: {rv:>8}  vs {cv:>8}  (Δ={delta:+})")

    # Individual trade details
    print_trade_details(recent_trades)

    # Brain prediction analysis
    print(f"\n{'='*70}")
    print("  Brain Prediction Quality at Entry (Recent Period)")
    print(f"{'='*70}")
    brain_stats = analyze_brain_predictions(recent_trades)
    for brain_id, stats in sorted(brain_stats.items()):
        print(
            f"  {brain_id:<35s}: {stats['count']:3d} entries, "
            f"direction_agree={stats['direction_agreement']:.0f}%, "
            f"avg_conf={stats['avg_confidence']:.4f}"
        )


if __name__ == "__main__":
    main()

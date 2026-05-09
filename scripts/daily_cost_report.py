"""Daily execution cost report — spread, commission, swap, slippage.

Reads the live trade journal and produces a per-day cost breakdown.
Outputs JSON for programmatic use and human-readable summary.

Usage: python scripts/daily_cost_report.py [--days 7]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

XAUUSD_SPREAD_COST_PER_LOT = 3.0  # ~0.30 pips × $10/pip (approximate)
XAUUSD_COMMISSION_PER_LOT = 7.0  # Round-turn commission per lot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily_cost_report")
    parser.add_argument("--journal-path", default="data/live_trade_journal.jsonl")
    parser.add_argument("--days", type=int, default=7, help="Number of days to report")
    parser.add_argument("--symbol", default="XAUUSDc")
    parser.add_argument("--output", default=None)
    return parser


def _parse_date(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "unknown"


def compute_cost_report(
    journal_path: str, *, days: int = 7, symbol: str = "XAUUSDc"
) -> dict[str, Any]:
    jp = Path(journal_path)
    if not jp.exists():
        return {"error": "journal_not_found", "path": str(jp)}

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    daily: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "open_count": 0,
            "close_count": 0,
            "modify_count": 0,
            "total_volume": 0.0,
            "estimated_spread_cost": 0.0,
            "estimated_commission": 0.0,
            "total_pnl": 0.0,
            "slippage_bps_sum": 0.0,
            "slippage_count": 0,
            "rejected_count": 0,
        }
    )

    total_opens = 0
    total_closes = 0
    total_volume = 0.0
    total_pnl = 0.0

    for line in jp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = rec.get("recorded_at", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if dt < cutoff:
            continue

        date_key = dt.strftime("%Y-%m-%d")
        action = rec.get("action", "unknown")
        ack = rec.get("ack_status", "unknown")
        vol = float(rec.get("volume", rec.get("effective_volume_hint", 0)) or 0)
        pnl = float(rec.get("pnl", 0) or 0)
        detail = rec.get("detail", {})

        if ack == "rejected":
            daily[date_key]["rejected_count"] += 1
            continue

        if action == "open":
            daily[date_key]["open_count"] += 1
            daily[date_key]["total_volume"] += vol
            total_opens += 1
            total_volume += vol
            # Estimated spread cost
            daily[date_key]["estimated_spread_cost"] += vol * XAUUSD_SPREAD_COST_PER_LOT
            daily[date_key]["estimated_commission"] += vol * XAUUSD_COMMISSION_PER_LOT
            # Slippage from detail
            if isinstance(detail, dict):
                slp = detail.get("slippage_bps", detail.get("slippage", 0))
                if slp:
                    daily[date_key]["slippage_bps_sum"] += float(slp)
                    daily[date_key]["slippage_count"] += 1

        elif action == "close":
            daily[date_key]["close_count"] += 1
            total_closes += 1
            daily[date_key]["total_pnl"] += pnl
            total_pnl += pnl

        elif action == "modify_sltp":
            daily[date_key]["modify_count"] += 1

    # Build daily summary
    daily_summary: list[dict[str, Any]] = []
    for dk in sorted(daily.keys()):
        d = daily[dk]
        net_cost = d["estimated_spread_cost"] + d["estimated_commission"]
        avg_slippage = d["slippage_bps_sum"] / max(d["slippage_count"], 1)
        daily_summary.append(
            {
                "date": dk,
                "opens": d["open_count"],
                "closes": d["close_count"],
                "modifies": d["modify_count"],
                "rejected": d["rejected_count"],
                "total_volume": round(d["total_volume"], 3),
                "estimated_spread_cost": round(d["estimated_spread_cost"], 2),
                "estimated_commission": round(d["estimated_commission"], 2),
                "total_cost": round(net_cost, 2),
                "total_pnl": round(d["total_pnl"], 2),
                "net_after_cost": round(d["total_pnl"] - net_cost, 2),
                "avg_slippage_bps": round(avg_slippage, 1),
            }
        )

    return {
        "period_days": days,
        "total_opens": total_opens,
        "total_closes": total_closes,
        "total_volume": round(total_volume, 3),
        "total_pnl": round(total_pnl, 2),
        "total_estimated_cost": round(
            total_volume * (XAUUSD_SPREAD_COST_PER_LOT + XAUUSD_COMMISSION_PER_LOT), 2
        ),
        "cost_params": {
            "spread_cost_per_lot": XAUUSD_SPREAD_COST_PER_LOT,
            "commission_per_lot": XAUUSD_COMMISSION_PER_LOT,
        },
        "daily": daily_summary,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = compute_cost_report(
        args.journal_path,
        days=args.days,
        symbol=args.symbol,
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(rendered, encoding="utf-8")
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())

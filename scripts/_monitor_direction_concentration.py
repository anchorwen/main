#!/usr/bin/env python3
"""
Direction concentration monitor — detects when all active strategies lean same way.

Usage: python scripts/_monitor_direction_concentration.py --data-dir data
Exit code: 0=balanced, 1=concentrated_warning, 2=critical_all_same

Iron Law #11 compliant — script-based, stdout is sole evidence.

Also registered as a scheduled task via ``run_monitor()`` so the
SchedulerService can invoke it every 4 hours without a subprocess.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any


def load_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def run_monitor(
    data_dir: str = "data",
    window_hours: int = 4,
    critical_ratio: float = 0.90,
    warning_ratio: float = 0.75,
    min_signals: int = 5,
) -> dict[str, Any]:
    """Run direction concentration check and return a result dict.

    Called by the scheduler service every 4 hours.  Returns a dict with
    keys ``status`` (balanced/warning/critical/insufficient), ``ratio``,
    ``dominant``, ``total_signals``, ``total_trades``, and ``detail``.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S")

    master = load_jsonl(f"{data_dir}/golden_master.jsonl")
    journal = load_jsonl(f"{data_dir}/live_trade_journal.augmented.jsonl")

    # ── Collect recent signals from golden_master ──
    gm_recent = [
        r for r in master if r.get("timestamp", "") >= cutoff or r.get("recorded_at", "") >= cutoff
    ]

    dir_counts: Counter[str] = Counter()
    by_strategy: dict[str, Counter[str]] = {}
    for r in gm_recent:
        direction: str = r.get("direction", "") or r.get("predicted_direction", "")
        strategy: str = r.get("strategy", "") or r.get("brain_id", "") or r.get("brain", "")
        if direction in ("LONG", "SHORT"):
            dir_counts[direction] += 1
            if strategy not in by_strategy:
                by_strategy[strategy] = Counter()
            by_strategy[strategy][direction] += 1

    # ── Collect actual trades from journal ──
    trades_recent = [
        e for e in journal if e.get("action") == "close" and e.get("recorded_at", "") >= cutoff
    ]

    trade_dir_counts: Counter[str] = Counter()
    for e in trades_recent:
        side = str(e.get("side", "")).upper()
        if side in ("LONG", "SHORT", "BUY", "SELL"):
            side = "LONG" if side in ("LONG", "BUY") else "SHORT"
            trade_dir_counts[side] += 1

    total = sum(dir_counts.values())
    total_trades = sum(trade_dir_counts.values())

    # ── Per-strategy breakdown ──
    strategy_detail: dict[str, dict[str, Any]] = {}
    for s in sorted(by_strategy.keys()):
        c = by_strategy[s]
        s_total = sum(c.values())
        long_pct = c.get("LONG", 0) / s_total * 100 if s_total > 0 else 0
        strategy_detail[s] = {
            "long": c.get("LONG", 0),
            "short": c.get("SHORT", 0),
            "long_pct": round(long_pct, 1),
        }

    # ── Concentration assessment ──
    if total < min_signals:
        return {
            "status": "insufficient",
            "ratio": 0,
            "dominant": "",
            "total_signals": total,
            "total_trades": total_trades,
            "window_hours": window_hours,
            "cutoff": cutoff[:19],
            "detail": strategy_detail,
        }

    max_dir = max(dir_counts.get("LONG", 0), dir_counts.get("SHORT", 0))
    dominant = "LONG" if dir_counts.get("LONG", 0) == max_dir else "SHORT"
    ratio = max_dir / total if total > 0 else 0

    if total_trades >= 3:
        max_trade_dir = max(trade_dir_counts.get("LONG", 0), trade_dir_counts.get("SHORT", 0))
        trade_ratio = max_trade_dir / total_trades if total_trades > 0 else 0
        trade_dominant = "LONG" if trade_dir_counts.get("LONG", 0) == max_trade_dir else "SHORT"
    else:
        trade_ratio = 0
        trade_dominant = ""

    if ratio >= critical_ratio or (trade_ratio >= critical_ratio and total_trades >= 3):
        status = "critical"
    elif ratio >= warning_ratio or (trade_ratio >= warning_ratio and total_trades >= 3):
        status = "warning"
    else:
        status = "balanced"

    return {
        "status": status,
        "ratio": round(ratio, 4),
        "dominant": dominant,
        "total_signals": total,
        "total_trades": total_trades,
        "trade_ratio": round(trade_ratio, 4) if total_trades >= 3 else 0,
        "trade_dominant": trade_dominant,
        "window_hours": window_hours,
        "cutoff": cutoff[:19],
        "detail": strategy_detail,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--window-hours", type=int, default=4)
    parser.add_argument("--critical-ratio", type=float, default=0.90)
    parser.add_argument("--warning-ratio", type=float, default=0.75)
    parser.add_argument("--min-signals", type=int, default=5)
    args = parser.parse_args()

    result = run_monitor(
        data_dir=args.data_dir,
        window_hours=args.window_hours,
        critical_ratio=args.critical_ratio,
        warning_ratio=args.warning_ratio,
        min_signals=args.min_signals,
    )

    detail = result.pop("detail", {})
    print("=== DIRECTION CONCENTRATION MONITOR ===")
    print(f"Window: last {result['window_hours']}h (since {result['cutoff']})")
    print(
        f"Signals: {result['total_signals']} total | "
        f"LONG={detail.get('LONG', '?')} SHORT={detail.get('SHORT', '?')}"
    )
    print(
        f"Trades:  {result['total_trades']} total | "
        f"trade_ratio={result.get('trade_ratio', 0):.0%}"
    )
    print()

    if detail:
        print(f"{'Strategy':<25} {'LONG':>6} {'SHORT':>6} {'Ratio':>8}")
        print(f"{'─'*47}")
        for s, d in sorted(detail.items()):
            if isinstance(d, dict):
                print(
                    f"{s:<25} {d.get('long',0):>6} {d.get('short',0):>6} {d.get('long_pct',0):>7.1f}%"
                )

    print()

    if result["status"] == "critical":
        print(
            f"🔴 CRITICAL: {result['ratio']:.0%} signals ({result['dominant']}), "
            f"{result['trade_ratio']:.0%} trades ({result['trade_dominant']}) "
            f"in same direction over {result['window_hours']}h"
        )
        print("   Risk: systematic directional bias → reversal kills all positions")
        sys.exit(2)
    elif result["status"] == "warning":
        print(
            f"🟡 WARNING: {result['ratio']:.0%} signals ({result['dominant']}), "
            f"{result['trade_ratio']:.0%} trades ({result['trade_dominant']}) "
            f"in same direction over {result['window_hours']}h"
        )
        sys.exit(1)
    elif result["status"] == "insufficient":
        print(
            f"⚠️  INSUFFICIENT DATA: only {result['total_signals']} signals, need {args.min_signals}"
        )
        sys.exit(0)
    else:
        print("🟢 BALANCED: direction distribution is healthy")
        sys.exit(0)


# ── Scheduler integration ──
def _scheduled_monitor() -> None:
    """Thin wrapper for the scheduler service — logs result and emits alert on critical."""
    import logging

    _logger = logging.getLogger(__name__)
    result = run_monitor(data_dir="data_btc", window_hours=4)
    status = result["status"]
    _logger.info(
        "[DIR_CONC] status=%s ratio=%.2f dominant=%s signals=%d trades=%d",
        status,
        result["ratio"],
        result["dominant"],
        result["total_signals"],
        result["total_trades"],
    )
    if status == "critical":
        try:
            from core.deployment.brain_alert import emit_brain_alert

            emit_brain_alert(
                "__system__",
                "direction_concentration_critical",
                {
                    "ratio": result["ratio"],
                    "dominant": result["dominant"],
                    "total_signals": result["total_signals"],
                    "total_trades": result["total_trades"],
                    "window_hours": result["window_hours"],
                    "detail": result.get("detail", {}),
                },
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _logger.exception("[DIR_CONC] Alert emission failed")


# Register so scheduler_service.py can discover via get_task("direction_concentration_monitor")
try:
    from core.deployment.scheduled_task_registry import register

    register("direction_concentration_monitor", _scheduled_monitor)
except (ImportError, RuntimeError):
    pass  # graceful degrade — task won't be scheduled if registry unavailable


if __name__ == "__main__":
    main()

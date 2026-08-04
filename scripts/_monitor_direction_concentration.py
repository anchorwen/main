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


# FIX-20260804-008: monitor ALL asset dirs, not just BTC.  The old hardcoded
# "data_btc" meant XAU direction concentration was never checked — the 99.7%
# SHORT collapse went completely silent.
DEFAULT_ASSET_DIRS: tuple[str, ...] = ("data", "data_btc")


def normalize_direction(value: Any) -> str:
    """Normalize a direction value to LONG/SHORT; '' for neutral/flat/unknown.

    FIX-20260804-008 defect #3: golden_master stores lowercase "short"/"long"
    while the monitor matched uppercase "SHORT" — normalized both sides.
    """
    s = str(value or "").strip().upper()
    if s in ("LONG", "BUY"):
        return "LONG"
    if s in ("SHORT", "SELL"):
        return "SHORT"
    return ""


def _extract_gm_direction(row: dict[str, Any]) -> str:
    """Extract a normalized direction from a golden_master row.

    FIX-20260804-008 defect #2: the real schema nests direction under
    ``outputs.<strategy>.direction`` (lowercase).  The old code read a top-level
    ``direction`` field that never exists → the monitor saw 0 signals and stayed
    silent (``INSUFFICIENT DATA``).
    """
    outputs = row.get("outputs")
    if isinstance(outputs, dict):
        for out in outputs.values():
            if isinstance(out, dict):
                d = normalize_direction(out.get("direction"))
                if d:
                    return d
    # legacy fallback for non-outputs rows
    for key in ("direction", "predicted_direction", "bias"):
        d = normalize_direction(row.get(key))
        if d:
            return d
    return ""


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
    # FIX-20260804-008 defect #2 (time filter): golden_master timestamps live in
    # ``timestamp_utc`` — the old filter read ``timestamp``/``recorded_at`` which
    # never exist on those rows → the monitor always saw 0 signals and stayed
    # silently ``insufficient``.
    gm_recent = [
        r
        for r in master
        if r.get("timestamp_utc", "") >= cutoff
        or r.get("timestamp", "") >= cutoff
        or r.get("recorded_at", "") >= cutoff
    ]

    dir_counts: Counter[str] = Counter()
    by_strategy: dict[str, Counter[str]] = {}
    for r in gm_recent:
        outputs = r.get("outputs")
        if isinstance(outputs, dict) and outputs:
            # golden_master nests per-strategy outputs — count each strategy vote
            for strat_name, out in outputs.items():
                if not isinstance(out, dict):
                    continue
                direction = normalize_direction(out.get("direction"))
                if not direction:
                    continue
                dir_counts[direction] += 1
                if strat_name not in by_strategy:
                    by_strategy[strat_name] = Counter()
                by_strategy[strat_name][direction] += 1
        else:
            direction = _extract_gm_direction(r)
            strategy = str(r.get("strategy", "") or r.get("brain_id", "") or r.get("brain", ""))
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
            "signals_long": dir_counts.get("LONG", 0),
            "signals_short": dir_counts.get("SHORT", 0),
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
        "signals_long": dir_counts.get("LONG", 0),
        "signals_short": dir_counts.get("SHORT", 0),
        "trade_ratio": round(trade_ratio, 4) if total_trades >= 3 else 0,
        "trade_dominant": trade_dominant,
        "window_hours": window_hours,
        "cutoff": cutoff[:19],
        "detail": strategy_detail,
    }


def _report_dir(data_dir: str, result: dict[str, Any], min_signals: int) -> str:
    """Print a per-asset report. Returns the status (worst-wins by caller)."""
    detail = result.get("detail", {})
    print(f"=== DIRECTION CONCENTRATION MONITOR [{data_dir}] ===")
    print(f"Window: last {result['window_hours']}h (since {result['cutoff']})")
    print(
        f"Signals: {result['total_signals']} total | "
        f"LONG={result.get('signals_long', 0)} SHORT={result.get('signals_short', 0)}"
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
    elif result["status"] == "warning":
        print(
            f"🟡 WARNING: {result['ratio']:.0%} signals ({result['dominant']}), "
            f"{result['trade_ratio']:.0%} trades ({result['trade_dominant']}) "
            f"in same direction over {result['window_hours']}h"
        )
    elif result["status"] == "insufficient":
        print(f"⚠️  INSUFFICIENT DATA: only {result['total_signals']} signals, need {min_signals}")
    else:
        print("🟢 BALANCED: direction distribution is healthy")
    return result["status"]


def main() -> None:
    # Windows GBK console can't encode emoji status markers — force UTF-8 output.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--data-dirs",
        nargs="*",
        default=None,
        help=f"Monitor multiple asset data dirs (overrides --data-dir). "
        f"Default scheduled scope: {', '.join(DEFAULT_ASSET_DIRS)}",
    )
    parser.add_argument("--window-hours", type=int, default=4)
    parser.add_argument("--critical-ratio", type=float, default=0.90)
    parser.add_argument("--warning-ratio", type=float, default=0.75)
    parser.add_argument("--min-signals", type=int, default=5)
    args = parser.parse_args()

    data_dirs: list[str] = args.data_dirs if args.data_dirs else [args.data_dir]

    # FIX-20260804-008: aggregate across all assets — worst status wins
    rank = {"balanced": 0, "insufficient": 0, "warning": 1, "critical": 2}
    worst: str = "balanced"
    for data_dir in data_dirs:
        result = run_monitor(
            data_dir=data_dir,
            window_hours=args.window_hours,
            critical_ratio=args.critical_ratio,
            warning_ratio=args.warning_ratio,
            min_signals=args.min_signals,
        )
        status = _report_dir(data_dir, result, args.min_signals)
        if rank[status] > rank[worst]:
            worst = status
        print()

    if worst == "critical":
        sys.exit(2)
    elif worst == "warning":
        sys.exit(1)
    else:
        sys.exit(0)


# ── Scheduler integration ──
def _scheduled_monitor() -> str:
    """Scheduler wrapper — checks ALL asset dirs, logs per-asset, alerts on critical.

    FIX-20260804-008 defect #1: hardcoded ``data_dir="data_btc"`` meant XAU was
    never monitored.  Now every configured asset dir is checked (worst wins).
    Returns the worst status (for testability).
    """
    import logging

    _logger = logging.getLogger(__name__)
    rank = {"balanced": 0, "insufficient": 0, "warning": 1, "critical": 2}
    worst: str = "balanced"
    for data_dir in DEFAULT_ASSET_DIRS:
        result = run_monitor(data_dir=data_dir, window_hours=4)
        status = result["status"]
        _logger.info(
            "[DIR_CONC] asset=%s status=%s ratio=%.2f dominant=%s signals=%d trades=%d",
            data_dir,
            status,
            result["ratio"],
            result["dominant"],
            result["total_signals"],
            result["total_trades"],
        )
        if rank[status] > rank[worst]:
            worst = status
        if status == "critical":
            try:
                from core.deployment.brain_alert import emit_brain_alert

                emit_brain_alert(
                    "__system__",
                    "direction_concentration_critical",
                    {
                        "asset_dir": data_dir,
                        "ratio": result["ratio"],
                        "dominant": result["dominant"],
                        "total_signals": result["total_signals"],
                        "total_trades": result["total_trades"],
                        "signals_long": result.get("signals_long", 0),
                        "signals_short": result.get("signals_short", 0),
                        "window_hours": result["window_hours"],
                        "detail": result.get("detail", {}),
                    },
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                _logger.exception("[DIR_CONC] Alert emission failed")
    return worst


# Register so scheduler_service.py can discover via get_task("direction_concentration_monitor")
try:
    from core.deployment.scheduled_task_registry import register

    register("direction_concentration_monitor", _scheduled_monitor)
except (ImportError, RuntimeError):
    pass  # graceful degrade — task won't be scheduled if registry unavailable


if __name__ == "__main__":
    main()

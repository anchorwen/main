#!/usr/bin/env python3
"""Data Health Check — unified data integrity monitoring for live trading.

Run from repo root:
  python scripts/run_data_health.py --base-dir data_btc --symbol BTCUSDc
  python scripts/run_data_health.py --base-dir data_btc --symbol BTCUSDc --mode light
  python scripts/run_data_health.py --base-dir data_btc --symbol BTCUSDc --output report.json

Intended for:
  - Manual invocation from CLI
  - Scheduled task / cron (exit codes: 0=OK, 1=WARNING, 2=CRITICAL)
  - Integration into daily_ops pipeline

Iron Law for Monitoring #3 (Decoupling):
  This script generates a report ONLY.  It does NOT send alerts.
  Alert routing is the caller's responsibility.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Data Health Check — unified data integrity monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-dir", default="data_btc", help="Data directory (default: data_btc)")
    p.add_argument("--symbol", default="BTCUSDc", help="Trading symbol (default: BTCUSDc)")
    p.add_argument(
        "--mode",
        choices=("full", "light"),
        default="full",
        help="Check mode: full (all checks + cross-source) or light (CRITICAL only, <50ms)",
    )
    p.add_argument("--output", "-o", default=None, help="Write JSON report to file")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI color codes")
    p.add_argument("--summary-only", action="store_true", help="Print summary only, skip per-source details")
    return p


def run_data_health(
    base_dir: str = "data_btc",
    symbol: str = "BTCUSDc",
    mode: str = "full",
) -> dict[str, Any]:
    """Run health check and return JSON-serializable report dict.

    This is the public API entry point for scheduled_task_registry or
    external callers.
    """
    from core.observability.data_health_service import DataHealthService

    svc = DataHealthService(base_dir=base_dir, symbol=symbol, mode=mode)
    if mode == "light":
        report = svc.run_lightweight()
    else:
        report = svc.run_full()
    svc.save_health_state(report)

    # Serialize to dict for JSON output
    return {
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "base_dir": report.base_dir,
        "symbol": report.symbol,
        "mode": mode,
        "alert_level": report.alert_level,
        "elapsed_ms": report.elapsed_ms,
        "aggregated": report.aggregated,
        "primary_codes": report.primary_codes,
        "sources": [
            {
                "source": s.source,
                "tier": s.tier.value,
                "status": s.status.value,
                "primary_code": s.primary_code,
                "message": s.message,
                "metrics": s.metrics,
                "checked_at": s.checked_at,
            }
            for s in report.sources
        ],
        "cross_checks": [
            {
                "check_name": c.check_name,
                "status": c.status.value,
                "primary_code": c.primary_code,
                "message": c.message,
                "metrics": c.metrics,
            }
            for c in report.cross_checks
        ],
        "orphans": [
            {"source_path": o.source_path, "pattern": o.pattern, "detail": o.detail}
            for o in report.orphans
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = run_data_health(base_dir=args.base_dir, symbol=args.symbol, mode=args.mode)

    # JSON output
    json_out = json.dumps(result, ensure_ascii=False, default=str, indent=2)

    if args.output:
        Path(args.output).write_text(json_out, encoding="utf-8")
        print(f"Report written to {args.output}")

    # Terminal summary
    RED = "" if args.no_color else "\033[91m"
    YELLOW = "" if args.no_color else "\033[93m"
    GREEN = "" if args.no_color else "\033[92m"
    RESET = "" if args.no_color else "\033[0m"

    level_color = {"OK": GREEN, "WARNING": YELLOW, "CRITICAL": RED}
    color = level_color.get(result["alert_level"], "")

    print(f"\n{color}Data Health Report — {result['alert_level']}{RESET}")
    print(f"  Symbol: {result['symbol']} | Mode: {result['mode']} | "
          f"Elapsed: {result['elapsed_ms']}ms")
    agg = result["aggregated"]
    print(f"  Sources: {agg['total_sources']} checked "
          f"({agg['pass_count']} pass, {agg['warn_count']} warn, "
          f"{agg['fail_count']} fail, {agg['missing_count']} missing)")
    if result["cross_checks"]:
        cross_warn = sum(1 for c in result["cross_checks"] if c["status"] != "pass")
        print(f"  Cross-checks: {len(result['cross_checks'])} ({cross_warn} warnings)")
    if result["orphans"]:
        print(f"  {RED}Orphans detected: {len(result['orphans'])}{RESET}")

    if not args.summary_only:
        print(f"\n  Source details:")
        for s in result["sources"]:
            status_color = {"pass": GREEN, "warn": YELLOW, "fail": RED, "missing": RED, "skipped": ""}
            sc = status_color.get(s["status"], "")
            print(f"    {sc}{s['status']:7s}{RESET} {s['source']:24s} — {s['primary_code']}")
            if s["message"]:
                print(f"           {s['message'][:150]}")

        if result["cross_checks"]:
            print(f"\n  Cross-source validation:")
            for c in result["cross_checks"]:
                sc = status_color.get(c["status"], "")
                print(f"    {sc}{c['status']:7s}{RESET} {c['check_name']} — {c['message'][:120]}")

        if result["orphans"]:
            print(f"\n  {RED}Orphan subsystems:{RESET}")
            for o in result["orphans"]:
                print(f"    - {o['source_path']}: {o['detail']}")

    if result["primary_codes"]:
        print(f"\n  Primary codes: {', '.join(result['primary_codes'])}")

    # Exit code
    if result["alert_level"] == "CRITICAL":
        return 2
    elif result["alert_level"] == "WARNING":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

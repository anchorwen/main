"""Feature store maintenance: incremental update, compaction, and stats.

Designed to be called independently — via CLI, Windows Task Scheduler, or cron.
Also callable from SchedulerService periodic tasks and daily_ops pipeline.

Usage:
  # Full maintenance (update + compact + stats)
  python scripts/feature_store_maintenance.py --base-dir data

  # Incremental update only (fetch new MT5 bars since last stored record)
  python scripts/feature_store_maintenance.py --base-dir data --update-only

  # Compact only (dedup + trim records older than 90 days)
  python scripts/feature_store_maintenance.py --base-dir data --compact-only --retention-days 90

  # Dry-run (report what would change without writing)
  python scripts/feature_store_maintenance.py --base-dir data --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "feature_store_maintenance.v1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0).isoformat()


def _resolve_store_path(base_dir: str | Path, feature_store_dir: str | None = None) -> Path:
    base = Path(base_dir)
    if feature_store_dir:
        p = Path(feature_store_dir)
        return p if p.is_absolute() else base / p
    return base / "feature_store"


def run_incremental_update(
    store_dir: str,
    symbol: str = "XAUUSDc",
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Fetch new MT5 features since the last stored record and write them."""
    from core.deployment.feature_update_producer import (
        build_v9_schema,
        produce_from_live_computer,
    )
    from core.features.computers.v9_live_computer import V9LiveFeatureComputer
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.update_job import IncrementalFeatureUpdateJob

    store = LocalFeatureStore(store_dir)
    computer = None  # mt5 is imported on-demand via MetaTrader5 below

    if computer is None:
        if not mt5_terminal_path:
            return {"step": "incremental_update", "status": "skipped", "reason": "no_mt5_path"}
        import MetaTrader5 as _mt5

        if not _mt5.initialize(path=mt5_terminal_path):
            return {
                "step": "incremental_update",
                "status": "error",
                "error": f"MT5 initialize failed: {_mt5.last_error()}",
            }
        computer = V9LiveFeatureComputer(_mt5, symbol)

    try:
        schema = build_v9_schema(symbol, "M5")
        job = IncrementalFeatureUpdateJob(
            feature_store=store,
            producer=lambda _: produce_from_live_computer(computer, schema, symbol),
            schema=schema,
        )
        result = job.run()
        return {
            "step": "incremental_update",
            "status": "ok",
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "records_written": result.records_written,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
        }
    except Exception as exc:  # BLE001:REVIEWED
        return {"step": "incremental_update", "status": "error", "error": str(exc)[:500]}
    finally:
        if computer is not None and hasattr(computer, "_mt5"):
            try:  # noqa: SIM105
                computer._mt5.shutdown()
            except Exception:  # BLE001:REVIEWED
                pass


def run_compaction(
    store_dir: str,
    *,
    retention_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dedup and optionally trim old records from the feature store."""
    from core.features.local_feature_store import LocalFeatureStore

    store = LocalFeatureStore(store_dir)
    try:
        results = store.compact(retention_days=retention_days, dry_run=dry_run)
        total_before = sum(r["before"] for r in results.values())
        total_after = sum(r["after"] for r in results.values())
        total_dupes = sum(r["duplicates_removed"] for r in results.values())
        total_trimmed = sum(r["trimmed_by_retention"] for r in results.values())
        return {
            "step": "compaction",
            "status": "ok",
            "dry_run": dry_run,
            "partitions": len(results),
            "records_before": total_before,
            "records_after": total_after,
            "duplicates_removed": total_dupes,
            "trimmed_by_retention": total_trimmed,
            "per_partition": results,
        }
    except Exception as exc:  # BLE001:REVIEWED
        return {"step": "compaction", "status": "error", "error": str(exc)[:500]}


def run_stats(store_dir: str) -> dict[str, Any]:
    """Collect feature store statistics."""
    from core.features.local_feature_store import LocalFeatureStore

    store = LocalFeatureStore(store_dir)
    try:
        stats = store.stats()
        total_records = sum(s["record_count"] for s in stats.values())
        total_bytes = sum(s["file_size_bytes"] for s in stats.values())
        partitions = list(stats.keys())
        return {
            "step": "stats",
            "status": "ok",
            "total_records": total_records,
            "total_file_size_bytes": total_bytes,
            "total_file_size_mb": round(total_bytes / (1024 * 1024), 2),
            "partition_count": len(stats),
            "partitions": partitions,
            "per_partition": stats,
        }
    except Exception as exc:  # BLE001:REVIEWED
        return {"step": "stats", "status": "error", "error": str(exc)[:500]}


def run_full_maintenance(
    base_dir: str = "data",
    *,
    symbol: str = "XAUUSDc",
    mt5_terminal_path: str | None = None,
    feature_store_dir: str | None = None,
    retention_days: int | None = 90,
    skip_update: bool = False,
    skip_compact: bool = False,
    skip_stats: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full feature store maintenance pipeline."""
    steps: list[dict[str, Any]] = []
    store_dir = str(_resolve_store_path(base_dir, feature_store_dir))

    if not skip_update:
        steps.append(
            run_incremental_update(store_dir, symbol=symbol, mt5_terminal_path=mt5_terminal_path)
        )

    if not skip_compact:
        steps.append(run_compaction(store_dir, retention_days=retention_days, dry_run=dry_run))

    if not skip_stats:
        steps.append(run_stats(store_dir))

    errors = [s for s in steps if s.get("status") == "error"]
    skipped = [s for s in steps if s.get("status") == "skipped"]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "base_dir": base_dir,
        "store_dir": store_dir,
        "dry_run": dry_run,
        "total_steps": len(steps),
        "errors": len(errors),
        "skipped": len(skipped),
        "steps": steps,
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feature_store_maintenance")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--symbol", default="XAUUSDc", help="Trading symbol")
    p.add_argument("--mt5-terminal-path", default=None, help="MT5 terminal64.exe path")
    p.add_argument("--feature-store-dir", default=None, help="Feature store directory override")
    p.add_argument("--retention-days", type=int, default=90, help="Trim records older than N days")
    p.add_argument("--update-only", action="store_true", help="Incremental update only")
    p.add_argument("--compact-only", action="store_true", help="Compaction only")
    p.add_argument("--stats-only", action="store_true", help="Stats only")
    p.add_argument("--skip-update", action="store_true", help="Skip incremental update")
    p.add_argument("--skip-compact", action="store_true", help="Skip compaction")
    p.add_argument("--skip-stats", action="store_true", help="Skip stats")
    p.add_argument("--dry-run", action="store_true", help="Report without writing changes")
    p.add_argument("--output", type=Path, default=None, help="Write report JSON to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    store_dir = str(_resolve_store_path(args.base_dir, args.feature_store_dir))

    if args.update_only:
        step = run_incremental_update(
            store_dir, symbol=args.symbol, mt5_terminal_path=args.mt5_terminal_path
        )
        report = {"schema_version": SCHEMA_VERSION, "generated_at": _utc_now_iso(), "steps": [step]}
    elif args.compact_only:
        step = run_compaction(store_dir, retention_days=args.retention_days, dry_run=args.dry_run)
        report = {"schema_version": SCHEMA_VERSION, "generated_at": _utc_now_iso(), "steps": [step]}
    elif args.stats_only:
        step = run_stats(store_dir)
        report = {"schema_version": SCHEMA_VERSION, "generated_at": _utc_now_iso(), "steps": [step]}
    else:
        report = run_full_maintenance(
            base_dir=args.base_dir,
            symbol=args.symbol,
            mt5_terminal_path=args.mt5_terminal_path,
            feature_store_dir=args.feature_store_dir,
            retention_days=args.retention_days,
            skip_update=args.skip_update,
            skip_compact=args.skip_compact,
            skip_stats=args.skip_stats,
            dry_run=args.dry_run,
        )

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    errors_val = report.get("errors", 0)
    if isinstance(errors_val, int) and errors_val > 0:
        return 2
    return 0


try:
    from core.deployment.scheduled_task_registry import register

    register("feature_store_maintenance", run_full_maintenance)
except ImportError:
    pass

if __name__ == "__main__":
    raise SystemExit(main())

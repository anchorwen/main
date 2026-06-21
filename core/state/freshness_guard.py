"""Freshness Guard — the detection layer of the State Governance Protocol.

Phase 4 of Plan B: monitors all registered state artifacts and raises
CRITICAL alerts when any file exceeds its declared TTL, is empty (0 bytes),
or goes missing entirely.

Architecture:
    Catalog (declares TTL) → Freshness Guard (checks age) → CRITICAL log

This is the "Failure Detection" complement to the "Error Prevention"
layers (Catalog + Write Gate + Atomic Write). Together:
    Layer 1: Data Catalog     — what exists, what shape it must have
    Layer 2: Write Gate       — validated BEFORE any byte touches disk
    Layer 3: Freshness Guard  — detect staleness / emptiness after write
    Layer 4: Cross-Symbol Guard — prevent cross-symbol pollution

This layer would have caught the "45-day XAU vacuum" within < 24 hours
instead of letting it fester unnoticed for 6 weeks.

Usage::

    # Programmatic
    from core.state.freshness_guard import check_catalog_freshness
    result = check_catalog_freshness()
    if result["stale"]:
        for entry in result["stale"]:
            logger.critical("Stale artifact: %s", entry)

    # CLI (outputs JSON to stdout, CRITICAL lines to stderr)
    python core/state/freshness_guard.py
    python core/state/freshness_guard.py --data-dirs data,data_btc

See Also:
    - Catalog:  core/state/catalog.py
    - Writer:   core/state/writer.py
    - DQAF-046: XAU dual-track feature pipeline (freshness was the gap)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class FreshnessEntry:
    """One freshness check result for a single artifact + data directory."""

    artifact_id: str
    symbol: str
    data_dir: str
    path: str
    status: str  # "healthy" | "stale" | "missing" | "empty" | "skipped"
    age_seconds: float = 0.0
    ttl_seconds: int = 0
    file_size_bytes: int = 0
    mtime_iso: str = ""


def _utc_iso_from_timestamp(ts: float) -> str:
    """Convert a Unix timestamp to ISO 8601 UTC string."""
    from datetime import UTC, datetime

    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None).isoformat()


def _fmt_age(seconds: float) -> str:
    """Human-readable age string."""
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# ═══════════════════════════════════════════════════════════════════════════════
# Core check
# ═══════════════════════════════════════════════════════════════════════════════

# Symbols we expect to find under each data directory.
_SYMBOL_FOR_DIR = {
    "data": "XAUUSDc",
    "data_btc": "BTCUSDc",
}


def _resolve_symbol(data_dir: str) -> str:
    """Derive the trading symbol from the data directory name."""
    # Use the basename, not the full path, so both "data" and "/abs/path/data" work.
    dir_name = Path(data_dir).name
    return _SYMBOL_FOR_DIR.get(dir_name, dir_name.upper())


def check_artifact_freshness(
    artifact: Any,  # StateArtifact (lazy import to avoid circular deps)
    data_dir: str | Path,
    now: float | None = None,
) -> FreshnessEntry:
    """Check a single artifact's freshness in one data directory.

    Args:
        artifact: A ``StateArtifact`` from the catalog.
        data_dir: Path to the symbol's data directory.
        now: Current Unix timestamp (injectable for testing).

    Returns:
        A ``FreshnessEntry`` with the check result.
    """
    if now is None:
        now = time.time()

    data_dir_path = Path(data_dir).resolve()
    target_path = data_dir_path / artifact.path_template
    symbol = _resolve_symbol(str(data_dir))

    # ── TTL of 0 means "skip freshness check" ──
    if artifact.ttl_seconds <= 0:
        return FreshnessEntry(
            artifact_id=artifact.logical_id,
            symbol=symbol,
            data_dir=str(data_dir_path),
            path=str(target_path),
            status="skipped",
            ttl_seconds=0,
        )

    # ── File does not exist ──
    if not target_path.exists():
        return FreshnessEntry(
            artifact_id=artifact.logical_id,
            symbol=symbol,
            data_dir=str(data_dir_path),
            path=str(target_path),
            status="missing",
            ttl_seconds=artifact.ttl_seconds,
        )

    # ── File exists — check size and age ──
    try:
        stat = target_path.stat()
    except OSError:
        return FreshnessEntry(
            artifact_id=artifact.logical_id,
            symbol=symbol,
            data_dir=str(data_dir_path),
            path=str(target_path),
            status="missing",  # unreadable → treat as missing
            ttl_seconds=artifact.ttl_seconds,
        )

    file_size = stat.st_size
    mtime = stat.st_mtime
    age = now - mtime

    # ── 0-byte file — CRITICAL: corruption or failed write ──
    if file_size == 0:
        return FreshnessEntry(
            artifact_id=artifact.logical_id,
            symbol=symbol,
            data_dir=str(data_dir_path),
            path=str(target_path),
            status="empty",
            age_seconds=age,
            ttl_seconds=artifact.ttl_seconds,
            file_size_bytes=0,
            mtime_iso=_utc_iso_from_timestamp(mtime),
        )

    # ── Stale check ──
    if age > artifact.ttl_seconds:
        return FreshnessEntry(
            artifact_id=artifact.logical_id,
            symbol=symbol,
            data_dir=str(data_dir_path),
            path=str(target_path),
            status="stale",
            age_seconds=age,
            ttl_seconds=artifact.ttl_seconds,
            file_size_bytes=file_size,
            mtime_iso=_utc_iso_from_timestamp(mtime),
        )

    # ── Healthy ──
    return FreshnessEntry(
        artifact_id=artifact.logical_id,
        symbol=symbol,
        data_dir=str(data_dir_path),
        path=str(target_path),
        status="healthy",
        age_seconds=age,
        ttl_seconds=artifact.ttl_seconds,
        file_size_bytes=file_size,
        mtime_iso=_utc_iso_from_timestamp(mtime),
    )


def check_catalog_freshness(
    data_dirs: list[str] | None = None,
    *,
    now: float | None = None,
    emit_alerts: bool = True,
) -> dict[str, Any]:
    """Check all catalog artifacts for freshness across all data directories.

    This is the main entry point.  It iterates every registered
    ``StateArtifact``, resolves its path under each configured data
    directory, and classifies it as healthy / stale / missing / empty.

    Args:
        data_dirs: List of data directory paths to scan.
                   Defaults to ``["data", "data_btc"]``.
        now: Current Unix timestamp (injectable for testing).
        emit_alerts: If True, print CRITICAL/WARNING lines to stderr.

    Returns:
        Dict with keys:
        - ``checked_at_utc``: ISO 8601 timestamp of the check
        - ``total``: total number of checks performed
        - ``healthy``: list of healthy FreshnessEntry dicts
        - ``stale``: list of stale FreshnessEntry dicts (CRITICAL)
        - ``missing``: list of missing FreshnessEntry dicts (WARNING)
        - ``empty``: list of empty-file FreshnessEntry dicts (CRITICAL)
        - ``skipped``: list of skipped FreshnessEntry dicts (TTL=0)
        - ``summary``: one-line human-readable summary
    """
    # Lazy import to avoid circular dependency at module level
    from core.state.catalog import CATALOG

    if now is None:
        now = time.time()

    if data_dirs is None:
        data_dirs = ["data", "data_btc"]

    results: dict[str, list[FreshnessEntry]] = {
        "healthy": [],
        "stale": [],
        "missing": [],
        "empty": [],
        "skipped": [],
    }

    for artifact in CATALOG.values():
        for data_dir in data_dirs:
            data_dir_path = Path(data_dir)
            if not data_dir_path.is_dir():
                continue  # data directory not configured for this symbol

            entry = check_artifact_freshness(artifact, data_dir, now=now)
            results[entry.status].append(entry)

    # ── Emit alerts ──
    if emit_alerts:
        _emit_alerts(results)

    # ── Build return dict ──
    total = sum(len(v) for v in results.values())

    def _entry_dict(e: FreshnessEntry) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_id": e.artifact_id,
            "symbol": e.symbol,
            "data_dir": e.data_dir,
            "path": e.path,
            "status": e.status,
        }
        if e.status in ("healthy", "stale"):
            d["age_seconds"] = e.age_seconds
            d["age_human"] = _fmt_age(e.age_seconds)
            d["ttl_seconds"] = e.ttl_seconds
            d["file_size_bytes"] = e.file_size_bytes
            d["mtime_iso"] = e.mtime_iso
        if e.status == "stale":
            d["overdue_seconds"] = e.age_seconds - e.ttl_seconds
            d["overdue_human"] = _fmt_age(e.age_seconds - e.ttl_seconds)
        if e.status == "empty":
            d["age_seconds"] = e.age_seconds
        return d

    stale_count = len(results["stale"])
    missing_count = len(results["missing"])
    empty_count = len(results["empty"])

    summary_parts = [f"{len(results['healthy'])} healthy"]
    if stale_count:
        summary_parts.append(f"{stale_count} STALE")
    if missing_count:
        summary_parts.append(f"{missing_count} MISSING")
    if empty_count:
        summary_parts.append(f"{empty_count} EMPTY")
    summary_parts.append(f"{len(results['skipped'])} skipped")

    return {
        "checked_at_utc": _utc_iso_from_timestamp(now),
        "total": total,
        "healthy": [_entry_dict(e) for e in results["healthy"]],
        "stale": [_entry_dict(e) for e in results["stale"]],
        "missing": [_entry_dict(e) for e in results["missing"]],
        "empty": [_entry_dict(e) for e in results["empty"]],
        "skipped": [_entry_dict(e) for e in results["skipped"]],
        "summary": ", ".join(summary_parts),
    }


def _emit_alerts(results: dict[str, list[FreshnessEntry]]) -> None:
    """Emit structured alert lines to stderr.

    - EMPTY files  → CRITICAL (data corruption / failed atomic write)
    - STALE files  → CRITICAL (pipeline stall — upstream stopped producing)
    - MISSING files → WARNING  (may be expected for single-symbol deployments)
    """
    for entry in results["empty"]:
        print(
            json.dumps(
                {
                    "level": "CRITICAL",
                    "guard": "freshness",
                    "event": "state_file_empty",
                    "artifact_id": entry.artifact_id,
                    "symbol": entry.symbol,
                    "path": entry.path,
                    "age_seconds": entry.age_seconds,
                    "message": (
                        f"CRITICAL: {entry.artifact_id} ({entry.symbol}) "
                        f"is EMPTY (0 bytes) — possible write failure or corruption"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    for entry in results["stale"]:
        overdue = entry.age_seconds - entry.ttl_seconds
        print(
            json.dumps(
                {
                    "level": "CRITICAL",
                    "guard": "freshness",
                    "event": "state_file_stale",
                    "artifact_id": entry.artifact_id,
                    "symbol": entry.symbol,
                    "path": entry.path,
                    "age_seconds": round(entry.age_seconds, 1),
                    "age_human": _fmt_age(entry.age_seconds),
                    "ttl_seconds": entry.ttl_seconds,
                    "overdue_seconds": round(overdue, 1),
                    "message": (
                        f"CRITICAL: {entry.artifact_id} ({entry.symbol}) "
                        f"is {_fmt_age(entry.age_seconds)} old "
                        f"(TTL={_fmt_age(entry.ttl_seconds)}) — "
                        f"upstream generator may be stalled"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    for entry in results["missing"]:
        print(
            json.dumps(
                {
                    "level": "WARNING",
                    "guard": "freshness",
                    "event": "state_file_missing",
                    "artifact_id": entry.artifact_id,
                    "symbol": entry.symbol,
                    "path": entry.path,
                    "message": (
                        f"WARNING: {entry.artifact_id} ({entry.symbol}) "
                        f"not found at {entry.path}"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """Run freshness check from the command line.

    Returns exit code: 0 if all healthy, 1 if any stale/empty/missing.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Freshness Guard — check all state artifacts for staleness",
    )
    parser.add_argument(
        "--data-dirs",
        default="data,data_btc",
        help="Comma-separated list of data directories (default: data,data_btc)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=True,
        help="Output result as JSON to stdout (default)",
    )
    parser.add_argument(
        "--no-json",
        dest="json_output",
        action="store_false",
        help="Output human-readable summary instead of JSON",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr alerts (only output JSON to stdout)",
    )
    args = parser.parse_args(argv)

    data_dirs = [d.strip() for d in args.data_dirs.split(",") if d.strip()]

    result = check_catalog_freshness(
        data_dirs=data_dirs,
        emit_alerts=not args.quiet,
    )

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Freshness Guard  --  {result['checked_at_utc']}")
        print(f"  {result['summary']}")
        for entry in result["stale"]:
            print(f"  [STALE]  {entry['artifact_id']:30s} {entry['symbol']:8s}  {entry['age_human']:>8s} old (TTL={entry['ttl_seconds']}s)")
        for entry in result["empty"]:
            print(f"  [EMPTY]  {entry['artifact_id']:30s} {entry['symbol']:8s}  {entry['path']}")
        for entry in result["missing"]:
            print(f"  [MISS]   {entry['artifact_id']:30s} {entry['symbol']:8s}  {entry['path']}")

    has_issues = bool(result["stale"] or result["empty"])
    return 1 if has_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

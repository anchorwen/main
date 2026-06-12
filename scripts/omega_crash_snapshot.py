#!/usr/bin/env python
"""Crash forensics — auto-capture evidence on system failure.

FIX-20260612-002: Phase 3 minimum viable of Systemic Operating System.
When the trading system crashes, capture a standardized evidence snapshot
before exit.  A cron/daemon can then pick up the snapshot and auto-run
dqaf_collect.py for human review.

Usage (in live_intent_loop / live_launcher crash handler)::

    from scripts.omega_crash_snapshot import capture_crash_snapshot
    try:
        main_loop()
    except Exception:
        capture_crash_snapshot("data", "live_intent_loop", sys.exc_info())
        raise
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def capture_crash_snapshot(
    base_dir: str,
    component: str,
    exc_info: Any = None,
    *,
    extra_context: dict[str, Any] | None = None,
) -> Path:
    """Capture a standardized crash evidence snapshot.

    Writes to ``{base_dir}/dqaf/crash_{timestamp}.json``.

    Args:
        base_dir: Data directory (e.g. "data", "data_btc").
        component: Which component crashed (e.g. "live_intent_loop").
        exc_info: sys.exc_info() tuple, or None.
        extra_context: Additional key-value pairs to include.

    Returns:
        Path to the snapshot file.
    """
    dqaf_dir = Path(base_dir) / "dqaf"
    dqaf_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
    snapshot_path = dqaf_dir / f"crash_{ts}.json"

    snapshot: dict[str, Any] = {
        "schema_version": "crash_snapshot.v1",
        "timestamp_utc": _utc_iso(),
        "component": component,
        "base_dir": str(base_dir),
        "hostname": os.environ.get("COMPUTERNAME", "unknown"),
        "pid": os.getpid(),
    }

    # ── Exception details ──
    if exc_info and len(exc_info) >= 3:
        exc_type, exc_value, exc_tb = exc_info
        snapshot["exception"] = {
            "type": exc_type.__name__ if exc_type else "Unknown",
            "message": str(exc_value) if exc_value else "",
            "traceback": "".join(traceback.format_exception(*exc_info)),
        }
    elif exc_info:
        snapshot["exception"] = {"raw": str(exc_info)}

    # ── Capture key state files (last N bytes) ──
    state_files = {
        "execution_state": "state/execution_state.json",
        "data_health_state": "state/data_health_state.json",
        "bar_sync_state": "bar_sync_state.json",
        "governance_state": "governance_state.json",
    }
    state_snapshots: dict[str, Any] = {}
    for name, rel_path in state_files.items():
        full_path = Path(base_dir) / rel_path
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                # Truncate to last 4KB
                if len(content) > 4096:
                    content = "...(truncated)\n" + content[-4096:]
                state_snapshots[name] = json.loads(content) if content.strip().startswith("{") else content[:2000]
            except Exception:
                state_snapshots[name] = "(unreadable)"
    snapshot["state_snapshots"] = state_snapshots

    # ── Extra context ──
    if extra_context:
        snapshot["extra_context"] = extra_context

    # ── Write atomically ──
    tmp_path = snapshot_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(str(tmp_path), str(snapshot_path))

    print(f"[Ω-CRASH] Evidence snapshot saved: {snapshot_path}", flush=True)
    return snapshot_path


def check_pending_crashes(base_dir: str) -> list[Path]:
    """Check for unprocessed crash snapshots.  Returns list of paths."""
    dqaf_dir = Path(base_dir) / "dqaf"
    if not dqaf_dir.exists():
        return []
    return sorted(dqaf_dir.glob("crash_*.json"))


def mark_processed(snapshot_path: Path) -> None:
    """Mark a crash snapshot as processed (rename to .processed)."""
    processed = snapshot_path.with_suffix(".processed")
    os.replace(str(snapshot_path), str(processed))


# ── CLI (for cron/manual use) ──

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ω Crash Forensics")
    parser.add_argument("--base-dir", nargs="+", default=["data", "data_btc"])
    parser.add_argument("--check", action="store_true", help="List pending crash snapshots")
    parser.add_argument("--auto-dqaf", action="store_true", help="Auto-run dqaf_collect for pending crashes")
    args = parser.parse_args()

    for base_dir in args.base_dir:
        pending = check_pending_crashes(base_dir)
        if args.check:
            print(f"{base_dir}: {len(pending)} pending crash snapshot(s)")
            for p in pending:
                print(f"  {p}")

        if args.auto_dqaf and pending:
            print(f"[Ω-CRASH] Auto-collecting evidence for {len(pending)} crash(es)...")
            for p in pending:
                # Extract timestamp from filename for docket ID
                ts = p.stem.replace("crash_", "")
                docket_id = f"CRASH-{ts[:8]}-{ts[9:15]}"
                print(f"  Would run: dqaf_collect.py --hours 2 --docket-id {docket_id}")
                # mark_processed(p)  # Uncomment when dqaf_collect is wired

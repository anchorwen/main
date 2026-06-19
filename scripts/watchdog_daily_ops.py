#!/usr/bin/env python
"""Lightweight watchdog: reminds operator to run daily_ops if overdue.

Usage (background):
    python scripts/watchdog_daily_ops.py --base-dir data_btc --interval-hours 6
    python scripts/watchdog_daily_ops.py --base-dir data --interval-hours 6

Checks daily_ops_state.json timestamp.  If daily_ops hasn't run in
``--max-age-hours`` (default 24), prints a reminder to stdout.
Optionally invokes daily_ops automatically with ``--auto-run``.

Designed to run as a background process alongside the launcher.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _hours_since_last_run(state_path: Path) -> float | None:
    """Return hours since daily_ops last ran, or None if never."""
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        last_utc = data.get("last_run_utc") or data.get("updated_utc", "")
        if not last_utc:
            return None
        last_dt = datetime.fromisoformat(last_utc.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        return (now - last_dt).total_seconds() / 3600.0
    except Exception:  # BLE001:REVIEWED
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="daily_ops watchdog")
    parser.add_argument("--base-dir", default="data_btc")
    parser.add_argument("--interval-hours", type=float, default=6,
                        help="How often to check (default: 6h)")
    parser.add_argument("--max-age-hours", type=float, default=24,
                        help="Max age before reminder (default: 24h)")
    parser.add_argument("--auto-run", action="store_true",
                        help="Automatically run daily_ops when overdue")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir)
    state_path = base_dir / "daily_ops_state.json"

    print(f"[watchdog:{args.base_dir}] Starting — check every {args.interval_hours}h, "
          f"remind after {args.max_age_hours}h, auto_run={args.auto_run}")

    while True:
        age_h = _hours_since_last_run(state_path)

        if age_h is None:
            print(f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} "
                  f"daily_ops never run — run: python scripts/daily_ops.py --base-dir {args.base_dir}")
        elif age_h > args.max_age_hours:
            msg = (f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} "
                   f"daily_ops OVERDUE: {age_h:.0f}h since last run")
            if args.auto_run:
                print(f"{msg} — auto-running...")
                try:
                    subprocess.run(
                        [sys.executable, "scripts/daily_ops.py", "--base-dir", str(args.base_dir)],
                        check=False, timeout=600,
                    )
                    print(f"[watchdog:{args.base_dir}] daily_ops completed")
                except Exception as exc:
                    print(f"[watchdog:{args.base_dir}] daily_ops failed: {exc}")
            else:
                print(f"{msg} — run: python scripts/daily_ops.py --base-dir {args.base_dir}")
        else:
            print(f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} "
                  f"daily_ops OK: {age_h:.0f}h since last run")

        time.sleep(args.interval_hours * 3600)


if __name__ == "__main__":
    sys.exit(main())

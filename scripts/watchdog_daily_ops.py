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
        # FIX-20260625-125: field is "last_daily_ops_utc" (float Unix timestamp),
        # NOT "last_run_utc" / "updated_utc".  Also accept ISO string for
        # backward-compat with any legacy format.
        last_utc = data.get("last_daily_ops_utc")
        if last_utc is None:
            return None
        if isinstance(last_utc, int | float):
            last_dt = datetime.fromtimestamp(float(last_utc), tz=UTC)
        else:
            last_dt = datetime.fromisoformat(str(last_utc).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        return (now - last_dt).total_seconds() / 3600.0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="daily_ops watchdog")
    parser.add_argument("--base-dir", default="data_btc")
    parser.add_argument(
        "--interval-hours", type=float, default=6, help="How often to check (default: 6h)"
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=24, help="Max age before reminder (default: 24h)"
    )
    parser.add_argument(
        "--auto-run", action="store_true", help="Automatically run daily_ops when overdue"
    )
    parser.add_argument(
        "--mt5-terminal-path",
        default=None,
        help="MT5 terminal64.exe path for PnL reconciliation. "
        "Auto-resolved from --base-dir if not specified: "
        "btc→D:\\MetaTrader 5\\terminal64.exe, xau→D:\\exness\\MetaTrader 5 EXNESS1\\terminal64.exe",
    )
    args = parser.parse_args(argv)

    # FIX-20260627-148: auto-resolve MT5 terminal path from base_dir
    # so daily_ops PnL reconciliation actually runs instead of being
    # silently skipped (mt5_terminal_path=None → status='mt5_unavailable').
    if args.mt5_terminal_path is None:
        if "btc" in str(args.base_dir).lower():
            args.mt5_terminal_path = r"D:\MetaTrader 5\terminal64.exe"
        else:
            args.mt5_terminal_path = r"D:\exness\MetaTrader 5 EXNESS1\terminal64.exe"

    base_dir = Path(args.base_dir)
    # FIX-20260625-125: state file lives under "state/" subdirectory
    # (Plan B FIX-20260622-001 migrated all state writes to StateWriter gate,
    #  which writes to state/daily_ops_state.json — but watchdog was missed)
    state_path = base_dir / "state" / "daily_ops_state.json"

    print(
        f"[watchdog:{args.base_dir}] Starting — check every {args.interval_hours}h, "
        f"remind after {args.max_age_hours}h, auto_run={args.auto_run}"
    )

    while True:
        age_h = _hours_since_last_run(state_path)

        if age_h is None:
            msg = f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} " f"daily_ops never run"
            if args.auto_run:
                print(f"{msg} — auto-running...")
                try:
                    subprocess.run(
                        [
                            sys.executable,
                            "scripts/daily_ops.py",
                            "--base-dir",
                            str(args.base_dir),
                            "--mt5-terminal-path",
                            args.mt5_terminal_path,
                        ],
                        check=False,
                        timeout=600,
                    )
                    print(f"[watchdog:{args.base_dir}] daily_ops completed")
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as exc:  # BLE001:FOG
                    print(f"[watchdog:{args.base_dir}] daily_ops failed: {exc}")
            else:
                print(
                    f"{msg} — run: python scripts/daily_ops.py --base-dir {args.base_dir}"
                    f' --mt5-terminal-path "{args.mt5_terminal_path}"'
                )
        elif age_h > args.max_age_hours:
            msg = (
                f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} "
                f"daily_ops OVERDUE: {age_h:.0f}h since last run"
            )
            if args.auto_run:
                print(f"{msg} — auto-running...")
                try:
                    subprocess.run(
                        [
                            sys.executable,
                            "scripts/daily_ops.py",
                            "--base-dir",
                            str(args.base_dir),
                            "--mt5-terminal-path",
                            args.mt5_terminal_path,
                        ],
                        check=False,
                        timeout=600,
                    )
                    print(f"[watchdog:{args.base_dir}] daily_ops completed")
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as exc:  # BLE001:FOG
                    print(f"[watchdog:{args.base_dir}] daily_ops failed: {exc}")
            else:
                print(
                    f"{msg} — run: python scripts/daily_ops.py --base-dir {args.base_dir}"
                    f' --mt5-terminal-path "{args.mt5_terminal_path}"'
                )
        else:
            print(
                f"[watchdog:{args.base_dir}] {_utc_iso()[:19]} "
                f"daily_ops OK: {age_h:.0f}h since last run"
            )

        time.sleep(args.interval_hours * 3600)


if __name__ == "__main__":
    sys.exit(main())

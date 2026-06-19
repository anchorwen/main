"""Python-native bridge supervisor with heartbeat logging.

Wraps ``mt5_bridge_worker`` in a continuous loop, writing a heartbeat
timestamp to ``bridge_supervisor.log`` before each poll cycle.  This
replaces the PowerShell ``run_bridge_forever.ps1`` / ``start_live_ops.ps1``
for environments where PowerShell is unavailable or undesirable.

Usage:
  python scripts/bridge_supervisor.py
  python scripts/bridge_supervisor.py --interval 5 --max-restarts 10
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _log(msg: str, log_path: str) -> None:
    ts = _utc_now()[:19].replace("T", " ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # never crash because of a log write failure


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bridge_supervisor")
    p.add_argument("--interval", type=int, default=5, help="Seconds between worker polls")
    p.add_argument(
        "--max-restarts", type=int, default=20, help="Max restarts before giving up (0 = unlimited)"
    )
    p.add_argument("--outbox-dir", default="data/mt5_outbox")
    p.add_argument("--receipt-dir", default="data/receipts")
    p.add_argument("--archive-dir", default="data/mt5_outbox_processed")
    p.add_argument("--journal-path", default="data/live_trade_journal.jsonl")
    p.add_argument("--protection-flag", default="data/live_dispatch_block.flag")
    p.add_argument("--mt5-terminal-path", default=None)
    p.add_argument("--target-symbol", default="XAUUSDc")
    p.add_argument("--log-path", default="data/reports/ops_logs/bridge_supervisor.log")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Push project root onto sys.path so worker imports resolve
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    _log("Bridge supervisor starting", args.log_path)
    _log(f"  interval={args.interval}s  max_restarts={args.max_restarts}", args.log_path)

    restart_count = 0
    total_cycles = 0

    while True:
        try:
            # Heartbeat before each poll
            _log(f"Poll cycle {total_cycles + 1} — heartbeat", args.log_path)

            from scripts.mt5_bridge_worker import run_worker

            # Build an argparse.Namespace mimicking the worker CLI
            worker_args = argparse.Namespace(
                outbox_dir=args.outbox_dir,
                receipt_dir=args.receipt_dir,
                archive_dir=args.archive_dir,
                journal_path=args.journal_path,
                protection_flag_path=args.protection_flag,
                default_volume=0.01,
                deviation=20,
                magic=90001,
                poll_seconds=args.interval,
                dry_run=False,
                once=True,
            )
            exit_code = run_worker(worker_args)
            total_cycles += 1

            if exit_code != 0:
                _log(f"Worker exited with code {exit_code} — will retry", args.log_path)

            # Successful cycle resets restart counter
            restart_count = 0

        except KeyboardInterrupt:
            _log("Bridge supervisor stopped by user (Ctrl+C)", args.log_path)
            return 0
        except Exception:  # BLE001:REVIEWED
            restart_count += 1
            total_cycles += 1
            _log(
                f"Worker crashed (restart {restart_count}/{args.max_restarts}): {traceback.format_exc()}",
                args.log_path,
            )
            if args.max_restarts > 0 and restart_count >= args.max_restarts:
                _log(f"Max restarts ({args.max_restarts}) reached — giving up", args.log_path)
                return 1

        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

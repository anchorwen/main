"""Live trading launcher — one command starts the full live pipeline.

Reads configs/live.yaml and launches:
  - mt5_bridge_worker (order execution → MT5)
  - live_intent_loop  (signal generation → outbox)

Both run as subprocesses managed by this supervisor. Ctrl+C gracefully
shuts down both processes.

Usage:
  python main.py live                    # via central hub
  python scripts/live_launcher.py        # direct
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent


def _utc_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_compact() -> str:
    """Compact timestamp for log filenames: 20260506T230400Z"""
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "")
        .replace("-", "")
        .replace(":", "")
        + "Z"
    )


def load_live_config(config_path: Path) -> dict[str, Any]:
    """Load live.yaml and extract live_trading section."""
    import yaml

    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    lt = cfg.get("live_trading", {})
    if not lt:
        raise ValueError("live_trading section not found in config")

    # Resolve mt5 path from mt5 section if not in live_trading
    if not lt.get("mt5_terminal_path"):
        lt["mt5_terminal_path"] = cfg.get("mt5", {}).get("terminal_path", "")
    if not lt.get("mt5_terminal_path"):
        raise ValueError("mt5_terminal_path is required in live_trading or mt5 section")

    return lt


def _stream_reader(
    proc: subprocess.Popen, prefix: str, stop_event: threading.Event, log_fh: TextIO | None = None
):
    """Read lines from a subprocess stdout, print with prefix, and optionally tee to a log file."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if stop_event.is_set():
                break
            line = line.rstrip("\n")
            if line:
                output = f"[{prefix}] {line}"
                print(output, flush=True)
                if log_fh is not None:
                    log_fh.write(output + "\n")
                    log_fh.flush()
    except (ValueError, OSError):
        pass


def _feedback_loop_runner(
    python: str,
    project_root: Path,
    base_dir: str,
    stop_event: threading.Event,
    log_fh: TextIO | None = None,
    interval_seconds: int = 300,
):
    """Run feedback_loop.py periodically to close the learning loop.

    Processes trade outcomes from the journal, updates brain performance
    tracker, and triggers online learner partial_fit for closed trades.
    """
    import time as _time

    cmd = [
        python,
        str(project_root / "scripts" / "feedback_loop.py"),
        "--multi-brain",
        "--base-dir",
        str(project_root / base_dir),
    ]

    # First run after 60s to let journal stabilize, then every interval_seconds
    initial_delay = 60
    if not stop_event.wait(initial_delay):
        pass  # not stopped yet

    while not stop_event.is_set():
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(project_root),
            )
            if result.stdout.strip():
                output = f"[feedback] {result.stdout.strip()}"
                print(output, flush=True)
                if log_fh is not None:
                    log_fh.write(output + "\n")
                    log_fh.flush()
            if result.stderr.strip():
                err_out = f"[feedback:err] {result.stderr.strip()}"
                print(err_out, flush=True)
                if log_fh is not None:
                    log_fh.write(err_out + "\n")
                    log_fh.flush()
        except subprocess.TimeoutExpired:
            msg = "[feedback] WARNING: feedback_loop timed out after 30s"
            print(msg, flush=True)
            if log_fh is not None:
                log_fh.write(msg + "\n")
                log_fh.flush()
        except Exception as exc:
            msg = f"[feedback] ERROR: {exc}"
            print(msg, flush=True)
            if log_fh is not None:
                log_fh.write(msg + "\n")
                log_fh.flush()

        # Sleep in small increments so we can respond to stop_event quickly
        remaining = interval_seconds
        while remaining > 0 and not stop_event.is_set():
            _time.sleep(min(5, remaining))
            remaining -= 5


def launch(config_path: str = "configs/live.yaml") -> int:
    """Launch the full live trading pipeline.

    Returns exit code (0 on clean shutdown, non-zero on error).
    """
    cfg = load_live_config(Path(config_path))

    python = sys.executable

    # ── Startup: clean orphan journal entries ──
    journal_path = PROJECT_ROOT / cfg["base_dir"] / "live_trade_journal.jsonl"
    logs_dir = PROJECT_ROOT / cfg["base_dir"] / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        from core.ledger.services.journal_cleanup import cleanup_orphan_opens

        orphan_count = cleanup_orphan_opens(journal_path, max_age_hours=24)
        if orphan_count > 0:
            print(f"[launcher] Cleaned {orphan_count} orphan open entries from journal", flush=True)
    except Exception as exc:
        print(f"[launcher] WARNING: orphan cleanup failed: {exc}", flush=True)

    # ── Open log file for this session ──
    log_path = logs_dir / f"live_launcher_{_utc_compact()}.log"
    log_fh = open(log_path, "a", encoding="utf-8")
    log_fh.write(f"\n{'='*60}\n")
    log_fh.write(f"  LIVE PIPELINE START — {_utc_iso()}\n")
    log_fh.write(f"{'='*60}\n\n")
    log_fh.flush()

    # ── Bridge worker command ──
    bridge_cmd = [
        python,
        "-u",
        str(PROJECT_ROOT / "scripts" / "mt5_bridge_worker.py"),
        "--outbox-dir",
        str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox"),
        "--receipt-dir",
        str(PROJECT_ROOT / cfg["base_dir"] / "receipts"),
        "--archive-dir",
        str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox_processed"),
        "--journal-path",
        str(PROJECT_ROOT / cfg["base_dir"] / "live_trade_journal.jsonl"),
        "--protection-flag-path",
        str(PROJECT_ROOT / cfg["base_dir"] / "live_dispatch_block.flag"),
        "--mt5-terminal-path",
        cfg["mt5_terminal_path"],
        "--poll-seconds",
        str(cfg["bridge"]["poll_seconds"]),
        "--deviation",
        str(cfg["bridge"]["deviation"]),
        "--magic",
        str(cfg["bridge"]["magic"]),
        "--default-volume",
        str(cfg["volume"]),
    ]

    # ── Intent loop command ──
    intent_cmd = [
        python,
        "-u",
        str(PROJECT_ROOT / "scripts" / "live_intent_loop.py"),
        "--config",
        config_path,
        "--mt5-terminal-path",
        cfg["mt5_terminal_path"],
        "--symbol",
        cfg["symbol"],
        "--volume",
        str(cfg["volume"]),
        "--interval-seconds",
        str(cfg["interval_seconds"]),
        "--confidence-threshold",
        str(cfg["confidence_threshold"]),
        "--sl-atr-mult",
        str(cfg["sl_atr_mult"]),
        "--tp-atr-mult",
        str(cfg["tp_atr_mult"]),
        "--cooldown-seconds",
        str(cfg["cooldown_seconds"]),
        "--max-positions",
        str(cfg["max_positions"]),
        "--normalization-config",
        cfg["normalization_config"],
        "--feature-store-dir",
        str(PROJECT_ROOT / cfg["feature_store_dir"]),
        "--base-dir",
        str(PROJECT_ROOT / cfg["base_dir"]),
    ]
    if cfg.get("multi_brain", True):
        intent_cmd.extend(
            [
                "--multi-brain",
                "--brains-dir",
                str(PROJECT_ROOT / cfg["brains_dir"]),
            ]
        )

    # ── Print startup banner ──
    def _echo(msg: str) -> None:
        print(msg, flush=True)
        log_fh.write(msg + "\n")
        log_fh.flush()

    _echo("=" * 60)
    _echo("  QUANT OS — LIVE TRADING")
    _echo(f"  Started: {_utc_iso()}")
    _echo(
        f"  Symbol: {cfg['symbol']}  Volume: {cfg['volume']}  Interval: {cfg['interval_seconds']}s"
    )
    _echo(
        f"  SL: {cfg['sl_atr_mult']}xATR  TP: {cfg['tp_atr_mult']}xATR  Cooldown: {cfg['cooldown_seconds']}s"
    )
    _echo(
        f"  Multi-brain: {cfg.get('multi_brain', True)}  Confidence threshold: {cfg['confidence_threshold']}"
    )
    if orphan_count > 0:
        _echo(f"  Orphan entries cleaned: {orphan_count}")
    _echo(f"  Log: {log_path}")
    _echo("=" * 60)
    _echo("")
    _echo("  Press Ctrl+C to stop all processes gracefully.")
    _echo("")

    # ── Launch subprocesses ──
    subprocess_env = {**dict(subprocess.os.environ), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    bridge_proc = subprocess.Popen(
        bridge_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Bridge worker started (pid={bridge_proc.pid})", flush=True)
    log_fh.write(f"[launcher] Bridge worker started (pid={bridge_proc.pid})\n")
    log_fh.flush()

    intent_proc = subprocess.Popen(
        intent_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Intent loop started (pid={intent_proc.pid})", flush=True)
    log_fh.write(f"[launcher] Intent loop started (pid={intent_proc.pid})\n")
    log_fh.flush()

    # ── Stream output from both ──
    stop_event = threading.Event()

    bridge_thread = threading.Thread(
        target=_stream_reader,
        args=(bridge_proc, "bridge", stop_event, log_fh),
        daemon=True,
    )
    intent_thread = threading.Thread(
        target=_stream_reader,
        args=(intent_proc, "intent", stop_event, log_fh),
        daemon=True,
    )
    # ── Feedback loop thread (periodic learning from trade outcomes) ──
    feedback_thread = threading.Thread(
        target=_feedback_loop_runner,
        args=(python, PROJECT_ROOT, cfg["base_dir"], stop_event, log_fh, 300),
        daemon=True,
    )
    feedback_thread.start()

    bridge_thread.start()
    intent_thread.start()

    # ── Signal handler for graceful shutdown ──
    exit_code = [0]

    def _on_signal(signum, frame):
        msg = f"\n[launcher] Received signal {signum}, shutting down..."
        print(msg, flush=True)
        log_fh.write(msg + "\n")
        log_fh.flush()
        stop_event.set()

        # Terminate both
        for proc, name in [(bridge_proc, "bridge"), (intent_proc, "intent")]:
            if proc.poll() is None:
                msg2 = f"[launcher] Terminating {name} (pid={proc.pid})..."
                print(msg2, flush=True)
                log_fh.write(msg2 + "\n")
                log_fh.flush()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    msg3 = f"[launcher] Force killing {name}..."
                    print(msg3, flush=True)
                    log_fh.write(msg3 + "\n")
                    log_fh.flush()
                    proc.kill()
                    proc.wait()

        msg4 = f"[launcher] All processes stopped. ({_utc_iso()})"
        print(msg4, flush=True)
        log_fh.write(msg4 + "\n")
        log_fh.flush()
        log_fh.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ── Wait for processes (block until signal) ──
    try:
        bridge_proc.wait()
        intent_proc.wait()
    except KeyboardInterrupt:
        _on_signal(signal.SIGINT, None)

    bridge_thread.join(timeout=3)
    intent_thread.join(timeout=3)
    stop_event.set()

    msg = f"[launcher] Pipeline exited ({_utc_iso()})"
    print(msg, flush=True)
    log_fh.write(msg + "\n")
    log_fh.flush()
    log_fh.close()

    return exit_code[0]


if __name__ == "__main__":
    raise SystemExit(launch())

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
from typing import Any

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


def _stream_reader(proc: subprocess.Popen, prefix: str, stop_event: threading.Event):
    """Read lines from a subprocess stdout and print with prefix."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if stop_event.is_set():
                break
            line = line.rstrip("\n")
            if line:
                print(f"[{prefix}] {line}", flush=True)
    except (ValueError, OSError):
        pass


def launch(config_path: str = "configs/live.yaml") -> int:
    """Launch the full live trading pipeline.

    Returns exit code (0 on clean shutdown, non-zero on error).
    """
    cfg = load_live_config(Path(config_path))

    python = sys.executable

    # ── Bridge worker command ──
    bridge_cmd = [
        python,
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
        str(PROJECT_ROOT / "scripts" / "live_intent_loop.py"),
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
    print("=" * 60, flush=True)
    print("  QUANT OS — LIVE TRADING", flush=True)
    print(f"  Started: {_utc_iso()}", flush=True)
    print(
        f"  Symbol: {cfg['symbol']}  Volume: {cfg['volume']}  "
        f"Interval: {cfg['interval_seconds']}s",
        flush=True,
    )
    print(
        f"  SL: {cfg['sl_atr_mult']}xATR  TP: {cfg['tp_atr_mult']}xATR  "
        f"Cooldown: {cfg['cooldown_seconds']}s",
        flush=True,
    )
    print(
        f"  Multi-brain: {cfg.get('multi_brain', True)}  "
        f"Confidence threshold: {cfg['confidence_threshold']}",
        flush=True,
    )
    print("=" * 60, flush=True)
    print("", flush=True)
    print("  Press Ctrl+C to stop all processes gracefully.", flush=True)
    print("", flush=True)

    # ── Launch subprocesses ──
    subprocess_env = {**dict(subprocess.os.environ), "PYTHONUTF8": "1"}
    bridge_proc = subprocess.Popen(
        bridge_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Bridge worker started (pid={bridge_proc.pid})", flush=True)

    intent_proc = subprocess.Popen(
        intent_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Intent loop started (pid={intent_proc.pid})", flush=True)

    # ── Stream output from both ──
    stop_event = threading.Event()

    bridge_thread = threading.Thread(
        target=_stream_reader,
        args=(bridge_proc, "bridge", stop_event),
        daemon=True,
    )
    intent_thread = threading.Thread(
        target=_stream_reader,
        args=(intent_proc, "intent", stop_event),
        daemon=True,
    )
    bridge_thread.start()
    intent_thread.start()

    # ── Signal handler for graceful shutdown ──
    exit_code = [0]

    def _on_signal(signum, frame):
        print(f"\n[launcher] Received signal {signum}, shutting down...", flush=True)
        stop_event.set()

        # Terminate both
        for proc, name in [(bridge_proc, "bridge"), (intent_proc, "intent")]:
            if proc.poll() is None:
                print(f"[launcher] Terminating {name} (pid={proc.pid})...", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"[launcher] Force killing {name}...", flush=True)
                    proc.kill()
                    proc.wait()

        print("[launcher] All processes stopped.", flush=True)
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

    return exit_code[0]


if __name__ == "__main__":
    raise SystemExit(launch())

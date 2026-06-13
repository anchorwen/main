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

import json
import os
import signal
import subprocess
import sys
import threading
import time as time_module
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
    """Load live.yaml and extract live_trading section.

    Also forwards top-level ``adapter`` and ``zmq`` sections so the
    launcher can auto-select the correct bridge transport (file vs ZMQ).
    """
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

    # Forward adapter + zmq config for bridge transport selection
    lt["adapter"] = cfg.get("adapter", {})
    lt["zmq"] = cfg.get("zmq", {})

    return lt


def _stream_reader(
    proc: subprocess.Popen, prefix: str, stop_event: threading.Event, log_fh: TextIO | None = None
):
    """Read lines from a subprocess stdout, print with prefix, and optionally tee to a log file."""
    try:
        assert proc.stdout is not None
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
        except Exception as exc:  # noqa: BLE001
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

    repair_report: dict[str, Any] = {}
    try:
        from core.ledger.services.journal_cleanup import repair_and_cleanup

        repair_report = repair_and_cleanup(
            journal_path,
            max_age_hours=24,
            dry_run=False,
            lock_dir=PROJECT_ROOT / cfg["base_dir"] / "locks",  # FIX-20260601-043
        )
        if repair_report.get("status") != "ok":
            print(
                f"[launcher] Journal repair: backfilled {repair_report.get('backfilled_magic',0)} magic, "
                f"{repair_report.get('backfilled_strategy',0)} strategy, "
                f"closed {repair_report.get('orphans_closed',0)} orphans, "
                f"{repair_report.get('duplicates_removed',0)} duplicates removed",
                flush=True,
            )
        if repair_report.get("orphans_closed", 0) > 0:
            tickets = repair_report.get("unclosed_tickets", [])
            print(
                f"[launcher] Unresolved tickets needing manual review: {tickets[:20]}", flush=True
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[launcher] WARNING: journal repair/cleanup failed: {exc}", flush=True)

    # ── Open log file for this session ──
    log_path = logs_dir / f"live_launcher_{_utc_compact()}.log"
    log_fh = open(log_path, "a", encoding="utf-8")
    log_fh.write(f"\n{'='*60}\n")
    log_fh.write(f"  LIVE PIPELINE START — {_utc_iso()}\n")
    log_fh.write(f"{'='*60}\n\n")
    log_fh.flush()

    # ── Bridge worker command ──
    # Read transport mode from adapter config (default: file IPC)
    adapter_cfg = cfg.get("adapter", {})
    use_zmq = adapter_cfg.get("name") == "mt5_zmq" if isinstance(adapter_cfg, dict) else False
    zmq_cfg = cfg.get("zmq", {}) if isinstance(cfg.get("zmq"), dict) else {}

    bridge_cmd = [
        python,
        "-u",
        str(PROJECT_ROOT / "scripts" / "mt5_bridge_worker.py"),
    ]

    if use_zmq:
        bridge_cmd.append("--zmq")
        bridge_cmd.extend([
            "--zmq-order-endpoint", zmq_cfg.get("order_endpoint", "tcp://127.0.0.1:5556"),
            "--zmq-ack-endpoint", zmq_cfg.get("ack_endpoint", "tcp://127.0.0.1:5557"),
        ])
    else:
        bridge_cmd.extend([
            "--outbox-dir", str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox"),
            "--receipt-dir", str(PROJECT_ROOT / cfg["base_dir"] / "receipts"),
            "--archive-dir", str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox_processed"),
        ])

    bridge_cmd.extend([
        "--journal-path",
        str(PROJECT_ROOT / cfg["base_dir"] / "live_trade_journal.jsonl"),
        "--protection-flag-path",
        str(PROJECT_ROOT / cfg["base_dir"] / "live_dispatch_block.flag"),
        "--health-path",
        str(PROJECT_ROOT / cfg["base_dir"] / "reports" / "mt5_bridge_health.json"),
        "--mt5-terminal-path",
        cfg["mt5_terminal_path"],
        "--deviation",
        str(cfg["bridge"]["deviation"]),
        "--magic",
        str(cfg["bridge"]["magic"]),
        "--default-volume",
        str(cfg["volume"]),
        "--default-symbol",
        cfg["symbol"],
    ])
    if not use_zmq:
        bridge_cmd.extend([
            "--poll-seconds", str(cfg["bridge"]["poll_seconds"]),
        ])

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

    # ── Alert hub (FIX-20260529-040) ──
    # alert is a top-level key in live.yaml, not under live_trading
    import yaml as _yaml

    _full_cfg = _yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    alert_cfg = _full_cfg.get("alert", {}) if isinstance(_full_cfg, dict) else {}
    if alert_cfg.get("enabled", False):
        intent_cmd.append("--alert")
        # Forward webhook URLs: try config first, then env var
        _slack_url = alert_cfg.get("channels", {}).get("slack_webhook_url", "") or os.getenv(
            "QUANTOS_SLACK_WEBHOOK_URL", ""
        )
        if _slack_url:
            intent_cmd.extend(["--slack-webhook", _slack_url])
        _ding_url = alert_cfg.get("channels", {}).get("dingtalk_webhook_url", "") or os.getenv(
            "QUANTOS_DINGTALK_WEBHOOK_URL", ""
        )
        if _ding_url:
            intent_cmd.extend(["--dingtalk-webhook", _ding_url])
            _ding_secret = alert_cfg.get("channels", {}).get("dingtalk_secret", "") or os.getenv(
                "QUANTOS_DINGTALK_SECRET", ""
            )
            if _ding_secret:
                intent_cmd.extend(["--dingtalk-secret", _ding_secret])

    # ── Safeguard modules (Pitfall 1-3) ──
    if cfg.get("bar_sync"):
        intent_cmd.extend(
            [
                "--bar-sync",
                "--bar-sync-timeout",
                str(cfg.get("bar_sync_timeout", 360)),
            ]
        )
    if cfg.get("use_exit_watchdog"):
        intent_cmd.append("--use-exit-watchdog")
    if cfg.get("use_limit_orders"):
        intent_cmd.append("--use-limit-orders")

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
    if repair_report.get("orphans_closed", 0) > 0:
        _echo(f"  Orphan entries cleaned: {repair_report.get('orphans_closed', 0)}")
    _safeguards: list[str] = []
    if cfg.get("bar_sync"):
        _safeguards.append("bar-sync")
    if cfg.get("use_exit_watchdog"):
        _safeguards.append("exit-watchdog")
    if cfg.get("use_limit_orders"):
        _safeguards.append("limit-monitor")
    if alert_cfg.get("enabled", False):
        _safeguards.append("alert-hub")
    if _safeguards:
        _echo(f"  Safeguards: {', '.join(_safeguards)}")

    # ── Clean stale lock files from previous crashed sessions ──
    _lock_dir = PROJECT_ROOT / cfg["base_dir"] / "locks"
    if _lock_dir.exists():
        for _lf in _lock_dir.glob("*.lock"):
            try:
                _data = json.loads(_lf.read_text(encoding="utf-8"))
                _pid = _data.get("pid", 0)
                if _pid:
                    try:
                        os.kill(_pid, 0)
                    except OSError:
                        _lf.unlink()
                        _echo(f"  Stale lock cleaned: {_lf.name} (pid={_pid} dead)")
            except Exception:  # noqa: BLE001
                pass

    _echo(f"  Log: {log_path}")
    _echo("=" * 60)
    _echo("")
    _echo("  Press Ctrl+C to stop all processes gracefully.")
    _echo("")

    # ── Pre-launch orphan check ──
    try:
        import subprocess as _sp

        _result = _sp.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        _lines = [l for l in _result.stdout.strip().split("\n") if l.strip()]
        _our_pid = os.getpid()
        _orphan_count = sum(1 for l in _lines if str(_our_pid) not in l)
        if _orphan_count >= 3:
            _echo(
                f"  WARNING: {_orphan_count} other Python processes detected — may cause MT5 contention"
            )
    except Exception:  # noqa: BLE001
        pass

    # ── FIX-019: Kill stale bridge/intent from previous crashed sessions ──
    # Without this, an old bridge surviving a launcher crash will poll the
    # same outbox directory alongside the new bridge → duplicate MT5 orders.
    _outbox_dir = str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox")
    _stale_killed = 0
    try:
        import subprocess as _sp

        _wmic_out = _sp.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline",
             "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        for _line in _wmic_out.stdout.split("\n"):
            if "mt5_bridge_worker.py" in _line and _outbox_dir in _line:
                _pid_str = _line.strip().split(",")[-1].strip()
                if _pid_str and _pid_str.isdigit():
                    _old_pid = int(_pid_str)
                    if _old_pid != os.getpid():
                        try:
                            os.kill(_old_pid, 9)
                            _stale_killed += 1
                            print(f"[launcher] Killed stale bridge PID={_old_pid}", flush=True)
                        except OSError:
                            pass
    except Exception:  # noqa: BLE001
        pass
    if _stale_killed:
        _echo(f"  Stale bridge cleaned: {_stale_killed} process(es) terminated")
        time_module.sleep(1.0)  # let OS reclaim the PID

    # ── Launch subprocesses ──
    # FIX-20260612-020: explicitly enable Golden Master recording.
    # GOLDEN_MASTER_RECORD defaults ON (unset != "0") but parent shell
    # may have it disabled.  Explicit "1" ensures recording for both BTC+XAU.
    subprocess_env = {**dict(os.environ), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "GOLDEN_MASTER_RECORD": "1"}

    # ── Separate log files for bridge and intent subprocess stdout ──
    # Writing directly to files avoids pipe-buffer deadlock: if the stream
    # reader thread dies, the subprocess will block on a full pipe.
    # (FIX-20260522-005 part B)
    bridge_log_path = logs_dir / f"bridge_{_utc_compact()}.log"
    intent_log_path = logs_dir / f"intent_{_utc_compact()}.log"
    bridge_log_fh = open(bridge_log_path, "a", encoding="utf-8")
    intent_log_fh = open(intent_log_path, "a", encoding="utf-8")

    bridge_proc = subprocess.Popen(
        bridge_cmd,
        stdout=bridge_log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Bridge worker started (pid={bridge_proc.pid})", flush=True)
    log_fh.write(f"[launcher] Bridge worker started (pid={bridge_proc.pid})\n")
    log_fh.flush()

    # ── FIX-20260612-001: Initialize restart tracking BEFORE intent start ──
    restart_counts: dict[str, int] = {"bridge": 0, "intent": 0}
    last_restart: dict[str, float] = {"bridge": 0.0, "intent": time_module.time()}

    intent_proc = subprocess.Popen(
        intent_cmd,
        stdout=intent_log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Intent loop started (pid={intent_proc.pid})", flush=True)
    log_fh.write(f"[launcher] Intent loop started (pid={intent_proc.pid})\n")
    log_fh.flush()

    # ── FIX-20260612-022: watchdog_daily_ops as managed subprocess ──
    # Runs daily_ops.py every 6h via the watchdog script.  Prevents
    # CAL_FEED_STALLED and DAILY_OPS_OVERDUE from recurring.
    _watchdog_cmd = [
        python, "-u",
        str(PROJECT_ROOT / "scripts" / "watchdog_daily_ops.py"),
        "--base-dir", str(cfg["base_dir"]),
        "--interval-hours", "6",
        "--max-age-hours", "24",
        "--auto-run",
    ]
    _watchdog_log_path = logs_dir / f"watchdog_{_utc_compact()}.log"
    _watchdog_log_fh = open(_watchdog_log_path, "a", encoding="utf-8")
    _watchdog_proc = subprocess.Popen(
        _watchdog_cmd,
        stdout=_watchdog_log_fh,
        stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8",
        env=subprocess_env,
    )
    print(f"[launcher] Watchdog started (pid={_watchdog_proc.pid})", flush=True)
    log_fh.write(f"[launcher] Watchdog started (pid={_watchdog_proc.pid})\n")
    log_fh.flush()

    # ── Stream output from all subprocesses ──
    stop_event = threading.Event()

    # No more pipe-based stream readers; subprocess output goes directly
    # to dedicated log files.  The launcher tees each new line into the
    # combined launcher log so nothing is lost from the unified view.
    def _file_watcher(path: Path, prefix: str, stop_event: threading.Event, log_fh: TextIO):
        """Tail *path* and tee each new line to the combined launcher log."""
        try:
            with open(path, encoding="utf-8") as fh:
                fh.seek(0, 2)  # start at end
                while not stop_event.is_set():
                    line = fh.readline()
                    if line:
                        line = line.rstrip("\n")
                        if line:
                            output = f"[{prefix}] {line}"
                            print(output, flush=True)
                            log_fh.write(output + "\n")
                            log_fh.flush()
                    else:
                        stop_event.wait(1.0)
        except Exception:  # noqa: BLE001
            pass

    bridge_thread = threading.Thread(
        target=_file_watcher,
        args=(bridge_log_path, "bridge", stop_event, log_fh),
        daemon=True,
    )
    intent_thread = threading.Thread(
        target=_file_watcher,
        args=(intent_log_path, "intent", stop_event, log_fh),
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

        # Terminate all managed processes
        for proc, name in [(_watchdog_proc, "watchdog"), (bridge_proc, "bridge"), (intent_proc, "intent")]:
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
        for _fh in [_watchdog_log_fh, bridge_log_fh, intent_log_fh, log_fh]:
            try:  # noqa: SIM105
                _fh.close()
            except Exception:  # noqa: BLE001
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ── Watchdog: monitor, restart, and stall-detect ──
    MAX_RESTARTS = 50  # max restarts within the window before giving up
    RESTART_WINDOW = 600  # seconds (10 min) — reset counter if last restart was older than this
    STALL_MINUTES = 15  # alert if no new decisions for this long
    CHECK_INTERVAL = 5  # seconds between health checks
    BRIDGE_STALL_MINUTES = 10  # alert if bridge processes no orders for this long

    def _graduated_cooldown(count: int) -> float:
        """Progressive backoff: 5s → 10s → 30s → 60s as restart count increases."""
        if count <= 5:
            return 5.0
        elif count <= 10:
            return 10.0
        elif count <= 20:
            return 30.0
        else:
            return 60.0

    def _restart_process(name: str, cmd: list[str]) -> subprocess.Popen | None:
        """Restart a crashed subprocess with graduated backoff. Returns new Popen or None if giving up."""
        nonlocal bridge_proc, intent_proc
        now = time_module.time()
        if now - last_restart[name] < RESTART_WINDOW:
            restart_counts[name] += 1
        else:
            restart_counts[name] = 1
        last_restart[name] = now

        if restart_counts[name] > MAX_RESTARTS:
            msg = (
                f"[launcher] CRITICAL: {name} crashed {MAX_RESTARTS}x "
                f"within {RESTART_WINDOW}s — giving up"
            )
            print(msg, flush=True)
            log_fh.write(msg + "\n")
            log_fh.flush()
            return None

        cooldown = _graduated_cooldown(restart_counts[name])
        msg = (
            f"[launcher] {name} died (restart {restart_counts[name]}/{MAX_RESTARTS}), "
            f"respawning in {cooldown:.0f}s..."
        )
        print(msg, flush=True)
        log_fh.write(msg + "\n")
        log_fh.flush()

        stop_event.wait(cooldown)
        if stop_event.is_set():
            return None

        try:
            new_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                env=subprocess_env,
            )
            msg2 = f"[launcher] {name} restarted (pid={new_proc.pid})"
            print(msg2, flush=True)
            log_fh.write(msg2 + "\n")
            log_fh.flush()

            # Restart stream reader thread for the new process
            reader_thread = threading.Thread(
                target=_stream_reader,
                args=(new_proc, name, stop_event, log_fh),
                daemon=True,
            )
            reader_thread.start()

            # Update the outer variable
            if name == "bridge":
                bridge_proc = new_proc
            else:
                intent_proc = new_proc

            return new_proc
        except Exception as exc:  # noqa: BLE001
            msg3 = f"[launcher] ERROR restarting {name}: {exc}"
            print(msg3, flush=True)
            log_fh.write(msg3 + "\n")
            log_fh.flush()
            return None

    # ── Stall detection helpers ──
    decisions_dir = PROJECT_ROOT / cfg["base_dir"] / "decisions"
    trade_journal_path = PROJECT_ROOT / cfg["base_dir"] / "live_trade_journal.jsonl"
    bridge_health_path = PROJECT_ROOT / cfg["base_dir"] / "reports" / "mt5_bridge_health.json"
    active_position_path = PROJECT_ROOT / cfg["base_dir"] / "state" / "active_position.json"
    governance_path = PROJECT_ROOT / cfg["base_dir"] / "governance_state.json"
    brain_perf_path = PROJECT_ROOT / cfg["base_dir"] / "brain_performance.json"
    daily_ops_state_path = PROJECT_ROOT / cfg["base_dir"] / "state" / "daily_ops_state.json"

    GOVERNANCE_STALE_HOURS = 26  # alert if governance_state.json older than this
    BRAIN_PERF_STALE_HOURS = 6  # alert if brain_performance.json older than this (feedback loop)
    DAILY_OPS_STALE_HOURS = 26  # alert if daily ops state older than this

    def _check_stall() -> list[str]:
        """Check for engine/bridge/governance stalls. Returns list of alert messages."""
        alerts: list[str] = []
        now = time_module.time()

        # 1. Engine liveness — check trade journal (primary) and decisions dir (fallback)
        engine_age = 9999.0
        if trade_journal_path.exists():
            engine_age = (now - trade_journal_path.stat().st_mtime) / 60
        if engine_age > STALL_MINUTES and decisions_dir.exists():
            for date_dir in decisions_dir.iterdir():
                if date_dir.is_dir():
                    for dec_file in date_dir.glob("*.jsonl"):
                        age_min = (now - dec_file.stat().st_mtime) / 60
                        if age_min < engine_age:
                            engine_age = age_min
        # ── FIX-20260612-001: Don't report stall right after restart ──
        # If the intent process was restarted within STALL_MINUTES,
        # the journal mtime reflects pre-crash state — NOT a stall.
        # Give the new process time to produce its first trade decision.
        _intent_uptime = (now - last_restart.get("intent", 0)) / 60
        if engine_age > STALL_MINUTES and _intent_uptime > STALL_MINUTES:
            alerts.append(f"ENGINE_STALL: no new decisions for {engine_age:.0f}m")

        # 2. Bridge health — check heartbeat freshness
        if bridge_health_path.exists():
            try:
                bh = json.loads(bridge_health_path.read_text(encoding="utf-8"))
                last_hb = bh.get("last_heartbeat_utc", "")
                if last_hb:
                    hb_dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                    hb_age = (
                        datetime.now(UTC).replace(tzinfo=None) - hb_dt.replace(tzinfo=None)
                    ).total_seconds() / 60
                    if hb_age > BRIDGE_STALL_MINUTES:
                        alerts.append(f"BRIDGE_STALL: no heartbeat for {hb_age:.0f}m")
            except Exception:  # noqa: BLE001
                pass

        # 3. Active position staleness
        if active_position_path.exists():
            try:
                ap = json.loads(active_position_path.read_text(encoding="utf-8"))
                saved_at = ap.get("saved_at_utc", "")
                if saved_at:
                    saved_dt = datetime.fromisoformat(saved_at)
                    age_h = (
                        datetime.now(UTC).replace(tzinfo=None) - saved_dt.replace(tzinfo=None)
                    ).total_seconds() / 3600
                    if age_h > 24:
                        alerts.append(f"STALE_ACTIVE_POSITION: position state {age_h:.0f}h old")
            except Exception:  # noqa: BLE001
                pass

        # 4. Governance staleness
        _gov_age_h = (
            (now - governance_path.stat().st_mtime) / 3600 if governance_path.exists() else 0
        )
        if _gov_age_h > GOVERNANCE_STALE_HOURS:
            alerts.append(f"GOVERNANCE_STALE: governance_state.json {_gov_age_h:.0f}h old")

        # 5. Brain performance tracker staleness (feedback loop stopped?)
        _bp_age_h = (
            (now - brain_perf_path.stat().st_mtime) / 3600 if brain_perf_path.exists() else 0
        )
        if _bp_age_h > BRAIN_PERF_STALE_HOURS:
            alerts.append(f"BRAIN_PERF_STALE: brain_performance.json {_bp_age_h:.0f}h old")

        # 6. Daily ops staleness (check state file — authoritative last-run record)
        _dops_age_h = (
            (now - daily_ops_state_path.stat().st_mtime) / 3600
            if daily_ops_state_path.exists()
            else 0
        )
        if _dops_age_h > DAILY_OPS_STALE_HOURS:
            alerts.append(f"DAILY_OPS_STALE: last run {_dops_age_h:.0f}h ago")

        return alerts

    _stall_alerted: set[str] = set()

    def _handle_stall_alerts(alerts: list[str]) -> None:
        """Log stall alerts, avoiding repeated spam for the same alert."""
        for alert in alerts:
            alert_key = alert.split(":")[0]
            if alert_key not in _stall_alerted:
                _stall_alerted.add(alert_key)
                msg = f"[launcher] ALERT: {alert}"
                print(msg, flush=True)
                log_fh.write(msg + "\n")
                log_fh.flush()

    # ── Stale active_position cleanup on startup ──
    if active_position_path.exists():
        try:
            ap = json.loads(active_position_path.read_text(encoding="utf-8"))
            saved_at = ap.get("saved_at_utc", "")
            if saved_at:
                saved_dt = datetime.fromisoformat(saved_at)
                age_h = (
                    datetime.now(UTC).replace(tzinfo=None) - saved_dt.replace(tzinfo=None)
                ).total_seconds() / 3600
                if age_h > 1:
                    active_position_path.unlink()
                    msg = f"[launcher] Cleaned stale active_position.json ({age_h:.0f}h old, ticket={ap.get('ticket')})"
                    print(msg, flush=True)
                    log_fh.write(msg + "\n")
                    log_fh.flush()
        except Exception:  # noqa: BLE001
            pass

    # ── Main watchdog loop ──
    try:
        while not stop_event.is_set():
            # Check subprocess health and restart if needed
            _bridge_rc = bridge_proc.poll()
            if _bridge_rc is not None:
                msg_rc = f"[launcher] bridge exited with code {_bridge_rc}"
                print(msg_rc, flush=True)
                log_fh.write(msg_rc + "\n")
                log_fh.flush()
                if _restart_process("bridge", bridge_cmd) is None:
                    break  # give up after too many restarts

            _intent_rc = intent_proc.poll()
            if _intent_rc is not None:
                msg_rc = f"[launcher] intent exited with code {_intent_rc}"
                print(msg_rc, flush=True)
                log_fh.write(msg_rc + "\n")
                log_fh.flush()
                if _restart_process("intent", intent_cmd) is None:
                    break  # give up after too many restarts

            # Watchdog is non-critical — log exit but don't restart
            _watchdog_rc = _watchdog_proc.poll()
            if _watchdog_rc is not None:
                msg_wd = f"[launcher] watchdog exited (code={_watchdog_rc}) — daily_ops auto-run disabled until restart"
                print(msg_wd, flush=True)
                log_fh.write(msg_wd + "\n")
                log_fh.flush()

            # Stall detection (every 2nd check = ~10s)
            if int(time_module.time()) % 10 < CHECK_INTERVAL:
                alerts = _check_stall()
                if alerts:
                    _handle_stall_alerts(alerts)

            stop_event.wait(CHECK_INTERVAL)

    except KeyboardInterrupt:
        _on_signal(signal.SIGINT, None)

    # ── Cleanup: terminate any still-running subprocesses ──
    for _proc, _name in [(_watchdog_proc, "watchdog"), (bridge_proc, "bridge"), (intent_proc, "intent")]:
        if _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    _proc.kill()
                    _proc.wait()
                except OSError:
                    pass
            _cw_msg = f"[launcher] Cleaned up {_name} subprocess (pid={_proc.pid})"
            print(_cw_msg, flush=True)
            log_fh.write(_cw_msg + "\n")
            log_fh.flush()

    bridge_thread.join(timeout=3)
    intent_thread.join(timeout=3)
    stop_event.set()

    msg = f"[launcher] Pipeline watchdog exited ({_utc_iso()})"
    print(msg, flush=True)
    log_fh.write(msg + "\n")
    log_fh.flush()
    for _fh in [bridge_log_fh, intent_log_fh, log_fh]:
        try:  # noqa: SIM105
            _fh.close()
        except Exception:  # noqa: BLE001
            pass

    return exit_code[0]


if __name__ == "__main__":
    raise SystemExit(launch(sys.argv[1] if len(sys.argv) > 1 else "configs/live.yaml"))

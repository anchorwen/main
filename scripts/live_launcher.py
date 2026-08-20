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


def _hours_since_last_run(state_path: Path) -> float | None:
    """Return hours since daily_ops last ran, or None if never.

    Ported from watchdog_daily_ops.py:30-49.
    Reads last_daily_ops_utc (float Unix timestamp) from state JSON.
    """
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        last_utc = data.get("last_daily_ops_utc")
        if last_utc is None:
            return None
        if isinstance(last_utc, int | float):
            last_dt = datetime.fromtimestamp(float(last_utc), UTC)
        else:
            last_dt = datetime.fromisoformat(str(last_utc).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        return (now - last_dt).total_seconds() / 3600.0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return None


def _daily_ops_scheduler(
    python: str,
    project_root: Path,
    base_dir: str,
    stop_event: threading.Event,
    log_fh: TextIO | None = None,
    mt5_terminal_path: str | None = None,
    interval_hours: float = 4,
    max_age_hours: float = 6,
):
    """Run daily_ops.py when overdue, as a managed subprocess.

    Replaces watchdog_daily_ops.py (previously a persistent subprocess).
    Follows the same pattern as _feedback_loop_runner — temporary subprocess
    spawned from a daemon thread, no persistent watchdog process.

    FIX-20260627-149: watchdog→thread integration, eliminates 2 persistent
    python processes (one per symbol launcher).
    """
    import time as _time

    state_path = project_root / base_dir / "state" / "daily_ops_state.json"

    # Initial delay to let the live system stabilise before first check
    if not stop_event.wait(60):
        pass

    while not stop_event.is_set():
        age_h = _hours_since_last_run(state_path)

        if age_h is None or age_h > max_age_hours:
            reason = (
                "never run" if age_h is None else f"overdue ({age_h:.0f}h > {max_age_hours}h max)"
            )
            msg = f"[daily_ops] Triggering: {reason}"
            print(msg, flush=True)
            if log_fh is not None:
                log_fh.write(msg + "\n")
                log_fh.flush()

            _cmd = [
                python,
                "-u",
                str(project_root / "scripts" / "daily_ops.py"),
                "--base-dir",
                str(project_root / base_dir),
            ]
            if mt5_terminal_path:
                _cmd.extend(["--mt5-terminal-path", mt5_terminal_path])

            try:
                result = subprocess.run(
                    _cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=600,
                    cwd=str(project_root),
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                )
                if result.returncode == 0:
                    msg = "[daily_ops] Completed successfully"
                else:
                    msg = f"[daily_ops] FAILED (rc={result.returncode})"
                    if result.stderr:
                        stderr_short = result.stderr.strip()[:500]
                        msg += f" — {stderr_short}"
                print(msg, flush=True)
                if log_fh is not None:
                    log_fh.write(msg + "\n")
                    log_fh.flush()
            except subprocess.TimeoutExpired:
                msg = "[daily_ops] WARNING: timed out after 600s"
                print(msg, flush=True)
                if log_fh is not None:
                    log_fh.write(msg + "\n")
                    log_fh.flush()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                msg = f"[daily_ops] ERROR: {exc}"
                print(msg, flush=True)
                if log_fh is not None:
                    log_fh.write(msg + "\n")
                    log_fh.flush()
        else:
            msg = f"[daily_ops] OK: {age_h:.0f}h since last run"
            print(msg, flush=True)
            if log_fh is not None:
                log_fh.write(msg + "\n")
                log_fh.flush()

        # Responsive sleep — check every interval, but respond to stop_event
        remaining = int(interval_hours * 3600)
        while remaining > 0 and not stop_event.is_set():
            _time.sleep(min(5, remaining))
            remaining -= 5


def _governance_scheduler_runner(
    project_root: Path,
    base_dir: str,
    stop_event: threading.Event,
    log_fh: TextIO | None = None,
    interval_seconds: int = 60,
):
    """Run the SSOT governance evaluation every ~60s (FIX-20260801-011).

    DQAF-20260801-010: The containerized path (apps/engine → scheduler_service)
    was the only deployment running GovernanceRuleEngine governance.  The
    bare-metal launcher path never started it, so live_intent_loop's DEPRECATED
    apply_promotion_decisions (BrainPnLStore last-20 window) was the only runtime
    governance writer → BTC_Swing_V4 live↔probation oscillation since 07-09.

    This thread runs the SAME brain_performance.json SSOT evaluation as the
    container path (core/deployment/governance_evaluator.py) and becomes the
    sole writer for automated lifecycle transitions in this deployment
    (Iron Law #14: No Siloed Reconciliation).
    """
    import time as _time

    from core.deployment.governance_evaluator import evaluate_governance_state
    from core.governance.governance_service import GovernanceService

    _gov_path = project_root / base_dir / "governance_state.json"

    while not stop_event.is_set():
        try:
            if not _gov_path.exists():
                _msg = f"[gov-eval] governance_state.json missing at {_gov_path} — skipping"
                print(_msg, flush=True)
                if log_fh is not None:
                    log_fh.write(_msg + "\n")
                    log_fh.flush()
            else:
                _gov = GovernanceService.load(str(_gov_path))
                _result = evaluate_governance_state(_gov, project_root / base_dir)
                _changed = [c for c in _result["changes"] if c != "no_changes"]
                _msg = (
                    f"[gov-eval] SSOT cycle: live_data={_result['brains_with_live_data']} "
                    f"decisions={len(_result['decisions'])} transitions={_changed}"
                )
                print(_msg, flush=True)
                if log_fh is not None:
                    log_fh.write(_msg + "\n")
                    log_fh.flush()

                # ── governance_events.jsonl: continue the promotion event stream
                #    under the SSOT sole writer (was written by the REMOVED
                #    live_intent_loop block, FIX-20260801-011).  Keeps the
                #    append-only audit trail + health-check source alive.
                _promoted = [
                    d
                    for d in _result["decisions"]
                    if d.get("action") == "promote" and d.get("approved") and d.get("target_status")
                ]
                if _promoted:
                    _gov_events_path = project_root / base_dir / "governance_events.jsonl"
                    try:
                        for _d in _promoted:
                            _gevt = {
                                "schema_version": "governance_event.v1",
                                "event": "brain_promoted",
                                "time": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                                "brain_id": _d["brain_id"],
                                "from_status": _d["current_status"],
                                "to_status": _d["target_status"],
                                "reasons": _d["reasons"],
                            }
                            with open(_gov_events_path, "a", encoding="utf-8") as _gf:
                                _gf.write(json.dumps(_gevt, ensure_ascii=False) + "\n")
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass  # event emission is non-critical
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            _msg = f"[gov-eval] ERROR: {exc}"
            print(_msg, flush=True)
            if log_fh is not None:
                log_fh.write(_msg + "\n")
                log_fh.flush()
        except Exception as exc:  # noqa: BLE001 — governance must never kill the launcher
            _msg = f"[gov-eval] ERROR (unexpected): {exc}"
            print(_msg, flush=True)
            if log_fh is not None:
                log_fh.write(_msg + "\n")
                log_fh.flush()

        # Responsive sleep — check every interval, but respond to stop_event
        remaining = int(interval_seconds)
        while remaining > 0 and not stop_event.is_set():
            _time.sleep(min(5, remaining))
            remaining -= 5


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
                encoding="utf-8",
                timeout=30,
                cwd=str(project_root),
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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

    # ── Death of Defaults (TECH_DEBT-010 Blueprint C) ──
    # ZMQ endpoint 必须由外层配置显式注入。未配置 order_endpoint 的 mt5_zmq
    # 启动即 Fail-Fast — 禁止静默落默认 5556 (XAU) 造成跨品种串台。
    if use_zmq and not zmq_cfg.get("order_endpoint"):
        raise RuntimeError(
            "Death of Defaults (TECH_DEBT-010 Blueprint C): adapter=mt5_zmq 但 "
            f"{str(PROJECT_ROOT / cfg.get('base_dir', '?') / 'config')} 缺少 "
            "zmq.order_endpoint 显式配置。多品种架构禁止 ZMQ 默认端口兜底, "
            "请在 yaml 显式注入 per-symbol endpoint (XAU: 5556 / BTC: 5558)。"
        )

    bridge_cmd = [
        python,
        "-u",
        str(PROJECT_ROOT / "scripts" / "mt5_bridge_worker.py"),
    ]

    if use_zmq:
        bridge_cmd.append("--zmq")
        bridge_cmd.extend(
            [
                "--zmq-order-endpoint",
                zmq_cfg["order_endpoint"],
                "--zmq-ack-endpoint",
                zmq_cfg.get("ack_endpoint", "tcp://127.0.0.1:5557"),
                # Phase 3: WAL dual-write — pass outbox/archive for 5s slow poll
                "--outbox-dir",
                str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox"),
                "--archive-dir",
                str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox_processed"),
            ]
        )
    else:
        bridge_cmd.extend(
            [
                "--outbox-dir",
                str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox"),
                "--receipt-dir",
                str(PROJECT_ROOT / cfg["base_dir"] / "receipts"),
                "--archive-dir",
                str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox_processed"),
            ]
        )

    bridge_cmd.extend(
        [
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
        ]
    )
    if not use_zmq:
        bridge_cmd.extend(
            [
                "--poll-seconds",
                str(cfg["bridge"]["poll_seconds"]),
            ]
        )

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
                str(cfg.get("bar_sync_timeout", 240)),  # FIX-20260820-001: sync default
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

    # ── Clean stale lock files from previous crashed or hung sessions ──
    # DQAF-20260616-004: previously only checked PID aliveness (os.kill(pid,0)).
    # A hung process has a live PID but never refreshes its lock → TTL expires.
    # Now also checks TTL: if age > ttl_seconds, the lock is stale even if the
    # process is technically alive (it's hung and will never recover).
    _lock_dir = PROJECT_ROOT / cfg["base_dir"] / "locks"
    if _lock_dir.exists():
        _now_utc = datetime.now(UTC).replace(tzinfo=None)
        for _lf in _lock_dir.glob("*.lock"):
            try:
                _data = json.loads(_lf.read_text(encoding="utf-8"))
                _pid = _data.get("pid", 0)
                if not _pid:
                    continue
                _stale = False
                # Check 1: PID dead → definitely stale
                # DQAF-20260619-001: Windows os.kill(pid,0) raises SystemError
                # (signal 0 unsupported), not OSError.  Catch both so the
                # TTL check (Check 2 below) is reachable on all platforms.
                try:
                    os.kill(_pid, 0)
                except (OSError, SystemError):
                    _stale = True
                    _reason = f"pid={_pid} dead"
                # Check 2: PID alive but TTL expired → hung process
                if not _stale:
                    _acquired_at = _data.get("acquired_at", "")
                    _ttl = _data.get("ttl_seconds", 300)
                    if _acquired_at:
                        _age = (
                            _now_utc - datetime.fromisoformat(_acquired_at).replace(tzinfo=None)
                        ).total_seconds()
                        if _age > _ttl:
                            _stale = True
                            _reason = f"pid={_pid} alive but TTL expired ({_age:.0f}s > {_ttl}s)"
                if _stale:
                    _lf.unlink()
                    _echo(f"  Stale lock cleaned: {_lf.name} ({_reason})")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
    # ── FIX-019: Kill stale bridge/intent from previous crashed sessions ──
    # Without this, an old bridge surviving a launcher crash will poll the
    # same outbox directory alongside the new bridge → duplicate MT5 orders.
    _outbox_dir = str(PROJECT_ROOT / cfg["base_dir"] / "mt5_outbox")
    _stale_killed = 0
    try:
        import subprocess as _sp

        _wmic_out = _sp.run(
            [
                "wmic",
                "process",
                "where",
                "name='python.exe'",
                "get",
                "processid,commandline",
                "/format:csv",
            ],
            capture_output=True,
            text=True,
            timeout=10,
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
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
    if _stale_killed:
        _echo(f"  Stale bridge cleaned: {_stale_killed} process(es) terminated")
        time_module.sleep(1.0)  # let OS reclaim the PID

    # ── Launch subprocesses ──
    # FIX-20260612-020: explicitly enable Golden Master recording.
    # GOLDEN_MASTER_RECORD defaults ON (unset != "0") but parent shell
    # may have it disabled.  Explicit "1" ensures recording for both BTC+XAU.
    subprocess_env = {
        **dict(os.environ),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "GOLDEN_MASTER_RECORD": "1",
    }

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

    # ── DQAF-20260618-001: Cold-start daily_ops pre-flight ──
    # If the process starts after the primary window (UTC 22:00–23:00) and
    # daily_ops hasn't run today, trigger it synchronously BEFORE the intent
    # loop starts its first cycle.  This closes the COLD_START_DAILY_OPS_GAP
    # where all three scheduling gates (primary window / 24h fallback /
    # watchdog max-age) fail to cover "restarted after window, before 24h".
    _dops_state_path = Path(cfg["base_dir"]) / "state" / "daily_ops_state.json"
    _now_utc_preflight = datetime.now(UTC)
    _primary_end = _now_utc_preflight.replace(hour=23, minute=0, second=0, microsecond=0)
    _past_primary = _now_utc_preflight >= _primary_end
    if _past_primary and _dops_state_path.exists():
        try:
            _dops = json.loads(_dops_state_path.read_text(encoding="utf-8"))
            _last_ts = float(_dops.get("last_daily_ops_utc", 0))
            _last_date = datetime.fromtimestamp(_last_ts, UTC).date() if _last_ts > 0 else None
            if _last_date != _now_utc_preflight.date():
                print(
                    f"[launcher] Cold-start daily_ops trigger: last={_last_date}, "
                    f"today={_now_utc_preflight.date()}, past primary window",
                    flush=True,
                )
                log_fh.write(
                    f"[launcher] Cold-start daily_ops trigger: "
                    f"last={_last_date}, today={_now_utc_preflight.date()}\n"
                )
                log_fh.flush()
                _mt5_path = cfg.get("mt5_terminal_path", "")
                _cold_cmd = [
                    python,
                    "-u",
                    str(PROJECT_ROOT / "scripts" / "daily_ops.py"),
                    "--base-dir",
                    str(cfg["base_dir"]),
                    "--skip-shadow",
                    "--skip-recap",
                ]
                if _mt5_path:
                    _cold_cmd.extend(["--mt5-terminal-path", str(_mt5_path)])
                _cold_result = subprocess.run(
                    _cold_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=300,
                    cwd=str(PROJECT_ROOT),
                    env=subprocess_env,
                )
                if _cold_result.returncode == 0:
                    print("[launcher] Cold-start daily_ops completed", flush=True)
                    log_fh.write("[launcher] Cold-start daily_ops completed\n")
                else:
                    print(
                        f"[launcher] Cold-start daily_ops FAILED (rc={_cold_result.returncode})",
                        flush=True,
                    )
                    log_fh.write(
                        f"[launcher] Cold-start daily_ops FAILED (rc={_cold_result.returncode})\n"
                    )
                    if _cold_result.stderr:
                        log_fh.write(_cold_result.stderr[:2000] + "\n")
                log_fh.flush()
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _cold_exc:  # BLE001:FOG
            print(f"[launcher] Cold-start daily_ops error: {_cold_exc}", flush=True)
            log_fh.write(f"[launcher] Cold-start daily_ops error: {_cold_exc}\n")
            log_fh.flush()
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

    # ── FIX-20260627-149: daily_ops scheduler as daemon thread ──
    # Runs daily_ops.py every ~4h when overdue.  Previously a persistent
    # watchdog_daily_ops.py subprocess; now a thread following the same
    # pattern as _feedback_loop_runner.  No extra python process needed.

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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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

    # ── Daily ops scheduler thread (FIX-20260627-149: replaces watchdog subprocess) ──
    _daily_ops_interval_h = 4
    _daily_ops_max_age_h = 6
    _daily_ops_thread = threading.Thread(
        target=_daily_ops_scheduler,
        args=(
            python,
            PROJECT_ROOT,
            cfg["base_dir"],
            stop_event,
            log_fh,
            cfg.get("mt5_terminal_path"),
            _daily_ops_interval_h,
            _daily_ops_max_age_h,
        ),
        daemon=True,
    )
    _daily_ops_thread.start()

    # ── FIX-20260801-011: SSOT governance scheduler thread ──
    # DQAF-20260801-010: The containerized deployment runs governance_eval via
    # SchedulerService, but the bare-metal launcher never started it — leaving
    # live_intent_loop's DEPRECATED apply_promotion_decisions (BrainPnLStore
    # last-20 window) as the only runtime governance writer → V4 live↔probation
    # oscillation since 07-09.  Run the brain_performance SSOT evaluation here
    # every 60s as the single governance writer (Iron Law #14).
    _governance_thread = threading.Thread(
        target=_governance_scheduler_runner,
        args=(PROJECT_ROOT, cfg["base_dir"], stop_event, log_fh, 60),
        daemon=True,
    )
    _governance_thread.start()

    # FIX-20260628-156 (L3): Validate Producer-Consumer Freshness Contract.
    # Guards against cross-fix drift: if the scheduler max_age is later
    # edited without updating catalog TTLs, this catches it at startup
    # (fail-fast) rather than days later via STALE alerts.
    try:
        from core.state.catalog import validate_freshness_contract

        _violations = validate_freshness_contract(_daily_ops_max_age_h * 3600, emit_warnings=True)
        if _violations:
            _n = len(_violations)
            _msg = f"[launcher] Freshness Contract: {_n} VIOLATION(S) — adjust catalog TTL or scheduler max_age"
            print(_msg, flush=True)
            if log_fh is not None:
                log_fh.write(_msg + "\n")
                log_fh.flush()
        else:
            _msg = "[launcher] Freshness Contract: OK"
            print(_msg, flush=True)
            if log_fh is not None:
                log_fh.write(_msg + "\n")
                log_fh.flush()
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, ImportError) as _fc_exc:
        _msg = f"[launcher] Freshness Contract: SKIPPED (import error: {_fc_exc})"
        print(_msg, flush=True)
        if log_fh is not None:
            log_fh.write(_msg + "\n")
            log_fh.flush()

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
        for proc, name in [
            (bridge_proc, "bridge"),
            (intent_proc, "intent"),
        ]:
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
        for _fh in [bridge_log_fh, intent_log_fh, log_fh]:
            try:  # noqa: SIM105
                _fh.close()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
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

            # Stall detection (every 2nd check = ~10s)
            if int(time_module.time()) % 10 < CHECK_INTERVAL:
                alerts = _check_stall()
                if alerts:
                    _handle_stall_alerts(alerts)

            stop_event.wait(CHECK_INTERVAL)

    except KeyboardInterrupt:
        _on_signal(signal.SIGINT, None)

    # ── Cleanup: terminate any still-running subprocesses ──
    for _proc, _name in [
        (bridge_proc, "bridge"),
        (intent_proc, "intent"),
    ]:
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    return exit_code[0]


if __name__ == "__main__":
    raise SystemExit(launch(sys.argv[1] if len(sys.argv) > 1 else "configs/live.yaml"))

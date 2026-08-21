"""P9 — Launcher hub heartbeat supervisor (TECH_DEBT-015 清偿).

Runs as an independent scheduled probe (schtasks ``QuantOS_Launcher_Guard``,
every 5 min). Its job: close the 5.8h unattended-downtime window (2026-08-10
SIGINT broadcast killed the HUB ``main.py live`` with no auto-recovery).

Three-state machine
-------------------
* ``HEALTHY``  — hub alive. Exit 0, no action.
* ``DEGRADED`` — hub dead, but at least one trading launcher still alive.
  **Fail-safe: alert only, NEVER restart.** Restarting the hub here would
  spawn a second launcher for a config that still has a live process →
  double launcher → double intent → double-open (IC: 双开绝对不能容忍).
* ``RECOVERY`` — hub dead AND no launcher alive (full-chain gap, the 8/10
  shape). Atomically restart the hub under a dual-start lock.

Dual-start protection (three defense layers)
--------------------------------------------
1. Lock file ``data/state/launcher_supervisor.lock`` (``O_CREAT|O_EXCL``):
   first probe wins; a fresh lock blocks concurrent restarts.
2. Re-check inside the lock: if a human / another supervisor already brought
   the hub up, skip.
3. DEGRADED never restarts, so the probe can never stack a hub on top of
   live trading processes.

Pure-logic surface is unit-tested in tests/scripts/test_launcher_supervisor.py
(TDD-first). Process enumeration reuses the wmic pattern from
live_launcher.py (pure stdlib, no psutil).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── matching markers (mirror live_launcher.py wmic enumeration) ─────
_HUB_RE = re.compile(r"main\.py\s+live\b")
_LAUNCHER_MARKER = "live_launcher.py"
_LAUNCHER_CONFIG_MARKERS = ("live.yaml", "live_btc.yaml")

# Dual-start lock TTL: ~2.5× the 5-min probe cadence. A lock older than this
# means the previous restart's owner died mid-flight → takeover is safe.
LOCK_STALENESS_SECONDS = 12 * 60

_WMIC_CMD = [
    "wmic",
    "process",
    "where",
    "name='python.exe'",
    "get",
    "processid,commandline",
    "/format:csv",
]


# ── pure matching ───────────────────────────────────────────────────
def hub_matches(cmdline: str) -> bool:
    """True if the command line is the live-trading HUB (`main.py live`).

    Word boundary after ``live`` distinguishes the subcommand from scripts
    like ``live_cycle.py`` / ``live_intent_loop.py``.
    """
    return _HUB_RE.search(cmdline) is not None


def launcher_matches(cmdline: str) -> bool:
    """True if the command line is a hub-managed launcher (this repo's).

    Requires BOTH the launcher script AND one of our config markers — the
    D:\\cursor launcher (P8: decommissioned, different config args) must not
    count as ours.
    """
    return _LAUNCHER_MARKER in cmdline and any(
        marker in cmdline for marker in _LAUNCHER_CONFIG_MARKERS
    )


def classify_state(hub_alive: bool, launcher_alive: bool) -> str:
    """Map process presence to the three-state machine."""
    if hub_alive:
        return "HEALTHY"
    if launcher_alive:
        return "DEGRADED"
    return "RECOVERY"


def should_restart(state: str) -> bool:
    """Only a full-chain gap (RECOVERY) ever triggers a hub restart."""
    return state == "RECOVERY"


def evaluate(lines: list[str]) -> tuple[str, list[int], list[int]]:
    """Scan raw wmic CSV lines → (state, hub_pids, launcher_pids)."""
    hub_pids: list[int] = []
    launcher_pids: list[int] = []
    for line in lines:
        pid = parse_pid_from_wmic_line(line)
        if pid is None:
            continue
        if hub_matches(line):
            hub_pids.append(pid)
        if launcher_matches(line):
            launcher_pids.append(pid)
    state = classify_state(hub_alive=bool(hub_pids), launcher_alive=bool(launcher_pids))
    return state, hub_pids, launcher_pids


def parse_pid_from_wmic_line(line: str) -> int | None:
    """Extract the trailing PID from one wmic `/format:csv` line.

    The PID is always the last comma-field (even when the command line itself
    contains commas, the quoted field never extends past it) — the same
    property live_launcher.py relies on.
    """
    line = line.strip()
    if not line:
        return None
    pid_str = line.split(",")[-1].strip()
    if not pid_str.isdigit():
        return None
    return int(pid_str)


# ── dual-start lock ─────────────────────────────────────────────────
def lock_payload(pid: int, now_ts: float) -> dict:
    return {"pid": pid, "started_at_unix": now_ts}


def lock_is_fresh(
    payload: dict,
    now_ts: float,
    staleness_seconds: float = LOCK_STALENESS_SECONDS,
) -> bool:
    """True when another restart is genuinely in progress (not stale)."""
    started = payload.get("started_at_unix")
    if not isinstance(started, int | float):
        return False
    age = now_ts - float(started)
    return 0.0 <= age < staleness_seconds


def read_lock(lock_path: str) -> dict:
    try:
        with open(lock_path, encoding="utf-8") as _f:
            data = json.load(_f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def acquire_lock(lock_path: str, pid: int, now: float) -> tuple[bool, str]:
    """Atomically acquire the dual-start lock.

    Returns ``(True, reason)`` on acquisition; ``(False, reason)`` when a
    fresh lock is held by another supervisor. A lock older than
    ``LOCK_STALENESS_SECONDS`` (owner died mid-restart) is taken over.
    """
    for _attempt in range(3):  # stale takeover may race with a remover
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            payload = read_lock(lock_path)
            if lock_is_fresh(payload, now):
                return False, f"lock held by pid {payload.get('pid')}"
            try:
                os.remove(lock_path)
            except OSError:
                return False, "stale lock unlink failed"
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as _f:
            json.dump(lock_payload(pid, now), _f)
        return True, "acquired"
    return False, "lock contention"


def release_lock(lock_path: str, owner_pid: int) -> bool:
    """Remove the lock only if we own it (non-owners must not clear it)."""
    payload = read_lock(lock_path)
    if payload.get("pid") != owner_pid:
        return False
    try:
        os.remove(lock_path)
    except OSError:
        return False
    return True


# ── I/O ─────────────────────────────────────────────────────────────
def enumerate_process_lines(timeout: float = 10.0) -> list[str]:
    """List python.exe command lines via wmic (reuses live_launcher.py)."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            _WMIC_CMD,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def hub_restart_command(project_root: str, python_exe: str) -> list[str]:
    return [python_exe, os.path.join(project_root, "main.py"), "live"]


def start_hub(project_root: Path, log_dir: Path) -> int | None:
    """Detach-start the hub; stdout/stderr tee to a per-run log file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"live_hub_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}.log"
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):  # Windows-only
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab") as _log:
        proc = subprocess.Popen(
            hub_restart_command(str(project_root), sys.executable),
            cwd=str(project_root),
            stdout=_log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    return proc.pid


def send_alert(title: str, text: str, webhook: str, secret: str = "") -> bool:
    """Fire a DingTalk markdown alert (Type A). Empty webhook → no-op."""
    if not webhook:
        return False
    try:
        from core.observability.alert_channels import DingTalkAlertChannel
    except (ImportError, RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return False
    channel = DingTalkAlertChannel(webhook_url=webhook, secret=secret)
    return channel.send(
        {
            "title": title,
            "text": text,
            "severity": "critical",
            "fired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


# ── orchestration ───────────────────────────────────────────────────
def resolve_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_probe_log(project_root: Path, line: str) -> None:
    """Append one probe line to ``data/logs/launcher_supervisor.log``.

    The probe self-logs so the scheduled task needs NO shell redirection
    (keeps the schtasks registration a plain ``python.exe + args`` action).
    Non-fatal: a log failure must never break the scan/restart decision.
    """
    log_dir = project_root / "data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "launcher_supervisor.log", "a", encoding="utf-8") as _f:
            _f.write(f"{line}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P9 launcher-hub heartbeat supervisor (TECH_DEBT-015)."
    )
    parser.add_argument("--project-root", default=None, help="repo root (auto-detect)")
    parser.add_argument(
        "--alert-webhook",
        default="",
        help="DingTalk webhook; empty → QUANTOS_DINGTALK_WEBHOOK_URL env / silent",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="wmic timeout")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan + report only, never restart / alert",
    )
    args = parser.parse_args(argv)

    project_root = Path(args.project_root) if args.project_root else resolve_project_root()
    state_dir = project_root / "data" / "state"
    lock_path = state_dir / "launcher_supervisor.lock"

    lines = enumerate_process_lines(timeout=args.timeout)
    state, hub_pids, launcher_pids = evaluate(lines)

    state_line = (
        f"[launcher_supervisor] state={state} hub_pids={hub_pids} "
        f"launcher_pids={launcher_pids} ts={_utc_ts()}"
    )
    print(state_line, flush=True)
    _append_probe_log(project_root, state_line)

    if state == "HEALTHY":
        # No action; the next RECOVERY tick's takeover handles any stale lock.
        return 0

    if state == "DEGRADED":
        # Fail-safe: hub dead but trading still alive → ALERT ONLY.
        msg = (
            f"hub (main.py live) 死亡但 launcher 仍存活 {launcher_pids} — 降级态只告警不动作, "
            "双开防护保持, 请人工拉起 hub"
        )
        if args.dry_run:
            print(f"[dry-run] would alert DEGRADED: {msg}", flush=True)
        else:
            send_alert("[QuantOS P9] HUB DEGRADED", msg, args.alert_webhook)
            _append_probe_log(project_root, "[launcher_supervisor] DEGRADED alert dispatched")
        return 0

    # RECOVERY: full-chain gap → atomic hub restart under dual-start lock.
    # `should_restart` is the single-source policy predicate (only RECOVERY
    # ever restarts; a future 4th state defaults to no-action).
    if not should_restart(state):
        print(f"[launcher_supervisor] no-action for state={state}", flush=True)
        return 0
    if args.dry_run:
        print("[dry-run] would restart hub (main.py live)", flush=True)
        return 0

    ok, reason = acquire_lock(str(lock_path), os.getpid(), time.time())
    if not ok:
        print(f"[launcher_supervisor] skip restart — {reason}", flush=True)
        return 0
    try:
        # Dual-start protection #1: re-check the hub INSIDE the lock (race guard).
        state2, hub_pids2, _launcher_pids2 = evaluate(enumerate_process_lines(timeout=args.timeout))
        if state2 != "RECOVERY":
            print(
                f"[launcher_supervisor] re-check state={state2} hub_pids={hub_pids2} "
                "— hub already up, skip restart",
                flush=True,
            )
            return 0
        pid = start_hub(project_root, project_root / "data" / "logs")
        restart_line = f"[launcher_supervisor] HUB RESTARTED pid={pid} ts={_utc_ts()}"
        print(restart_line, flush=True)
        _append_probe_log(project_root, restart_line)
        send_alert(
            "[QuantOS P9] HUB AUTO-RESTARTED",
            f"5.8h 空窗防护: hub 失活被探针原子拉起 (pid={pid}, ts={_utc_ts()})",
            args.alert_webhook,
        )
        return 0
    finally:
        release_lock(str(lock_path), os.getpid())


if __name__ == "__main__":
    sys.exit(main())

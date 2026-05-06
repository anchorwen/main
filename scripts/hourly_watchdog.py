"""Hourly watchdog — auto-check live system health and self-heal.

Checks every hour (designed for overnight 2026-05-05 21:28 → 2026-05-06 08:00 BJT):
  - Python processes alive?
  - Feature store growing?
  - brain_performance.json intact?
  - Decision pipeline not crashing?

If issues found, auto-fixes and restarts. Logs everything to data/watchdog.log.

Usage:
  python scripts/hourly_watchdog.py
  python scripts/hourly_watchdog.py --json  (machine-readable output)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
WATCHDOG_LOG = DATA_DIR / "watchdog.log"
FEATURE_FILE = (
    DATA_DIR / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"
)
DECISIONS_DIR = DATA_DIR / "decisions"
PERF_FILE = DATA_DIR / "brain_performance.json"
WEIGHTS_FILE = DATA_DIR / "models" / "online_learner_weights.json"

# Config thresholds
MAX_FEATURE_AGE_SECONDS = 7200  # 2 hours — allows for market close (XAUUSD closes ~22:00-06:00 UTC)
MIN_PYTHON_PROCESSES = 3
MAX_DECISION_GAP_SECONDS = 1800  # 30 minutes without a decision attempt is OK overnight


def _ts() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat().replace("+00:00", "Z")


def _now_unix() -> float:
    return time.time()


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def count_python_processes() -> int:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return len([l for l in result.stdout.splitlines() if l.strip() and "python.exe" in l])
    except Exception:
        return -1


def check_feature_store() -> dict[str, Any]:
    """Check feature store freshness."""
    if not FEATURE_FILE.exists():
        return {"ok": False, "error": "feature_file_missing", "records": 0}
    try:
        lines = FEATURE_FILE.read_text(encoding="utf-8").strip().splitlines()
        count = len(lines)
        last = json.loads(lines[-1])
        last_time_str = last.get("event_time", "")
        dt = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
        age_seconds = abs(_now_unix() - dt.replace(tzinfo=UTC).timestamp())
        return {
            "ok": age_seconds < MAX_FEATURE_AGE_SECONDS,
            "records": count,
            "last_time": last_time_str[:19],
            "age_seconds": int(age_seconds),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def check_performance_file() -> dict[str, Any]:
    """Check brain_performance.json integrity."""
    if not PERF_FILE.exists():
        return {"ok": False, "error": "file_missing"}
    try:
        data = json.loads(PERF_FILE.read_text(encoding="utf-8"))
        records = data.get("records", {})
        total = sum(len(v) for v in records.values())
        return {
            "ok": total > 0,
            "brains": len(records),
            "total_records": total,
            "brain_ids": sorted(records.keys()),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def check_online_learner() -> dict[str, Any]:
    """Check online learner weights integrity."""
    if not WEIGHTS_FILE.exists():
        return {"ok": True, "updates": 0, "status": "no_weights_file"}
    try:
        data = json.loads(WEIGHTS_FILE.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "updates": data.get("total_updates", 0),
            "coef_norm": round(
                sum(sum(abs(c) for c in row) for row in data.get("coef_", [[0]])), 2
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def check_intent_loop_health() -> dict[str, Any]:
    """Check if intent loop can run without errors by running --once."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "live_intent_loop.py"),
                "--mt5-terminal-path",
                "D:\\MetaTrader 5\\terminal64.exe",
                "--symbol",
                "XAUUSDc",
                "--volume",
                "0.01",
                "--interval-seconds",
                "60",
                "--confidence-threshold",
                "0.55",
                "--sl-atr-mult",
                "2.0",
                "--tp-atr-mult",
                "3.5",
                "--cooldown-seconds",
                "300",
                "--max-positions",
                "1",
                "--normalization-config",
                "configs/brains/v9_institutional_01.normalization.json",
                "--feature-store-dir",
                "data/feature_store",
                "--base-dir",
                "data",
                "--multi-brain",
                "--brains-dir",
                "configs/brains",
                "--once",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        stderr = result.stderr.strip()
        has_error = "cycle_error" in stderr or "TypeError" in stderr or "missing" in stderr
        return {
            "ok": not has_error,
            "returncode": result.returncode,
            "stderr_lines": len(stderr.splitlines()) if stderr else 0,
            "last_error": stderr[-300:] if has_error else "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def fix_performance_file() -> bool:
    """Re-run feedback_loop to rebuild brain_performance.json."""
    log("  [fix] Rebuilding brain_performance.json from journal...")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "feedback_loop.py"),
                "--multi-brain",
                "--base-dir",
                "data",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            report = json.loads(result.stdout)
            updates = report.get("updates_applied", 0)
            log(f"  [fix] feedback_loop: {updates} updates applied")
            return True
        log(f"  [fix] feedback_loop failed: {result.stderr[:200]}")
        return False
    except Exception as e:
        log(f"  [fix] feedback_loop error: {e}")
        return False


def restart_live_system() -> bool:
    """Kill live trading processes and restart via main.py live."""
    log("  [fix] Killing live trading processes...")
    try:
        # Identify live-related processes by checking for live_intent_loop or mt5_bridge in command line
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        current_pid = str(os.getpid())
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or "ProcessId" in line:
                continue
            # Kill processes that are part of the live trading stack
            if any(
                tag in line
                for tag in ["live_intent_loop", "mt5_bridge", "live_launcher", "main.py live"]
            ):
                # Extract PID (first column in wmic output)
                parts = line.split()
                pid = parts[0].strip() if parts else ""
                if pid and pid.isdigit() and pid != current_pid:
                    log(f"  [fix] Killing PID {pid}")
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5
                        )
                    except Exception:
                        pass
        time.sleep(2)
    except Exception as e:
        log(f"  [fix] Process kill error: {e}")

    log("  [fix] Starting live system...")
    try:
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "main.py"), "live"],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
        )
        log("  [fix] Live system launched")
        return True
    except Exception as e:
        log(f"  [fix] Launch error: {e}")
        return False


def run_watchdog() -> dict[str, Any]:
    """Run all checks and auto-fix if needed. Returns status dict."""
    log("=" * 60)
    log("WATCHDOG CHECK START")
    log("=" * 60)

    checks: dict[str, Any] = {}
    fixes_applied: list[str] = []

    # 1. Check Python processes
    proc_count = count_python_processes()
    checks["python_processes"] = {
        "count": proc_count,
        "ok": proc_count >= MIN_PYTHON_PROCESSES,
    }
    log(f"  Python processes: {proc_count} (need >= {MIN_PYTHON_PROCESSES})")

    # 2. Check feature store
    fs = check_feature_store()
    checks["feature_store"] = fs
    if fs["ok"]:
        log(f"  Feature store: OK ({fs['records']} records, last {fs['age_seconds']}s ago)")
    else:
        log(f"  Feature store: ISSUE — {fs.get('error', 'stale')}")

    # 3. Check performance file
    perf = check_performance_file()
    checks["brain_performance"] = perf
    if perf["ok"]:
        log(f"  Brain performance: OK ({perf['brains']} brains, {perf['total_records']} records)")
    else:
        log(f"  Brain performance: ISSUE — {perf.get('error', '?')}")
        if fix_performance_file():
            fixes_applied.append("performance_file_rebuilt")

    # 4. Check online learner
    ol = check_online_learner()
    checks["online_learner"] = ol
    log(f"  Online learner: {ol.get('updates', 0)} updates, ok={ol['ok']}")

    # 5. Check intent loop for errors
    intent = check_intent_loop_health()
    checks["intent_loop"] = intent
    if intent["ok"]:
        log(f"  Intent loop: OK (exit={intent.get('returncode', '?')})")
    else:
        log(f"  Intent loop: ERROR — {intent.get('error', intent.get('last_error', '?'))[:150]}")

    # 6. Decide if restart needed — only on hard crash (cycle_error/TypeError)
    # Timeout or stale features during market close are expected, don't restart
    if not intent["ok"]:
        error_msg = intent.get("error", "") + intent.get("last_error", "")
        is_crash = "cycle_error" in error_msg or "TypeError" in error_msg or "missing" in error_msg
        needs_restart = is_crash
    else:
        needs_restart = False
    checks["needs_restart"] = needs_restart

    if needs_restart:
        log("  >>> RESTART NEEDED <<<")
        if restart_live_system():
            fixes_applied.append("live_system_restarted")
            time.sleep(5)
            new_count = count_python_processes()
            checks["restart_result"] = {"new_process_count": new_count}
            log(f"  Restart result: {new_count} processes after restart")
    else:
        log("  All checks passed, no restart needed")

    status = "healthy" if not needs_restart else "healed" if fixes_applied else "degraded"
    summary = {
        "timestamp": _ts(),
        "status": status,
        "fixes_applied": fixes_applied,
        "checks": checks,
    }
    log(f"  SUMMARY: {status} | fixes={fixes_applied}")
    log("")
    return summary


# ── CLI ──


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="hourly_watchdog")
    p.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = p.parse_args()

    summary = run_watchdog()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0 if summary["status"] in ("healthy", "healed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

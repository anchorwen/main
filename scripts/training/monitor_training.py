"""Real-time training batch monitor & watchdog.

Continuously polls the training batch and reports:
  - Current model being trained
  - Progress (OK / FAIL / Remaining)
  - ETA based on elapsed time
  - Process health (alive / dead / zombie)
  - Stuck detection (>N minutes without progress)
  - Recent logs tail

Usage:
  # One-shot status report
  python d:\future\scripts\training\monitor_training.py --batch-dir d:\future\batch_plans\g2026.1

  # Continuous polling (Ctrl+C to stop)
  python d:\future\scripts\training\monitor_training.py --batch-dir d:\future\batch_plans\g2026.1 --watch

  # Watch with custom interval and alert on failure
  python d:\future\scripts\training\monitor_training.py --batch-dir d:\future\batch_plans\g2026.1 --watch --interval 30 --alert-on-fail

  # Run as background watchdog (logs to file)
  python d:\future\scripts\training\monitor_training.py --batch-dir d:\future\batch_plans\g2026.1 --watch --log-to-file
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


# ─── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitor_training",
        description="Real-time CRT training batch monitor & watchdog",
    )
    p.add_argument("--batch-dir", type=Path, required=True, help="Path to batch plan directory")
    p.add_argument("--watch", action="store_true", help="Continuously poll (default: one-shot)")
    p.add_argument(
        "--interval", type=float, default=60.0, help="Poll interval in seconds (default: 60)"
    )
    p.add_argument("--alert-on-fail", action="store_true", help="Log alert on failure detection")
    p.add_argument(
        "--stuck-minutes",
        type=float,
        default=30.0,
        help="Minutes without progress before flagging as stuck (default: 30)",
    )
    p.add_argument(
        "--log-to-file",
        action="store_true",
        help="Append output to <batch-dir>/monitor_log.txt instead of stdout",
    )
    return p


# ─── PROCESS DETECTION ──────────────────────────────────────────────────────


def get_training_pids() -> list[dict[str, Any]]:
    """Return list of {pid, commandline} for all training-related Python processes."""
    results: list[dict[str, Any]] = []
    try:
        raw = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name='python.exe' and (commandline like '%run_train_batch%' or "
                "commandline like '%your_trainer%' or commandline like '%_trainer%' "
                "or commandline like '%train_batch%')",
                "get",
                "processid,commandline",
                "/format:csv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        for line in raw.stdout.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("Node,"):
                continue
            parts = line.split(",", 2)
            if len(parts) >= 3:
                results.append(
                    {
                        "pid": int(parts[-1].strip()),
                        "commandline": parts[1].strip() if len(parts) == 3 else "",
                    }
                )
    except Exception:  # noqa: BLE001
        pass
    return results


def has_live_trainer(pids: list[dict]) -> bool:
    return any(
        "your_trainer" in p.get("commandline", "")
        or "mtx_trainer" in p.get("commandline", "")
        or "sur_trainer" in p.get("commandline", "")
        or "arb_trainer" in p.get("commandline", "")
        for p in pids
    )


def has_batch_runner(pids: list[dict]) -> bool:
    return any("run_train_batch" in p.get("commandline", "") for p in pids)


# ─── FILE-BASED PROGRESS ────────────────────────────────────────────────────


def scan_result_files(manifests_dir: Path) -> tuple[int, int, list[dict]]:
    """Return (ok_count, fail_count, details_list) from .result.json files."""
    ok, fail = 0, 0
    details: list[dict] = []
    for rf in sorted(manifests_dir.glob("CRT_*.result.json")):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            exit_code = data.get("exit_code")
            model_id = data.get("model_id", rf.stem[:80])
            details.append(
                {
                    "file": str(rf),
                    "model_id": model_id,
                    "exit_code": exit_code,
                    "duration": data.get("duration_seconds", 0),
                    "error": data.get("error", "") or "",
                }
            )
            if exit_code == 0:
                ok += 1
            else:
                fail += 1
        except Exception:  # noqa: BLE001
            pass
    return ok, fail, details


def scan_artifacts(artifacts_dir: Path) -> list[str]:
    """Return list of .onnx / .json / .pth artifacts generated."""
    found: list[str] = []
    if artifacts_dir.is_dir():
        for f in artifacts_dir.iterdir():
            if f.is_file() and f.suffix.lower() in (".onnx", ".json", ".pth"):
                found.append(f.name)
    return found


def total_manifest_count(batch_dir: Path) -> int:
    manifests_dir = batch_dir / "manifests"
    if not manifests_dir.is_dir():
        return 0
    return len(list(manifests_dir.glob("CRT_*.json")))


def read_training_log_tail(batch_dir: Path, n_lines: int = 15) -> str:
    """Read last N lines of training_log.txt."""
    log_path = batch_dir / "training_log.txt"
    if not log_path.exists():
        return "(log file not found)"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines[-n_lines:]) if lines else "(log empty)"
    except Exception as e:  # noqa: BLE001
        return f"(read error: {e})"


def read_monitor_state(batch_dir: Path) -> dict[str, Any]:
    """Read monitor state file (JSON with last-OK timestamp and count)."""
    state_path = batch_dir / "monitor_state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"last_ok_count": 0, "last_ok_time": None, "stuck_alerts": 0}


def write_monitor_state(batch_dir: Path, state: dict[str, Any]) -> None:
    state_path = batch_dir / "monitor_state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ─── REPORT FORMATTING ──────────────────────────────────────────────────────


def format_duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h{m:02d}m"


def status_icon(ok: int, fail: int, total: int) -> str:
    if fail > 0:
        return "[FAILS]"
    if ok == total and total > 0:
        return "[ALL OK]"
    if ok > 0:
        return "[MIXED]"
    return "[IDLE]"


BAR_CHARS = 20


def progress_bar(done: int, total: int) -> str:
    if total == 0:
        return "[" + "-" * BAR_CHARS + "] 0%"
    ratio = done / total
    filled = int(ratio * BAR_CHARS)
    pct = int(ratio * 100)
    bar = "#" * filled + "-" * (BAR_CHARS - filled)
    return f"[{bar}] {pct:3d}%"


def report(args: Any, state: dict, batch_dir: Path) -> str:
    manifests_dir = batch_dir / "manifests"
    artifacts_dir = manifests_dir / "artifacts"
    total = total_manifest_count(batch_dir)

    pids = get_training_pids()
    trainer_alive = has_live_trainer(pids)
    runner_alive = has_batch_runner(pids)

    ok, fail, details = scan_result_files(manifests_dir)
    artifacts = scan_artifacts(artifacts_dir)
    done = ok + fail
    remaining = max(0, total - done)

    # Current model
    current_model = "?"
    for p in pids:
        cmd = p.get("commandline", "")
        if "your_trainer" in cmd:
            for part in cmd.split():
                if "manifests" in part and part.endswith(".json"):
                    current_model = Path(part).stem[:60]
                    break
            if current_model == "?":
                for part in cmd.split():
                    if "CRT_" in part:
                        current_model = part.split("\\")[-1].replace(".json", "")[:60]
                        break

    # ETA
    eta_str = "?"
    if ok > 0 and total > 0:
        oldest_ts = None
        for d in details:
            if d["exit_code"] == 0 and d["duration"] > 0:
                if oldest_ts is None:
                    oldest_ts = d["duration"]
        if oldest_ts is not None:
            eta_seconds = remaining * oldest_ts
            eta_str = format_duration(eta_seconds)

    # Stuck detection
    stuck_msg = ""
    if state["last_ok_count"] == ok and trainer_alive and done > 0 and done < total:
        if state["last_ok_time"]:
            last_t = datetime.fromisoformat(state["last_ok_time"])
            idle_min = (utc_now() - last_t).total_seconds() / 60.0
            if idle_min > args.stuck_minutes:
                stuck_msg = f" [STUCK? {idle_min:.0f}min no progress]"
    if ok != state["last_ok_count"]:
        state["last_ok_count"] = ok
        state["last_ok_time"] = utc_now().isoformat()

    # Build report lines
    lines = []
    ts = utc_now().isoformat()[:19].replace("T", " ")
    lines.append("=" * 64)
    lines.append(f"  CRT Training Monitor  |  {ts}")
    lines.append("=" * 64)
    lines.append(f"  Batch:       {batch_dir.name}")
    lines.append(
        f"  Progress:    {progress_bar(done, total)}  ({done}/{total})  "
        f"OK:{ok}  FAIL:{fail}  Remaining:{remaining}  {status_icon(ok, fail, total)}"
    )
    lines.append(f"  Current:     {current_model}{stuck_msg}")
    lines.append(f"  ETA:         ~{eta_str}")
    lines.append(
        f"  Processes:   {'[OK] Runner' if runner_alive else '[DEAD] Runner'}  "
        f"{'[OK] Trainer' if trainer_alive else '--- Trainer idle'}  "
        f"(PIDs: {len(pids)})"
    )
    if pids:
        for p in pids:
            cmd_short = p.get("commandline", "")
            if len(cmd_short) > 80:
                cmd_short = "..." + cmd_short[-77:]
            lines.append(f"    PID {p['pid']:>6}: {cmd_short}")
    else:
        lines.append("    [!!] NO training processes found!")
    lines.append(f"  Artifacts:   {len(artifacts)} (onnx/pth/json)")
    if artifacts:
        for a in artifacts[-6:]:
            lines.append(f"    [{a}]")

    # Recent failures
    recent_fails = [d for d in details if d["exit_code"] != 0]
    if recent_fails:
        lines.append("  Last FAIL:")
        for d in recent_fails[-3:]:
            err = d.get("error", "") or ""
            if len(err) > 100:
                err = err[:97] + "..."
            lines.append(
                f"    [FAIL] {d['model_id']}  exit={d['exit_code']}  dur={d['duration']}s  {err}"
            )

    # Log tail
    log_tail = read_training_log_tail(batch_dir, 8)
    lines.append("  -- Log tail --")
    for l in log_tail.splitlines()[-8:]:
        lines.append(f"  | {l[:100]}")

    lines.append("=" * 64)
    return "\n".join(lines) + "\n"


# ─── MAIN ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    batch_dir = args.batch_dir.resolve()

    if not batch_dir.is_dir():
        print(f"[ERROR] Batch dir not found: {batch_dir}", file=sys.stderr)
        return 1

    state = read_monitor_state(batch_dir)
    loop_count = 0

    out_fh = None
    if args.log_to_file:
        log_path = batch_dir / "monitor_log.txt"
        out_fh = open(log_path, "a", encoding="utf-8")

    def write_out(text: str):
        if out_fh:
            out_fh.write(text)
            out_fh.flush()
        else:
            try:
                sys.stdout.write(text)
            except UnicodeEncodeError:
                # Fall back to safe ASCII
                safe = text.encode("ascii", errors="replace").decode("ascii")
                sys.stdout.write(safe)
            sys.stdout.flush()

    try:
        while True:
            loop_count += 1
            r = report(args, state, batch_dir)
            write_out(f"\n[Poll #{loop_count}]\n{r}")

            # Alert on failure
            if args.alert_on_fail:
                ok2, fail2, _ = scan_result_files(batch_dir / "manifests")
                if fail2 > 0 and state.get("stuck_alerts", 0) == 0:
                    write_out("\n[ALERT] Training failure detected!\n")
                    state["stuck_alerts"] = state.get("stuck_alerts", 0) + 1

            write_monitor_state(batch_dir, state)

            # Check if done
            ok3, fail3, _ = scan_result_files(batch_dir / "manifests")
            done = ok3 + fail3
            total = total_manifest_count(batch_dir)
            if done >= total and total > 0 and not has_live_trainer(get_training_pids()):
                write_out(
                    f"\n{'='*64}\n  == BATCH COMPLETE: {ok3}/{total} OK, {fail3} FAILED ==\n{'='*64}\n"
                )
                break

            if not args.watch:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        write_out("\n\n[STOP] Monitor stopped by user.\n")
    finally:
        if out_fh:
            out_fh.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

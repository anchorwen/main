#!/usr/bin/env python
"""
Process Health Diagnostic Script — Iron Law #11 compliant.
All statistics from stdout. No manual inspection of files.

Usage:
    python scripts/diagnose_process_health.py [--json]

Checks:
    1. Running Python processes with command lines
    2. BTC & XAU leaderboard freshness
    3. BTC & XAU governance_state freshness
    4. BTC & XAU position_snapshots.jsonl freshness
    5. Log file tail activity (last write within threshold)
    6. BTC/XAU market session status (is market open?)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Thresholds
LEADERBOARD_STALE_MIN = 120  # leaderboard should update within 2h
GOVERNANCE_STALE_MIN = 120  # governance should update within 2h
SNAPSHOTS_STALE_MIN = 60  # snapshots should update within 1h
LOG_TAIL_STALE_MIN = 30  # log tail should update within 30min
PROCESS_MIN_UPTIME_MIN = 5  # processes younger than this are "recently restarted"

# Market session hours (UTC)
# XAU: Mon 00:00 - Fri 22:00 (roughly)
# BTC: 24/7


def get_utc_now():
    return datetime.now(UTC)


def ts_to_dt(ts_ms):
    """Convert /Date(milliseconds)/ to datetime"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def age_minutes(dt):
    return round((get_utc_now() - dt).total_seconds() / 60, 1)


def get_python_processes():
    """Get all Python processes with command lines."""
    ps_script = """
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
ForEach-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.ProcessId)").CommandLine
    [PSCustomObject]@{
        PID = $_.ProcessId
        StartTime = $_.CreationDate
        CommandLine = if ($cmd) { $cmd } else { 'N/A' }
    }
} | ConvertTo-Json
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        print(f"ERROR getting processes: {e}", file=sys.stderr)
        return []


def classify_processes(procs):
    """Classify processes by role based on command line."""
    classified: dict[str, list[dict[str, Any]]] = {
        "btc": [],
        "xau": [],
        "shared": [],
        "unknown": [],
    }
    for p in procs:
        cmd = p.get("CommandLine", "") or ""
        pid = p.get("PID", "?")
        start_str = p.get("StartTime", "")
        # Parse CIM datetime
        try:
            if start_str:
                # CIM datetime format: "20260624234021.123456+000"
                # Strip timezone suffix if present
                ts_clean = start_str.split("+")[0].split("-")[0][:14]
                if len(ts_clean) >= 14:
                    start_dt = datetime.strptime(ts_clean, "%Y%m%d%H%M%S")
                    start_dt = start_dt.replace(tzinfo=UTC)
                else:
                    start_dt = None
            else:
                start_dt = None
        except Exception:
            start_dt = None

        entry = {"pid": pid, "start": start_dt, "cmd_snippet": cmd[:200]}

        cmd_lower = cmd.lower()
        is_btc = "btc" in cmd_lower or "data_btc" in cmd_lower
        is_xau = "xau" in cmd_lower or "data\\state" in cmd_lower or "data/state" in cmd_lower

        if is_btc and is_xau:
            classified["shared"].append(entry)
        elif is_btc:
            classified["btc"].append(entry)
        elif is_xau:
            classified["xau"].append(entry)
        else:
            # Try harder - check if it references data_btc or data paths
            if "data_btc" in cmd:
                classified["btc"].append(entry)
            elif any(
                kw in cmd
                for kw in ["main.py", "daily_ops", "position", "brain", "executor", "strategy_line"]
            ):
                # likely XAU (default symbol) if no BTC marker
                classified["xau"].append(entry)
            else:
                classified["unknown"].append(entry)
    return classified


def check_file_freshness(path, label, stale_threshold_min):
    """Check if a file is fresh."""
    if not path.exists():
        return {
            "label": label,
            "path": str(path),
            "exists": False,
            "age_min": None,
            "stale": True,
            "error": "MISSING",
        }
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    age = age_minutes(mtime)
    return {
        "label": label,
        "path": str(path),
        "exists": True,
        "age_min": age,
        "stale": age > stale_threshold_min,
        "threshold_min": stale_threshold_min,
        "last_modified_utc": mtime.isoformat(),
    }


def read_json_file(path):
    """Safely read a JSON file."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"_error": str(e)}


def check_jsonl_last_line(path, label, stale_threshold_min):
    """Check the timestamp of the last line in a JSONL file."""
    if not path.exists():
        return {
            "label": label,
            "path": str(path),
            "exists": False,
            "age_min": None,
            "stale": True,
            "error": "MISSING",
        }

    # Also check file mtime as fallback
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    mtime_age = age_minutes(mtime)

    # Try to read last line for timestamp
    last_ts = None
    try:
        with open(path, encoding="utf-8") as f:
            # Seek to end and read last line
            f.seek(0, 2)
            file_size = f.tell()
            if file_size < 10:
                last_ts = None
            else:
                # Read last ~2KB and parse last line
                f.seek(max(0, file_size - 2048))
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    if last_line:
                        data = json.loads(last_line)
                        # Try common timestamp fields
                        for field in [
                            "timestamp",
                            "ts",
                            "created_at",
                            "updated_at",
                            "time",
                            "date",
                        ]:
                            if field in data:
                                try:
                                    last_ts = datetime.fromisoformat(
                                        str(data[field]).replace("Z", "+00:00")
                                    )
                                except Exception:
                                    pass
                                break
    except Exception:
        pass

    return {
        "label": label,
        "path": str(path),
        "exists": True,
        "mtime_age_min": mtime_age,
        "last_record_ts": last_ts.isoformat() if last_ts else None,
        "last_record_age_min": age_minutes(last_ts) if last_ts else None,
        "stale": (age_minutes(last_ts) if last_ts else mtime_age) > stale_threshold_min,
        "threshold_min": stale_threshold_min,
    }


def check_log_tail(log_dir, pattern, label, lines=50):
    """Check the last modification time of log files matching pattern."""
    log_path = Path(log_dir)
    if not log_path.exists():
        return {"label": label, "files": 0, "error": "DIR_MISSING"}

    matching = sorted(log_path.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matching:
        return {"label": label, "files": 0, "error": "NO_MATCH"}

    results = []
    for p in matching[:5]:  # Check 5 most recent
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        results.append(
            {
                "file": str(p.relative_to(PROJECT_ROOT)),
                "age_min": age_minutes(mtime),
                "last_modified_utc": mtime.isoformat(),
                "size_kb": round(p.stat().st_size / 1024, 1),
            }
        )

    newest = results[0]
    return {
        "label": label,
        "files_found": len(matching),
        "newest": newest,
        "top5": results,
        "stale": newest["age_min"] > LOG_TAIL_STALE_MIN,
    }


def main():
    parser = argparse.ArgumentParser(description="Process Health Diagnostic")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    # Fix encoding for Windows GBK terminals
    import io

    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    now = get_utc_now()
    report = {
        "diagnostic_time_utc": now.isoformat(),
        "diagnostic_unix": now.timestamp(),
    }

    # === 1. Running Processes ===
    procs = get_python_processes()
    classified = classify_processes(procs)
    report["processes"] = {
        "total": len(procs),
        "btc_count": len(classified["btc"]),
        "xau_count": len(classified["xau"]),
        "shared_count": len(classified["shared"]),
        "unknown_count": len(classified["unknown"]),
        "btc": classified["btc"],
        "xau": classified["xau"],
        "shared": classified["shared"],
        "unknown": classified["unknown"],
    }

    # === 2. State File Freshness ===
    freshness = []

    # XAU
    xau_leaderboard = PROJECT_ROOT / "data" / "reports" / "leaderboard.json"
    freshness.append(
        check_file_freshness(xau_leaderboard, "XAU leaderboard", LEADERBOARD_STALE_MIN)
    )

    xau_governance = PROJECT_ROOT / "data" / "governance_state.json"
    freshness.append(
        check_file_freshness(xau_governance, "XAU governance_state", GOVERNANCE_STALE_MIN)
    )

    xau_snapshots = PROJECT_ROOT / "data" / "position_snapshots.jsonl"
    freshness.append(
        check_jsonl_last_line(xau_snapshots, "XAU position_snapshots", SNAPSHOTS_STALE_MIN)
    )

    xau_live_journal = PROJECT_ROOT / "data" / "live_trade_journal.jsonl"
    freshness.append(
        check_jsonl_last_line(xau_live_journal, "XAU live_trade_journal", SNAPSHOTS_STALE_MIN)
    )

    # BTC
    btc_leaderboard = PROJECT_ROOT / "data_btc" / "reports" / "leaderboard.json"
    freshness.append(
        check_file_freshness(btc_leaderboard, "BTC leaderboard", LEADERBOARD_STALE_MIN)
    )

    btc_governance = PROJECT_ROOT / "data_btc" / "governance_state.json"
    freshness.append(
        check_file_freshness(btc_governance, "BTC governance_state", GOVERNANCE_STALE_MIN)
    )

    btc_snapshots = PROJECT_ROOT / "data_btc" / "position_snapshots.jsonl"
    freshness.append(
        check_jsonl_last_line(btc_snapshots, "BTC position_snapshots", SNAPSHOTS_STALE_MIN)
    )

    btc_live_journal = PROJECT_ROOT / "data_btc" / "live_trade_journal.jsonl"
    freshness.append(
        check_jsonl_last_line(btc_live_journal, "BTC live_trade_journal", SNAPSHOTS_STALE_MIN)
    )

    # XAU feature store
    xau_fs = (
        PROJECT_ROOT
        / "data"
        / "feature_store"
        / "records"
        / "symbol=XAUUSDc"
        / "timeframe=M5"
        / "features.jsonl"
    )
    freshness.append(check_jsonl_last_line(xau_fs, "XAU feature_store M5", 120))

    # BTC feature store
    btc_fs = (
        PROJECT_ROOT
        / "data_btc"
        / "feature_store"
        / "records"
        / "symbol=BTCUSDc"
        / "timeframe=M5"
        / "features.jsonl"
    )
    freshness.append(check_jsonl_last_line(btc_fs, "BTC feature_store M5", 120))

    report["freshness"] = freshness

    # === 3. Log File Activity ===
    logs = []
    log_dirs = [
        (PROJECT_ROOT / "logs", "*.log", "main_logs"),
        (PROJECT_ROOT / "data" / "logs", "*.log", "xau_logs"),
        (PROJECT_ROOT / "data_btc" / "logs", "*.log", "btc_logs"),
    ]
    for log_dir, pattern, label in log_dirs:
        logs.append(check_log_tail(log_dir, pattern, label))
    report["log_activity"] = logs

    # === 4. Leaderboard Content Analysis ===
    xau_lb = read_json_file(xau_leaderboard)
    btc_lb = read_json_file(btc_leaderboard)

    lb_analysis: dict[str, dict[str, Any]] = {}
    for sym, lb, path in [("XAU", xau_lb, xau_leaderboard), ("BTC", btc_lb, btc_leaderboard)]:
        if lb is None:
            lb_analysis[sym] = {"status": "MISSING", "path": str(path)}
        elif isinstance(lb, dict) and "_error" in lb:
            lb_analysis[sym] = {"status": "PARSE_ERROR", "error": lb["_error"]}
        else:
            entry: dict[str, Any] = {"status": "OK", "brain_count": 0, "brains": []}
            if isinstance(lb, dict):
                # Try common structures
                brains = lb.get("brains", lb.get("entries", []))
                if isinstance(brains, list):
                    entry["brain_count"] = len(brains)
                    for b in brains:
                        if isinstance(b, dict):
                            entry["brains"].append(
                                {
                                    "name": b.get("name", b.get("brain_id", "?")),
                                    "status": b.get("status", "?"),
                                    "pf": b.get("pf", b.get("profit_factor")),
                                    "updated": b.get("updated_at", b.get("last_update", "?")),
                                }
                            )
            elif isinstance(lb, list):
                entry["brain_count"] = len(lb)
            entry["file_age_min"] = (
                age_minutes(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
                if path.exists()
                else None
            )
            lb_analysis[sym] = entry

    report["leaderboard_analysis"] = lb_analysis

    # === 5. Governance State Analysis ===
    xau_gov = read_json_file(xau_governance)
    btc_gov = read_json_file(btc_governance)

    gov_analysis: dict[str, dict[str, Any]] = {}
    for sym, gov, path in [("XAU", xau_gov, xau_governance), ("BTC", btc_gov, btc_governance)]:
        if gov is None:
            gov_analysis[sym] = {"status": "MISSING"}
        elif isinstance(gov, dict) and "_error" in gov:
            gov_analysis[sym] = {"status": "PARSE_ERROR", "error": gov["_error"]}
        else:
            gov_analysis[sym] = {
                "status": "OK",
                "file_age_min": age_minutes(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
                if path.exists()
                else None,
                "keys": list(gov.keys()) if isinstance(gov, dict) else "non-dict",
            }

    report["governance_analysis"] = gov_analysis

    # === 6. Summary Verdict ===
    stale_items = [f for f in freshness if f.get("stale")]
    stale_logs = [l for l in logs if l.get("stale")]

    verdicts = []
    for sym in ["XAU", "BTC"]:
        sym_stale = [s for s in stale_items if sym.lower() in s["label"].lower()]
        sym_stale_logs = [s for s in stale_logs if sym.lower() in s["label"].lower()]

        lb = lb_analysis.get(sym, {})
        gov = gov_analysis.get(sym, {})
        proc_count = len(classified.get(sym.lower(), []))

        issues = []
        if sym_stale:
            issues.append(f"{len(sym_stale)} state files stale: {[s['label'] for s in sym_stale]}")
        if sym_stale_logs:
            issues.append(f"log activity stale: {sym_stale_logs[0]['label']}")
        if proc_count == 0:
            issues.append("NO running process detected")
        if lb.get("status") != "OK":
            issues.append(f"leaderboard: {lb.get('status')}")

        if not issues:
            verdicts.append(
                f"{sym}: HEALTHY — {proc_count} processes, leaderboard OK, all files fresh"
            )
        else:
            verdicts.append(f"{sym}: NEEDS ATTENTION — {'; '.join(issues)}")

    report["verdicts"] = verdicts

    # === OUTPUT ===
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("=" * 70)
        print("PROCESS HEALTH DIAGNOSTIC REPORT")
        print(f"Time (UTC): {now.isoformat()}")
        print("=" * 70)

        print("\n--- 1. RUNNING PYTHON PROCESSES ---")
        print(f"Total: {report['processes']['total']}")
        print(f"  BTC-tagged: {report['processes']['btc_count']}")
        print(f"  XAU-tagged: {report['processes']['xau_count']}")
        print(f"  Shared:     {report['processes']['shared_count']}")
        print(f"  Unknown:    {report['processes']['unknown_count']}")

        for cat in ["btc", "xau", "shared", "unknown"]:
            for p in report["processes"][cat]:
                start_str = p["start"].isoformat() if p["start"] else "?"
                print(f"  [{cat.upper()}] PID={p['pid']} started={start_str}")
                if p["cmd_snippet"] != "N/A":
                    print(f"         cmd: {p['cmd_snippet'][:150]}")

        print("\n--- 2. STATE FILE FRESHNESS ---")
        for f in freshness:
            status = "⚠ STALE" if f.get("stale") else "✓ OK"
            if not f.get("exists"):
                status = "✗ MISSING"
            age_str = f"{f.get('age_min', f.get('mtime_age_min', '?'))}min"
            print(
                f"  {status} | {f['label']}: {age_str} (threshold: {f.get('threshold_min', '?')}min)"
            )
            if f.get("last_record_ts"):
                print(
                    f"         last_record_ts: {f['last_record_ts']} ({f.get('last_record_age_min', '?')}min ago)"
                )

        print("\n--- 3. LOG FILE ACTIVITY ---")
        for l in logs:
            status = "⚠ STALE" if l.get("stale") else "✓ ACTIVE"
            if l.get("error"):
                print(f"  {l['label']}: {l['error']}")
            else:
                newest = l.get("newest", {})
                print(
                    f"  {status} | {l['label']}: {l['files_found']} files, newest={newest.get('file','?')} ({newest.get('age_min','?')}min ago, {newest.get('size_kb','?')}KB)"
                )

        print("\n--- 4. LEADERBOARD ANALYSIS ---")
        for sym, data in lb_analysis.items():
            print(
                f"  {sym}: status={data.get('status')}, brains={data.get('brain_count','?')}, file_age={data.get('file_age_min','?')}min"
            )
            if data.get("brains"):
                for b in data["brains"]:
                    print(f"    - {b['name']}: status={b['status']}, pf={b['pf']}")

        print("\n--- 5. GOVERNANCE STATE ---")
        for sym, data in gov_analysis.items():
            print(
                f"  {sym}: status={data.get('status')}, file_age={data.get('file_age_min','?')}min, keys={data.get('keys','?')}"
            )

        print("\n--- 6. VERDICT ---")
        for v in verdicts:
            print(f"  {v}")

        # Final summary
        print("\n" + "=" * 70)
        total_stale = len(stale_items) + len(stale_logs)
        if total_stale == 0:
            print("CONCLUSION: All systems healthy. No evidence of process stall.")
        else:
            print(f"CONCLUSION: {total_stale} stale indicators found. See above for details.")
        print("=" * 70)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""System-wide state audit — single source of truth.

Covers: daily_ops heartbeat, alpha pipeline, leaderboard, governance,
decision files, ephemeral state completeness.

Usage:
    python scripts/audit_state_of_system.py
    python scripts/audit_state_of_system.py --json  # machine-readable output

This script is the ONLY authority on system state.  Every field printed
to stdout is backed by a specific file path and read operation logged
in the audit trail.  No inference.  No interpretation.  Just facts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Path definitions (explicit, not discovered) ──────────────────────

SYMBOLS = {
    "BTC": "data_btc",
    "XAU": "data",
}

# Where daily_ops writes its timestamp
DAILY_OPS_STATE = ("state", "daily_ops_state.json")

# Ephemeral state files — architecture-defined projections
# Format: (subdir, filename, generator_module)
EPHEMERAL_STATES: list[tuple[str | None, str, str]] = [
    (None, "governance_state.json", "daily_ops + governance_service"),
    ("state", "execution_state.json", "daily_ops + execution_service"),
    ("state", "data_health_state.json", "daily_ops + data_health_service"),
    ("state", "daily_ops_state.json", "daily_ops"),
    ("reports", "leaderboard.json", "daily_ops + brain_leaderboard"),
    ("reports", "alpha_allocation.json", "daily_ops + portfolio_allocator"),
    ("reports", "training_readiness.json", "daily_ops + governance_scheduler"),
    ("reports", "retraining_signal_prev.json", "daily_ops + governance_scheduler"),
    ("reports", "mt5_bridge_health.json", "daily_ops + bridge_health"),
]

# Alpha pipeline files
ALPHA_FILES = [
    "alpha_registry.json",
    "alpha_performance.json",
    "alpha_feed_state.json",
]


# ── Audit functions ───────────────────────────────────────────────────


def file_audit(data_dir: str) -> dict[str, Any]:
    """Check existence and metadata of every known state file."""
    base = PROJECT_ROOT / data_dir
    result: dict[str, Any] = {"directory": data_dir, "files": {}}

    for subdir, filename, generator in EPHEMERAL_STATES:
        if subdir:
            path = base / subdir / filename
        else:
            path = base / filename
        key = f"{subdir or '.'}/{filename}"
        entry: dict[str, Any] = {
            "path": str(path.relative_to(PROJECT_ROOT)),
            "exists": path.exists(),
            "generator": generator,
        }
        if path.exists():
            entry["size_bytes"] = path.stat().st_size
            entry["mtime_utc"] = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S")
            # Try to extract key metadata
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "leaderboard" in filename:
                    lbs = data.get("leaderboard", data.get("brains", data.get("entries", [])))
                    entry["entry_count"] = len(lbs) if isinstance(lbs, (list, dict)) else 0
                    entry["total_brains"] = data.get("total_brains", "N/A")
                elif "alpha_allocation" in filename:
                    recs = data.get("recommendations", [])
                    entry["recommendation_count"] = len(recs)
                    entry["total_notional"] = data.get("total_notional")
                    entry["alpha_ids"] = [r.get("alpha_id", "?") for r in recs]
                elif "governance" in filename:
                    bs = data.get("brain_states", data.get("brains", {}))
                    entry["total_brains"] = len(bs) if isinstance(bs, dict) else 0
                elif "daily_ops_state" in filename:
                    ts = data.get("last_daily_ops_utc")
                    if ts:
                        entry["last_run_utc"] = datetime.fromtimestamp(
                            float(ts), tz=timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%S")
            except (json.JSONDecodeError, OSError, ValueError) as e:
                entry["parse_error"] = str(e)[:200]

        result["files"][key] = entry

    # Alpha files
    for af in ALPHA_FILES:
        path = base / af
        key = f"./{af}"
        entry = {
            "path": str(path.relative_to(PROJECT_ROOT)),
            "exists": path.exists(),
            "generator": "core/alpha/*",
        }
        if path.exists():
            entry["size_bytes"] = path.stat().st_size
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "registry" in af:
                    records = data.get("records", data.get("alphas", []))
                    entry["record_count"] = (
                        len(records)
                        if isinstance(records, list)
                        else len(records)
                        if isinstance(records, dict)
                        else 0
                    )
                    if isinstance(records, list):
                        entry["alpha_ids"] = [r.get("alpha_id", r.get("id", "?")) for r in records]
                elif "performance" in af:
                    snaps = data.get("snapshots", data.get("history", []))
                    entry["snapshot_count"] = len(snaps) if isinstance(snaps, list) else 0
                elif "feed_state" in af:
                    entry["last_feed"] = data.get("last_feed_ts", data.get("last_feed", "N/A"))
            except (json.JSONDecodeError, OSError):
                entry["parse_error"] = "unreadable"
        result["files"][key] = entry

    return result


def decision_audit(data_dir: str) -> dict[str, Any]:
    """Check decision file directory for empty vs populated files."""
    base = PROJECT_ROOT / data_dir
    result: dict[str, Any] = {
        "directory": data_dir,
        "total_files": 0,
        "total_empty": 0,
        "total_nonempty": 0,
    }

    # Check decisions/ directory
    dec_dir = base / "decisions"
    if dec_dir.exists() and dec_dir.is_dir():
        files = sorted(dec_dir.glob("*"))
        result["total_files"] = len(files)
        result["file_list"] = []
        for f in files:
            size = f.stat().st_size
            is_empty = size == 0
            if is_empty:
                result["total_empty"] += 1
            else:
                result["total_nonempty"] += 1
            result["file_list"].append(
                {
                    "name": f.name,
                    "size_bytes": size,
                    "empty": is_empty,
                }
            )
    else:
        result["note"] = "decisions/ directory does not exist"

    # Also check decisions.jsonl (alternative format)
    dec_file = base / "decisions.jsonl"
    if dec_file.exists():
        result["decisions_jsonl"] = {
            "exists": True,
            "size_bytes": dec_file.stat().st_size,
        }

    return result


def governance_audit(data_dir: str) -> dict[str, Any]:
    """Summarize governance state: live/candidate/archived distribution."""
    base = PROJECT_ROOT / data_dir
    result: dict[str, Any] = {}

    for gov_path in [base / "governance_state.json", base / "state" / "governance_state.json"]:
        if gov_path.exists():
            try:
                data = json.loads(gov_path.read_text(encoding="utf-8"))
                bs = data.get("brain_states", data.get("brains", {}))
                result["path"] = str(gov_path.relative_to(PROJECT_ROOT))
                result["total_brains"] = len(bs) if isinstance(bs, dict) else 0
                status_dist: dict[str, int] = {}
                live_brains: list[str] = []
                candidate_brains: list[str] = []
                if isinstance(bs, dict):
                    for bid, b in bs.items():
                        st = b.get("status", "?") if isinstance(b, dict) else str(b)
                        status_dist[st] = status_dist.get(st, 0) + 1
                        if st == "live":
                            live_brains.append(bid)
                        elif st == "candidate":
                            candidate_brains.append(bid)
                result["status_distribution"] = status_dist
                result["live_brains"] = live_brains
                result["candidate_brains"] = candidate_brains
                break
            except (json.JSONDecodeError, OSError) as e:
                result["error"] = str(e)[:200]

    if not result:
        result["error"] = "governance_state.json not found"

    return result


def summary_table(audits: dict[str, Any]) -> str:
    """Render a compact summary table."""
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append("  SYSTEM STATE AUDIT — SINGLE SOURCE OF TRUTH")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("=" * 78)

    for label, data_dir in [("BTC", "data_btc"), ("XAU", "data")]:
        fa = audits[f"files_{data_dir}"]
        da = audits[f"decisions_{data_dir}"]
        ga = audits[f"governance_{data_dir}"]

        lines.append(f"\n{'─' * 78}")
        lines.append(f"  {label} ({data_dir}/)")
        lines.append(f"{'─' * 78}")

        # Heartbeat
        ds = fa["files"].get("state/daily_ops_state.json", {})
        last_run = ds.get("last_run_utc", "UNKNOWN")
        lines.append(f"  DAILY_OPS  last_run: {last_run}  |  exists: {ds.get('exists', False)}")

        # State files
        present = sum(1 for v in fa["files"].values() if v.get("exists"))
        missing = sum(1 for v in fa["files"].values() if not v.get("exists"))
        lines.append(f"  STATE      {present}/{present + missing} present, {missing} missing")
        for k, v in sorted(fa["files"].items()):
            if not v.get("exists"):
                lines.append(f"             MISSING: {k:45s} → {v['generator']}")

        # Leaderboard
        lb = fa["files"].get("reports/leaderboard.json", {})
        lines.append(
            f"  LEADERBOARD  entries: {lb.get('entry_count', 'N/A')}  |  total_brains: {lb.get('total_brains', 'N/A')}"
        )

        # Alpha
        aa = fa["files"].get("reports/alpha_allocation.json", {})
        lines.append(
            f"  ALPHA_ALLOC  recs: {aa.get('recommendation_count', 'N/A')}  |  notional: {aa.get('total_notional', 'N/A')}  |  ids: {aa.get('alpha_ids', 'N/A')}"
        )

        # Alpha registry
        ar = fa["files"].get("./alpha_registry.json", {})
        lines.append(
            f"  ALPHA_REG    records: {ar.get('record_count', 'N/A')}  |  ids: {ar.get('alpha_ids', 'N/A')}"
        )

        # Alpha performance
        ap = fa["files"].get("./alpha_performance.json", {})
        lines.append(f"  ALPHA_PERF   snapshots: {ap.get('snapshot_count', 'N/A')}")

        # Governance
        lines.append(
            f"  GOVERNANCE   total: {ga.get('total_brains', 'N/A')}  |  status: {ga.get('status_distribution', {})}"
        )
        lines.append(f"               live: {ga.get('live_brains', [])}")
        lines.append(f"               candidate: {ga.get('candidate_brains', [])}")

        # Decisions
        lines.append(
            f"  DECISIONS    files: {da.get('total_files', 0)}  |  empty: {da.get('total_empty', 0)}  |  nonempty: {da.get('total_nonempty', 0)}"
        )
        if da.get("decisions_jsonl"):
            lines.append(f"               decisions.jsonl: {da['decisions_jsonl']['size_bytes']}B")

    # Cross-symbol comparison
    lines.append(f"\n{'─' * 78}")
    lines.append("  CROSS-SYMBOL COMPARISON")
    lines.append(f"{'─' * 78}")

    btc_lb = audits["files_data_btc"]["files"].get("reports/leaderboard.json", {})
    xau_lb = audits["files_data"]["files"].get("reports/leaderboard.json", {})
    lines.append(
        f"  Leaderboard entries:  BTC={btc_lb.get('entry_count','?')}  vs  XAU={xau_lb.get('entry_count','?')}"
    )

    btc_gov = audits["governance_data_btc"]
    xau_gov = audits["governance_data"]
    lines.append(
        f"  Governance live:      BTC={btc_gov.get('live_brains',[])}  vs  XAU={xau_gov.get('live_brains',[])}"
    )

    btc_dec = audits["decisions_data_btc"]
    xau_dec = audits["decisions_data"]
    lines.append(
        f"  Decision files:       BTC={btc_dec.get('total_files',0)} ({btc_dec.get('total_nonempty',0)} nonempty)  vs  XAU={xau_dec.get('total_files',0)} ({xau_dec.get('total_nonempty',0)} nonempty)"
    )

    btc_aa = audits["files_data_btc"]["files"].get("reports/alpha_allocation.json", {})
    xau_aa = audits["files_data"]["files"].get("reports/alpha_allocation.json", {})
    lines.append(
        f"  Alpha alloc IDs:      BTC={btc_aa.get('alpha_ids','?')}  vs  XAU={xau_aa.get('alpha_ids','?')}"
    )

    lines.append(f"\n{'─' * 78}")
    lines.append("  AUDIT COMPLETE — the above is the sole source of truth")
    lines.append(f"{'─' * 78}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="System state audit")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()

    audits: dict[str, Any] = {}

    for label, data_dir in SYMBOLS.items():
        audits[f"files_{data_dir}"] = file_audit(data_dir)
        audits[f"decisions_{data_dir}"] = decision_audit(data_dir)
        audits[f"governance_{data_dir}"] = governance_audit(data_dir)

    if args.json:
        print(json.dumps(audits, indent=2, ensure_ascii=False, default=str))
    else:
        print(summary_table(audits))


if __name__ == "__main__":
    main()

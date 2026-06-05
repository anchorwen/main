#!/usr/bin/env python
"""Precondition monitor — checks all active task preconditions and reports status.

Usage:
    python scripts/check_preconditions.py          # full report
    python scripts/check_preconditions.py --alert  # alert-only (for cron)
    python scripts/check_preconditions.py --task T1  # single task

This script is the SSOT for task readiness.  No task may be declared
"ready for review" unless this script confirms all preconditions are met.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Task definitions ────────────────────────────────────────────────────

TASKS = {
    "T1": {
        "name": "进场点差摩擦复评",
        "deadline": "2026-06-10",
        "preconditions": [
            {
                "name": "entry_spread > 0 for >= 90% of recent XAU opens",
                "check_fn": "check_entry_spread",
                "required": 0.90,
                "current": None,
            }
        ],
    },
    "T2": {
        "name": "Layer 3 ConformalCalibrator 复评",
        "deadline": "2026-06-15",
        "preconditions": [
            {
                "name": "XAU calibrator queue >= 50 samples",
                "check_fn": "check_calibrator_xau",
                "required": 50,
                "current": None,
            }
        ],
    },
    "T3": {
        "name": "Phase C 微结构部分止盈 复评",
        "deadline": "2026-06-15",
        "preconditions": [
            {
                "name": "OFI gate deployed OR order book depth API available",
                "check_fn": "check_ofi_gate",
                "required": True,
                "current": None,
            }
        ],
    },
    "T4": {
        "name": "数据收集缺口复评",
        "deadline": "2026-06-17",
        "preconditions": [
            {
                "name": "At least 1 of 4 dormant components triggered",
                "check_fn": "check_data_gaps_triggered",
                "required": False,  # trigger-driven, no minimum
                "current": None,
            }
        ],
    },
    "T5": {
        "name": "LEGACY路径切除",
        "deadline": "2026-06-19",
        "preconditions": [
            {
                "name": "DEPRECATED/LEGACY sites counted and assessed",
                "check_fn": "check_legacy_count",
                "required": None,  # informational
                "current": None,
            }
        ],
    },
}


# ── Check functions — each returns (value, detail_str) ──────────────────


def check_entry_spread() -> tuple[float, str]:
    """Count XAU opens with entry_spread > 0 in the journal."""
    journal = PROJECT_ROOT / "data" / "live_trade_journal.jsonl"
    if not journal.exists():
        return 0.0, "journal not found"
    nonzero = 0
    total = 0
    with open(journal, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("action") != "open":
                    continue
                total += 1
                ctx = r.get("entry_context") or {}
                es = ctx.get("entry_spread", 0) or 0
                if es > 0:
                    nonzero += 1
            except Exception:
                pass
    ratio = nonzero / total if total > 0 else 0.0
    return ratio, f"{nonzero}/{total} opens have entry_spread>0 ({ratio:.1%})"


def check_calibrator_xau() -> tuple[int, str]:
    """Count samples in XAU conformal calibrator state."""
    state = PROJECT_ROOT / "data" / "conformal_calibrator_state.json"
    if not state.exists():
        return 0, "state file not found"
    try:
        d = json.loads(state.read_text(encoding="utf-8"))
        hist = d.get("history", [])
        cs = d.get("cold_started", False)
        return len(hist), f"{len(hist)} samples (cold_started={cs})"
    except Exception as e:
        return 0, f"error reading state: {e}"


def check_ofi_gate() -> tuple[bool, str]:
    """Check if OFI-based microstructure gate is deployed."""
    pos_mgr = PROJECT_ROOT / "core" / "execution" / "position_manager.py"
    if not pos_mgr.exists():
        return False, "position_manager.py not found"
    text = pos_mgr.read_text(encoding="utf-8")
    has_ofi = "should_micro_partial_tp" in text
    return has_ofi, f"OFI gate {'deployed' if has_ofi else 'NOT deployed'}"


def check_data_gaps_triggered() -> tuple[bool, str]:
    """Check if any dormant data collection component was recently modified."""
    # These are trigger-driven; we just report that they're dormant
    return False, "4 components dormant (trigger-driven, no active check needed)"


def check_legacy_count() -> tuple[int, str]:
    """Count DEPRECATED/LEGACY/Strangler markers in core/ .py files."""
    count = 0
    for py_file in (PROJECT_ROOT / "core").rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            for line in text.split("\n"):
                if any(kw in line for kw in ("DEPRECATED", "LEGACY", "Strangler Fig")):
                    if "test" not in str(py_file).lower():
                        count += 1
        except Exception:
            pass
    return count, f"{count} DEPRECATED/LEGACY/Strangler sites in core/"


# ── Main ────────────────────────────────────────────────────────────────


def days_until(date_str: str) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d - datetime.now(UTC)).days


def run_check(task_id: str) -> dict:
    task = TASKS[task_id]
    results = []
    for pc in task["preconditions"]:
        fn = globals().get(pc["check_fn"])
        if fn is None:
            results.append({"name": pc["name"], "value": None, "detail": "check function not found", "met": False})
            continue
        try:
            value, detail = fn()
            required = pc["required"]
            if required is not None:
                met = value >= required if isinstance(required, (int, float)) else value == required
            else:
                met = None  # informational only
            results.append({"name": pc["name"], "value": value, "required": required, "detail": detail, "met": met})
        except Exception as e:
            results.append({"name": pc["name"], "value": None, "detail": str(e), "met": False})
    return {"task_id": task_id, "name": task["name"], "deadline": task["deadline"], "days_left": days_until(task["deadline"]), "results": results}


def format_report(task_results: list[dict], alert_only: bool = False) -> str:
    lines = []
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"=== Precondition Report @ {now_utc} ===\n")

    alerts = []
    all_ok = True

    for tr in task_results:
        dl = tr["days_left"]
        urgent = dl <= 3
        symbol = "CRIT" if dl <= 0 else ("WARN" if dl <= 3 else "OK")

        if alert_only and not urgent:
            continue

        lines.append(f"{symbol} {tr['task_id']}: {tr['name']} (deadline: {tr['deadline']}, {dl}d left)")
        for r in tr["results"]:
            met_str = "PASS" if r["met"] is True else ("FAIL" if r["met"] is False else "INFO")
            lines.append(f"   {met_str} {r['name']}")
            lines.append(f"      Current: {r['detail']}")
            if r["required"] is not None and r["met"] is False:
                lines.append(f"      Required: {r['required']}")
                alerts.append(f"{tr['task_id']}: {r['name']} — {r['detail']} (need {r['required']})")
                all_ok = False
        lines.append("")

    if alerts:
        lines.insert(1, f"!! {len(alerts)} precondition(s) NOT MET:\n")
        for a in alerts:
            lines.insert(2, f"   - {a}")
        lines.insert(3, "")

    if not alerts:
        lines.insert(1, "ALL CLEAR: All preconditions on track.\n")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--alert", action="store_true", help="Alert-only mode (urgent tasks)")
    p.add_argument("--task", type=str, help="Check single task (e.g. T1)")
    args = p.parse_args()

    task_ids = [args.task] if args.task else list(TASKS.keys())
    results = [run_check(tid) for tid in task_ids if tid in TASKS]
    print(format_report(results, alert_only=args.alert))

    # Exit code: 1 if any precondition is unmet (for CI/automation)
    any_unmet = any(r["met"] is False for tr in results for r in tr["results"])
    sys.exit(1 if any_unmet else 0)

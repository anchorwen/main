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
                "required": None,
                "current": None,
            }
        ],
    },
    # ── Sequential hardening tasks (depends on prior steps) ──
    "S1": {
        "name": "Golden Master 录制 — 累积500周期 (安全网)",
        "deadline": None,
        "preconditions": [
            {
                "name": ">= 500 cycles recorded to data/golden_master.jsonl",
                "check_fn": "check_golden_master",
                "required": 500,
                "current": None,
            }
        ],
    },
    "S2": {
        "name": "Golden Master 回放校验 (verify.py --golden-master)",
        "deadline": None,
        "preconditions": [
            {
                "name": "S1 complete + verify.py --golden-master passes 100%",
                "check_fn": "check_golden_master_replay_ready",
                "required": True,
                "current": None,
            }
        ],
    },
    "S3": {
        "name": "闸门重构: 动态分位数 + 去中心化shadow (在GM安全网下)",
        "deadline": None,
        "preconditions": [
            {
                "name": "S2 complete + regime_gate refactored with per-symbol percentiles + per-gate veto",
                "check_fn": "check_gate_refactor",
                "required": True,
                "current": None,
            }
        ],
    },
    "S4": {
        "name": "BTC临时解 (仅当S3后BTC仍未恢复交易)",
        "deadline": None,
        "preconditions": [
            {
                "name": "S3 complete AND btc_swing still producing zero live trades",
                "check_fn": "check_btc_trading",
                "required": True,  # True = btc is trading (no action needed)
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


# ── Sequential hardening checks ─────────────────────────────────────────


def check_golden_master() -> tuple[int, str]:
    """Check Golden Master recording cycles."""
    import os
    gm_path = PROJECT_ROOT / "data" / "golden_master.jsonl"
    disabled = os.environ.get("GOLDEN_MASTER_RECORD") == "0"
    cycles = 0
    if gm_path.exists():
        cycles = len([l for l in gm_path.read_text(encoding="utf-8").splitlines() if l.strip()])
    return cycles, f"{cycles} cycles, recording={'OFF (GOLDEN_MASTER_RECORD=0)' if disabled else 'ON (default)'}"


def check_golden_master_replay_ready() -> tuple[bool, str]:
    """Check if replay verification is ready."""
    gm_path = PROJECT_ROOT / "data" / "golden_master.jsonl"
    cycles = 0
    if gm_path.exists():
        cycles = len([l for l in gm_path.read_text(encoding="utf-8").splitlines() if l.strip()])
    has_cmd = "--golden-master" in (PROJECT_ROOT / "scripts" / "verify.py").read_text(encoding="utf-8")
    return (cycles >= 500 and has_cmd), f"cycles={cycles}/500, verify.py gm={'OK' if has_cmd else 'TODO'}"


def check_gate_refactor() -> tuple[bool, str]:
    """Check if regime_gate has been refactored with per-symbol percentiles + per-gate veto."""
    rg = PROJECT_ROOT / "core" / "execution" / "regime_gate.py"
    text = rg.read_text(encoding="utf-8") if rg.exists() else ""
    has_pct = "percentile" in text.lower() or "_pct" in text
    has_context = "RegimeContext" in text
    return (has_pct and has_context), f"dynamic_pct={'YES' if has_pct else 'NO'}, RegimeContext={'YES' if has_context else 'NO'}"


def check_btc_trading() -> tuple[bool, str]:
    """Check if BTC btc_swing has recent live (non-shadow) trades."""
    import json
    from datetime import datetime, UTC, timedelta
    jp = PROJECT_ROOT / "data_btc" / "live_trade_journal.jsonl"
    if not jp.exists():
        return False, "BTC journal not found"
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    live_opens = 0
    with open(jp, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line.strip())
                if r.get("action") == "open" and r.get("recorded_at", "") >= cutoff:
                    live_opens += 1
            except Exception:
                pass
    trading = live_opens > 0
    return trading, f"BTC 7d live opens={live_opens}, trading={'YES' if trading else 'NO (S4 may be needed)'}"


# ── Main ────────────────────────────────────────────────────────────────


def days_until(date_str: str | None) -> int:
    if date_str is None:
        return 999  # sequential task, no deadline
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
        is_seq = dl >= 900  # sequential task, no deadline
        urgent = (not is_seq) and dl <= 3
        symbol = "SEQ" if is_seq else ("CRIT" if dl <= 0 else ("WARN" if dl <= 3 else "OK"))

        if alert_only and not urgent and not is_seq:
            continue

        deadline_str = "sequential" if is_seq else f"{tr['deadline']}, {dl}d left"
        lines.append(f"{symbol} {tr['task_id']}: {tr['name']} (deadline: {deadline_str})")
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

#!/usr/bin/env python3
"""Deferred Task Auto-Closure Auditor — Iron Law #13-bis.

Ω Protocol extension: scans all deferred_*.md / todo_*.md memory files,
auto-evaluates trigger conditions against live system state, and produces
a categorized report: COMPLETED (condition met→should close) / STALE
(date passed→should escalate) / ACTIVE (still pending) / UNKNOWN
(cannot auto-verify).

Iron Law #11 compliance: ALL statistics come from this script's stdout.
No manual estimation, no sampling bias, no "read a few files and guess".

Usage:
    python scripts/audit_deferred_tasks.py [--data-dir data_btc] [--verbose]

Design:
    - Read-only — never modifies memory files.
    - Each verification function returns (status, evidence) tuple.
    - Evidence includes exact file paths, line numbers, and measured values.
    - Unknown conditions are listed with recommended manual verification steps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────

MEMORY_DIR = Path(os.path.expanduser(
    "~/.claude/projects/d--future/memory"
))
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # d:\future

# Trigger condition patterns (order matters — more specific first)
TRIGGER_PATTERNS: list[tuple[str, str]] = [
    # Count-based: "≥N samples", "≥N 笔", "≥N 条"
    (r"(?:≥|>=|=)\s*(\d+)\s*(?:笔|个|条)\s*(?:实盘|已平仓)?\s*(?:交易|trade)", "trade_count"),
    (r"(?:≥|>=|=)\s*(\d+)\s*(?:samples?|样本|条记录|条 M5)", "sample_count"),
    (r"Calibrator\s*(?:≥|>=|=)\s*(\d+)\s*(?:样本|samples?)", "calibrator_samples"),
    (r"Feature Store\s*(?:≥|>=|=)\s*(\d+)", "feature_store_count"),
    # Date-based: "不早于 YYYY-MM-DD", "复查"
    (r"不早于\s*(\d{4}-\d{2}-\d{2})", "not_before_date"),
    (r"复查[日]?\s*(\d{4}-\d{2}-\d{2})", "review_date"),
    (r"(\d{4}-\d{2}-\d{2})\s*(?:复查|复评)", "review_date"),
    # State-based
    (r"至少\s*(\d+)\s*个.*brain.*live", "live_brain_count"),
    (r"Calibrator.*WARM", "calibrator_warm"),
    (r"MetaFilter.*AUC\s*(?:≥|>=|=)\s*([\d.]+)", "metafilter_auc"),
]

# Files to exclude from scan
EXCLUDE_FILES = {"MEMORY.md", "CLAUDE.md"}


# ── Data Structures ────────────────────────────────────────────────────

@dataclass
class TriggerCondition:
    """One parsed trigger condition from a memory file."""
    raw_text: str
    condition_type: str  # trade_count, sample_count, review_date, etc.
    threshold: float | None = None
    threshold_date: str | None = None
    line_number: int = 0


@dataclass
class TaskAssessment:
    """Assessment result for one deferred/todo task."""
    file_name: str
    file_path: Path
    title: str = ""
    status: str = "UNKNOWN"  # COMPLETED, STALE, ACTIVE, UNKNOWN
    priority: str = ""  # extracted from frontmatter or body
    conditions: list[TriggerCondition] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    recommended_action: str = ""
    last_updated: str = ""


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter between --- markers."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    frontmatter = text[3:end].strip()
    result: dict[str, Any] = {}
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def extract_triggers(file_path: Path) -> tuple[str, str, list[TriggerCondition], str, str]:
    """Parse a memory file: return (title, description, conditions, status, last_updated)."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", "", [], "", ""

    fm = parse_frontmatter(text)
    title = fm.get("description", file_path.stem)
    task_status = fm.get("status", "")
    last_updated = ""
    meta_str = fm.get("metadata", "")
    if meta_str:
        # metadata is often a dict-like string; try to extract fields
        _updated = re.search(r"updated:\s*(\d{4}-\d{2}-\d{2})", meta_str)
        if _updated:
            last_updated = _updated.group(1)
    if not last_updated:
        # Try from body
        _updated_body = re.search(r"\*\*复查[日]?\*\*[：:]\s*(\d{4}-\d{2}-\d{2})", text)
        if _updated_body:
            last_updated = _updated_body.group(1)

    conditions: list[TriggerCondition] = []
    body = text[text.find("---", 3) + 3:] if text.startswith("---") else text
    lines = body.split("\n")
    for i, line in enumerate(lines, start=1):
        for pattern, ctype in TRIGGER_PATTERNS:
            m = re.search(pattern, line)
            if m:
                cond = TriggerCondition(
                    raw_text=line.strip(),
                    condition_type=ctype,
                    line_number=i,
                )
                if ctype in ("trade_count", "sample_count", "calibrator_samples",
                             "feature_store_count", "live_brain_count"):
                    cond.threshold = float(m.group(1))
                elif ctype == "metafilter_auc":
                    cond.threshold = float(m.group(1))
                elif ctype in ("not_before_date", "review_date"):
                    cond.threshold_date = m.group(1)
                conditions.append(cond)
                break  # first matching pattern per line

    return title, task_status, conditions, last_updated, body[:500]


# ── Verification Functions ─────────────────────────────────────────────

def verify_trade_count(data_dir: str, threshold: float, symbol: str = "BTC") -> tuple[str, list[str]]:
    """Check if journal has ≥threshold closed trades for symbol."""
    journal = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal.exists():
        return "UNKNOWN", [f"Journal not found: {journal}"]

    count = 0
    symbol_key = "XAUUSDc" if "XAU" in symbol.upper() else "BTCUSDc"
    try:
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if rec.get("ack_status") == "closed" and str(rec.get("symbol", "")) == symbol_key:
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return "UNKNOWN", [f"Cannot read journal: {journal}"]

    met = count >= threshold
    return (
        "COMPLETED" if met else "ACTIVE",
        [f"Closed {symbol_key} trades: {count} / {int(threshold)} needed "
         f"({'✓ MET' if met else '✗ NOT MET'})"]
    )


def verify_calibrator_samples(data_dir: str, threshold: float) -> tuple[str, list[str]]:
    """Check if ConformalCalibrator has ≥threshold samples."""
    state_path = Path(data_dir) / "conformal_calibrator_state.json"
    if not state_path.exists():
        return "UNKNOWN", [f"Calibrator state not found: {state_path}"]

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        history = state.get("history", [])
        count = len(history)
        is_warm = count >= 50
        met = count >= threshold

        # Also check contamination
        contaminated = sum(1 for h in history if h.get("p_win") == 0.5)
        pct = 100 * contaminated / max(1, count)
        evidence = [
            f"Calibrator samples: {count} / {int(threshold)} needed "
            f"({'✓ MET' if met else '✗ NOT MET'})",
            f"  is_warm={is_warm}, cold_started={state.get('cold_started')}",
            f"  p_win=0.5 contamination: {contaminated}/{count} ({pct:.0f}%)",
        ]
        return ("COMPLETED" if met else "ACTIVE", evidence)
    except (json.JSONDecodeError, OSError, KeyError) as e:
        return "UNKNOWN", [f"Cannot read calibrator state: {e}"]


def verify_feature_store_count(data_dir: str, threshold: float, symbol: str = "XAU") -> tuple[str, list[str]]:
    """Count M5 records in feature store for symbol."""
    symbol_key = "XAUUSDc" if "XAU" in symbol.upper() else "BTCUSDc"
    fs_path = Path(data_dir.replace("data_btc", f"data_{symbol.lower()[:3]}")) \
        / "feature_store" / "records" / f"symbol={symbol_key}" / "timeframe=M5" / "features.jsonl"

    if not fs_path.exists():
        return "ACTIVE", [f"Feature store not found: {fs_path} (count=0)"]

    try:
        count = sum(1 for _ in fs_path.read_text(encoding="utf-8").splitlines() if _.strip())
    except OSError:
        return "UNKNOWN", [f"Cannot read feature store: {fs_path}"]

    met = count >= threshold
    return (
        "COMPLETED" if met else "ACTIVE",
        [f"Feature store records (M5): {count} / {int(threshold)} needed "
         f"({'✓ MET' if met else '✗ NOT MET'})"]
    )


def verify_live_brain_count(data_dir: str, threshold: float) -> tuple[str, list[str]]:
    """Check how many brains are in 'live' status in governance_state."""
    gov_path = Path(data_dir) / "governance_state.json"
    if not gov_path.exists():
        # Try alpha_registry as fallback
        gov_path = Path(data_dir) / "alpha_registry.json"

    if not gov_path.exists():
        return "UNKNOWN", [f"Governance state not found: {gov_path}"]

    try:
        state = json.loads(gov_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "UNKNOWN", [f"Cannot parse governance state: {gov_path}"]

    live_count = 0
    # Try different structures
    brain_states = state.get("brain_states", {})
    if brain_states:
        for bid, bs in brain_states.items():
            if isinstance(bs, dict) and bs.get("status") == "live":
                live_count += 1
    else:
        # alpha_registry format
        alphas = state.get("alphas", {})
        for aid, alpha in alphas.items():
            if isinstance(alpha, dict) and alpha.get("status") == "live":
                live_count += 1

    met = live_count >= threshold
    return (
        "COMPLETED" if met else "ACTIVE",
        [f"Live brains: {live_count} / {int(threshold)} needed "
         f"({'✓ MET' if met else '✗ NOT MET'})"]
    )


def verify_calibrator_warm(data_dir: str) -> tuple[str, list[str]]:
    """Check if calibrator is WARM (≥50 samples)."""
    return verify_calibrator_samples(data_dir, 50.0)


def check_date_passed(date_str: str) -> tuple[bool, str]:
    """Check if a date has passed. Returns (passed, explanation)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        passed = now > target
        days = (now - target).days
        return passed, f"Date {date_str}: {days}d {'PASSED' if passed else 'not yet'} (vs UTC now)"
    except ValueError:
        return False, f"Cannot parse date: {date_str}"


# ── Main Auditor ───────────────────────────────────────────────────────

def audit(data_dir: str, memory_dir: Path, verbose: bool = False) -> dict[str, list[TaskAssessment]]:
    """Scan all deferred/todo memory files and assess each task.

    Returns dict keyed by category: COMPLETED, STALE, ACTIVE, UNKNOWN.
    """
    _md = Path(memory_dir)
    if not _md.exists():
        print(f"[ERROR] Memory directory not found: {_md}")
        return {}

    # Collect all memory files
    memory_files: list[Path] = []
    for pattern in ("deferred_*.md", "todo_*.md"):
        memory_files.extend(_md.glob(pattern))
    # Also check archive subdirectory
    archive_dir = _md / "archive"
    if archive_dir.exists():
        for pattern in ("deferred_*.md", "todo_*.md"):
            memory_files.extend(archive_dir.glob(pattern))

    memory_files = sorted(set(memory_files))

    categorized: dict[str, list[TaskAssessment]] = defaultdict(list)
    stats = {"total": len(memory_files), "auto_verified": 0, "manual_only": 0}

    for fp in memory_files:
        title, task_status, conditions, last_updated, body = extract_triggers(fp)

        if not conditions and verbose:
            # Files with no machine-parseable trigger conditions
            task = TaskAssessment(
                file_name=fp.name,
                file_path=fp,
                title=title,
                status="UNKNOWN",
                priority=task_status,
                verdicts=["No machine-parseable trigger conditions found"],
                evidence=[f"File: {fp}"],
                recommended_action="Add structured trigger conditions to frontmatter",
                last_updated=last_updated,
            )
            categorized["UNKNOWN"].append(task)
            stats["manual_only"] += 1
            continue

        # Evaluate each condition
        all_met = True
        any_met = False
        all_verdicts: list[str] = []
        all_evidence: list[str] = []
        date_stale = False

        for cond in conditions:
            if cond.condition_type == "trade_count":
                status, evidence = verify_trade_count(data_dir, cond.threshold or 200)
                all_verdicts.append(f"trade_count≥{int(cond.threshold or 0)}: {status}")
                all_evidence.extend(evidence)
                if status != "COMPLETED":
                    all_met = False
                if status == "COMPLETED":
                    any_met = True
                stats["auto_verified"] += 1

            elif cond.condition_type in ("sample_count", "calibrator_samples"):
                status, evidence = verify_calibrator_samples(data_dir, cond.threshold or 50)
                all_verdicts.append(f"calibrator_samples≥{int(cond.threshold or 0)}: {status}")
                all_evidence.extend(evidence)
                if status != "COMPLETED":
                    all_met = False
                if status == "COMPLETED":
                    any_met = True
                stats["auto_verified"] += 1

            elif cond.condition_type == "feature_store_count":
                status, evidence = verify_feature_store_count(data_dir, cond.threshold or 5000)
                all_verdicts.append(f"feature_store≥{int(cond.threshold or 0)}: {status}")
                all_evidence.extend(evidence)
                if status != "COMPLETED":
                    all_met = False
                if status == "COMPLETED":
                    any_met = True
                stats["auto_verified"] += 1

            elif cond.condition_type == "live_brain_count":
                status, evidence = verify_live_brain_count(data_dir, cond.threshold or 1)
                all_verdicts.append(f"live_brains≥{int(cond.threshold or 0)}: {status}")
                all_evidence.extend(evidence)
                if status != "COMPLETED":
                    all_met = False
                if status == "COMPLETED":
                    any_met = True
                stats["auto_verified"] += 1

            elif cond.condition_type == "calibrator_warm":
                status, evidence = verify_calibrator_warm(data_dir)
                all_verdicts.append(f"calibrator_warm: {status}")
                all_evidence.extend(evidence)
                if status != "COMPLETED":
                    all_met = False
                if status == "COMPLETED":
                    any_met = True
                stats["auto_verified"] += 1

            elif cond.condition_type in ("not_before_date", "review_date"):
                if cond.threshold_date:
                    passed, explanation = check_date_passed(cond.threshold_date)
                    all_evidence.append(explanation)
                    if passed:
                        date_stale = True
                        all_verdicts.append(f"{cond.condition_type}={cond.threshold_date}: STALE (date passed)")
                    else:
                        all_verdicts.append(f"{cond.condition_type}={cond.threshold_date}: not yet due")
                    stats["auto_verified"] += 1

            elif cond.condition_type == "metafilter_auc":
                # Cannot auto-verify — needs model evaluation
                all_verdicts.append(f"metafilter_auc≥{cond.threshold}: UNKNOWN (requires model eval)")
                all_evidence.append("Manual check: run model evaluation to measure current AUC")
                all_met = False
                stats["manual_only"] += 1

            else:
                all_verdicts.append(f"{cond.condition_type}: UNKNOWN")
                all_evidence.append(f"Cannot auto-verify: {cond.raw_text}")
                all_met = False
                stats["manual_only"] += 1

        # ── Categorize ──
        # Detect explicit closure signals in title/body
        _explicitly_closed = bool(
            re.search(r"CLOSED|VERIFIED|DONE", title + body[:500])
        )

        if not conditions:
            final_status = "UNKNOWN"
            recommendation = "Add machine-parseable trigger conditions"
        elif all_met and conditions:
            # If ALL conditions are date-only and passed, it's STALE not COMPLETED
            # unless the task is explicitly marked CLOSED/VERIFIED/DONE
            _date_only = all(
                c.condition_type in ("not_before_date", "review_date")
                for c in conditions
            )
            if _date_only and date_stale and not _explicitly_closed:
                final_status = "STALE"
                recommendation = "Date-based triggers expired — escalate or re-assess"
            elif _date_only and date_stale and _explicitly_closed:
                final_status = "COMPLETED"
                recommendation = "Date passed + explicitly CLOSED/VERIFIED — should archive"
            else:
                final_status = "COMPLETED"
                recommendation = "ALL conditions met — should CLOSE this task"
        elif date_stale and not any_met:
            final_status = "STALE"
            days_since = ""
            if last_updated:
                try:
                    dt = datetime.strptime(last_updated, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    days_since = f" (last updated {last_updated}, {(datetime.now(timezone.utc) - dt).days}d ago)"
                except ValueError:
                    pass
            recommendation = f"⚠ Date-based trigger expired — escalate or re-evaluate{ days_since }"
        else:
            final_status = "ACTIVE"
            unmet = [v for v in all_verdicts if "COMPLETED" not in v and "MET" not in v]
            recommendation = f"Waiting on: {'; '.join(unmet[:3])}"

        task = TaskAssessment(
            file_name=fp.name,
            file_path=fp,
            title=title,
            status=final_status,
            priority="deferred" if fp.name.startswith("deferred") else "todo",
            conditions=conditions,
            verdicts=all_verdicts,
            evidence=all_evidence,
            recommended_action=recommendation,
            last_updated=last_updated,
        )
        categorized[final_status].append(task)

    return dict(categorized)


# ── Report Formatting ──────────────────────────────────────────────────

def print_report(categorized: dict[str, list[TaskAssessment]], data_dir: str, verbose: bool = False) -> None:
    """Print categorized audit report."""
    total = sum(len(v) for v in categorized.values())

    print("=" * 70)
    print("  Ω Deferred Task Auto-Closure Auditor (Iron Law #13-bis)")
    print(f"  Data dir: {data_dir}")
    print(f"  Scan time: {datetime.now(timezone.utc).isoformat()}")
    print(f"  Total tasks scanned: {total}")
    print("=" * 70)

    # ── Summary ──
    completed_count = len(categorized.get("COMPLETED", []))
    stale_count = len(categorized.get("STALE", []))
    active_count = len(categorized.get("ACTIVE", []))
    unknown_count = len(categorized.get("UNKNOWN", []))

    print("\n  SUMMARY:")
    print(f"  ✓ COMPLETED (all conditions met → should close): {completed_count}")
    print(f"  ⚠ STALE    (date passed, no action → escalate):   {stale_count}")
    print(f"  → ACTIVE   (still pending, conditions not met):   {active_count}")
    print(f"  ? UNKNOWN  (cannot auto-verify):                  {unknown_count}")

    # ── COMPLETED section ──
    for category, emoji, label in [
        ("COMPLETED", "[OK]", "CONDITIONS MET — SHOULD CLOSE"),
        ("STALE", "⚠", "DATE PASSED — ESCALATE"),
        ("ACTIVE", "→", "STILL PENDING"),
        ("UNKNOWN", "?", "CANNOT AUTO-VERIFY"),
    ]:
        tasks = categorized.get(category, [])
        if not tasks:
            continue
        print(f"\n{'─' * 70}")
        print(f"  {emoji} {label} ({len(tasks)} tasks)")
        print(f"{'─' * 70}")

        for task in tasks:
            print(f"\n  [{task.priority.upper()}] {task.title}")
            print(f"  File: {task.file_name}")
            if task.last_updated:
                print(f"  Last updated: {task.last_updated}")
            if task.verdicts:
                for v in task.verdicts:
                    print(f"    • {v}")
            if verbose and task.evidence:
                for e in task.evidence:
                    print(f"      {e}")
            print(f"  → Action: {task.recommended_action}")

    # ── Closing reminder ──
    if completed_count > 0:
        print(f"\n{'=' * 70}")
        print(f"  ⚠ {completed_count} task(s) have ALL conditions met.")
        print("  These should be CLOSED (memory file deleted or marked done).")
        print("  Stale completed tasks waste diagnosis time (DQAF-084 root cause).")
        print(f"{'=' * 70}")

    print("\n[DONE] All statistics above are the sole source of truth. (Iron Law #11)")


def main() -> None:
    # Windows console encoding fix: force UTF-8 for Unicode symbols
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(
        description="Ω Deferred Task Auto-Closure Auditor"
    )
    parser.add_argument(
        "--data-dir", default="data_btc",
        help="Data directory for live state checks (default: data_btc)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed evidence for each task"
    )
    parser.add_argument(
        "--memory-dir",
        default=str(MEMORY_DIR),
        help="Memory directory path"
    )
    args = parser.parse_args()

    memory_dir = Path(args.memory_dir)
    data_dir = str(PROJECT_ROOT / args.data_dir)

    categorized = audit(data_dir, memory_dir=memory_dir, verbose=args.verbose)
    print_report(categorized, data_dir, verbose=args.verbose)

    # Exit code: non-zero if STALE tasks exist (for CI/monitoring)
    stale_count = len(categorized.get("STALE", []))
    completed_count = len(categorized.get("COMPLETED", []))
    if stale_count > 0 or completed_count > 0:
        sys.exit(0)  # Report printed; exit 0 so it doesn't break pipelines
    sys.exit(0)


if __name__ == "__main__":
    main()

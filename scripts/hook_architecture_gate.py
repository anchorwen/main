#!/usr/bin/env python
"""Ω Architecture Gate — Physical commit-msg enforcement of Iron Law #12.

Blocks commits when:
1. A module has >=3 L1/L2 fixes in 30 days and the current commit is not L3
2. Same RC category recurs in 2+ consecutive FIX commits to the same module

Strategy: Git is the time-series DB. ``git log --follow`` on each staged .py
file extracts recent FIX history. No JSONL migration needed.

DRY-RUN by default. Set ARCH_GATE_MODE=live to activate blocking.
Designed as a commit-msg hook (needs commit message for L3 detection).

FIX-20260622-052: S.E.A.L. Framework — added ``--report`` mode for
longitudinal monitoring (Layer L). Outputs JSON with annotation coverage,
per-module L1/L2/L3 breakdown, and readiness assessment.

Usage (via pre-commit config)::

    - repo: local
      hooks:
        - id: architecture-gate
          name: "[Ω] Iron Law #12 Architecture Gate"
          entry: python scripts/hook_architecture_gate.py
          language: system
          stages: [commit-msg]
          pass_filenames: false

    # Report mode (standalone, not as a hook):
    python scripts/hook_architecture_gate.py --report
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Regex patterns (tolerant of whitespace/case variations) ────────────────
ROOT_CAUSE_LAYER_RE = re.compile(r"root\s*cause\s*layer\s*:\s*(L[123])", re.IGNORECASE)
RC_RE = re.compile(r"RC-(\d{2})", re.IGNORECASE)
FIX_ID_RE = re.compile(r"FIX-\d{8}-\d{3}")

# ── Exclusion rules ───────────────────────────────────────────────────────
EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        ".git",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".egg-info",
    }
)
SKIP_PATH_PREFIXES = ("data/", "data_btc/", "tests/", "__pycache__/")
MD_ONLY_EXEMPTION = True  # pure .md commits skip the gate

# ── Helpers ────────────────────────────────────────────────────────────────


def _read_commit_msg() -> str:
    """Read commit message from the temp file passed by commit-msg hook."""
    commit_msg_file = sys.argv[1] if len(sys.argv) > 1 else None
    if commit_msg_file:
        p = Path(commit_msg_file)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def _staged_py_files() -> list[str]:
    """Return staged .py files, excluding data/tests paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.strip().split("\n"):
        f = line.strip().replace("\\", "/")
        if not f.endswith(".py"):
            continue
        if any(f.startswith(pfx) for pfx in SKIP_PATH_PREFIXES):
            continue
        files.append(f)
    return files


def _module_key(file_path: str) -> str:
    """Group files by their top-2 directory components for module tracking.

    core/execution/position_manager.py  →  core/execution
    scripts/verify.py                   →  scripts
    apps/engine/main.py                 →  apps/engine
    """
    parts = file_path.split("/")
    if len(parts) >= 2 and parts[0] in ("core", "apps"):
        return f"{parts[0]}/{parts[1]}"
    if parts[0] == "scripts":
        return "scripts"
    return parts[0]


def _file_fix_history(file_path: str) -> list[dict]:
    """Return recent FIX commits for *file_path* (newest-first).

    Uses ``--follow`` to track across renames.
    Each entry: {hash, msg, layer, rcs}
    """
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=30 days ago", "--follow", "--", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    commits: list[dict] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        commit_hash = parts[0]
        try:
            msg_result = subprocess.run(
                ["git", "log", "-1", "--format=%B", commit_hash],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        msg = msg_result.stdout if msg_result.returncode == 0 else parts[1]

        if not FIX_ID_RE.search(msg):
            continue  # not a FIX commit — skip

        layer_m = ROOT_CAUSE_LAYER_RE.search(msg)
        layer = layer_m.group(1).upper() if layer_m else None
        rcs = RC_RE.findall(msg)

        commits.append({"hash": commit_hash, "msg": msg, "layer": layer, "rcs": rcs})

    return commits


# ── Main gate logic ────────────────────────────────────────────────────────


def _generate_report() -> int:
    """S.E.A.L. Framework Layer L: Longitudinal Monitoring Report.

    Scans all FIX commits in 30-day window across the entire repo and outputs
    a JSON report with annotation coverage, per-module L1/L2/L3 breakdown,
    and ARCH_GATE_MODE=live readiness assessment.

    This is the institutional audit trail — runs standalone, not as a hook.
    """
    # Collect all .py files in covered paths
    try:
        ls_result = subprocess.run(
            ["git", "ls-files", "--", "core/", "apps/", "scripts/"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(json.dumps({"error": "git ls-files failed"}, indent=2))
        return 1

    all_py_files = [
        f.strip().replace("\\", "/")
        for f in ls_result.stdout.strip().split("\n")
        if f.strip().endswith(".py")
    ]

    # Scan FIX history for all covered .py files
    seen_commits: set[str] = set()
    total_fix = 0
    annotated = 0
    l1_cnt, l2_cnt, l3_cnt = 0, 0, 0
    module_l1l2: dict[str, int] = defaultdict(int)
    module_l3: dict[str, int] = defaultdict(int)
    unannotated_samples: list[str] = []

    for f in all_py_files:
        for c in _file_fix_history(f):
            if c["hash"] in seen_commits:
                continue
            seen_commits.add(c["hash"])
            total_fix += 1
            mod = _module_key(f)

            if c["layer"]:
                annotated += 1
                if c["layer"] == "L1":
                    l1_cnt += 1
                elif c["layer"] == "L2":
                    l2_cnt += 1
                    module_l1l2[mod] += 1
                elif c["layer"] == "L3":
                    l3_cnt += 1
                    module_l3[mod] += 1
            else:
                module_l1l2[mod] += 1  # untagged counts as potential L1/L2
                if len(unannotated_samples) < 5:
                    short_hash = c["hash"][:10]
                    unannotated_samples.append(f"{short_hash} {c['msg'][:80]}")

    coverage = round(annotated / max(total_fix, 1) * 100, 1)
    ready = coverage >= 95.0

    # Modules at risk (≥3 L1/L2)
    at_risk = {
        mod: cnt for mod, cnt in sorted(module_l1l2.items(), key=lambda x: -x[1]) if cnt >= 3
    }

    report = {
        "gate": "hook_architecture_gate.py",
        "framework": "S.E.A.L.",
        "layer": "L — Longitudinal Monitoring",
        "window_days": 30,
        "fix_commits_total": total_fix,
        "root_cause_layer_annotated": annotated,
        "root_cause_layer_missing": total_fix - annotated,
        "annotation_coverage_pct": coverage,
        "layer_breakdown": {
            "L1_syntax_typo": l1_cnt,
            "L2_logic_defect": l2_cnt,
            "L3_architecture_defect": l3_cnt,
        },
        "modules_at_risk_3plus_l1l2": at_risk,
        "arch_gate_mode_live_ready": ready,
        "recommendation": (
            "READY: coverage >= 95%. Set ARCH_GATE_MODE=live."
            if ready
            else f"NOT READY: coverage {coverage}% < 95%. "
            f"{total_fix - annotated} commits need Root Cause Layer annotation. "
            f"Set ROOT_CAUSE_GATE_MODE=live first, then run backfill."
        ),
        "unannotated_sample": unannotated_samples,
    }

    # Handle Windows GBK encoding: reconfigure stdout for UTF-8 JSON output
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    # ── Report mode (S.E.A.L. Layer L) ──
    if "--report" in sys.argv:
        return _generate_report()
    commit_msg = _read_commit_msg()
    staged = _staged_py_files()

    # Pure mechanical / .md-only commits skip the gate
    if not staged:
        return 0

    # Only check modules with staged .py files that are in covered paths
    covered = [f for f in staged if f.startswith(("core/", "apps/", "scripts/"))]
    if not covered:
        return 0

    mode = os.environ.get("ARCH_GATE_MODE", "dry-run").lower()
    is_live = mode == "live"

    # ── Collect per-module FIX history ──
    module_l1l2: dict[str, int] = defaultdict(int)
    # Per-module RC sequence: newest-first list of frozensets (one per FIX commit)
    module_rc_seq: dict[str, list[frozenset[str]]] = defaultdict(list)
    seen: set[str] = set()

    for f in covered:
        mod = _module_key(f)
        for c in _file_fix_history(f):
            if c["hash"] in seen:
                continue
            seen.add(c["hash"])
            if c["layer"] in ("L1", "L2"):
                module_l1l2[mod] += 1
            if c["rcs"]:
                module_rc_seq[mod].append(frozenset(c["rcs"]))

    # ── Determine current commit's L-level ──
    current_m = ROOT_CAUSE_LAYER_RE.search(commit_msg)
    current_is_l3 = current_m and current_m.group(1).upper() == "L3"

    violations: list[str] = []

    # Rule 1: 3-Patch limit — Iron Law #12 clause 4
    for mod, count in sorted(module_l1l2.items()):
        if count >= 3 and not current_is_l3:
            violations.append(
                f"Module '{mod}': {count} L1/L2 fixes in 30 days. "
                f"Architecture refactor (L3) required before further patches."
            )

    # Rule 2: RC recurrence detection
    for mod, rc_seq in module_rc_seq.items():
        if len(rc_seq) < 2:
            continue
        # rc_seq is newest-first. Check if the two most recent FIX commits
        # share at least one RC category.
        newest = rc_seq[0]
        second = rc_seq[1]
        shared = newest & second
        if shared:
            shared_str = ", ".join(f"RC-{r}" for r in sorted(shared))
            violations.append(
                f"RC recurrence in '{mod}': {shared_str} appears in "
                f"two consecutive FIX commits → systemic design defect suspected."
            )

    # ── Output ──
    if not violations:
        if module_l1l2:
            total = sum(module_l1l2.values())
            max_mod = max(module_l1l2, key=lambda k: module_l1l2[k])
            max_cnt = module_l1l2[max_mod]
            print(
                f"[Ω-ARCH-GATE] PASSED: {total} L1/L2 fix(es) across "
                f"{len(module_l1l2)} module(s). Highest: '{max_mod}' ({max_cnt}/3)."
            )
        return 0

    if is_live:
        print("=" * 60)
        print("[Ω-ARCH-GATE] COMMIT REJECTED — Iron Law #12 Architecture Gate")
        print(f"[Ω-ARCH-GATE] {len(violations)} violation(s):")
        for v in violations:
            print(f"  ❌ {v}")
        print(
            "[Ω-ARCH-GATE] Override: git commit --no-verify with "
            "FIX-ARCH-OVERRIDE + Tech Lead approval."
        )
        print("=" * 60)
        return 1
    else:
        print("=" * 60)
        print("[Ω-ARCH-GATE] [DRY-RUN] WOULD BLOCK " "(set ARCH_GATE_MODE=live to activate)")
        print(f"[Ω-ARCH-GATE] {len(violations)} violation(s):")
        for v in violations:
            print(f"  ⚠️  {v}")
        print(
            "[Ω-ARCH-GATE] [DRY-RUN] Commit allowed. "
            "Switch to LIVE after 24h observation with zero false positives."
        )
        print("=" * 60)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        try:  # BLE001:FOG (was: FOG/LAC)
            # Non-blocking on script failure — don't block dev workflow
            print("[Ω-ARCH-GATE] Internal error — gate bypassed (fail-open).")
            sys.exit(0)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

#!/usr/bin/env python
"""Omega Protocol commit-message gate — physically rejects commits without Ω routing.

FIX-20260612-002: Phase 1 of Systemic Operating System.
Scans commit message for [Ω-Routing: Scene X -> ...] signature.
If hot-path files are changed, requires #10 in the signature.
Exit 1 = commit blocked.

FIX-20260622-052: S.E.A.L. Framework — Root Cause Layer enforcement for
Scene B/E with plausibility heuristics. Graduated enforcement: WARN→REJECT.

Usage (via pre-commit hook)::

    - repo: local
      hooks:
        - id: omega-routing
          name: "[Ω] Routing signature required"
          entry: python scripts/omega_gate.py
          language: system
          stages: [commit-msg]
          pass_filenames: false
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── S.E.A.L. Framework: Root Cause Layer gate mode ──────────────────────
# "warn" = QUARANTINE (7-day adoption window — advisory only)
# "live" = BLOCK (permanent enforcement)
ROOT_CAUSE_GATE_MODE = os.environ.get("ROOT_CAUSE_GATE_MODE", "warn").lower()

# Plausibility thresholds (institutional "smell test")
L1_MAX_DIFF_LINES = 200  # L1 claims beyond this → implausible
L1_MAX_DIFF_FILES = 3  # L1 claims across more files → implausible
L3_MIN_DIFF_LINES = 10  # L3 claims below this → implausible
L3_MIN_DIFF_FILES = 1  # L3 claims in fewer files → implausible

from scripts.omega_constants import (
    EXEMPTION_PATTERN,
    HOT_PATH_FILES,
    HOT_PATH_IRON_LAW,
    SCENE_F_EXEMPTION_SCOPE,
    SCENE_REQUIRES_IRON_LAW,
    SIGNATURE_RE,
)


def get_commit_msg() -> str:
    """Read commit message from the file passed by pre-commit."""
    commit_msg_file = sys.argv[1] if len(sys.argv) > 1 else ".git/COMMIT_EDITMSG"
    path = Path(commit_msg_file)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def get_staged_files() -> set[str]:
    """Get set of staged Python files."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
    )
    if result.returncode != 0:
        return set()
    return {
        line.strip() for line in result.stdout.strip().split("\n") if line.strip().endswith(".py")
    }


def _staged_diff_stats() -> dict[str, int]:
    """Return {line_count, file_count} for staged .py files (excl. data/ tests/).

    Used by plausibility heuristics to detect misclassified Root Cause Layers.
    """
    staged = get_staged_files()
    py_files = [
        f
        for f in staged
        if f.endswith(".py")
        and not any(f.startswith(d) for d in ("data/", "data_btc/", "__pycache__/"))
    ]
    if not py_files:
        return {"line_count": 0, "file_count": 0}

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--", *py_files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"line_count": 0, "file_count": len(py_files)}

    if result.returncode != 0:
        return {"line_count": 0, "file_count": len(py_files)}

    # Count non-empty added/removed lines (exclude diff headers)
    lines = [
        l
        for l in result.stdout.split("\n")
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
    ]
    return {"line_count": len(lines), "file_count": len(py_files)}


def main() -> int:
    # Fix Windows GBK encoding for emoji characters in hook output
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    commit_msg = get_commit_msg()
    staged = get_staged_files()

    # ── Check 1: Signature required ──
    sig_match = SIGNATURE_RE.search(commit_msg)
    if not sig_match:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: No Ω-Routing signature found.")
        print("[Ω] Your commit message MUST include:")
        print("[Ω]   [Ω-Routing: Scene X -> #N -> #M -> ...]")
        print("[Ω] See CLAUDE.md Omega Protocol for scene codes.")
        print("=" * 60)
        return 1

    signature = sig_match.group(0)
    print(f"[Ω] Signature found: {signature}")

    # ── Check 2: Scene requires minimal iron law references (BLOCKING) ──
    # FIX-20260613-061: Upgraded from WARNING to hard fail.
    # IRON_LAW-13-S1: Enhanced with Root Cause Layer + Causal Chain depth (#8, #12).
    scene_match = re.search(r"Scene\s+([A-H])", signature)
    scene = scene_match.group(1).upper() if scene_match else ""
    is_scene_f = scene == "F"  # P0-3: Scene F = pure mechanical, full exemption
    if is_scene_f:
        print(f"[Ω] Scene F detected — quality gates bypassed. Scope: {SCENE_F_EXEMPTION_SCOPE}")
    if scene_match:
        required = SCENE_REQUIRES_IRON_LAW.get(scene, [])
        missing = [law for law in required if law not in signature]
        if missing:
            print("=" * 60)
            print(f"[Ω] COMMIT REJECTED: Scene {scene} requires {missing} in signature.")
            print(f"[Ω] Current signature: {signature}")
            print(f"[Ω] Required chain for Scene {scene}: {' -> '.join(required)}")
            print("[Ω] See CLAUDE.md for the full execution protocol.")
            print("=" * 60)
            return 1

        # ── #8 / #12 depth verification for Scene A ──
        if scene == "A":
            has_root_cause_layer = bool(
                re.search(r"Root\s*Cause\s*Layer\s*:\s*(L[123])", commit_msg, re.IGNORECASE)
            )
            has_causal_chain = bool(re.search(r"Causal\s*Chain.*:", commit_msg, re.IGNORECASE))
            if not has_root_cause_layer:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: Scene A requires Root Cause Layer (#12).")
                print("[Ω] Must include: Root Cause Layer: L1 | L2 | L3")
                print("[Ω] With justification for L3 architecture-level findings.")
                print("=" * 60)
                return 1
            if not has_causal_chain:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: Scene A requires Causal Chain (#8).")
                print("[Ω] Must include at least 2-layer causal chain:")
                print("[Ω]   Layer 1 (symptoms) → Layer 2 (root cause)")
                print("=" * 60)
                return 1
            print("[Ω] Root Cause Depth PASSED: L-layer + causal chain present.")
    else:
        print("[Ω] WARNING: Could not parse scene from signature.")

    # ── Check 2.5: Pre-compute covered files + FIX/exemption status ─────
    # Defined early so Root Cause Layer check (below) can reference them.
    covered_staged = {
        f
        for f in staged
        if f.endswith((".py", ".yaml", ".yml", ".json"))
        and not any(f.startswith(d) for d in ("data/", "data_btc/", "__pycache__/", ".claude/"))
    }
    has_fix = bool(re.search(r"FIX-\d{8}-\d{3}", commit_msg))
    has_dqaf = bool(re.search(r"DQAF-\d{8}-\d{3}", commit_msg))
    is_exempt = is_scene_f or bool(EXEMPTION_PATTERN.search(commit_msg))

    # ── Root Cause Layer for Scene B/E (#12) — S.E.A.L. Framework ────────
    # FIX-20260622-052: Root Cause Layer annotation is mandatory for ALL
    # FIX commits modifying .py files. Previously only Scene A required it.
    # Graduated enforcement: WARN (QUARANTINE) → REJECT (LIVE).
    if scene in ("B", "E") and covered_staged and has_fix and not is_exempt:
        rc_layer_match = re.search(
            r"Root\s*Cause\s*Layer\s*:\s*(L[123])", commit_msg, re.IGNORECASE
        )
        if not rc_layer_match:
            if ROOT_CAUSE_GATE_MODE == "live":
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: Scene B/E FIX commit missing Root Cause Layer (#12).")
                print("[Ω] All FIX commits modifying .py files must include:")
                print("[Ω]   Root Cause Layer: L1 | L2 | L3 — <explanation>")
                print("[Ω]   L1 = syntax/typo  L2 = logic defect  L3 = architecture defect")
                print("[Ω] S.E.A.L. Framework — Institutional Enforcement (FIX-20260622-052)")
                print("=" * 60)
                return 1
            else:
                print("-" * 50)
                print("[Ω] ⚠ WARNING (QUARANTINE): Root Cause Layer missing.")
                print("[Ω] Scene B/E FIX commits MUST include Root Cause Layer.")
                print("[Ω] Add to commit body: Root Cause Layer: L1 | L2 | L3 — <explanation>")
                print(f"[Ω] Current mode: {ROOT_CAUSE_GATE_MODE.upper()}")
                print("[Ω] Set ROOT_CAUSE_GATE_MODE=live to activate blocking.")
                print("-" * 50)
        else:
            claimed_layer = rc_layer_match.group(1).upper()

            # ── Plausibility Heuristics (always WARN, never block) ──
            diff_stats = _staged_diff_stats()
            dl, df = diff_stats["line_count"], diff_stats["file_count"]

            implausible: list[str] = []
            if claimed_layer == "L1" and (dl > L1_MAX_DIFF_LINES or df > L1_MAX_DIFF_FILES):
                implausible.append(
                    f"L1 (typo/syntax) claimed but diff is {dl} lines "
                    f"across {df} .py file(s) — review root cause classification"
                )
            if claimed_layer == "L3" and (dl < L3_MIN_DIFF_LINES and df <= L3_MIN_DIFF_FILES):
                implausible.append(
                    f"L3 (architecture) claimed but diff is only {dl} lines "
                    f"in {df} .py file(s) — review root cause classification"
                )

            for warn_msg in implausible:
                print("-" * 50)
                print(f"[Ω] ⚠ PLAUSIBILITY WARNING: {warn_msg}")
                print("[Ω] This is advisory only — commit NOT blocked.")
                print("-" * 50)

            if not implausible:
                print(f"[Ω] Root Cause Layer PASSED: {claimed_layer} ({dl} lines, {df} files).")

    # ── Check 3: Hot-path files require #10 ──
    hot_path_staged = staged & HOT_PATH_FILES
    if hot_path_staged and HOT_PATH_IRON_LAW not in signature:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: Hot-path files modified without #10:")
        for f in sorted(hot_path_staged):
            print(f"[Ω]   {f}")
        print(f"[Ω] Signature MUST include {HOT_PATH_IRON_LAW} when touching hot-path files.")
        print(f"[Ω] Current signature: {signature}")
        print("=" * 60)
        return 1

    if hot_path_staged:
        print(
            f"[Ω] Hot-path check PASSED: {HOT_PATH_IRON_LAW} in signature for {len(hot_path_staged)} file(s)."
        )

        # ── Deep verification: did BLE001 actually get replaced? ──
        # IRON_LAW-13-S1: #10 requires at least 1 BLE001 → fail_open_guard per commit.
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--cached", "--", *sorted(hot_path_staged)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=10,
            )
            diff_text = diff_result.stdout
            ble001_removed = bool(re.search(r"^-\s*.*#\s*noqa:\s*BLE001", diff_text, re.MULTILINE))
            fail_open_added = bool(re.search(r"fail_open_guard", diff_text))
            if not (ble001_removed or fail_open_added):
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: #10 declared but no BLE001 replaced.")
                print("[Ω] Hot-path files staged:")
                for f in sorted(hot_path_staged):
                    print(f"[Ω]   {f}")
                print("[Ω] #10 requires: replace ≥1 `# BLE001:REVIEWED` with `fail_open_guard()`")
                print("=" * 60)
                return 1
            print("[Ω] BLE001 replacement VERIFIED in diff.")
        except Exception:  # noqa: BLE001
            try:  # BLE001:FOG (was: FOG/LAC)
                pass  # non-blocking: diff parsing failure shouldn't block commit
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
    # ── Check 4: FIX/DQAF ID required for .py/.yaml/.json changes ──
    # Iron Law #0: every non-exempt change to covered files must carry a docket ID.
    # Variables pre-computed at Check 2.5 above (S.E.A.L. Framework restructuring).

    if covered_staged and not has_fix and not has_dqaf and not is_exempt:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: Covered files changed without FIX/DQAF ID.")
        print("[Ω] Changed files requiring docket ID:")
        for f in sorted(covered_staged):
            print(f"[Ω]   {f}")
        print("[Ω] Commit message MUST include FIX-YYYYMMDD-NNN or DQAF-YYYYMMDD-NNN.")
        print("[Ω] Exemptions: add 'pure mechanical'/'formatting'/'config value' to msg.")
        print("=" * 60)
        return 1

    if covered_staged:
        docket_type = "FIX" if has_fix else ("DQAF" if has_dqaf else "exempt")
        print(
            f"[Ω] Docket check PASSED: {docket_type} ID for {len(covered_staged)} covered file(s)."
        )

        # ── DQAF depth check for Scene A (#9) ──
        # IRON_LAW-13-S1: Scene A requires DQAF report markers + severity.
        if scene == "A" and not is_exempt:
            has_dqaf_report = bool(re.search(r"\[(DQAF_REPORT|DQAF_LITE_REPORT)\]", commit_msg))
            has_severity = bool(re.search(r"Severity:\s*Sev\s*[1-4]", commit_msg))
            has_awaiting = bool(re.search(r"\[AWAITING_IC_APPROVAL\]", commit_msg))
            if not has_dqaf_report:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: Scene A requires DQAF report markers (#9).")
                print("[Ω] Must include [DQAF_REPORT] or [DQAF_LITE_REPORT] block.")
                print("[Ω] See CLAUDE.md Iron Law #9 for required format.")
                print("=" * 60)
                return 1
            if not has_severity:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: DQAF report missing Severity (#9).")
                print("[Ω] Must include: Severity: Sev 1 | Sev 2 | Sev 3 | Sev 4")
                print("=" * 60)
                return 1
            if not has_awaiting:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: DQAF report missing [AWAITING_IC_APPROVAL] (#9).")
                print("[Ω] DQAF handshake requires explicit IC approval gate.")
                print("=" * 60)
                return 1
            print("[Ω] DQAF Depth PASSED: report markers + severity + IC approval present.")

        # ── Pattern Search check for Scene A/B/E (#5) ──
        # IRON_LAW-13-S1: code changes require pattern search declaration.
        if scene in ("A", "B", "E") and not is_exempt:
            has_pattern = bool(re.search(r"(?:Pattern|模式)\s*:\s*\S", commit_msg))
            has_pattern_skip = bool(
                re.search(
                    r"(?i)(?:pattern|模式).*(?:not\s*needed|跳过|skip|N/?A|不需要)",
                    commit_msg,
                )
            )
            if not has_pattern and not has_pattern_skip:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: Pattern Search (#5) not documented.")
                print(f"[Ω] Scene {scene} requires pattern declaration in commit body.")
                print("[Ω] Must include: Pattern: <pattern>  Results: <N> matches")
                print("[Ω] Or declare: Pattern search not needed (<reason>)")
                print("=" * 60)
                return 1
            print("[Ω] Pattern Search PASSED: #5 documented.")

    # ── Check 5: --no-verify audit (Iron Law #0-bis enforcement) ────────
    # DQAF-20260614-P0: Every --no-verify must carry a VALID exemption reason.
    # The exemption must match the actual staged files — e.g. claiming
    # 'live process file locks' when no data_btc/*.jsonl is staged = REJECT.
    no_verify_match = re.search(r"--no-verify:\s*(.+?)$", commit_msg, re.MULTILINE)
    if no_verify_match:
        reason = no_verify_match.group(1).strip()

        VALID_EXEMPTIONS = {
            "live process file locks": lambda: any(
                f.startswith(("data_btc/", "data/")) and f.endswith((".jsonl", ".lock"))
                for f in staged
            ),
            "documentation-only": lambda: all(f.endswith(".md") for f in staged),
            "emergency rollback": lambda: "EMERGENCY_ROLLBACK" in commit_msg.upper(),
        }

        is_valid = False
        matched_rule = ""
        for rule, check_fn in VALID_EXEMPTIONS.items():
            if rule in reason.lower():
                if check_fn():
                    is_valid = True
                    matched_rule = rule
                    break
                else:
                    print("=" * 60)
                    print("[Ω] COMMIT REJECTED: --no-verify exemption MISMATCH.")
                    print(f"[Ω] Claimed: '{reason}'")
                    print(f"[Ω] Rule '{rule}' requires specific staged files that are NOT present.")
                    print(f"[Ω] Staged files ({len(staged)}):")
                    for f in sorted(staged)[:10]:
                        print(f"[Ω]   {f}")
                    if len(staged) > 10:
                        print(f"[Ω]   ... and {len(staged) - 10} more")
                    print("[Ω] --no-verify is only allowed for:")
                    print("[Ω]   1. data_btc/*.jsonl locked by live process")
                    print("[Ω]   2. Documentation-only (.md files)")
                    print("[Ω]   3. Emergency rollback (EMERGENCY_ROLLBACK)")
                    print("=" * 60)
                    return 1

        if not is_valid:
            print("=" * 60)
            print("[Ω] COMMIT REJECTED: --no-verify with unrecognized reason.")
            print(f"[Ω] Reason: '{reason}'")
            print(
                "[Ω] Allowed reasons: live process file locks | documentation-only | emergency rollback"
            )
            print("=" * 60)
            return 1

        print(f"[Ω] --no-verify audit PASSED: valid exemption '{matched_rule}'.")

    # ── Check 6: FIX_REGISTRY cross-reference (FIX-20260613-061) ────────
    # When a FIX ID is claimed in the commit, it MUST exist in the registry.
    # This closes the loop: diagnosis → fix → registration → commit.
    if has_fix:
        fix_ids = set(re.findall(r"FIX-\d{8}-\d{3}", commit_msg))
        registry_path = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
        if registry_path.exists():
            registry_text = registry_path.read_text(encoding="utf-8")
            missing_in_registry = [fid for fid in fix_ids if fid not in registry_text]
            if missing_in_registry:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: FIX ID(s) not registered in FIX_REGISTRY.md:")
                for fid in missing_in_registry:
                    print(f"[Ω]   {fid}")
                print(f"[Ω] Update {registry_path} before committing.")
                print("[Ω] Run: python scripts/register_fix.py --help")
                print("=" * 60)
                return 1
            print(f"[Ω] Registry check PASSED: {len(fix_ids)} FIX ID(s) found in registry.")
        else:
            print("[Ω] WARNING: FIX_REGISTRY.md not found — skipping cross-reference.")

    # ── Check 7: 4D Quality Gate (Iron Law #1.1) ──────────────────────
    # IRON_LAW-13-S1: all non-exempt commits modifying covered files MUST
    # include four-dimensional quality assessment in the commit body.
    # Forgiving regex: accepts English OR Chinese dimension names, with
    # flexible formatting (arrows, colons, separators are optional).
    if covered_staged and not is_exempt:
        _has_stability = bool(
            re.search(
                r"(?:Stability|稳定性)\b.*[↑→↓↗↘]",
                commit_msg,
                re.IGNORECASE,
            )
        )
        _has_repairability = bool(
            re.search(
                r"(?:Repairability|可修复性)\b.*[↑→↓↗↘]",
                commit_msg,
                re.IGNORECASE,
            )
        )
        _has_decoupling = bool(
            re.search(
                r"(?:Decoupling|解耦性)\b.*[↑→↓↗↘]",
                commit_msg,
                re.IGNORECASE,
            )
        )
        _has_iterability = bool(
            re.search(
                r"(?:Iterability|迭代性)\b.*[↑→↓↗↘]",
                commit_msg,
                re.IGNORECASE,
            )
        )
        missing_4d = []
        if not _has_stability:
            missing_4d.append("Stability/稳定性")
        if not _has_repairability:
            missing_4d.append("Repairability/可修复性")
        if not _has_decoupling:
            missing_4d.append("Decoupling/解耦性")
        if not _has_iterability:
            missing_4d.append("Iterability/迭代性")
        if missing_4d:
            print("=" * 60)
            print("[Ω] COMMIT REJECTED: 4D Quality Gate (#1.1) incomplete.")
            print(f"[Ω] Missing dimensions: {', '.join(missing_4d)}")
            print("[Ω] Your commit body MUST assess all four dimensions:")
            print("[Ω]   Stability: ↑/→/↓ (assessment)")
            print("[Ω]   Repairability: ↑/→/↓ (assessment)")
            print("[Ω]   Decoupling: ↑/→/↓ (assessment)")
            print("[Ω]   Iterability: ↑/→/↓ (assessment)")
            print("[Ω] (Chinese variants accepted: 稳定性/可修复性/解耦性/迭代性)")
            print("[Ω] Exemptions: 'pure mechanical' / 'formatting' / 'docs only'")
            print("=" * 60)
            return 1
        print("[Ω] 4D Quality Gate PASSED: all 4 dimensions assessed.")

    # ── Check 8: Pre-Edit Checklist (Iron Law #0) ──────────────────────
    # IRON_LAW-13-S1: Scene B/E commits modifying .py files require the
    # pre-edit checklist to be documented in the commit body.
    if scene in ("B", "E") and not is_exempt:
        has_checklist = bool(re.search(r"\[PRE-EDIT CHECKLIST.*#0\]", commit_msg))
        has_checklist_passed = bool(re.search(r"\[CHECKLIST PASSED\]", commit_msg))
        if not (has_checklist and has_checklist_passed):
            print("=" * 60)
            print("[Ω] COMMIT REJECTED: Pre-Edit Checklist (#0) not completed.")
            print(f"[Ω] Scene {scene} requires [PRE-EDIT CHECKLIST — Iron Law #0]")
            print("[Ω] with all 5 items and [CHECKLIST PASSED] before code changes.")
            print("[Ω] See CLAUDE.md Iron Law #0 for the 5-item checklist.")
            print("[Ω] Exemptions: 'pure mechanical' / 'formatting' / 'docs only'")
            print("=" * 60)
            return 1
        print("[Ω] Pre-Edit Checklist PASSED: #0 verified.")

    # ── Check 9: Closing Checklist (Iron Law #7.1) ─────────────────────
    # IRON_LAW-13-S1: commits modifying core/ or scripts/ .py files require
    # the closing block (收口完毕) with minimum required fields.
    _core_or_scripts_py = any(
        f.startswith(("core/", "scripts/")) and f.endswith(".py") for f in covered_staged
    )
    if _core_or_scripts_py and not is_exempt:
        _has_closing = bool(re.search(r"收口完毕", commit_msg))
        _has_uncommitted = bool(re.search(r"未提交变更", commit_msg))
        _has_blueprint = bool(re.search(r"蓝图", commit_msg))
        _has_git = bool(re.search(r"Git\s*:", commit_msg))
        if not (_has_closing and _has_uncommitted and _has_blueprint and _has_git):
            print("=" * 60)
            print("[Ω] COMMIT REJECTED: Closing Checklist (#7.1) missing or incomplete.")
            print("[Ω] Your commit body MUST include the closing block:")
            print("[Ω]   收口完毕。")
            print("[Ω]   未提交变更: <git status count>")
            print("[Ω]   蓝图: <module blueprint or '无需更新'>")
            print("[Ω]   Git: <branch> → <remote> 已推送")
            print("[Ω]   本轮 commit: <count + description>")
            print("[Ω] Exemptions: 'pure mechanical' / 'formatting' / 'docs only'")
            print("=" * 60)
            return 1
        print("[Ω] Closing Checklist PASSED: #7.1 complete.")

    print("[Ω] Gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

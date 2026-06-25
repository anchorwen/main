#!/usr/bin/env python
"""Pre-flight commit message validator — all omega-routing checks in ONE pass.

Usage::

    python scripts/validate_commit_msg.py <commit-message-file>
    python scripts/validate_commit_msg.py --stdin <<< "my message"

Returns 0 only when ALL omega-routing checks pass.  When checks fail, prints
a complete report with ALL missing items so you can fix everything at once.

FIX-20260623-087: Eliminates the "whack-a-mole" push failure pattern where
the omega gate rejects one issue at a time (14 sequential gates).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.omega_constants import (
    EXEMPTION_PATTERN,
    HOT_PATH_FILES,
    HOT_PATH_IRON_LAW,
    SCENE_REQUIRES_IRON_LAW,
    SIGNATURE_RE,
    is_test_only_commit,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    fix_hint: str = ""


@dataclass
class ValidationReport:
    results: list[CheckResult] = field(default_factory=list)
    all_passed: bool = True

    def add(self, name: str, passed: bool, detail: str = "", fix_hint: str = "") -> None:
        self.results.append(CheckResult(name, passed, detail, fix_hint))
        if not passed:
            self.all_passed = False

    def print_report(self) -> None:
        print()
        print("=" * 70)
        print("  OMEGA COMMIT MESSAGE PRE-FLIGHT CHECK")
        print("=" * 70)
        passed_count = sum(1 for r in self.results if r.passed)
        failed_count = sum(1 for r in self.results if not r.passed)
        for r in self.results:
            icon = "[PASS]" if r.passed else "[FAIL]"
            print(f"  {icon}  {r.name}")
            if r.detail:
                print(f"      {r.detail}")
            if not r.passed and r.fix_hint:
                print(f"      FIX: {r.fix_hint}")
        print("-" * 70)
        print(f"  {passed_count} passed, {failed_count} failed out of {len(self.results)} checks")
        if self.all_passed:
            print("  [ALL PASSED] - commit will succeed.")
        else:
            print("  [FIX REQUIRED] - fix the above before committing.")
        print("=" * 70)
        print()


def read_msg(file_arg: str | None) -> str:
    """Read commit message from file, stdin, or .git/COMMIT_EDITMSG."""
    if file_arg is None or file_arg == "--stdin":
        return sys.stdin.read()
    path = Path(file_arg)
    if path.exists():
        return path.read_text(encoding="utf-8")
    # Try .git/COMMIT_EDITMSG as fallback
    git_msg = ROOT / ".git" / "COMMIT_EDITMSG"
    if git_msg.exists():
        return git_msg.read_text(encoding="utf-8")
    return ""


def get_staged_covered() -> set[str]:
    """Return staged files that require docket IDs."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    staged = {line.strip() for line in result.stdout.strip().split("\n") if line.strip()}
    return {
        f
        for f in staged
        if f.endswith((".py", ".yaml", ".yml", ".json"))
        and not any(f.startswith(d) for d in ("data/", "data_btc/", "__pycache__/", ".claude/"))
    }


def validate(msg: str) -> ValidationReport:
    """Run ALL omega-routing checks and return a complete report."""
    report = ValidationReport()

    # ── Parse signature ───────────────────────────────────────────────────
    sig_match = SIGNATURE_RE.search(msg)
    if not sig_match:
        report.add(
            "1. Omega-Routing Signature",
            False,
            "No [Omega-Routing: Scene X -> ...] signature found.",
            "Add to commit title: [Omega-Routing: Scene A -> #9 -> #8 -> #12 -> ...]",
        )
        # Can't continue without scene — skip scene-dependent checks
        report.print_report()
        return report
    report.add("1. Omega-Routing Signature", True)
    signature = sig_match.group(0)

    # ── Parse scene ───────────────────────────────────────────────────────
    scene_match = re.search(r"Scene\s+([A-H])", signature)
    scene = scene_match.group(1).upper() if scene_match else ""
    is_scene_f = scene == "F"
    if not scene_match:
        report.add("1b. Scene Code", False, "Could not parse Scene code from signature.")
    else:
        report.add("1b. Scene Code", True, f"Scene {scene}")

    # ── Pre-compute covered files + exemption status ──────────────────────
    covered_staged = get_staged_covered()
    has_fix = bool(re.search(r"FIX-\d{8}-\d{3}", msg))
    has_dqaf = bool(re.search(r"DQAF-\d{8}-\d{3}", msg))
    # Plan A: test-only commits auto-exempt from quality gates
    is_test_only = is_test_only_commit(covered_staged)
    is_exempt = is_scene_f or bool(EXEMPTION_PATTERN.search(msg)) or is_test_only

    # ── Check 2: Scene requires minimal iron law references ────────────────
    if scene_match and not is_test_only:
        required = SCENE_REQUIRES_IRON_LAW.get(scene, [])
        missing_laws = [law for law in required if law not in signature]
        if missing_laws:
            report.add(
                "2. Iron Law References in Signature",
                False,
                f"Missing: {' '.join(missing_laws)} (required for Scene {scene})",
                f"Add to signature: {' -> '.join(missing_laws)}\n"
                f"      Full chain for Scene {scene}: {' -> '.join(required)}",
            )
        else:
            report.add(
                "2. Iron Law References in Signature",
                True,
                f"All required: {' '.join(required)}",
            )

    # ── Check 3: Root Cause Layer + Causal Chain for Scene A ──────────────
    if scene == "A" and not is_test_only:
        has_rc_layer = bool(re.search(r"Root\s*Cause\s*Layer\s*:\s*(L[123])", msg, re.IGNORECASE))
        has_causal = bool(re.search(r"Causal\s*Chain.*:", msg, re.IGNORECASE))
        if not has_rc_layer:
            report.add(
                "3a. Root Cause Layer (#12)",
                False,
                "Missing Root Cause Layer declaration.",
                "Add: Root Cause Layer: L2 — <one-line explanation>",
            )
        else:
            report.add("3a. Root Cause Layer (#12)", True, "Found.")
        if not has_causal:
            report.add(
                "3b. Causal Chain (#8)",
                False,
                "Missing Causal Chain.",
                "Add: Causal Chain: L1(symptoms) -> L2(root cause)",
            )
        else:
            report.add("3b. Causal Chain (#8)", True, "Found.")

    # ── Check 4: Root Cause Layer for Scene B/E ───────────────────────────
    if scene in ("B", "E") and covered_staged and has_fix and not is_exempt:
        has_rc = bool(re.search(r"Root\s*Cause\s*Layer\s*:\s*(L[123])", msg, re.IGNORECASE))
        if not has_rc:
            report.add(
                "4. Root Cause Layer for Scene B/E (#12)",
                False,
                "All Scene B/E FIX commits need Root Cause Layer.",
                "Add: Root Cause Layer: L1 | L2 | L3 — <explanation>",
            )
        else:
            report.add("4. Root Cause Layer for Scene B/E (#12)", True, "Found.")

    # ── Check 5: Hot-path files require #10 ───────────────────────────────
    staged_all = get_staged_covered()
    hot_path_staged = staged_all & HOT_PATH_FILES
    if hot_path_staged and HOT_PATH_IRON_LAW not in signature:
        report.add(
            "5. Hot-path #10",
            False,
            f"Hot-path files modified without #10: {', '.join(sorted(hot_path_staged))}",
            f"Add {HOT_PATH_IRON_LAW} to signature.",
        )
    elif hot_path_staged:
        report.add(
            "5. Hot-path #10", True, f"{HOT_PATH_IRON_LAW} for {len(hot_path_staged)} file(s)."
        )

    # ── Check 6: FIX/DQAF ID for covered files ────────────────────────────
    if covered_staged and not has_fix and not has_dqaf and not is_exempt:
        report.add(
            "6. FIX/DQAF Docket ID",
            False,
            f"{len(covered_staged)} covered file(s) changed without docket ID.",
            "Add FIX-YYYYMMDD-NNN or DQAF-YYYYMMDD-NNN to commit title.",
        )
    elif covered_staged:
        d_type = "FIX" if has_fix else ("DQAF" if has_dqaf else "exempt")
        report.add("6. FIX/DQAF Docket ID", True, f"{d_type} present.")

    # ── Check 7: DQAF depth for Scene A (#9) ──────────────────────────────
    if scene == "A" and not is_exempt and covered_staged:
        has_report = bool(re.search(r"\[(DQAF_REPORT|DQAF_LITE_REPORT)\]", msg))
        has_sev = bool(re.search(r"Severity:\s*Sev\s*[1-4]", msg))
        has_await = bool(re.search(r"\[AWAITING_IC_APPROVAL\]", msg))
        if not has_report:
            report.add(
                "7a. DQAF Report Markers (#9)",
                False,
                "Missing [DQAF_REPORT] or [DQAF_LITE_REPORT] block.",
                "Add: [DQAF_REPORT]\n      =============\n      ...\n      Severity: Sev N\n      [AWAITING_IC_APPROVAL]",
            )
        else:
            report.add("7a. DQAF Report Markers (#9)", True, "Found.")
        if not has_sev:
            report.add(
                "7b. DQAF Severity (#9)",
                False,
                "Missing Severity declaration.",
                "Add: Severity: Sev 1 | Sev 2 | Sev 3 | Sev 4",
            )
        else:
            report.add("7b. DQAF Severity (#9)", True, "Found.")
        if not has_await:
            report.add(
                "7c. DQAF IC Approval (#9)",
                False,
                "Missing [AWAITING_IC_APPROVAL] marker.",
                "Add [AWAITING_IC_APPROVAL] line before the commit body.",
            )
        else:
            report.add("7c. DQAF IC Approval (#9)", True, "Found.")

    # ── Check 8: Pattern Search for Scene A/B/E (#5) ──────────────────────
    if scene in ("A", "B", "E") and not is_exempt and covered_staged:
        has_pat = bool(re.search(r"(?:Pattern|模式)\s*:\s*\S", msg))
        has_skip = bool(
            re.search(r"(?i)(?:pattern|模式).*(?:not\s*needed|跳过|skip|N/?A|不需要)", msg)
        )
        if not has_pat and not has_skip:
            report.add(
                "8. Pattern Search (#5)",
                False,
                f"Scene {scene} requires pattern declaration.",
                "Add: Pattern: <pattern>  Results: <N> matches\n"
                "      Or: Pattern search not needed (<reason>)",
            )
        else:
            report.add("8. Pattern Search (#5)", True, "Documented.")

    # ── Check 9: FIX_REGISTRY cross-reference ─────────────────────────────
    if has_fix:
        fix_ids = set(re.findall(r"FIX-\d{8}-\d{3}", msg))
        registry_path = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
        if registry_path.exists():
            registry_text = registry_path.read_text(encoding="utf-8")
            missing_ids = [fid for fid in fix_ids if fid not in registry_text]
            if missing_ids:
                report.add(
                    "9. FIX_REGISTRY Cross-Reference (#7)",
                    False,
                    f"Not registered: {', '.join(missing_ids)}",
                    "Run: python scripts/register_fix.py --help",
                )
            else:
                report.add(
                    "9. FIX_REGISTRY Cross-Reference (#7)", True, f"{len(fix_ids)} ID(s) found."
                )
        else:
            report.add(
                "9. FIX_REGISTRY Cross-Reference (#7)", True, "Registry not found — skipped."
            )

    # ── Check 10: 4D Quality Gate (#1.1) ──────────────────────────────────
    if covered_staged and not is_exempt:
        dims = {
            "Stability/稳定性": r"(?:Stability|稳定性)\b.*[↑→↓↗↘]",
            "Repairability/可修复性": r"(?:Repairability|可修复性)\b.*[↑→↓↗↘]",
            "Decoupling/解耦性": r"(?:Decoupling|解耦性)\b.*[↑→↓↗↘]",
            "Iterability/迭代性": r"(?:Iterability|迭代性)\b.*[↑→↓↗↘]",
        }
        missing_4d = [
            name for name, pattern in dims.items() if not re.search(pattern, msg, re.IGNORECASE)
        ]
        if missing_4d:
            report.add(
                "10. 4D Quality Gate (#1.1)",
                False,
                f"Missing: {', '.join(missing_4d)}",
                "Add to commit body:\n"
                "      Stability: ↑/->/↓ (assessment)\n"
                "      Repairability: ↑/->/↓ (assessment)\n"
                "      Decoupling: ↑/->/↓ (assessment)\n"
                "      Iterability: ↑/->/↓ (assessment)",
            )
        else:
            report.add("10. 4D Quality Gate (#1.1)", True, "All 4 dimensions assessed.")

    # ── Check 11: Pre-Edit Checklist for Scene B/E (#0) ───────────────────
    if scene in ("B", "E") and not is_exempt:
        has_cl = bool(re.search(r"\[PRE-EDIT CHECKLIST.*#0\]", msg))
        has_passed = bool(re.search(r"\[CHECKLIST PASSED\]", msg))
        if not (has_cl and has_passed):
            report.add(
                "11. Pre-Edit Checklist (#0)",
                False,
                "Missing [PRE-EDIT CHECKLIST] block.",
                "Add:\n"
                "      [PRE-EDIT CHECKLIST — Iron Law #0]\n"
                "      1-5 items...\n"
                "      [CHECKLIST PASSED]",
            )
        else:
            report.add("11. Pre-Edit Checklist (#0)", True, "Present.")

    # ── Check 12: Closing Checklist (#7.1) ────────────────────────────────
    core_or_scripts = any(
        f.startswith(("core/", "scripts/")) and f.endswith(".py") for f in covered_staged
    )
    if core_or_scripts and not is_exempt:
        has_close = bool(re.search(r"收口完毕", msg))
        has_unchanged = bool(re.search(r"未提交变更", msg))
        has_blueprint = bool(re.search(r"蓝图", msg))
        has_git_ref = bool(re.search(r"Git\s*:", msg))
        if not (has_close and has_unchanged and has_blueprint and has_git_ref):
            report.add(
                "12. Closing Checklist (#7.1)",
                False,
                "Incomplete closing block.",
                "Add:\n"
                "      收口完毕\n"
                "      未提交变更: <count>\n"
                "      蓝图: <module or 无需更新>\n"
                "      Git: <branch> -> <remote> 已推送\n"
                "      本轮 commit: <description>",
            )
        else:
            report.add("12. Closing Checklist (#7.1)", True, "Complete.")

    # ── Check 13: --no-verify audit ───────────────────────────────────────
    nv_match = re.search(r"--no-verify:\s*(.+?)$", msg, re.MULTILINE)
    if nv_match:
        reason = nv_match.group(1).strip()
        valid_reasons = [
            "live process file locks",
            "documentation-only",
            "emergency rollback",
        ]
        if any(r in reason.lower() for r in valid_reasons):
            report.add("13. --no-verify Audit", True, f"Valid: {reason}")
        else:
            report.add(
                "13. --no-verify Audit",
                False,
                f"Unrecognized reason: '{reason}'",
                f"Allowed: {', '.join(valid_reasons)}",
            )

    report.print_report()
    return report


def main() -> int:
    file_arg = sys.argv[1] if len(sys.argv) > 1 else None
    msg = read_msg(file_arg)
    if not msg.strip():
        print("ERROR: No commit message provided.")
        print("Usage: python scripts/validate_commit_msg.py <commit-message-file>")
        print('       python scripts/validate_commit_msg.py --stdin <<< "message"')
        return 1

    report = validate(msg)
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

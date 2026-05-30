"""Unified brain lifecycle CLI — single entry point for all brain operations.

Replaces the scattered manual workflow of editing live.yaml, governance_state.json,
and config files.  A brain config JSON in ``configs/brains/`` is the single source
of truth; this CLI validates, registers, lists, and retires brains in one place.

Usage:
  # Register a new brain (one command)
  python scripts/brain.py register configs/brains/my_brain.json

  # Register with custom initial status
  python scripts/brain.py register configs/brains/my_brain.json --status shadow

  # Dry-run: validate only, no writes
  python scripts/brain.py register configs/brains/my_brain.json --dry-run

  # Validate all brain configs
  python scripts/brain.py validate

  # Validate + auto-repair governance
  python scripts/brain.py validate --repair

  # List all brains
  python scripts/brain.py list
  python scripts/brain.py list --group statarb_dynamic
  python scripts/brain.py list --verbose

  # Retire a brain
  python scripts/brain.py retire OU_Params_V6_Sniper
  python scripts/brain.py retire OU_Params_V6_Sniper --dry-run

  # Promote / demote a brain (auto-advances one step)
  python scripts/brain.py promote OU_Params_V6_Sniper
  python scripts/brain.py promote OU_Params_V6_Sniper --to live
  python scripts/brain.py demote OU_Params_V6_Sniper
  python scripts/brain.py demote OU_Params_V6_Sniper --to retired
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# register
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_register(
    config_path: Path,
    *,
    status: str = "shadow",
    dry_run: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Register a brain from its config JSON in one shot.

    Validates the config via BrainRegistrationGate, then uses
    BrainLifecycleManager.register_brain() to update live.yaml and
    governance_state.json atomically.
    """
    from core.deployment.brain_lifecycle_manager import BrainLifecycleManager

    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}", file=sys.stderr)
        return 2

    # Pre-validate
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ERROR] Cannot parse config: {exc}", file=sys.stderr)
        return 2

    brain_id = cfg.get("brain_id", config_path.stem)

    if dry_run:
        print(f"[dry-run] Would register brain_id='{brain_id}' with status='{status}'")
        print(f"[dry-run] Config path: {config_path}")
        print(f"[dry-run] brain_type: {cfg.get('brain_type', '?')}")
        print(f"[dry-run] contract_group: {cfg.get('contract_group', '?')}")
        print("[dry-run] No files written.")
        return 0

    mgr = BrainLifecycleManager(project_root=project_root)
    report = mgr.register_brain(str(config_path), initial_status=status)

    if report.errors:
        print(f"[FAIL] Registration of '{brain_id}' had errors:", file=sys.stderr)
        for err in report.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[OK] Brain '{brain_id}' registered successfully")
    if report.config_validated:
        print("  config_validated: true")
    if report.quality_gate_passed:
        print("  quality_gate: passed")
    if report.governance_registered:
        print(f"  governance: registered as '{status}'")
    if report.live_yaml_added:
        print("  live.yaml: entry added")
    if report.warnings:
        for w in report.warnings:
            print(f"  [WARN] {w}")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# validate
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_validate(
    *,
    repair: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Run full startup integrity checks across all brains.

    With --repair, auto-registers disk brains missing from governance.
    """
    from core.deployment.brain_lifecycle_manager import BrainLifecycleManager

    mgr = BrainLifecycleManager(project_root=project_root)
    report = mgr.verify_startup_integrity(auto_repair=repair)

    issues = 0
    if report.missing_config_files:
        issues += len(report.missing_config_files)
        print(
            f"[ISSUE] live.yaml entries with missing config files ({len(report.missing_config_files)}):"
        )
        for entry in report.missing_config_files:
            print(f"  - {entry}")

    if report.missing_yaml_entries:
        print(
            f"[INFO] Brains on disk not in live.yaml registry_entries "
            f"({len(report.missing_yaml_entries)} — auto-discovery handles these):"
        )
        for entry in report.missing_yaml_entries:
            print(f"  - {entry}")

    if report.missing_artifacts:
        issues += len(report.missing_artifacts)
        print(f"[ISSUE] Missing model artifacts ({len(report.missing_artifacts)}):")
        for entry in report.missing_artifacts:
            print(f"  - {entry}")

    if report.governance_orphans:
        issues += len(report.governance_orphans)
        print(f"[ISSUE] Governance orphans ({len(report.governance_orphans)}):")
        for entry in report.governance_orphans:
            print(f"  - {entry}")

    if report.hardcoded_path_mismatches:
        issues += len(report.hardcoded_path_mismatches)
        print(f"[ISSUE] Path/hash mismatches ({len(report.hardcoded_path_mismatches)}):")
        for entry in report.hardcoded_path_mismatches:
            print(f"  - {entry}")

    if report.alignment_hard_fails:
        issues += len(report.alignment_hard_fails)
        print(f"[HARD FAIL] brain→live alignment ({len(report.alignment_hard_fails)}):")
        for entry in report.alignment_hard_fails:
            print(f"  - {entry}")

    if report.alignment_warnings:
        print(f"[WARN] brain→live alignment ({len(report.alignment_warnings)}):")
        for entry in report.alignment_warnings:
            print(f"  - {entry}")

    if report.auto_registered:
        print(f"[REPAIR] Auto-registered in governance ({len(report.auto_registered)}):")
        for entry in report.auto_registered:
            print(f"  - {entry}")

    if report.auto_deleted:
        print(f"[REPAIR] SSOT enforcement — deleted from governance ({len(report.auto_deleted)}):")
        for entry in report.auto_deleted:
            print(f"  - {entry}")

    if report.contract_violations:
        issues += len(report.contract_violations)
        print(f"[CONTRACT VIOLATION] SSOT breaches ({len(report.contract_violations)}):")
        for entry in report.contract_violations:
            print(f"  - {entry}")

    if report.missing_norm_configs:
        issues += len(report.missing_norm_configs)
        print(f"[ISSUE] Missing normalization configs ({len(report.missing_norm_configs)}):")
        for entry in report.missing_norm_configs:
            print(f"  - {entry}")

    if report.valid:
        print("[OK] All brain integrity checks passed")
        return 0
    else:
        print(f"\n[FAIL] {issues} issue(s) found")
        return 1


# ═══════════════════════════════════════════════════════════════════════════════
# list
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_list(
    *,
    group: str | None = None,
    verbose: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """List all registered brains."""
    from core.brains.brain_registry import BrainRegistry

    registry = BrainRegistry.instance()
    entries = registry.list_by_group(group) if group else registry.list_all()

    if not entries:
        print("No brains found" + (f" in group '{group}'" if group else ""))
        return 0

    print(f"{'brain_id':<40} {'type':<20} {'status':<12} {'group':<20} {'weight':>8}")
    print("-" * 100)
    for e in sorted(entries, key=lambda x: (x.contract_group, x.brain_id)):
        print(
            f"{e.brain_id:<40} {e.brain_type:<20} {e.status:<12} "
            f"{e.contract_group:<20} {e.vote_weight:>8.2f}"
        )

    if verbose:
        print("\n─ Details ─")
        for e in sorted(entries, key=lambda x: (x.contract_group, x.brain_id)):
            print(f"\n  {e.brain_id}:")
            print(f"    brain_type:      {e.brain_type}")
            print(f"    brain_role:      {e.brain_role}")
            print(f"    contract_group:  {e.contract_group}")
            print(f"    status:          {e.status}")
            print(f"    vote_weight:     {e.vote_weight}")
            print(f"    magic:           {e.magic}")
            print(f"    artifact:        {e.artifact_path}")
            print(f"    feature_schema:  {e.feature_schema}")
            print(f"    training_horizon: {e.training_horizon}")
            if e.training_params:
                print(f"    training_params:  {json.dumps(e.training_params, indent=2)}")

    print(f"\nTotal: {len(entries)} brain(s)")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# retire
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_retire(
    brain_id: str,
    *,
    dry_run: bool = False,
    archive_artifacts: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Retire a brain in one atomic transaction."""
    from core.deployment.brain_lifecycle_manager import BrainLifecycleManager

    mgr = BrainLifecycleManager(project_root=project_root)
    report = mgr.retire_brain(
        brain_id,
        archive_artifacts=archive_artifacts,
        dry_run=dry_run,
    )

    if dry_run:
        print(f"[dry-run] Would retire '{brain_id}':")
        print(f"  governance_updated:  {report.governance_updated}")
        print(f"  transition_logged:   {report.transition_logged}")
        print(f"  config_archived:     {report.config_archived}")
        print(f"  live_yaml_removed:   {report.live_yaml_removed}")
        if report.reference_warnings:
            print(f"  reference_warnings ({len(report.reference_warnings)}):")
            for w in report.reference_warnings:
                print(f"    - {w}")
        return 0

    if report.errors:
        print(f"[FAIL] Retirement of '{brain_id}' had errors:", file=sys.stderr)
        for err in report.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[OK] Brain '{brain_id}' retired")
    if report.governance_updated:
        print("  governance: updated to 'retired'")
    if report.live_yaml_removed:
        print("  live.yaml: entry removed")
    if report.config_archived:
        print(f"  config: archived to {report.config_archived}")
    if report.artifact_report:
        print("  artifacts:")
        for a in report.artifact_report:
            print(f"    - {a}")
    if report.reference_warnings:
        print(f"  [WARN] Hardcoded references still exist ({len(report.reference_warnings)}):")
        for w in report.reference_warnings:
            print(f"    - {w}")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# promote / demote
# ═══════════════════════════════════════════════════════════════════════════════

# Natural advancement chain (lower index = lower status)
_STATUS_CHAIN = ["candidate", "probation", "live"]
# Descending chain for demotion
_DEMOTE_CHAIN = ["live", "probation", "frozen", "retired"]


def _auto_next_status(current: str, direction: str) -> str | None:
    """Return the natural next status for promote/demote, or None if terminal."""
    if direction == "promote":
        chain = _STATUS_CHAIN
        try:
            idx = chain.index(current)
            if idx + 1 < len(chain):
                return chain[idx + 1]
        except ValueError:
            pass
        # Special cases: frozen → probation
        if current == "frozen":
            return "probation"
        return None
    else:  # demote
        chain = _DEMOTE_CHAIN
        try:
            idx = chain.index(current)
            if idx + 1 < len(chain):
                return chain[idx + 1]
        except ValueError:
            pass
        # Special cases: candidate → retired
        if current == "candidate":
            return "retired"
        return None


def _do_transition(
    brain_id: str,
    target_status: str | None,
    *,
    direction: str,
    reason: str = "",
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Execute a governance transition for a single brain."""
    from core.governance.governance_service import GovernanceService

    gov_path = project_root / "data" / "governance_state.json"
    if not gov_path.exists():
        print(f"[ERROR] governance_state.json not found at {gov_path}", file=sys.stderr)
        return 2

    gov = GovernanceService.load(str(gov_path))
    state = gov.get_brain_state(brain_id)
    if state is None:
        print(f"[ERROR] Brain '{brain_id}' not found in governance_state.json", file=sys.stderr)
        return 2

    current = state["status"]
    if target_status is None:
        target_status = _auto_next_status(current, direction)

    if target_status is None:
        print(
            f"[INFO] Brain '{brain_id}' is at terminal status '{current}' — no {direction} possible"
        )
        return 0

    if target_status == current:
        print(f"[INFO] Brain '{brain_id}' is already '{current}'")
        return 0

    effective_reason = reason or f"manual:{direction}_command"
    result = gov.transition(brain_id, target_status, reason=effective_reason)

    if result.get("action") in ("transitioned", "registered"):
        gov.save(str(gov_path))
        print(f"[OK] Brain '{brain_id}': {current} → {target_status}")
        return 0
    else:
        print(
            f"[FAIL] Cannot {direction} '{brain_id}' from '{current}' to '{target_status}': "
            f"{result.get('reason', 'unknown')}",
            file=sys.stderr,
        )
        return 1


def cmd_promote(
    brain_id: str,
    *,
    to: str | None = None,
    reason: str = "",
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Promote a brain to the next status (or a specific target)."""
    return _do_transition(
        brain_id, to, direction="promote", reason=reason, project_root=project_root
    )


def cmd_demote(
    brain_id: str,
    *,
    to: str | None = None,
    reason: str = "",
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Demote a brain to the next lower status (or a specific target)."""
    return _do_transition(
        brain_id, to, direction="demote", reason=reason, project_root=project_root
    )


# ═══════════════════════════════════════════════════════════════════════════════
# reconcile
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_reconcile(
    *,
    auto_fix: bool = False,
    cleanup_ledger: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """One-click SSOT reconciliation: align governance + live.yaml + ledger with configs."""
    import json
    import os

    brains_dir = project_root / "configs" / "brains"
    gov_path = project_root / "data" / "governance_state.json"
    live_path = project_root / "configs" / "live.yaml"
    ledger_path = project_root / "data" / "brain_pnl_ledger.json"
    perf_path = project_root / "data" / "brain_performance.json"

    mode = "AUTO-FIX" if auto_fix else "DRY-RUN"
    print(f"[reconcile] Mode: {mode} (config is SSOT)")
    issues_fixed = 0

    # ── Load all sources ──
    brain_configs: dict[str, dict] = {}
    for fname in sorted(os.listdir(brains_dir)):
        if not fname.endswith(".json") or "normalization" in fname:
            continue
        path = brains_dir / fname
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            # Only process brain registry entries (skip meta filter configs etc.)
            if cfg.get("schema_version") != "brain_registry_entry.v1":
                continue
            bid = cfg.get("brain_id", fname)
            brain_configs[bid] = {"config": cfg, "path": str(path), "fname": fname}
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [SKIP] {fname}: {exc}")
            continue

    gov_data = {}
    if gov_path.exists():
        gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
    gov_states = gov_data.get("brain_states", {})

    live_yaml = ""
    if live_path.exists():
        live_yaml = live_path.read_text(encoding="utf-8")

    ledger_data = {}
    if ledger_path.exists():
        ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))

    perf_data = {}
    if perf_path.exists():
        perf_data = json.loads(perf_path.read_text(encoding="utf-8"))

    # ── 1. Config → Governance status alignment ──
    print("\n── 1. Governance status alignment ──")
    for bid, info in brain_configs.items():
        cfg = info["config"]
        cfg_status = cfg.get("status", "shadow")
        cfg_vw = cfg.get("vote_weight", 0.5)
        gov_state = gov_states.get(bid, {})
        gov_status = gov_state.get("status")
        gov_vw = gov_state.get("vote_weight")

        if gov_status is None:
            print(f"  MISSING: {bid} not in governance — registering as {cfg_status}")
            if auto_fix:
                gov_states[bid] = {"status": cfg_status, "vote_weight": cfg_vw}
                issues_fixed += 1
        elif gov_status != cfg_status:
            print(f"  DRIFT: {bid} config={cfg_status} gov={gov_status} → align to {cfg_status}")
            if auto_fix:
                gov_states[bid]["status"] = cfg_status
                issues_fixed += 1

        if gov_vw is not None and gov_vw != cfg_vw:
            print(f"  VOTE_DRIFT: {bid} config={cfg_vw} gov={gov_vw} → align to {cfg_vw}")
            if auto_fix:
                gov_states[bid]["vote_weight"] = cfg_vw
                issues_fixed += 1

    # ── 2. Frozen/retired brains → disable in live.yaml ──
    print("\n── 2. live.yaml enabled status ──")
    for bid, info in brain_configs.items():
        cfg_status = info["config"].get("status", "")
        fname = info["fname"]
        if cfg_status in ("frozen", "retired"):
            is_enabled = f"configs/brains/{fname}" in live_yaml and "enabled: true" in live_yaml.split(f"configs/brains/{fname}")[1][:30] if f"configs/brains/{fname}" in live_yaml else False
            should_be = "enabled: false"
            if auto_fix and is_enabled:
                print(f"  FIX: {bid} ({cfg_status}) → setting enabled: false")
                old_entry = f"  - path: configs/brains/{fname}\n    enabled: true"
                new_entry = f"  - path: configs/brains/{fname}\n    enabled: false"
                live_yaml = live_yaml.replace(old_entry, new_entry)
                issues_fixed += 1

    # ── 3. Archived brains → remove from governance ──
    print("\n── 3. Governance orphan cleanup ──")
    archive_dir = brains_dir / "archive_deprecated"
    if archive_dir.exists():
        for fname in sorted(os.listdir(str(archive_dir))):
            if not fname.endswith(".json"):
                continue
            try:
                cfg = json.loads((archive_dir / fname).read_text(encoding="utf-8"))
                bid = cfg.get("brain_id", "")
                if bid and bid in gov_states:
                    print(f"  ORPHAN: {bid} archived but in governance → removing")
                    if auto_fix:
                        del gov_states[bid]
                        issues_fixed += 1
            except (json.JSONDecodeError, OSError):
                pass

    # ── 4. Ledger zombie cleanup ──
    if cleanup_ledger:
        print("\n── 4. PnL Ledger zombie cleanup ──")
        active_bids = set(brain_configs.keys()) | set(gov_states.keys())
        settled = ledger_data.get("settled", {})
        zombies = [b for b in settled if b not in active_bids]
        print(f"  Zombies in ledger: {len(zombies)}/{len(settled)}")
        for bid in zombies[:5]:
            print(f"    {bid} ({len(settled[bid])} entries)")
        if auto_fix and zombies:
            retired_dir = project_root / "data" / "ledger" / "retired"
            retired_dir.mkdir(parents=True, exist_ok=True)
            retired_path = retired_dir / "brain_pnl_ledger_retired.json"
            retired_data = {}
            if retired_path.exists():
                retired_data = json.loads(retired_path.read_text(encoding="utf-8"))
            for bid in zombies:
                retired_data[bid] = settled.pop(bid)
            if auto_fix:
                retired_path.write_text(json.dumps(retired_data, indent=2, ensure_ascii=False), encoding="utf-8")
                ledger_data["settled"] = settled
                print(f"  Moved {len(zombies)} zombie brains to {retired_path}")

        # Performance contamination
        perf_bids = list(perf_data.keys())
        perf_zombies = [b for b in perf_bids if b not in active_bids and b != "schema_version"]
        print(f"\n  Zombies in brain_performance: {len(perf_zombies)}/{len(perf_bids)}")
        if auto_fix and perf_zombies:
            for bid in perf_zombies:
                if bid in perf_data:
                    del perf_data[bid]
            print(f"  Removed {len(perf_zombies)} zombies from brain_performance")

    # ── 5. Save ──
    if auto_fix and issues_fixed > 0:
        print(f"\n[reconcile] Saving {issues_fixed} fixes...")
        gov_path.write_text(json.dumps({**gov_data, "brain_states": gov_states}, indent=2, ensure_ascii=False), encoding="utf-8")
        live_path.write_text(live_yaml, encoding="utf-8")
        if cleanup_ledger:
            ledger_path.write_text(json.dumps(ledger_data, indent=2, ensure_ascii=False), encoding="utf-8")
            perf_path.write_text(json.dumps(perf_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[reconcile] DONE: {issues_fixed} issue(s) fixed.")
    elif not auto_fix:
        print(f"\n[reconcile] DRY-RUN complete. {issues_fixed} issue(s) would be fixed.")
        print("  Re-run with --auto-fix to apply changes.")
    else:
        print("\n[reconcile] No issues found. System is consistent.")

    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brain",
        description="Unified brain lifecycle CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # register
    reg = sub.add_parser("register", help="Register a new brain from a config JSON")
    reg.add_argument("config", type=Path, help="Path to brain_registry_entry.v1 JSON file")
    reg.add_argument(
        "--status",
        type=str,
        default="shadow",
        choices=["shadow", "candidate", "probation"],
        help="Initial governance status (default: shadow)",
    )
    reg.add_argument("--dry-run", action="store_true", help="Validate only, no writes")

    # validate
    val = sub.add_parser("validate", help="Run full brain integrity checks")
    val.add_argument(
        "--repair",
        action="store_true",
        help="Auto-register disk brains missing from governance",
    )

    # list
    lst = sub.add_parser("list", help="List all registered brains")
    lst.add_argument("--group", "-g", type=str, default=None, help="Filter by contract_group")
    lst.add_argument("--verbose", "-v", action="store_true", help="Show full details")

    # promote
    prom = sub.add_parser("promote", help="Promote a brain (candidate→probation→live)")
    prom.add_argument("brain_id", type=str, help="Brain ID to promote")
    prom.add_argument(
        "--to",
        type=str,
        default=None,
        choices=["probation", "live"],
        help="Target status (default: auto-advance one step)",
    )
    prom.add_argument("--reason", type=str, default="", help="Reason for promotion")

    # demote
    dem = sub.add_parser("demote", help="Demote a brain (live→probation→frozen→retired)")
    dem.add_argument("brain_id", type=str, help="Brain ID to demote")
    dem.add_argument(
        "--to",
        type=str,
        default=None,
        choices=["probation", "frozen", "retired"],
        help="Target status (default: auto-advance one step down)",
    )
    dem.add_argument("--reason", type=str, default="", help="Reason for demotion")

    # retire
    ret = sub.add_parser("retire", help="Retire a brain")
    ret.add_argument("brain_id", type=str, help="Brain ID to retire")
    ret.add_argument(
        "--archive-artifacts",
        action="store_true",
        help="Also move model artifacts to retired/ (default: keep in place)",
    )
    ret.add_argument("--dry-run", action="store_true", help="Preview only, no changes")

    # reconcile
    rec = sub.add_parser(
        "reconcile",
        help="Auto-align governance, live.yaml, and ledger with brain configs (config is SSOT)",
    )
    rec.add_argument(
        "--auto-fix",
        action="store_true",
        help="Apply fixes (default: dry-run, report only)",
    )
    rec.add_argument(
        "--cleanup-ledger",
        action="store_true",
        help="Also remove zombie brain PnL records from ledger",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "register":
        return cmd_register(
            args.config,
            status=args.status,
            dry_run=args.dry_run,
        )
    elif args.command == "validate":
        return cmd_validate(repair=args.repair)
    elif args.command == "list":
        return cmd_list(group=args.group, verbose=args.verbose)
    elif args.command == "promote":
        return cmd_promote(args.brain_id, to=args.to, reason=args.reason)
    elif args.command == "demote":
        return cmd_demote(args.brain_id, to=args.to, reason=args.reason)
    elif args.command == "retire":
        return cmd_retire(
            args.brain_id,
            dry_run=args.dry_run,
            archive_artifacts=args.archive_artifacts,
        )
    elif args.command == "reconcile":
        return cmd_reconcile(auto_fix=args.auto_fix, cleanup_ledger=args.cleanup_ledger)
    else:
        print(f"[ERROR] Unknown command: {args.command}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

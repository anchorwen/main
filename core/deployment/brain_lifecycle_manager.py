"""Brain Lifecycle Manager — one-shot retirement, registration, and integrity checks.

Central orchestrator for brain lifecycle operations.  Does NOT depend on MT5 or
any runtime state; operates on files and the GovernanceService.

Usage::

    mgr = BrainLifecycleManager(project_root=PROJECT_ROOT)
    report = mgr.retire_brain("V9_Institutional_01", dry_run=True)
    report = mgr.register_brain("configs/brains/new_brain.json")
    integrity = mgr.verify_startup_integrity()
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.deployment.path_defaults import BRAINS_DIR, LIVE_YAML_PATH, RETIRED_BRAINS_DIR

# ── Report data classes ────────────────────────────────────────────────────


@dataclass
class RetirementReport:
    brain_id: str = ""
    governance_updated: bool = False
    config_archived: str | None = None
    live_yaml_updated: bool = False
    artifact_report: list[str] = field(default_factory=list)
    reference_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RegistrationReport:
    brain_id: str = ""
    config_validated: bool = False
    artifact_found: bool = False
    norm_config_found: bool = False
    governance_registered: bool = False
    live_yaml_added: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quality_gate_passed: bool = False


@dataclass
class IntegrityReport:
    valid: bool = True
    missing_config_files: list[str] = field(default_factory=list)
    missing_yaml_entries: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)
    missing_norm_configs: list[str] = field(default_factory=list)
    governance_orphans: list[str] = field(default_factory=list)
    pnl_ledger_orphans: list[str] = field(default_factory=list)
    hardcoded_path_mismatches: list[str] = field(default_factory=list)


@dataclass
class ReferenceAuditReport:
    scanned_files: int = 0
    hardcoded_brain_paths: list[tuple[str, int, str]] = field(default_factory=list)
    hardcoded_model_paths: list[tuple[str, int, str]] = field(default_factory=list)
    hardcoded_norm_paths: list[tuple[str, int, str]] = field(default_factory=list)
    stale_references: list[str] = field(default_factory=list)


# ── Manager ─────────────────────────────────────────────────────────────────

_REQUIRED_BRAIN_FIELDS = {"brain_id", "brain_type", "artifact_path", "schema_version"}


class BrainLifecycleManager:
    """Central orchestrator for brain registration, retirement, and integrity."""

    def __init__(
        self,
        project_root: Path | None = None,
        base_dir: str = "data",
        brains_dir: str = BRAINS_DIR,
        retired_dir: str = RETIRED_BRAINS_DIR,
        live_yaml_path: str = LIVE_YAML_PATH,
    ) -> None:
        self._project_root = project_root or Path.cwd()
        self._base_dir = self._project_root / base_dir
        self._brains_dir = self._project_root / brains_dir
        self._retired_dir = self._project_root / retired_dir
        self._live_yaml_path = self._project_root / live_yaml_path

    # ── helpers ─────────────────────────────────────────────────────────

    def _find_config_by_brain_id(self, brain_id: str) -> Path | None:
        """Scan the brains directory for a config whose brain_id field matches."""
        for cfg in sorted(self._brains_dir.glob("*.json")):
            if "normalization" in cfg.name:
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if data.get("brain_id") == brain_id:
                    return cfg
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _load_live_yaml(self) -> dict[str, Any]:
        import yaml

        if not self._live_yaml_path.exists():
            return {}
        return yaml.safe_load(self._live_yaml_path.read_text(encoding="utf-8")) or {}

    def _save_live_yaml(self, config: dict[str, Any]) -> None:
        import yaml

        self._live_yaml_path.write_text(
            yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _load_governance_service(self):
        """Load GovernanceService from the standard state path, or return a fresh one."""
        from core.governance.governance_service import GovernanceService

        state_path = self._base_dir / "governance_state.json"
        if state_path.exists():
            return GovernanceService.load(str(state_path))
        return GovernanceService()

    def _save_governance_service(self, gov) -> None:
        state_path = self._base_dir / "governance_state.json"
        gov.save(str(state_path))

    @staticmethod
    def _scan_brain_configs(brains_dir: Path) -> dict[str, dict]:
        """Return {brain_id: config_dict} for all brain configs on disk."""
        result: dict[str, dict] = {}
        if not brains_dir.exists():
            return result
        for cfg in sorted(brains_dir.glob("*.json")):
            if "normalization" in cfg.name.lower():
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                bid = data.get("brain_id")
                if bid:
                    result[bid] = data
            except (json.JSONDecodeError, OSError):
                continue
        return result

    # ── one-shot retirement ──────────────────────────────────────────────

    def retire_brain(
        self,
        brain_id: str,
        *,
        archive_config: bool = True,
        archive_artifacts: bool = False,
        scan_references: bool = True,
        dry_run: bool = False,
    ) -> RetirementReport:
        """Retire a brain in one shot.

        Args:
            brain_id: The brain to retire.
            archive_config: Move the config JSON to retired/ directory.
            archive_artifacts: Also move model artifacts (default False — manual review).
            scan_references: Scan codebase for hardcoded references to this brain's files.
            dry_run: Report what would happen without making changes.
        """
        report = RetirementReport(brain_id=brain_id)

        # 1. Governance state transition
        gov = self._load_governance_service()
        if not dry_run:
            result = gov.transition(brain_id, "retired", reason="manual:lifecycle_manager")
            if result.get("action") in ("transitioned", "registered"):
                report.governance_updated = True
                self._save_governance_service(gov)
            else:
                report.errors.append(f"governance_transition_failed: {result}")
        else:
            state = gov.get_brain_state(brain_id)
            report.governance_updated = state is not None or True  # would succeed

        # 2. Archive config file
        config_path = self._find_config_by_brain_id(brain_id)
        if config_path and archive_config:
            target = self._retired_dir / config_path.name
            if not dry_run:
                self._retired_dir.mkdir(parents=True, exist_ok=True)
                config_path.rename(target)
            report.config_archived = str(target)

        # 3. Update live.yaml
        if self._live_yaml_path.exists():
            live = self._load_live_yaml()
            entries = live.get("brains", {}).get("registry_entries", [])
            updated = False
            for entry in entries:
                path_str = entry.get("path", "")
                if config_path and Path(path_str).name == config_path.name:
                    if not dry_run:
                        entry["enabled"] = False
                    updated = True
                    break
            if updated:
                report.live_yaml_updated = True
                if not dry_run:
                    self._save_live_yaml(live)

        # 4. Report artifact paths (do NOT auto-delete)
        if config_path:
            try:
                cfg = (
                    json.loads(config_path.read_text(encoding="utf-8"))
                    if config_path.exists()
                    else {}
                )
            except (json.JSONDecodeError, OSError):
                cfg = {}
            for key in ("artifact_path", "normalization_config_path"):
                val = cfg.get(key)
                if val and isinstance(val, str):
                    artifact = self._project_root / val
                    if artifact.exists():
                        report.artifact_report.append(
                            f"{'would_archive' if archive_artifacts else 'orphan'}: {val}"
                        )
                    else:
                        report.artifact_report.append(f"already_missing: {val}")

        # 5. Scan for hardcoded references
        if scan_references and config_path:
            refs = self._scan_hardcoded_refs_for_path(str(config_path.name))
            report.reference_warnings = refs

        return report

    # ── one-shot registration ────────────────────────────────────────────

    def register_brain(
        self,
        config_path: str | Path,
        *,
        initial_status: str = "shadow",
        dry_run: bool = False,
    ) -> RegistrationReport:
        """Register a new brain in one shot."""
        report = RegistrationReport()
        cfg_path = Path(config_path)
        if not cfg_path.is_absolute():
            cfg_path = (self._project_root / cfg_path).resolve()

        # 1. Validate config exists and has required fields
        if not cfg_path.exists():
            report.errors.append(f"config_file_missing: {cfg_path}")
            return report

        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            report.errors.append(f"config_parse_error: {exc}")
            return report

        report.brain_id = cfg.get("brain_id", "")
        missing = _REQUIRED_BRAIN_FIELDS - set(cfg.keys())
        if missing:
            report.errors.append(f"missing_required_fields: {missing}")
            return report
        report.config_validated = True

        # 2. Verify artifact exists
        artifact = cfg.get("artifact_path", "")
        if artifact:
            art_path = self._project_root / artifact
            report.artifact_found = art_path.exists()
            if not report.artifact_found:
                report.errors.append(f"artifact_missing: {artifact}")

        # 3. Verify normalization config if present
        norm_path = cfg.get("normalization_config_path", "")
        if norm_path:
            np = self._project_root / norm_path
            report.norm_config_found = np.exists()
            if not report.norm_config_found:
                report.errors.append(f"normalization_config_missing: {norm_path}")

        # 4. Quality gate: verify training metrics meet minimum thresholds
        #    (proxy for forward-Sharpe until walk-forward pipeline is built)
        train_sharpe = cfg.get("train_sharpe")
        train_winrate = cfg.get("train_winrate")
        train_profit_factor = cfg.get("train_profit_factor")
        train_max_dd = cfg.get("train_max_dd")

        if train_sharpe is not None and train_sharpe <= 0:
            report.errors.append(f"quality_gate_failed: train_sharpe={train_sharpe} (require > 0)")
        if train_winrate is not None and train_winrate <= 0.50:
            report.errors.append(
                f"quality_gate_failed: train_winrate={train_winrate} (require > 0.50)"
            )
        if train_profit_factor is not None and train_profit_factor <= 1.0:
            report.errors.append(
                f"quality_gate_failed: train_profit_factor={train_profit_factor} (require > 1.0)"
            )
        if train_max_dd is not None and train_max_dd >= 99.0:
            report.warnings.append(
                f"quality_gate_warning: train_max_dd={train_max_dd}% — likely overfit"
            )
        if report.errors:
            return report
        report.quality_gate_passed = True

        # 5. Check no duplicate brain_id
        existing = self._scan_brain_configs(self._brains_dir)
        if report.brain_id in existing and existing[report.brain_id] != cfg:
            report.errors.append(f"duplicate_brain_id: {report.brain_id}")

        # 5. Add to live.yaml
        if self._live_yaml_path.exists() and not dry_run:
            live = self._load_live_yaml()
            entries: list = live.setdefault("brains", {}).setdefault("registry_entries", [])
            rel_path = str(cfg_path.relative_to(self._project_root)).replace("\\", "/")
            if not any(e.get("path", "") == rel_path for e in entries):
                entries.append(
                    {
                        "path": f"configs/brains/{cfg_path.name}",
                        "enabled": True,
                    }
                )
                report.live_yaml_added = True
                self._save_live_yaml(live)

        # 6. Register in governance
        if not dry_run:
            gov = self._load_governance_service()
            gov.register_brain(report.brain_id, initial_status=initial_status)
            self._save_governance_service(gov)
            report.governance_registered = True

        return report

    # ── startup integrity ────────────────────────────────────────────────

    def verify_startup_integrity(self, *, fail_fast: bool = False) -> IntegrityReport:
        """Cross-validate live.yaml entries, disk files, governance state, and PnL ledger.

        Raises RuntimeError if *fail_fast* is True and any mismatch is found.
        """
        report = IntegrityReport()

        # ── live.yaml entries → disk ──
        if self._live_yaml_path.exists():
            live = self._load_live_yaml()
            entries = live.get("brains", {}).get("registry_entries", [])
            for entry in entries:
                if not entry.get("enabled", True):
                    continue
                path_str = entry.get("path", "")
                entry_path = Path(path_str)
                if not entry_path.is_absolute():
                    entry_path = (self._project_root / entry_path).resolve()
                if not entry_path.exists():
                    report.missing_config_files.append(path_str)
        else:
            report.missing_config_files.append(f"live_yaml_missing: {self._live_yaml_path}")

        # ── disk → live.yaml ──
        disk_brains = self._scan_brain_configs(self._brains_dir)
        if self._live_yaml_path.exists():
            live = self._load_live_yaml()
            entries = live.get("brains", {}).get("registry_entries", [])
            yaml_names = set()
            for entry in entries:
                p = entry.get("path", "")
                yaml_names.add(Path(p).name)
            for cfg_name in sorted(self._brains_dir.glob("*.json")):
                if "normalization" in cfg_name.name.lower():
                    continue
                if cfg_name.name not in yaml_names:
                    report.missing_yaml_entries.append(
                        str(cfg_name.relative_to(self._project_root))
                    )

        # ── artifact paths ──
        for _bid, cfg in disk_brains.items():
            for key in ("artifact_path", "normalization_config_path"):
                val = cfg.get(key)
                if val and isinstance(val, str):
                    p = self._project_root / val
                    if not p.exists():
                        if "normalization" in key:
                            report.missing_norm_configs.append(f"{_bid}: {val}")
                        else:
                            report.missing_artifacts.append(f"{_bid}: {val}")

        # ── governance orphans ──
        gov_path = self._base_dir / "governance_state.json"
        if gov_path.exists():
            try:
                gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
                for bid in gov_data.get("brain_states", {}):
                    # Check if config exists on disk (by scanning for matching brain_id)
                    found = bid in disk_brains
                    # Also check retired dir
                    if not found and self._retired_dir.exists():
                        for rc in self._retired_dir.glob("*.json"):
                            try:
                                rc_data = json.loads(rc.read_text(encoding="utf-8"))
                                if rc_data.get("brain_id") == bid:
                                    found = True
                                    break
                            except (json.JSONDecodeError, OSError):
                                continue
                    if not found:
                        report.governance_orphans.append(bid)
            except (json.JSONDecodeError, OSError):
                pass

        # ── validate DEFAULT_* paths ──
        from core.deployment.path_defaults import validate_defaults

        for name, exists in validate_defaults().items():
            if not exists:
                report.hardcoded_path_mismatches.append(f"path_defaults.{name}")

        # ── assess validity ──
        report.valid = not (
            report.missing_config_files
            or report.missing_yaml_entries
            or report.missing_artifacts
            or report.missing_norm_configs
            or report.hardcoded_path_mismatches
        )

        if fail_fast and not report.valid:
            raise RuntimeError(f"Startup integrity check failed: {report}")

        return report

    # ── reference audit ──────────────────────────────────────────────────

    def audit_hardcoded_references(
        self,
        scan_dirs: list[str] | None = None,
    ) -> ReferenceAuditReport:
        """Scan Python files for hardcoded paths to configs/brains/ and data/models/."""
        if scan_dirs is None:
            scan_dirs = ["scripts", "core", "tests", "apps"]

        report = ReferenceAuditReport()
        patterns = {
            "brain": re.compile(r"""["'](configs/brains/[^"']+\.json)["']"""),
            "model": re.compile(r"""["'](data/models/[^"']+)["']"""),
            "norm": re.compile(r"""["'](configs/brains/[^"']*normalization[^"']*\.json)["']"""),
        }

        for scan_dir in scan_dirs:
            dir_path = self._project_root / scan_dir
            if not dir_path.exists():
                continue
            for py_file in dir_path.rglob("*.py"):
                report.scanned_files += 1
                try:
                    lines = py_file.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for lineno, line in enumerate(lines, 1):
                    for cat, pat in patterns.items():
                        for match in pat.finditer(line):
                            path_str = match.group(1)
                            entry = (str(py_file.relative_to(self._project_root)), lineno, path_str)
                            if cat == "brain":
                                report.hardcoded_brain_paths.append(entry)
                            elif cat == "model":
                                report.hardcoded_model_paths.append(entry)
                            else:
                                report.hardcoded_norm_paths.append(entry)
                            # Check if the referenced file exists
                            resolved = self._project_root / path_str
                            if not resolved.exists():
                                report.stale_references.append(f"{entry[0]}:{entry[1]}: {path_str}")

        return report

    def _scan_hardcoded_refs_for_path(self, filename: str) -> list[str]:
        """Quick scan for hardcoded references to a specific filename. Returns warnings."""
        warnings: list[str] = []
        pattern = re.compile(re.escape(filename))
        for scan_dir in ("scripts", "core", "tests", "apps"):
            dir_path = self._project_root / scan_dir
            if not dir_path.exists():
                continue
            for py_file in dir_path.rglob("*.py"):
                try:
                    text = py_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        warnings.append(
                            f"{py_file.relative_to(self._project_root)}:{lineno}: {line.strip()[:120]}"
                        )
        return warnings

    # ── PnL pruning ──────────────────────────────────────────────────────

    def prune_retired_pnl(self) -> dict[str, int]:
        """Remove PnL records for all brains currently in retired status.

        Checks both brain_states (status==retired) and the transition_log
        (last transition was to retired) since retired brains are eventually
        removed from brain_states but may remain in the PnL ledger.
        """
        retired_ids: set[str] = set()

        # 1. brain_states entries with status == retired
        gov = self._load_governance_service()
        for bid, s in gov.get_all_states().items():
            if s.get("status") == "retired":
                retired_ids.add(bid)

        # 2. transition_log entries — brains whose last recorded transition was to retired
        gov_path = self._base_dir / "governance_state.json"
        if gov_path.exists():
            try:
                gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
                log = gov_data.get("transition_log", [])
                # Build {brain_id: last_to_status} from transition log
                last_to: dict[str, str] = {}
                for entry in log:
                    bid = entry.get("brain_id", "")
                    to_status = entry.get("to_status", "")
                    if bid and to_status:
                        # entries are in chronological order; later wins
                        last_to[bid] = to_status
                for bid, to_status in last_to.items():
                    if to_status == "retired":
                        retired_ids.add(bid)
            except (json.JSONDecodeError, OSError):
                pass

        ledger_path = self._base_dir / "brain_pnl_ledger.json"
        if not ledger_path.exists():
            return {}

        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        settled: dict = ledger.get("settled", {})
        removed: dict[str, int] = {}
        for bid in list(settled.keys()):
            if bid in retired_ids:
                removed[bid] = len(settled.pop(bid, []))

        if removed:
            ledger["settled"] = settled
            # Create backup before overwriting
            backup = ledger_path.with_suffix(".json.bak")
            ledger_path.rename(backup)
            ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False, default=str))

        return removed

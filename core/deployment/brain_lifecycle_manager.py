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
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.deployment.path_defaults import BRAINS_DIR, LIVE_YAML_PATH, RETIRED_BRAINS_DIR


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _utc_now_compact() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


# ── Report data classes ────────────────────────────────────────────────────


@dataclass
class RetirementReport:
    brain_id: str = ""
    governance_updated: bool = False
    transition_logged: bool = False
    config_archived: str | None = None
    live_yaml_removed: bool = False
    pnl_archived: bool = False
    atomic_success: bool = False
    rollback_triggered: bool = False
    artifact_report: list[str] = field(default_factory=list)
    reference_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    # ── brain→live alignment (Layer 3 institutional validator) ──
    alignment_hard_fails: list[str] = field(default_factory=list)
    alignment_warnings: list[str] = field(default_factory=list)
    alignment_ensemble_warnings: list[str] = field(default_factory=list)
    # ── auto-repair tracking ──
    auto_registered: list[str] = field(default_factory=list)
    auto_deleted: list[str] = field(default_factory=list)
    # ── SSOT enforcement ──
    contract_violations: list[str] = field(default_factory=list)


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
        """Retire a brain in one atomic transaction.

        Transaction boundary: governance_state.json + live.yaml + config move.
        If any step fails, all changes are rolled back.

        Args:
            brain_id: The brain to retire.
            archive_config: Move the config JSON to retired/ directory.
            archive_artifacts: Also move model artifacts (default False — manual review).
            scan_references: Scan codebase for hardcoded references to this brain's files.
            dry_run: Report what would happen without making changes.
        """
        report = RetirementReport(brain_id=brain_id)
        config_path = self._find_config_by_brain_id(brain_id)

        if dry_run:
            state = self._load_governance_service().get_brain_state(brain_id)
            report.governance_updated = state is not None
            report.transition_logged = state is not None
            if config_path:
                report.config_archived = str(self._retired_dir / config_path.name)
            report.live_yaml_removed = config_path is not None
            report.pnl_archived = self._pnl_exists(brain_id)
            if scan_references and config_path:
                report.reference_warnings = self._scan_hardcoded_refs_for_path(
                    str(config_path.name)
                )
            return report

        # ── Atomic transaction ──
        from core.deployment.atomic_file_writer import AtomicFileWriter

        gov_state_path = self._base_dir / "governance_state.json"
        targets = [gov_state_path]
        if self._live_yaml_path.exists():
            targets.append(self._live_yaml_path)

        writer = AtomicFileWriter(targets)
        try:
            writer.backup()

            # 1. Governance state transition (in memory, then staged to file)
            gov = self._load_governance_service()
            result = gov.transition(brain_id, "retired", reason="manual:lifecycle_manager")
            if result.get("action") not in ("transitioned", "registered"):
                report.errors.append(f"governance_transition_failed: {result}")
                writer.rollback()
                report.rollback_triggered = True
                return report
            report.governance_updated = True
            report.transition_logged = True
            gov.save(str(gov_state_path))

            # 2. Build new live.yaml in memory, remove brain entry
            if config_path and self._live_yaml_path.exists():
                live = self._load_live_yaml()
                entries = live.get("brains", {}).get("registry_entries", [])
                removed = False
                new_entries = []
                for entry in entries:
                    path_str = entry.get("path", "")
                    if Path(path_str).name == config_path.name:
                        removed = True
                        continue
                    new_entries.append(entry)
                if removed:
                    live["brains"]["registry_entries"] = new_entries
                    self._save_live_yaml(live)
                    report.live_yaml_removed = True
                else:
                    report.warnings.append(
                        f"brain_id={brain_id} not found in live.yaml registry_entries"
                    )

            # 3. Archive config file
            if config_path and archive_config:
                self._retired_dir.mkdir(parents=True, exist_ok=True)
                target = self._retired_dir / config_path.name
                # Avoid overwriting existing retired config
                if target.exists():
                    target = target.with_name(f"{target.stem}_{_utc_now_compact()}{target.suffix}")
                config_path.rename(target)
                report.config_archived = str(target)

            # 4. Cold-storage PnL transfer (never delete)
            report.pnl_archived = self._archive_retired_pnl(brain_id)

            # 5. Report artifact paths (do NOT auto-delete)
            if config_path and config_path.exists():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    cfg = {}
            elif config_path:
                # Config was moved; try reading from retired location
                archived = report.config_archived
                if archived:
                    try:
                        cfg = json.loads(Path(archived).read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        cfg = {}
                else:
                    cfg = {}
            else:
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

            # 6. Scan for hardcoded references
            if scan_references and config_path:
                report.reference_warnings = self._scan_hardcoded_refs_for_path(
                    str(config_path.name)
                )

            writer.commit()
            report.atomic_success = True

        except Exception as exc:
            report.errors.append(f"retirement_transaction_failed: {exc}")
            try:
                writer.rollback()
                report.rollback_triggered = True
            except Exception as rb_exc:
                report.errors.append(f"rollback_failed: {rb_exc}")

        return report

    # ── PnL cold storage ────────────────────────────────────────────────

    def _archive_retired_pnl(self, brain_id: str) -> bool:
        """Move a single brain's PnL records from hot ledger to cold storage.

        Uses per-brain sharding (data/ledger/retired/{brain_id}.json) to avoid
        concurrent-write corruption from multi-process retirement.
        """
        ledger_path = self._base_dir / "brain_pnl_ledger.json"
        if not ledger_path.exists():
            return False

        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        settled = ledger.get("settled", {})
        records = settled.pop(brain_id, None)
        if records is None:
            return False

        # Write cold-storage file
        cold_dir = self._base_dir / "ledger" / "retired"
        cold_dir.mkdir(parents=True, exist_ok=True)
        cold_entry = {
            "brain_id": brain_id,
            "archived_at": _utc_now_iso(),
            "final_status": "retired",
            "record_count": len(records),
            "records": records,
        }
        cold_path = cold_dir / f"{brain_id}.json"
        cold_path.write_text(
            json.dumps(cold_entry, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        # Update hot ledger
        ledger["settled"] = settled
        backup_path = ledger_path.with_suffix(".json.bak")
        ledger_path.rename(backup_path)
        ledger_path.write_text(
            json.dumps(ledger, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        return True

    def _pnl_exists(self, brain_id: str) -> bool:
        """Check if a brain has PnL records in the hot ledger."""
        ledger_path = self._base_dir / "brain_pnl_ledger.json"
        if not ledger_path.exists():
            return False
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            return brain_id in ledger.get("settled", {})
        except (json.JSONDecodeError, OSError):
            return False

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

    # ── brain→live alignment validator (Layer 3 institutional) ──────────

    _HORIZON_EXPANSION_WARN_RATIO = 1.5
    _TP_DEVIATION_WARN_PCT = 0.15

    def validate_brain_live_alignment(self, report: IntegrityReport) -> None:
        """Institutional Layer 3 validator: cross-validate brain training_params
        against live.yaml strategy_line parameters.

        **Vertical checks** (per brain→strategy):
        - HARD FAIL: live SL tighter than training SL (model drawdown tolerance truncated)
        - HARD FAIL: live horizon < training horizon (prediction window amputated)
        - WARNING:   live horizon > training horizon × 1.5 (prediction expired)
        - WARNING:   live TP deviates from training TP by > 15%

        **Horizontal checks** (cross-brain ensemble):
        - WARNING:   brains in same contract_group have inconsistent training SL/TP
        """
        if not self._live_yaml_path.exists():
            return

        live = self._load_live_yaml()
        strategy_lines = live.get("strategy_lines", {})
        if not strategy_lines:
            return

        disk_brains = self._scan_brain_configs(self._brains_dir)
        if not disk_brains:
            return

        for sname, scfg in strategy_lines.items():
            if not isinstance(scfg, dict):
                continue
            if not scfg.get("enabled", True):
                continue

            # ── live parameters ──
            live_sl_cfg = scfg.get("sl", {})
            live_sl = live_sl_cfg.get("base_atr_mult") if isinstance(live_sl_cfg, dict) else None
            live_tp_cfg = scfg.get("tp", {})
            live_tp = live_tp_cfg.get("base_atr_mult") if isinstance(live_tp_cfg, dict) else None
            exit_cfg = scfg.get("exit", {})
            live_exit_cycles = (
                exit_cfg.get("time_exit_cycles") if isinstance(exit_cfg, dict) else None
            )

            # ── find brains in this contract_group ──
            group_brains: list[dict] = []
            for _bid, cfg in disk_brains.items():
                if cfg.get("contract_group") == sname:
                    group_brains.append(cfg)

            if not group_brains:
                continue

            # ── Horizontal: cross-brain ensemble consistency ──
            group_sl_values: set[float] = set()
            group_tp_values: set[float] = set()
            for cfg in group_brains:
                tp = cfg.get("training_params", {})
                if tp.get("sl_atr_mult") is not None:
                    group_sl_values.add(float(tp["sl_atr_mult"]))
                if tp.get("tp_atr_mult") is not None:
                    group_tp_values.add(float(tp["tp_atr_mult"]))

            if len(group_sl_values) > 1:
                report.alignment_ensemble_warnings.append(
                    f"ENSEMBLE_SL_MISMATCH:{sname}: brains have inconsistent "
                    f"training SL: {sorted(group_sl_values)} — "
                    f"consider splitting into separate strategy lines"
                )
            if len(group_tp_values) > 1:
                report.alignment_ensemble_warnings.append(
                    f"ENSEMBLE_TP_MISMATCH:{sname}: brains have inconsistent "
                    f"training TP: {sorted(group_tp_values)}"
                )

            # ── Vertical: per-brain checks ──
            for cfg in group_brains:
                bid = cfg.get("brain_id", "?")
                tp = cfg.get("training_params", {})
                if not tp:
                    continue

                train_sl = tp.get("sl_atr_mult")
                train_tp = tp.get("tp_atr_mult")
                train_horizon = tp.get("horizon_bars")

                # --- SL hard fail: live tighter than training ---
                if train_sl is not None and live_sl is not None and live_sl < train_sl:
                    report.alignment_hard_fails.append(
                        f"SL_TIGHTENED:{sname}:{bid}: live SL={live_sl} < "
                        f"training SL={train_sl}. Model drawdown tolerance amputated."
                    )

                # --- Horizon hard fail: live shorter than training ---
                if train_horizon is not None and train_horizon > 0 and live_exit_cycles is not None:
                    if live_exit_cycles < train_horizon:
                        report.alignment_hard_fails.append(
                            f"HORIZON_TRUNCATED:{sname}:{bid}: live time_exit_cycles="
                            f"{live_exit_cycles} < training horizon={train_horizon}. "
                            f"Prediction window amputated."
                        )
                    elif live_exit_cycles > train_horizon * self._HORIZON_EXPANSION_WARN_RATIO:
                        report.alignment_warnings.append(
                            f"HORIZON_EXPANDED:{sname}:{bid}: live time_exit_cycles="
                            f"{live_exit_cycles} significantly exceeds training horizon="
                            f"{train_horizon} (ratio={live_exit_cycles / train_horizon:.1f}x). "
                            f"Model prediction may have expired before exit."
                        )

                # --- TP deviation warning ---
                if train_tp is not None and live_tp is not None and train_tp > 0:
                    tp_dev = abs(live_tp - train_tp) / train_tp
                    if tp_dev > self._TP_DEVIATION_WARN_PCT:
                        report.alignment_warnings.append(
                            f"TP_DEVIATION:{sname}:{bid}: live TP={live_tp} deviates "
                            f"from training TP={train_tp} by {tp_dev:.0%}"
                        )

    # ── startup integrity ────────────────────────────────────────────────

    def verify_startup_integrity(
        self, *, fail_fast: bool = False, auto_repair: bool = False
    ) -> IntegrityReport:
        """Cross-validate live.yaml, disk files, governance state, PnL ledger,
        transition_log coverage, ensemble references, capability handshake,
        and artifact hash integrity.

        **SSOT Contract (Single Source of Truth):**
        Physical files in ``configs/brains/`` are the absolute authority.
        ``governance_state.json`` is a pure state vassal — it reflects disk,
        never drives it.

        When *auto_repair* is True:
        - Brains on disk missing from governance → auto-registered as ``candidate``.
        - Governance entries WITHOUT matching disk configs → **DELETED** (key removed).
          No freeze, no retire — the entry is physically erased from the JSON dict.
          This is the "governance must reflect disk" enforcement.
        - ``missing_yaml_entries`` does NOT invalidate the report (auto-discovery
          via BrainRegistry handles it).

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

        # ── disk → live.yaml (informational only when auto-discovery is active) ──
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
                # Skip non-brain configs (e.g., filter configs with "filter_id" instead of "brain_id")
                try:
                    data = json.loads(cfg_name.read_text(encoding="utf-8"))
                    if not data.get("brain_id"):
                        continue
                except (json.JSONDecodeError, OSError):
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

        # ── governance: disk brains missing from governance → auto-register ──
        gov_data: dict = {}
        gov_path = self._base_dir / "governance_state.json"
        if gov_path.exists():
            try:
                gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        gov_brain_ids = set(gov_data.get("brain_states", {}).keys()) if gov_data else set()
        missing_from_gov = set(disk_brains.keys()) - gov_brain_ids

        if missing_from_gov and auto_repair:
            gov = self._load_governance_service()
            for bid in sorted(missing_from_gov):
                cfg = disk_brains[bid]
                cfg_status = cfg.get("status", "candidate")
                # Use the config's declared status, but default to candidate
                # for safety (never auto-promote to live)
                initial = cfg_status if cfg_status in ("candidate", "shadow") else "candidate"
                gov.register_brain(bid, initial_status=initial)
                report.auto_registered.append(f"{bid}:{initial}")
                logging.warning(
                    "BrainLifecycleManager: auto-registered '%s' in governance as '%s'",
                    bid,
                    initial,
                )
            self._save_governance_service(gov)
            # Reload gov_data for subsequent checks
            try:
                gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        elif missing_from_gov:
            for bid in sorted(missing_from_gov):
                report.hardcoded_path_mismatches.append(
                    f"{bid}: on disk but NOT in governance_state.json "
                    f"(run with --auto-repair or use 'brain register')"
                )

        # ── governance orphans (in governance but not on disk) ──
        # SSOT CONTRACT: Physical files are law.  governance_state.json is a
        # pure state vassal.  If a brain exists in governance but has NO config
        # on disk and NO retired config, it is state contamination and MUST be
        # physically deleted from the JSON dict — not frozen, not retired.
        if gov_data:
            for bid in list(gov_data.get("brain_states", {}).keys()):
                found = bid in disk_brains
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
                    report.contract_violations.append(
                        f"SSOT_VIOLATION:{bid}: in governance_state.json but "
                        f"no config file on disk — state contamination"
                    )

        if report.governance_orphans and auto_repair:
            # ── Dictator Governance Engine: physical deletion ──
            # These entries have no config on disk and no retired config.
            # The SSOT contract demands their keys be removed from governance.
            gov = self._load_governance_service()
            for bid in report.governance_orphans:
                if bid in gov._brain_states:
                    del gov._brain_states[bid]
                    report.auto_deleted.append(bid)
                    logging.warning(
                        "BrainLifecycleManager: SSOT enforcement — deleted '%s' "
                        "from governance_state.json (no config file on disk)",
                        bid,
                    )
            self._save_governance_service(gov)
            # Reload gov_data for subsequent checks
            try:
                gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # ── transition_log coverage ──
        if gov_data:
            bs_ids = set(gov_data.get("brain_states", {}).keys())
            tl_ids = set(e.get("brain_id") for e in gov_data.get("transition_log", []))
            uncovered = bs_ids - tl_ids
            if uncovered:
                report.hardcoded_path_mismatches.append(
                    f"brain_states without transition_log: {sorted(uncovered)}"
                )

        # ── brain_id consistency (live.yaml path → config → brain_id) ──
        if self._live_yaml_path.exists():
            live = self._load_live_yaml()
            entries = live.get("brains", {}).get("registry_entries", [])
            for entry in entries:
                path_str = entry.get("path", "")
                cfg_path = Path(path_str)
                if not cfg_path.is_absolute():
                    cfg_path = (self._project_root / cfg_path).resolve()
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                        cfg_bid = cfg.get("brain_id", "")
                        expected_name = f"{cfg_bid}.json"
                        if cfg_path.name != expected_name:
                            report.hardcoded_path_mismatches.append(
                                f"config path mismatch: {path_str} → brain_id='{cfg_bid}' "
                                f"(expected filename: {expected_name})"
                            )
                    except (json.JSONDecodeError, OSError):
                        pass

        # ── magic uniqueness ──
        magic_map: dict[int, str] = {}
        for bid, cfg in disk_brains.items():
            magic = cfg.get("magic")
            if magic is not None:
                if magic in magic_map:
                    report.hardcoded_path_mismatches.append(
                        f"magic collision: {magic} used by '{magic_map[magic]}' and '{bid}'"
                    )
                else:
                    magic_map[magic] = bid

        # ── ensemble reference validity ──
        try:
            from core.runtime.signal_pipeline import validate_ensemble_references

            bs = gov_data.get("brain_states", {})
            ensemble_errors = validate_ensemble_references(bs)
            for err in ensemble_errors:
                report.hardcoded_path_mismatches.append(f"ensemble: {err}")
        except Exception:
            pass

        # ── contract_group validity ──
        known_cgs: set[str] = set()
        if self._live_yaml_path.exists():
            live = self._load_live_yaml()
            known_cgs = set(live.get("strategy_lines", {}).keys())
        if known_cgs:
            for bid, cfg in disk_brains.items():
                cg = cfg.get("contract_group", "")
                if cg and cg not in known_cgs:
                    report.hardcoded_path_mismatches.append(
                        f"{bid}: contract_group='{cg}' not in live.yaml strategy_lines: "
                        f"{sorted(known_cgs)}"
                    )

        # ── capability handshake: schema support ──
        try:
            from core.features.feature_service import FeatureService

            available = FeatureService.available_schemas()
            for bid, cfg in disk_brains.items():
                schema_id = cfg.get("feature_schema_id", "")
                if schema_id and schema_id not in available:
                    report.hardcoded_path_mismatches.append(
                        f"{bid}: schema '{schema_id}' NOT supported by FeatureService. "
                        f"Available: {sorted(available)}"
                    )
        except Exception:
            pass

        # ── artifact_hash integrity ──
        import hashlib

        for bid, cfg in disk_brains.items():
            expected_hash = cfg.get("artifact_hash", "")
            if not expected_hash:
                continue
            artifact_path = cfg.get("artifact_path", "")
            if not artifact_path:
                continue
            fp = Path(artifact_path)
            if not fp.is_absolute():
                fp = (self._project_root / fp).resolve()
            if not fp.exists():
                continue
            try:
                actual_hash = hashlib.sha256(fp.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    report.hardcoded_path_mismatches.append(
                        f"{bid}: artifact_hash mismatch — config={expected_hash[:16]}... "
                        f"file={actual_hash[:16]}..."
                    )
            except OSError:
                pass

        # ── validate DEFAULT_* paths ──
        from core.deployment.path_defaults import validate_defaults

        for name, exists in validate_defaults().items():
            if not exists:
                report.hardcoded_path_mismatches.append(f"path_defaults.{name}")

        # ── Layer 3: brain→live alignment (institutional validator) ──
        self.validate_brain_live_alignment(report)

        # ── assess validity ──
        # missing_yaml_entries is informational when auto-discovery is active
        # (BrainRegistryService.list_active_entries() discovers from disk)
        report.valid = not (
            report.missing_config_files
            or report.missing_artifacts
            or report.missing_norm_configs
            or report.hardcoded_path_mismatches
            or report.alignment_hard_fails
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

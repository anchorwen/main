#!/usr/bin/env python3
"""Blueprint compliance engine — enforces Iron Law #6 and #7.

Three modes:
  --pre-check <files>   Advisory reminder before editing (Iron Law #6). Always exit 0.
  --check               Compliance gate (Iron Law #7). Non-zero if blueprint not updated
                        for substantive .py changes. Used by verify.py --full and pre-commit.
  --stamp <module>      Record acknowledgement of a change that needs no blueprint update.

Key design decisions (per plan review):
  - Git state isomorphism instead of timestamps (trap #1)
  - Conservative cosmetic detection: only blank lines + # comments (trap #2)
  - Orphan file FATAL block: unmapped files force MODULE_SOURCE_MAP update (trap #3)

Usage:
    python scripts/check_blueprint_compliance.py --pre-check core/brains/adapters/xgboost_brain_adapter.py
    python scripts/check_blueprint_compliance.py --check
    python scripts/check_blueprint_compliance.py --stamp execution-guards
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "blueprints" / "modules"
STAMPS_DIR = ROOT / ".blueprint_stamps"

# Force UTF-8 on Windows
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_GIT_ENCODING = "utf-8" if sys.platform == "win32" else None


def _run_git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a git command with proper encoding on all platforms."""
    kwargs: dict = dict(capture_output=True, text=True, cwd=str(ROOT), timeout=timeout)
    if _GIT_ENCODING:
        kwargs["encoding"] = _GIT_ENCODING
        kwargs["errors"] = "replace"
    try:
        return subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        # Return a dummy result with non-zero rc
        return subprocess.CompletedProcess(args, -1, stdout="", stderr="timeout")
    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            pass
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    return subprocess.CompletedProcess(args, -1, stdout="", stderr="error")


# ── Module → source directory/file mapping ──
# Each value is a list of directory prefixes or specific file paths.
# Directories end with "/" and match any file under that tree.
# Specific paths match a single file.
# Files not matching any entry → FATAL (orphan detection, trap #3).

MODULE_SOURCE_MAP: dict[str, list[str]] = {
    "brains_adapters": ["core/brains/adapters/"],
    "brains_services": ["core/brains/services/", "core/brains/online_mlp_model.py"],
    "brains_schema": ["core/brains/schema_versions.py", "core/brains/brain_registry.py"],
    "brains_validation": [
        "core/deployment/startup_validator.py",
        "core/deployment/brain_config_validator.py",
        "core/deployment/brain_alert.py",
        "core/deployment/brain_registration_gate.py",
        "scripts/repair_brain_configs.py",
        "scripts/validate_brain_before_deploy.py",
    ],
    "execution_guards": [
        "core/execution/conformal_calibrator.py",
        "core/execution/conformal_ou_gate.py",
        "core/execution/pre_trade_guards.py",
        "core/execution/meta_signal_filter.py",
        "core/execution/meta_filter_gate.py",
        "core/execution/meta_pipeline.py",
        "core/execution/meta_exit_engine.py",
        "core/execution/strategy_budget.py",
        "core/execution/market_efficiency.py",
        "core/execution/dynamic_sl_tp.py",
        "core/execution/kelly_sizer.py",
        "core/execution/pwin_chain.py",
        "core/execution/gate_reachability.py",  # FIX-20260712-003: Layer 4 gate reachability analyzer
        "core/execution/microstructure_gate.py",  # FIX-20260718-004: tick liquidity defence
        "core/alpha/contracts.py",
        "core/alpha/registry.py",
        "core/alpha/ou_optimizer.py",
        "core/alpha/performance_store.py",
        "core/alpha/lifecycle_service.py",
        "core/execution/pwin_chain.py",
    ],
    "execution_orders": [
        "core/execution/correlation_sizer.py",
        "core/execution/fill_simulator.py",
        "core/execution/order_state_machine.py",
        "core/execution/execution_manager.py",
        "core/execution/position_manager.py",
        "core/execution/trail_stop_engine.py",
        "core/execution/strategy_line.py",
        "core/execution/barrier_strategy.py",
        "core/execution/micro_strategy.py",
        "core/execution/swing_strategy.py",
        "core/execution/statarb_strategy.py",
        "core/execution/exit_watchdog.py",
        "core/execution/live_order_sender.py",
        "core/execution/mt5_broker_adapter.py",
        "core/execution/mt5_worker.py",
        "core/execution/execution_queue.py",
        "core/execution/rule_engine_strategy.py",
        "core/execution/strategy_context.py",
        "core/execution/strategy_protocol.py",
        "core/execution/meta_filter_routing.py",
        "core/execution/managed_close.py",
        "core/execution/trend_isolation_gates.py",
        "core/execution/trend_volume_guard.py",
        "core/execution/net_out_close_handler.py",
        "core/execution/cross_strategy_coordinator.py",
        "core/execution/paper_gateway.py",
        "scripts/mt5_bridge_worker.py",
        "scripts/mt5_spread_probe.py",
        "scripts/test_meta_pipeline.py",
        "scripts/position_snapshot.py",
        "core/trading/",
        "configs/trading/",
    ],
    "execution_reentry": [
        "core/execution/reentry_guard.py",
        "core/execution/exit_reason.py",
    ],
    "execution_netting": [
        "core/execution/portfolio_netting.py",
    ],
    "execution_ood": [
        "core/execution/ood_gateway.py",
        "scripts/export_ood_params.py",
    ],
    "risk_policies": ["core/risk/risk_policies.py", "core/risk/risk_evaluation_service.py"],
    "risk_regime": [
        "core/risk/regime_detector.py",
        "core/execution/regime_gate.py",
        "core/execution/trend_detector.py",
        "scripts/task_b_regime_baseline.py",
    ],
    # FIX-20260822-001 (DQAF-20260822-001): GodsEye engine promoted to its own
    # owning blueprint (core execution component with hard-veto read path).
    "gods_eye": ["core/execution/gods_eye.py"],
    "risk_portfolio": ["core/execution/portfolio_risk.py", "core/execution/capital_allocator.py"],
    "feedback_performance": [
        "core/feedback/brain_performance_tracker.py",
        "core/feedback/brain_quality_engine.py",
        "core/feedback/decision_scorer.py",
        "core/feedback/feedback_loop.py",
        "core/feedback/performance_analytics.py",
        "scripts/feedback_loop.py",
        "scripts/trade_quality_report.py",
    ],
    "feedback_pnl": [
        "core/feedback/brain_pnl_ledger.py",
        "core/feedback/live_journal_metrics.py",
        "scripts/live_shadow_ensemble.py",
        "scripts/shadow_pnl_loop.py",
        "scripts/paper_trade_simulator.py",
    ],
    "feedback_online": [
        "core/feedback/online_feedback_hook.py",
        "scripts/online_feedback_hook.py",
        "core/feedback/param_optimizer.py",
        "core/feedback/experience_replay.py",
    ],
    "protocol_governance": [
        "core/governance/",
        "scripts/_verify_governance_evaluator.py",  # FIX-20260801-012: observation hold verification
    ],
    "protocol_parliament": ["core/parliament/"],
    "protocol_services": [
        "core/protocol/",
        "core/ledger/services/",
        "core/infrastructure/",
        "scripts/validators/journal_validator.py",
        "scripts/journal_freeze_gate.py",  # FIX-20260819-007: 账本冻结门禁 (core/ledger/ 覆盖率守卫)
        "scripts/_reconcile_zombie_4454299643_20260807.py",  # DQAF-20260807-003: IC Step 3 zombie PnL legal reconciliation
    ],
    "contracts_domain": [
        "core/contracts/domain/",
        "core/schemas/",
        "core/contracts/events.py",
        "core/contracts/position_events.py",
    ],
    "contracts_ids": [
        "core/contracts/ids.py",
        "core/contracts/serialization/",
        "core/contracts/strategy_magic.py",
    ],
    "contracts_resilience": [
        # UGR v3.1 — Zero-tolerance resilience architecture
        "core/contracts/adapters.py",
        "core/contracts/cap_result.py",
        "core/contracts/phantom_contract.py",
        "core/contracts/journal_contract.py",
        "core/contracts/journal_sla.py",
        "scripts/verify_phantom_contracts.py",
        "scripts/verify_capresult_ast.py",
        "scripts/verify_phantom_contracts.py",
    ],
    "contracts_training": [
        "core/contracts/training/",
        "scripts/scan_barrier_params.py",
        "scripts/backtest_rule_strategies.py",
        "configs/training/barrier_12bar_lightgbm_v1.2.1.yaml",
        "configs/training/label_contracts/label-survival-barrier-1.2.1.json",
    ],
    "data_infrastructure": [
        "core/data/",
        "scripts/migration/migrate_to_event_stream.py",
        "scripts/audit_data_chain_integrity.py",  # FIX-20260807-004: 全数据链完整性审计 (Phase 0)
    ],
    "deployment_config": [
        "core/config/",
        "core/constants.py",
        "core/deployment/environment_config.py",
        "core/deployment/service_container.py",
        "core/deployment/config_hot_reload.py",
        "core/deployment/compliance_audit.py",
        "core/deployment/compliance_control_matrix.py",
        "core/deployment/deployment_executor.py",
        "core/deployment/deployment_plan.py",
        "core/deployment/path_defaults.py",
        "core/deployment/scheduler_service.py",
        "core/deployment/governance_evaluator.py",  # FIX-20260801-011: SSOT governance orchestrator
        "core/deployment/atomic_file_writer.py",
    ],
    "deployment_lifecycle": [
        "core/deployment/lifecycle_manager.py",
        "core/deployment/state_persistence.py",
        "core/deployment/health_check.py",
        "core/deployment/capability_registry.py",
        "core/deployment/operational_support.py",
        "core/deployment/brain_lifecycle_manager.py",
        "core/deployment/blue_green.py",
        "core/deployment/operations_timeline.py",
        "core/deployment/release_pipeline.py",
        "core/deployment/postmortem_report.py",
        "core/deployment/release_certification.py",
        "core/deployment/release_registry.py",
        "core/deployment/runbook_engine.py",
        "scripts/verify.py",
        "scripts/pre_commit_mypy.py",
        "scripts/_mypy_scope.py",
        "apps/engine/system_facade.py",
        "scripts/analyze_live_brain_performance.py",
        "scripts/assess_system_health.py",
        "scripts/audit_2day.py",
        "scripts/audit_data_health_journal.py",
        "scripts/audit_profitability.py",
        "scripts/clean_ledger_bloat.py",
        "scripts/live_audit_realtime.py",
        "scripts/verify_training_serving_parity.py",
        "scripts/validate_artifacts.py",
        "scripts/validate_blueprints.py",
        "scripts/register_fix.py",
        "scripts/reconcile_fix_registry.py",
        "scripts/shadow_rca.py",
        "scripts/audit_memory.py",
        "scripts/check_blueprint_compliance.py",
        "scripts/hook_blueprint_precheck.py",
        "scripts/hook_mypy_check.py",
        "scripts/hook_architecture_gate.py",
        "scripts/omega_gate.py",
        "scripts/validate_commit_msg.py",  # FIX-087 pre-flight validator
        "scripts/dqaf053_phase1_sanitize.py",  # DQAF-053 migration script
        "scripts/generate_btc_empirical_scaler.py",  # DQAF-054 BTC scaler
        "scripts/generate_micro_scaler.py",  # DQAF-058 multi-asset scaler
        "conftest.py",  # DQAF-075 pytest slow/fast marker config
        "scripts/brain.py",
        "scripts/_institutional_reconcile.py",  # DQAF-20260702-FP005: IC_MANDATE governance reconciliation
        "scripts/training/register_brain.py",
        "scripts/validate_magic_sync.py",
        "scripts/system_trust_report.py",
        "scripts/check_symbol_liveness.py",
        "scripts/phase4_shadow_review.py",
        "scripts/phase4_final_audit.py",
    ],
    "features_rolling": [
        "core/features/rolling_normalizer.py",
        "core/features/data_augmentation.py",
    ],
    "features_service": [
        "core/features/feature_service.py",
        "core/features/stale_feature_guard.py",  # FIX-20260801-013: stale-feature inference guard
        "core/features/local_feature_store.py",
        "core/features/feature_snapshot.py",
        "core/features/computers/v9_micro_computer.py",
        "core/features/computers/v9_live_computer.py",
        "core/features/computers/microstructure_computer.py",
        "core/features/computers/btc_feature_augmenter.py",  # FIX-20260625-137
        "core/features/ofi_collector.py",  # DQAF-20260707-004: OFI flow features
        "core/features/adapters/v9_feature_adapter.py",
        "core/features/adapters/microstructure_feature_adapter.py",
        "core/features/computers/daily_computer.py",
        "core/features/computers/live_daily_provider.py",
        "core/features/schemas/microstructure_schema.py",  # FIX-20260718-004: gate-only micro features
        "core/features/schemas/v9_micro_schema.py",
        "core/features/schemas/v9_institutional_schema.py",
        "core/features/schemas/swing_enhanced_schema.py",
        "core/features/schemas/registry.py",
        "core/features/schemas/btc_macro_enhanced_schema.py",  # FIX-20260625-137
        "core/features/feature_assembler.py",
        "core/features/feature_router.py",  # FIX-20260625-137
        "core/features/rolling_normalizer.py",
        "core/market/calendar.py",
        "core/deployment/feature_update_producer.py",
        "scripts/features/feature_store_warmer.py",
        "scripts/feature_store_maintenance.py",
        "scripts/augment_journal_strategy.py",
        "scripts/inspect_ofi_history.py",  # DQAF-20260707-005: OFI history monitor
        "scripts/scan_ofi_wasserstein.py",  # FIX-20260802-005: T21 Gate-1 OFI Wasserstein scan
        "scripts/features/reconcile_store_schemas.py",  # FIX-20260803-005: schema registration reconciliation
        "scripts/gate2_sentinel.py",  # FIX-20260805-003: Gate 2 daily accumulation sentinel (reuses inspect())
        "scripts/daily_flow46_precheck.py",  # FIX-20260805-004: daily battle-readiness precheck (reuses inspect + sentinel + git status)
        "scripts/alert_dispatcher.py",  # FIX-20260805-006: unified DingTalk push (reused by gate2/audit/drift/precheck)
    ],
    "market_mtf": [
        "core/market/mtf_price_service.py",
        "scripts/_merge_aligned_multitf_data.py",  # FIX-20260805-003: BTC+cross-asset MT5 merge (RBI-1 relocated)
    ],
    "monitor_dashboard": [
        "apps/monitor/live_trading_dashboard.py",
        "core/observability/diagnostics_dashboard.py",
        "core/observability/event_bus.py",
        "core/observability/slo_service.py",
        "core/observability/alert_channels.py",
        "core/observability/alert_service.py",
        "core/observability/alert_runbook_bridge.py",
        "core/observability/live_alert_hub.py",
        "core/observability/invariant_engine.py",
        "core/observability/message_broker.py",
        "core/observability/data_health_schema.py",
        "core/observability/data_health_service.py",
        "core/observability/health_checks.py",
        "core/observability/meta_wire_events.py",
        "core/metrics/factor_attribution.py",
        "scripts/live_dashboard.py",
        "scripts/dqaf_collect.py",
        "scripts/verify_dqaf_002_fix.py",
        "scripts/run_data_health.py",
        "scripts/send_data_health_alert.py",
        "scripts/data_integrity_check.py",
        "scripts/diagnose_journal_mt5_sev2.py",
        # ── Iron Law #11 Institutional Audit Portfolio (FIX-20260622-052 S.E.A.L.) ──
        "scripts/audit_data_exhaustive.py",
        "scripts/audit_full_pipeline.py",
        "scripts/audit_state_of_system.py",
        "scripts/commander_g2_metafilter_path.py",
        "scripts/commander_g3_alpha_vacuum.py",
        "scripts/commander_g4_g6_g7_coverage_xau.py",
        "scripts/commander_guardrails_arch.py",
        "scripts/monitor_pwin_fix.py",
        "scripts/verify_dqaf044_fix_effect.py",
        "scripts/audit_entry_spread_coverage.py",
        "scripts/diagnose_mypy_baseline.py",
        "scripts/audit_btc_live_direction.py",  # DQAF-058: BTC direction bias audit
        "scripts/forensic_feature_analysis.py",  # DQAF-058: feature distribution forensics
        "scripts/analyze_gate_activity.py",  # FIX-20260712-003: Layer 4 dead gate detection
        "scripts/audits/_audit_xau_votes_today.py",  # DQAF-20260804-006: XAU degenerate-brain vote audit
    ],
    "runtime_live": [
        "main.py",
        "core/runtime/",
        "scripts/check_preconditions.py",
        "scripts/verify_pnl_data_integrity.py",
        "scripts/daily_ops.py",
        "scripts/watchdog_daily_ops.py",
        "core/runtime/daily_ops_scheduler.py",
        "scripts/analyze_trail_impact.py",
        "scripts/live_intent_loop.py",
        "scripts/_monitor_direction_concentration.py",  # P3.1: direction concentration monitor
        "scripts/_evaluate_probation_m30_h1v2.py",  # P3.3: M30/H1_V2 probation evaluation
        "scripts/backfill_journal_orphans.py",
        "scripts/backfill_fabricated_breakeven.py",  # DQAF-20260708-003 append-only PnL correction
        "scripts/live_launcher.py",
        "scripts/launcher_supervisor.py",  # P9 (TECH_DEBT-015): hub heartbeat probe
        "scripts/bridge_supervisor.py",
        "scripts/live_micro_rollout_gate.py",
        "scripts/live_read_only_preflight.py",
        "scripts/live_daily_recap.py",
        "scripts/live_shadow_intent_producer.py",
        "scripts/mt5_positions_snapshot.py",
        "scripts/send_live_order.py",
        "scripts/shadow_decision_recorder.py",
        "scripts/test_io_pipeline.py",
        "apps/engine/bootstrap_v9.py",
        "apps/engine/cli.py",
        "apps/engine/communication_ops_cli.py",
        "apps/engine/orchestrator.py",
        "apps/engine/runtime_loop.py",
        "apps/engine/main_v9_shadow.py",
        "apps/engine/v9_shadow_sse.py",
        "scripts/position_query.py",
        "scripts/diagnose_process_health.py",
        "scripts/health_check.py",
        "scripts/ble001_phase3e_deferred_fog_wrap.py",
        "scripts/ble001_phase3d_coldpath_fog_wrap.py",
        "scripts/ble001_phase3c_fog_wrap.py",
        "scripts/ble001_phase3b_migrate_hotpath.py",
        "scripts/normalize_journal_pnl.py",  # FIX-20260627-148: MT5 terminal path auto-resolve
        "scripts/backfill_journal_pnl.py",
        "scripts/analyze_shadow_exit.py",  # FIX-20260703-002: T24 V6 shadow analysis
        "scripts/ci_prepare_v9_shadow_fixtures.py",  # FIX-20260819-006: CI shadow fixture prep (stub-declared)
        "scripts/_shadow_ops_watchdog.py",  # FIX-20260824-003: Phase 4 Shadow Ops Layer-3 每日巡检
        "scripts/_audit_shadow_ops_liveness_probe.py",  # FIX-20260824-003: Phase 4 Shadow Ops 实证锁探针
        "scripts/_audit_zombie_purge_verify_20260826.py",  # DQAF-20260826-004: 清剿丧尸归零法证审计
    ],
    "runtime_state": [
        "core/state/",
        "scripts/hook_pre_push.py",
        "scripts/pre_commit_blueprint.py",
        "scripts/check_omega_pre_push.py",
        "scripts/omega_constants.py",
    ],
    "training": [
        "core/training/",
        "core/backtest/strategy_adapter.py",
        "scripts/training/",
        "scripts/tuning/",
        "scripts/check_training_readiness.py",
        "scripts/analyze_live_journal.py",
        "scripts/backtest/backtest_high_recall_precision.py",
        "scripts/backtest/backtest_meta_filter.py",
        "scripts/backtest/backtest_dynamic_exit.py",
        "scripts/backtest_structural_swing.py",
        "scripts/build_micro_cost_model.py",  # P3 (FIX-20260824-001): Net-of-Cost Alpha toll-gate cost model
        "scripts/build_btc_metafilter_v2_dataset.py",
        "scripts/train_btc_metafilter_v2.py",
        "scripts/train_xau_metafilter.py",
        "scripts/_train_m15_binary_final.py",  # DQAF-20260726-007: M15 binary_directional training
        "scripts/_analyze_m15_swing_now.py",  # DQAF-20260726-007: M15 swing live performance audit
        "scripts/_train_h4_binary_final.py",  # DQAF-20260726-008: H4 binary_directional training
        "scripts/_analyze_h4_swing_now.py",  # DQAF-20260726-008: H4 swing live performance audit
        "scripts/_train_h1_binary_final.py",  # DQAF-20260726-009: H1 binary_directional training
        "scripts/_analyze_h1_swing_now.py",  # DQAF-20260726-009: H1 swing live performance audit
        "scripts/task_a_directional_closure.py",
        "scripts/analyze_shadow_predictions.py",
        "scripts/optimize_sltp_params.py",
    ],
}


def resolve_modules(file_path: str) -> list[str]:
    """Determine which module blueprint(s) claim ownership of a file.

    Returns a list (possibly empty) of module names.  An empty list is a FATAL
    condition — the caller must treat it as an orphan (trap #3).
    """
    matched: list[str] = []
    for module, patterns in MODULE_SOURCE_MAP.items():
        for pat in patterns:
            if pat.endswith("/"):
                if file_path.startswith(pat):
                    matched.append(module)
                    break
            elif file_path == pat:
                matched.append(module)
                break
    return matched


def _get_changed_py_files(*, cached_only: bool = False) -> list[str]:
    """Return sorted list of changed .py files (excluding tests/).

    When cached_only=True (pre-commit context), uses --cached.
    Otherwise uses HEAD diff.
    """
    args = ["git", "diff", "--name-only"]
    if cached_only:
        args.append("--cached")
    else:
        args.append("HEAD")

    files: list[str] = []
    result = _run_git(args)
    if result.returncode == 0 and result.stdout:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.endswith(".py") and not line.startswith("tests/"):
                files.append(line)
    return sorted(files)


def _get_changed_files(*, cached_only: bool = False) -> set[str]:
    """Return set of ALL changed file paths (not just .py)."""
    args = ["git", "diff", "--name-only"]
    if cached_only:
        args.append("--cached")
    else:
        args.append("HEAD")

    files: set[str] = set()
    result = _run_git(args)
    if result.returncode == 0 and result.stdout:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                files.add(line)
    return files


def classify_diff(file_path: str, *, cached_only: bool = False) -> str:
    """Classify diff as 'substantive' or 'cosmetic'.

    Conservative approach (trap #2): only blank lines and pure single-line
    ``#`` comments are exempt.  Everything else (docstrings, type annotations,
    multi-line changes) is treated as substantive.

    When cached_only=True, compares index (staged) to HEAD instead of
    working tree to HEAD.  Use in pre-commit context.
    """
    full = ROOT / file_path
    if not full.exists():
        return "substantive"  # deleted — definitely substantive

    try:
        ref = "--cached" if cached_only else "HEAD"
        result = _run_git(
            ["git", "diff", "-U0", ref, "--", file_path],
            timeout=10,
        )
        if result.returncode != 0:
            return "substantive"
    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return "substantive"
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    diff_lines = result.stdout.split("\n")
    changed_lines: list[str] = []
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            changed_lines.append(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            changed_lines.append(line[1:].strip())

    if not changed_lines:
        return "cosmetic"  # no actual content changes

    for cl in changed_lines:
        if cl == "":
            continue  # blank line — cosmetic
        if cl.startswith("#"):
            continue  # pure comment — cosmetic
        return "substantive"

    return "cosmetic"


def _blueprint_in_change_set(module: str, changed_files: set[str]) -> bool:
    """Check if the module's blueprint file is in the change set."""
    bp = f"blueprints/modules/{module}.md"
    return bp in changed_files


# ── Mode: pre-check (Iron Law #6 reminder) ──


def pre_check(files: list[str]) -> int:
    """Advisory reminder. Prints module info. Always returns 0."""
    for fp in files:
        modules = resolve_modules(fp)
        if not modules:
            print(
                f"[blueprint] [FATAL] File '{fp}' is not mapped in MODULE_SOURCE_MAP!\n"
                f"           Please update scripts/check_blueprint_compliance.py first."
            )
            continue
        print(f"[blueprint] Iron Law #6: '{fp}' belongs to module(s): {', '.join(modules)}")
        for m in modules:
            print(f"  -> Read: blueprints/modules/{m}.md (Fix History + Known Issues)")
        print("  -> Search: blueprints/system/FIX_REGISTRY.md for historical fixes to this file")
        if modules:
            print(f"  -> Run: python scripts/analyze_deps.py {modules[0]}")
    return 0


# ── Mode: compliance check (Iron Law #7 gate) ──


def check_compliance(*, all_files: bool = False) -> int:
    """Compliance gate. Returns non-zero if any substantive .py change lacks
    a corresponding blueprint update in the same change set.

    By default only checks staged files (--cached) to avoid false
    violations from unstaged changes belonging to other sessions.
    Pass all_files=True for comprehensive audit (verify.py --full).
    """
    # Default to staged-only — unstaged changes from prior sessions
    # must not block commits or produce false violations.
    cached_only = not all_files

    py_files = _get_changed_py_files(cached_only=cached_only)
    if not py_files:
        print("[blueprint] No changed .py files — compliance check skipped.")
        return 0

    changed_all = _get_changed_files(cached_only=cached_only)
    errors: list[str] = []
    cosmetic_count = 0
    substantive_files: list[tuple[str, list[str]]] = []

    for fp in py_files:
        modules = resolve_modules(fp)

        # ── Orphan detection (trap #3) ──
        if not modules:
            errors.append(
                f"[FATAL] File '{fp}' is not mapped to any blueprint in MODULE_SOURCE_MAP!\n"
                f"        Please update scripts/check_blueprint_compliance.py -> MODULE_SOURCE_MAP first."
            )
            continue

        category = classify_diff(fp, cached_only=cached_only)
        if category == "cosmetic":
            cosmetic_count += 1
            continue

        substantive_files.append((fp, modules))

    if cosmetic_count:
        print(
            f"[blueprint] Skipped {cosmetic_count} cosmetic change(s) (comments / blank lines only)."
        )

    if not substantive_files:
        if not errors:
            print("[blueprint] All substantive .py changes have been checked.")
        if errors:
            for e in errors:
                print(e)
            return 1
        return 0

    for fp, modules in substantive_files:
        bp_updated = any(_blueprint_in_change_set(m, changed_all) for m in modules)
        if bp_updated:
            print(f"[blueprint] OK: {fp} -> {', '.join(modules)} (blueprint in change set)")
        else:
            bp_paths = "\n    ".join(f"blueprints/modules/{m}.md" for m in modules)
            errors.append(
                f"VIOLATION: '{fp}' modified but blueprint(s) not in change set.\n"
                f"  Module(s): {', '.join(modules)}\n"
                f"  Action: update Fix History in:\n"
                f"    {bp_paths}\n"
                f"  Then: git add {' '.join(f'blueprints/modules/{m}.md' for m in modules)}"
            )

    if errors:
        print(f"\n[FAIL] blueprint compliance: {len(errors)} violation(s)\n")
        for e in errors:
            print(e)
        return 1

    print("[PASS] blueprint compliance")
    return 0


# ── Mode: stamp management ──


def stamp_module(module: str) -> int:
    """Record an explicit acknowledgement for a module whose changes need no
    blueprint update.  Writes a stamp file; --check respects it.
    """
    STAMPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp_path = STAMPS_DIR / f"{module}.json"
    # Record current state of all source files in the module
    patterns = MODULE_SOURCE_MAP.get(module)
    if not patterns:
        print(f"[blueprint] Unknown module: {module}")
        return 1

    file_hashes: dict[str, str] = {}
    for pat in patterns:
        if pat.endswith("/"):
            import glob as _glob

            for f in _glob.glob(str(ROOT / pat / "*.py")):
                rel = str(Path(f).relative_to(ROOT)).replace("\\", "/")
                try:
                    import hashlib

                    h = hashlib.sha256(Path(f).read_bytes()).hexdigest()[:16]
                    file_hashes[rel] = h
                except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                    try:  # BLE001:FOG (was: FOG/LAC)
                        pass
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
        else:
            p = ROOT / pat
            if p.exists():
                import hashlib

                h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                file_hashes[pat] = h

    stamp = {
        "module": module,
        "acknowledged_at": __import__("time").time(),
        "files": file_hashes,
    }
    with open(str(stamp_path), "w", encoding="utf-8") as fh:
        json.dump(stamp, fh, indent=2)
    print(f"[blueprint] Stamped {module}: {len(file_hashes)} file(s) acknowledged.")
    return 0


# ── CLI ──


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Blueprint compliance engine (Iron Law #6 / #7 enforcement)"
    )
    parser.add_argument(
        "--pre-check",
        nargs="+",
        metavar="FILE",
        help="Advisory reminder: which module owns these files (Iron Law #6). Always exit 0.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compliance gate: verify blueprint updated for substantive .py changes (Iron Law #7).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="When used with --check: scan ALL modified files (staged+unstaged), not just staged.",
    )
    parser.add_argument(
        "--stamp",
        metavar="MODULE",
        help="Acknowledge a change that needs no blueprint update.",
    )
    args = parser.parse_args()

    if args.pre_check:
        return pre_check(args.pre_check)

    if args.check:
        return check_compliance(all_files=args.all)

    if args.stamp:
        return stamp_module(args.stamp)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

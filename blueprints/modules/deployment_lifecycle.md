# Deployment / Lifecycle

## Purpose
System lifecycle management: graceful startup/shutdown, state persistence, health checks, scheduler service, and operations maturity tracking.

## Key Files
| File | Role |
|------|------|
| `core/deployment/lifecycle_manager.py` | `LifecycleManager` — startup/shutdown, state persistence, health checks |
| `core/deployment/state_persistence.py` | `StatePersistence` — JSON state save/load |
| `core/deployment/health_check.py` | `HealthCheckService` — liveness + readiness probes |
| `core/deployment/scheduler_service.py` | `SchedulerService` — scheduled task runner |
| `core/deployment/scheduled_task_registry.py` | `ScheduledTaskRegistry` |
| `core/deployment/operational_support.py` | Operational support utilities |
| `core/deployment/operations_timeline.py` | `OperationsTimelineService` |
| `core/deployment/ops_maturity.py` | `OpsMaturityService` |
| `core/deployment/postmortem_report.py` | `PostmortemReportService` |
| `core/deployment/feature_update_producer.py` | Feature update event producer |
| `core/deployment/governance_summary.py` | Governance summary generation |
| `scripts/brain.py` | Unified brain lifecycle CLI (register/list/validate/retire) |
| `scripts/validate_artifacts.py` | OU artifact parameter contract validator — bounds + cross-file drift |
| `core/constants.py` | `DAILY_OPS_INTERVAL_SECONDS`, `MODE_STALE_SECONDS` |

## Data Flow
```
startup → LifecycleManager.initialize()
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  State    Health    Scheduler
  Persist  Checks    Service
              ↓
    shutdown → LifecycleManager.shutdown()
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts | domain_keys, SystemModeState | State management |
| state | SystemModeStore, ControlSnapshotService | Runtime state |
| observability | metric_names | Lifecycle metrics |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | LifecycleManager | Live trading lifecycle |
| apps/engine/ | LifecycleManager, StatePersistence | CLI lifecycle |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260610-007 | 2026-06-10 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/send_data_health_alert.py`→`monitor_dashboard`, `scripts/build_btc_metafilter_v2_dataset.py`+`scripts/train_btc_metafilter_v2.py`→`training`. FIX-007 DingTalk+MLOps. | RC-09 |
| FIX-20260610-004 | 2026-06-10 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/analyze_trail_impact.py` → `runtime_live`. DQAF-20260610-001. | RC-09 |
| FIX-20260608-007 | 2026-06-08 | agent | — | MODULE_SOURCE_MAP: add `core/execution/pwin_chain.py` → execution_guards (S3 refactoring new file). | RC-09 |
| FIX-20260608-004 | 2026-06-08 | cursor-agent | — | MODULE_SOURCE_MAP: add `core/deployment/feature_update_producer.py` → features_service module. Required for FIX-004 multi-TF Feature Store blueprint compliance. | RC-09 |
| FIX-20260607-146 | 2026-06-07 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/tuning/`, `scripts/analyze_live_journal.py` to training module. V4+LGB_V1 brain configs archived. V7+V8 brain configs registered. | RC-09 |
| FIX-20260606-136 | 2026-06-06 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/dqaf_collect.py` to `monitor_dashboard` module (DQAF v1.0 ECoL evidence collection script). Also updated `monitor_dashboard.md` Fix History. | RC-09 |
| FIX-20260531-003 | 2026-05-31 | cursor-agent | — | MODULE_SOURCE_MAP: add `main.py` → runtime_live, `core/infrastructure/` → protocol_services (both were unmapped → orphan compliance FATAL). Also add `regime_gate.py` fix + `distributed_lock.py` tmp cleanup to respective module blueprints. | RC-09 |
| FIX-20260529-040 | 2026-05-29 | cursor-agent | — | MODULE_SOURCE_MAP: add core/observability/alert_channels.py, alert_service.py, alert_runbook_bridge.py, live_alert_hub.py to monitor_dashboard module (were unmapped → orphan compliance error). | RC-09 |
| FIX-20260529-035 | 2026-05-29 | cursor-agent | — | P0.2+P1: (1) Killed silent assassin — `except Exception: pass` in scheduler_service governance pipeline replaced with Fail-Loud (`logger.exception` + `emit_brain_alert("pnl_pipeline_failure")`). (2) SSOT enforcement — `compute_performance_from_ledger()` replaced with `BrainPnLStore.get_all_metrics()` in scheduler_service. (3) P0.1 State Injection — performance_metrics written to `governance_service.set_performance_metrics()` for all brains with settled trades. | RC-06 |
| FIX-20260529-034 | 2026-05-29 | cursor-agent | — | SSOT status reconciliation: `verify_startup_integrity()` now reconciles governance "retired" → "candidate" when active config exists on disk (config is law). `GovernanceService.register_brain()` + auto-registration path now populate transition_log. Fixes retired-reversion loop. | RC-09, RC-11 |
| FIX-20260529-031b | 2026-05-29 | cursor-agent | — | MODULE_SOURCE_MAP: add `core/execution/paper_gateway.py` to `execution_orders` module (was unmapped → orphan compliance error). | RC-09 |
| FIX-20260528-024b | 2026-05-28 | cursor-agent | — | verify.py `run_pytest()` 4-iteration fix: v1 `capture_output=True` → pipe deadlock. v2 `tempfile` → no deadlock but swallowed output 130s. v3 inherit stdout + catch `KeyboardInterrupt`. v4 (final): fixed stdout UTF-8 rewrap losing line buffering — `io.TextIOWrapper` defaults to full buffering, causing `print()` output to appear AFTER pytest dots. Added `sys.stdout.reconfigure(line_buffering=True)`. | RC-06 |
| FIX-20260528-021 | 2026-05-28 | cursor-agent | — | MODULE_SOURCE_MAP: add `core/features/schemas/swing_enhanced_schema.py` to `features_service` module (new swing enhanced 35-dim schema file). | RC-09 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | MODULE_SOURCE_MAP: add `core/features/schemas/registry.py` to `features_service` module (new SSOT file). | RC-09 |
| FIX-20260528-023 | 2026-05-28 | cursor-agent | — | `_REQUIRED_BRAIN_FIELDS` expanded from 4 to 6: added `contract_group` and `training_contract`. These fields were missing from Swing_V9 brain configs, causing `brain_hard_muted_contract` at startup. `brain.py register` now rejects any config lacking these fields before registration. | RC-09 |
| FIX-20260525-013 | 2026-05-25 | cursor-agent | — | Artifact parameter contract validator: validate_artifacts.py with OU bounds + cross-file drift detection. Integrated into verify.py --quick/--full. Prevents parameter regression like z_entry 1.3→3.9. | RC-07 |
| FIX-20260527-003 | 2026-05-27 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/online_feedback_hook.py` to `feedback_online` module (was unmapped → fatal compliance error). | RC-04 |
| FIX-20260525-010 | 2026-05-25 | cursor-agent | — | MODULE_SOURCE_MAP: add trail_stop_engine.py to execution_orders module. | RC-04 |
| FIX-20260525-009 | 2026-05-25 | cursor-agent | — | MODULE_SOURCE_MAP: add mt5_worker.py to execution_orders, live_daily_provider.py to features_service. | RC-04 |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M12: BrainLifecycleManager auto-repair now always registers brains as "candidate" — removed "shadow" registration which creates governance deadlock (shadow wasn't in VALID_TRANSITIONS before H3 fix). | RC-09 |
| FIX-20260524-033 | 2026-05-24 | cursor-agent | — | Batch mypy type safety: postmortem_report.py (3→0 — dict[str, Any] annot), release_pipeline.py (1→0 — dict[str, Any] annot). MODULE_SOURCE_MAP: add release_pipeline.py to deployment_lifecycle. | type-confusion |
| FIX-20260524-011 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add scripts/feedback_loop.py to feedback_performance module for Batch C variable shadowing fix | RC-02 |
| FIX-20260524-013 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add scripts/backtest/backtest_dynamic_exit.py to training module for Batch D backtest mypy cleanup | RC-02 |
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP expansion: 8 entries across 4 modules for Batch G non-test mypy cleanup (v9_shadow_sse, _diag_cycle_stall, live_daily_recap, feature_store_maintenance, journal_validator, trade_quality_report) | RC-02 |
| FIX-20260523-004 | 2026-05-23 | cursor-agent | — | MODULE_SOURCE_MAP expansion: add market_mtf module mapping for core/market/mtf_price_service.py | RC-06 |
| FIX-20260524-006 | 2026-05-24 | cursor-agent | — | SSOT Dictator Governance Engine: verify_startup_integrity(auto_repair=True) now enforces "physical files are law" — governance entries WITHOUT matching disk configs are physically deleted (key removed, not frozen/retired). 20 state contamination entries cleaned (2 zombies, 16 frozen, 1 orphan Online_MLP_V1, 1 LightGBM_V1_Institutional). governance_state.json: 23→3. | RC-11 (state-contamination) |
| FIX-20260524-001 | 2026-05-24 | cursor-agent | — | Brain registration single source of truth: auto-governance registration in verify_startup_integrity(auto_repair=True), scripts/brain.py unified CLI for register/list/validate/retire, hardcoded DEFAULT_BRAIN_REGISTRATIONS replaced with auto-discovery | RC-09 |
| FIX-20260523-008 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP expansion: add core/execution/conformal_calibrator.py to execution_guards for Track 3d Conformal OU Gate compliance coverage | RC-09 |
| FIX-20260523-007 | 2026-05-23 | cursor-agent | — | MODULE_SOURCE_MAP expansion: add scripts/daily_ops.py to runtime_live, core/feedback/experience_replay.py to feedback_online for mini-batch online learning compliance coverage | RC-06 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues | type-confusion |
| FIX-20260521-008 | 2026-05-21 | cursor-agent | — | MODULE_SOURCE_MAP expansion: add mt5_spread_probe.py, meta_filter_gate.py, test_meta_pipeline.py, backtest_high_recall_precision.py, backtest_meta_filter.py to module mappings | RC-06 |
| FIX-20260520-027 | 2026-05-20 | cursor-agent | — | Institutional brain→live alignment validator (Layer 3): validate_brain_live_alignment() added to BrainLifecycleManager with hard fail (SL tightening, horizon truncation) + warnings (horizon expansion, TP deviation) + ensemble cross-brain consistency. Integrated into verify_startup_integrity() + live_intent_loop JSON surface. | RC-09 |
| FIX-20260519-005 | 2026-05-19 | cursor-agent | — | verify.py subprocess encoding fix: 3处check_blueprint_compliance/validate_blueprints子进程调用添加encoding=utf-8 + errors=replace + None-safe stdout(stderr) | RC-06 |
| FIX-20260517-011 | 2026-05-17 | cursor-agent | — | Brain ecosystem cleanup: removed 6 retired brains from live.yaml, 12 zombie governance entries, 3 stale configs, fixed crt_sur_chlg_g2026.json features field, registered Meta_Stage1_Huber_V1 orphan config. | RC-11 |
| FIX-20260518-031 | 2026-05-18 | cursor-agent | — | Retired brain cleanup: removed 12 retired brain_states from governance_state.json, moved 6 retired brain configs to archive_deprecated/, emptied ENSEMBLE_GROUPS (all 3 referenced brains were retired). | RC-11 |
| FIX-20260517-013 | 2026-05-17 | cursor-agent | — | MODULE_SOURCE_MAP: added scripts/shadow_pnl_loop.py to feedback_pnl mapping for blueprint compliance coverage. | config-drift |
| FIX-20260518-042 | 2026-05-18 | cursor-agent | — | MODULE_SOURCE_MAP: added scripts/validate_brain_before_deploy.py to brains_validation mapping for blueprint compliance coverage of new deployment quality gate script. | config-drift |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor→Executor pipeline: scheduler_service governance_eval replaced direct apply_promotion_decisions() call with BrainPromotionEvaluator.evaluate_all() → GovernanceRuleEngine.execute_transitions() chain. Eliminates dual-write conflict between rule engine and promotion evaluator. | contract-violation |
| FIX-20260516-009 | 2026-05-16 | cursor-agent | — | Governance state integrity: fixed run_promotion.py dual-write bug (apply_decisions + ensure_governance_registration now append to transition_log). Removed 12 zombie brain_states, reconciled 6 inconsistencies, registered 5 new brains, deleted 4 stale configs. governance_state.json added to git tracking. | RC-06, RC-10 |
| FIX-20260515-010 | 2026-05-15 | cursor-agent | — | Aggressive data cleanup: removed 2 frozen brain configs + 4 associated model files, 29 orphaned model files, 4 orphaned training NPZs, 2 .bak files, 4 dangling training contracts, 5 April decision directories, 10 frozen governance entries. Cleaned live.yaml disabled entries. | stale-data |
| FIX-20260515-009 | 2026-05-15 | cursor-agent | — | Auto-shadow mechanism: ShadowTracker integrated into governance eval (scheduler_service.py), train.py auto-register in live.yaml + governance_state.json, vote_weight=0.0 for shadow brains. | missing-feature |
| FIX-20260515-008 | 2026-05-15 | cursor-agent | — | Watchdog cleanup: deleted scripts/hourly_watchdog.py (May 5 experiment), data/watchdog.log. Updated ADR-006. Fixed verify.py deleted-file filter. | config-drift |
| FIX-20260515-007 | 2026-05-15 | cursor-agent | a4a1005 | New swing models (5 brain IDs) not registered in governance_state.json. Added all 5 with candidate status for PnL tracking and automated promotion eligibility. | config-drift |
| FIX-20260517-010 | 2026-05-17 | cursor-agent | — | MODULE_SOURCE_MAP: added core/execution/dynamic_sl_tp.py to execution_guards mapping (SL/TP formula fix). | config-drift |
| FIX-20260519-001 | 2026-05-19 | cursor-agent | — | Pre-commit deadlock fix: validate_blueprints.py `check_source_blueprint_freshness()` now only checks `--cached` in pre-commit context. Breaks the recursive stash paradox where unstaged FIX entries from prior sessions revert to HEAD during pre-commit stash, producing false STALE/ORPHAN violations that block commits. | RC-06 |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: verify.py blueprint compliance check in --quick mode. New feature. | process-violation |
| FIX-20260519-003 | 2026-05-19 | cursor-agent | — | MODULE_SOURCE_MAP: added correlation_sizer.py to execution_orders, kelly_sizer.py to execution_guards, startup_validator.py to brains_validation. | config-drift |
| FIX-20260519-004 | 2026-05-19 | cursor-agent | — | Defense-in-depth deadlock prevention: check_blueprint_compliance.py --check defaults to staged-only (--all for deep audit), classify_diff supports cached_only, validate_blueprints.py unified changed_all tracking removes dead code + second-pass git calls, verify.py --full uses --all. Breaks the unstaged-files-from-prior-sessions false-violation loop at all three check paths. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `LifecycleManager.initialize()` → `bool` | main.py, live_launcher | Stable |
| `LifecycleManager.shutdown()` → `None` | main.py, live_launcher | Stable |
| `StatePersistence.save(key, data)` / `.load(key)` → `dict | None` | All services | Stable |
| `HealthCheckService.liveness()` / `.readiness()` → `bool` | monitor | Stable |

## Verification
```bash
python -m pytest tests/ -k "lifecycle or health" -q
```

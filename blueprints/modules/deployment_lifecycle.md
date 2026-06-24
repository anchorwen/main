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
| FIX-20260624-119 | 2026-06-24 | cursor-agent | — | **MODULE_SOURCE_MAP: `core/observability/event_bus.py` added to monitor_dashboard**. New file registered as part of EventBus FOG over-narrowing fix. | RC-09 |
| FIX-20260624-117 | 2026-06-24 | cursor-agent | 018e58e5 | P1-1+P1-2 followup: omega_gate.py + validate_commit_msg.py import from omega_constants (Scene F exemption) | config-drift |
| FIX-20260624-115 | 2026-06-24 | cursor-agent | 14b4c6da | P0-3 followup: add check_omega_pre_push.py to MODULE_SOURCE_MAP under runtime_state | config-drift |
| FIX-20260624-113 | 2026-06-24 | cursor-agent | a2c77b03 | P0-2 followup: add pre_commit_blueprint.py to MODULE_SOURCE_MAP under runtime_state | config-drift |
| UGR-B01 | 2026-06-24 | cursor-agent | — | **MODULE_SOURCE_MAP: `core/contracts/phantom_contract.py` + `scripts/verify_phantom_contracts.py` → contracts_resilience**. New phantom contract module + verifier script registered. | RC-09 |
| UGR-A07 | 2026-06-24 | cursor-agent | — | **MODULE_SOURCE_MAP: `core/contracts/adapters.py` → contracts_resilience**. New adapter module registered. Also fixed pre-existing mypy return-statement error in _run_git(). | RC-09 |
| UGR-A06 | 2026-06-24 | cursor-agent | — | **MODULE_SOURCE_MAP: `core/observability/invariant_engine.py` → monitor_dashboard**. New InvariantEngine file registered in source map. | RC-09 |
| UGR-P01 | 2026-06-24 | cursor-agent | — | **MODULE_SOURCE_MAP: `contracts_resilience` module registered**. New module for UGR v3.1 zero-tolerance resilience architecture (CapResult, Phantom Contracts, TypedClock, WAL, SupervisedScheduler). Blueprint: `blueprints/modules/contracts_resilience.md`. | RC-09 — new module not yet registered |
| FIX-20260623-087 | 2026-06-23 | cursor-agent | 3dbc07ec | Commit Message Pre-Flight Validator: single-pass omega-routing validation script. Runs all 14 checks at once, reports all failures with fix hints. Eliminates whack-a-mole push pattern. | config-drift |
| FIX-20260623-086 | 2026-06-23 | cursor-agent | 5f77c70a | CI Red-X: PowerShell to bash shell migration for fast track step. pwsh $LASTEXITCODE + 2>&1 does not preserve Python exit codes reliably. | config-drift |
| FIX-20260623-084 | 2026-06-23 | cursor-agent | — | **DQAF-084: MODULE_SOURCE_MAP — `core/contracts/position_events.py` → contracts_domain**. Previously unmapped contract file now tracked (PositionClosed + p_win for calibrator). | L1 — orphan file never registered in MODULE_SOURCE_MAP |
| FIX-20260623-067 | 2026-06-23 | cursor-agent | — | **DQAF-067: MODULE_SOURCE_MAP — `scripts/audit_data_exhaustive.py` registered in observability**. Previously unmapped orphan script now tracked. | L1 — orphan file never registered in MODULE_SOURCE_MAP |
| FIX-20260623-075 | 2026-06-23 | cursor-agent | — | **DQAF-075: Pytest CI Slow/Fast Split — Marker-Based Test Segregation**. Registered `slow` marker (pyproject.toml + conftest.py). Auto-mark `tests/engine/` as slow via `pytest_collection_modifyitems` (7 exemptions). CI split: fast track `-m "not slow" -o "addopts="` (~180s, no coverage) + slow track `-m "slow"` (with coverage). Eliminates CI timeout risk; instant root-cause attribution (engine vs logic). | RC-09 — CI timeout caused by undifferentiated test suite; slow tests not isolatable |
| FIX-20260622-059 | 2026-06-22 | cursor-agent | — | **DQAF-059 Magic Drift Attribution Loss — Complete Repair**. See FIX-20260622-059 in FIX_REGISTRY.md and DQAF-20260622-059 in DQAF_DOCKET_REGISTRY. | L2 — MAGIC_TO_STRATEGY missing entries; L3 — no SSOT for magic↔strategy mapping |
| FIX-20260622-058 | 2026-06-22 | cursor-agent | — | **DQAF-058: MODULE_SOURCE_MAP — `scripts/generate_micro_scaler.py` + `core/observability/health_checks.py` registered**. New multi-asset scaler generator (supersedes BTC-only `generate_btc_empirical_scaler.py`). `health_checks.py` mapped to `monitor_dashboard` (previously unmapped orphan → FATAL). `pyproject.toml`: BLE001 per-file-ignore for pre-existing `BLE001:FOG` patterns in health_checks.py. | L3 — health_checks.py never registered in MODULE_SOURCE_MAP (23-day gap); scaler generator was BTC-only |
| FIX-20260622-055 | 2026-06-22 | cursor-agent | — | **DQAF-055: MODULE_SOURCE_MAP — `scripts/live_shadow_ensemble.py` → `feedback_pnl`**. Newly tracked script (previously unmapped orphan → FATAL). Previously hardcoded `scaler_path=None` in `MicrostructureFeatureAdapter` instantiation. | L2 — DQAF-054 site sweep missed this script |
| FIX-20260622-054 | 2026-06-22 | cursor-agent | — | **DQAF-054: `scripts/generate_btc_empirical_scaler.py` — BTC-dedicated micro scaler generator**. Read-only ingestion from Feature Store (6,350 M5 records) → 9 microstructure features → StandardScaler JSON. 投委会 VETO on cross-symbol reuse confirmed: BTC micro features statistically distinct from XAU (hl_ratio mean 0.593 vs 0.002). MODULE_SOURCE_MAP: script → deployment_lifecycle. | L3 — BTC had no micro scaler file; cross-symbol reuse prohibited by microstructure non-homogeneity |
| FIX-20260622-053 | 2026-06-22 | cursor-agent | — | **DQAF-053 Global State Reconciliation & Detox (3-Phase)**. Phase 1: migration script (`dqaf053_phase1_sanitize.py`) — removed corrupted `5\terminal64.exe` from alpha_performance.json, cleaned 13 orphan entries + ghost `alpha_xau_live`, backfilled strategy_class/assets for Swing_V9 + btc_swing records, purged calibrator FIFOs (BTC 500→0, XAU 500→0), reset BTC feed watermark. Phase 2: cold_explore defense-in-depth filter in `_step_calibrator_feed()` — builds `cold_explore_msg_ids` blacklist in Pass 1, skips in Pass 2 with `skipped_cold_explore` counter. Phase 3: fixed audit script path bugs (commander_guardrails_arch.py, commander_g3_alpha_vacuum.py → `reports/` subdirectory) and UnicodeEncodeError (monitor_pwin_fix.py → ASCII markers + stdout hardening). API additions: `AlphaPerformanceStore.remove_alpha()` + `list_ids()`, `ConformalCalibrator.reset_history()`. `_infer_strategy_class()` substring fallback for `btc_swing` prefixed IDs. MODULE_SOURCE_MAP: migration script → deployment_lifecycle. | L3 — cold_explore bypass path contaminated calibrator via journal without defense-in-depth filter; process monitoring gap (BTC feed frozen 3 days); orphan/ghost state accumulation without periodic reconciliation |
| FIX-20260622-052x | 2026-06-22 | cursor-agent | — | **MODULE_SOURCE_MAP: 8 Iron Law #11 audit scripts → monitor_dashboard**. Previously untracked ad-hoc diagnostic scripts from multi-docket audit campaigns (DQAF-043/044/G2-G7) registered as permanent institutional audit assets. `check_blueprint_compliance.py` MODULE_SOURCE_MAP updated for the 8 new entries. 3 pre-existing mypy type annotation errors fixed during onboarding. | RC-12 (missing-feature: scripts existed but were never tracked as system assets) |
| FIX-20260622-052 | 2026-06-22 | cursor-agent | — | **S.E.A.L. Framework: Root Cause Layer structural enforcement (S+E+L)**. Created `.gitcommit-template` (S — structural prevention) with mandatory institutional fields. Upgraded `omega_gate.py` (E — enforcement) to require Root Cause Layer annotation for Scene B/E FIX commits (was Scene A only), with plausibility heuristics (L1 on 200+ line diff → WARN, L3 on <10 lines → WARN). Added `--report` mode to `hook_architecture_gate.py` (L — longitudinal monitoring) outputting JSON with annotation coverage, per-module L1/L2/L3 breakdown, and ARCH_GATE_MODE=live readiness. Graduated enforcement: WARN (QUARANTINE) → REJECT (LIVE). MODULE_SOURCE_MAP: registered `scripts/omega_gate.py` + `scripts/hook_architecture_gate.py` → `deployment_lifecycle` (previously unmapped orphan). | L3 — architecture defect: Root Cause Layer enforced only for Scene A; 97.7% of FIX commits (Scene B/E) exempt from annotation requirement, rendering the 3-patch architecture gate structurally incapable of triggering |
| FIX-20260622-051 | 2026-06-22 | cursor-agent | — | **verify.py: config consistency WARN→FATAL + train-serve SL/TP mismatch detection (DQAF-051)**. `_check_config_consistency()`: missing `label_contract` upgraded from warning→fatal error (SystemExit(1)). New Check 3a: cross-references label_contract SL/TP against strategy line serve values. Pre-loads live YAML configs for strategy_lines lookup. Part of DQAF-051 Train-Serve Calibration Chasm — prevents silent config drift from reaching production. | L3 — label_contract was optional (no enforcement) + RC-09 |
| FIX-20260622-050 | 2026-06-22 | cursor-agent | — | **MODULE_SOURCE_MAP: core/alpha/lifecycle_service.py → execution_guards**. New alpha lifecycle service file registered (previously unmapped → orphan FATAL). Part of DQAF-050 Cold-Start Double Deadlock fix. | RC-09 |
| FIX-20260622-049 | 2026-06-22 | cursor-agent | — | **MODULE_SOURCE_MAP: core/alpha/contracts.py + core/alpha/registry.py → execution_guards**. Two new alpha subsystem files registered (previously unmapped → orphan FATAL). | RC-09 |
| FIX-20260622-005 | 2026-06-22 | cursor-agent | — | **MODULE_SOURCE_MAP: hook_pre_push.py → runtime_state + BLE001 noqa (3 sites)**. Pre-push CI-mirror gate was unmapped → orphan compliance FATAL. Also added noqa annotations to 3 pre-existing reviewed bare excepts (already wrapped with fail_open_guard). | RC-09 |
| FIX-20260621-031 | 2026-06-21 | cursor-agent | — | **Market-session-aware stale file checks**: system_trust_report.py now calls detect_session() per symbol (XAU→forex_24_5, BTC→crypto_24_7). When risk_tier="off" (weekend), stale file checks bypassed with [BYPASSED: MARKET_OFF] marker. BTC (24/7, 0 stale) serves as control group. MODULE_SOURCE_MAP updated. | RC-06 |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **MODULE_SOURCE_MAP: feature_assembler.py + rolling_normalizer.py 注册到 features_service** | L3 — 遗漏注册 |
| FIX-20260613-078 | 2026-06-13 | cursor-agent | 0699235 | STR Section 6b: candidate signal diversity detection. Flags brain pairs with >90% directional agreement using ledger_events SignalSettled data. Detected V11_H1≡V11_M15 (100% short) + XAU Brain_Trend cloning. | missing-validation |
| FIX-20260613-073 | 2026-06-13 | cursor-agent | 5a01fec | SL Performance Diagnostic: Iron Law #11 script. Revealed 97% BTC SL exits have zero trail advancement — SL hit before trail activates. TP exits net +61 positive. | missing-validation |
| FIX-20260613-072 | 2026-06-13 | cursor-agent | 0460a51 | System Trust Report: deterministic single-script health check. 6 sections with auto-VERDICT. Frozen contamination detection (weight>0=FAIL, weight=0=WARN). Iron Law #11 compliant. | contract-violation |
| FIX-20260613-066 | 2026-06-13 | cursor-agent | c992678 | Audit Script None Defense: analyze_live_journal.py guards trade_side and n_brains against None before format strings. Prevents TypeError crash in Section 4 and ensures Section 5-6 output. | missing-null-check |
| FIX-20260613-065 | 2026-06-13 | cursor-agent | c992678 | Blueprint Reconciliation Script: reconcile_fix_registry.py batch backfills 63 orphan + 28 missing FIX entries across 13 module blueprints. One-shot backlog clearance for Iron Law #7 compliance. | contract-violation |
| FIX-20260613-039 | 2026-06-13 | cursor-agent | — | **MODULE_SOURCE_MAP: exit_reason.py → execution_reentry**. New canonical SSOT file for exit reason taxonomy. | RC-09 |
| FIX-20260613-038 | 2026-06-13 | cursor-agent | — | **MODULE_SOURCE_MAP: rule_engine_strategy + meta_filter_routing + managed_close + trend_isolation_gates + net_out_close_handler**: 5 previously unmapped execution/ files registered (orphan trap #3). Also backtest_structural_swing→training. | RC-09 |
| FIX-20260611-022 | 2026-06-11 | cursor-agent | 19e002b | Register data_infrastructure in EXPECTED_MODULES list (validate_blueprints.py). | contract-violation |
| FIX-20260610-008 | 2026-06-10 | cursor-agent | — | 配置一致性静态闸门 (`scripts/verify.py` +140: `_check_config_consistency()`). V9/V10补全label_contract(SL=3.0/TP=2.0生存模式,需专属策略线). V5 XAU残留清理(`live.yaml` enabled→false). DQAF-20260610-002. | RC-09, RC-12 |
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
| FIX-20260530-078 | 2026-05-30 | cursor-agent | — | 36 unit tests for fault_handler (20) + meta_signal_filter (16). Both modules previously had zero test coverage — crash-loop, KBInterrupt guard, filter logic now tested. | RC-07 |
| FIX-20260602-056 | 2026-06-02 | cursor-agent | — | **test_contract_group_pipeline updated for FIX-052**: `test_pipeline_only_one_group_active_reduced_confidence` expected old self-normalized conf=0.65. Updated to FIX-052 raw-conf behavior (0.85×0.65≈0.55). | RC-06 |
| FIX-20260606-132 | 2026-06-06 | cursor-agent | — | **BTC leaderboard PnL-based fallback**: `_step_retraining_check()` now falls back to PnL-based `BrainLeaderboard.rank()` when decision-based leaderboard returns 0 decisions. | RC-12 |
| FIX-20260612-007 | 2026-06-12 | cursor-agent | — | AlphaRegistry.load() resilience: replaced direct dict access for `name`/`version` with .get() defaults. Also guarded daily_ops step list comprehensions against None entries from fail_open_guard-wrapped steps. Fixes daily_ops crash in retraining step. | RC-07 |

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

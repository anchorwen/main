# Deployment / Config

## Purpose
Environment configuration, service container (dependency injection), config hot-reload, and release pipeline orchestration.

## Key Files
| File | Role |
|------|------|
| `core/deployment/environment_config.py` | `Environment` enum, `EnvironmentConfig` dataclass |
| `core/deployment/service_container.py` | DI container wiring ~60 services |
| `core/deployment/config_hot_reload.py` | `ConfigHotReload` — watches engine_config.json |
| `core/deployment/release_pipeline.py` | `ReleasePipelineService` — end-to-end dry-run release |
| `core/deployment/release_gate.py` | `ReleaseGateService` — readiness + runbook + SLO + config |
| `core/deployment/deployment_executor.py` | `DeploymentExecutor` — safe dry-run execution |
| `core/deployment/deployment_plan.py` | `DeploymentPlanService` — phased deployment plans |
| `core/deployment/release_readiness.py` | `ReleaseReadinessService` |
| `core/deployment/release_registry.py` | `ReleaseRegistryService` |
| `core/deployment/release_certification.py` | `ReleaseCertificationService` |
| `core/deployment/final_audit.py` | `FinalAuditService` |
| `core/deployment/evidence_bundle.py` | `EvidenceBundleService` |
| `core/deployment/compliance_audit.py` | `ComplianceAuditService` |
| `core/deployment/compliance_control_matrix.py` | `ComplianceControlMatrixService` |
| `core/deployment/compliance_export.py` | Compliance export |
| `core/deployment/permission_audit.py` | Permission audit |
| `core/deployment/rollback_drill.py` | `RollbackDrillService` |
| `core/deployment/runbook_engine.py` | `RunbookEngine` |
| `core/deployment/blue_green.py` | Blue-green deployment |
| `core/deployment/capability_registry.py` | Capability registry |

## Data Flow
```
environment_config.json → EnvironmentConfig → ServiceContainer
                                                    ↓
                                    All services wired with correct env settings
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts | domain_keys | Evidence/pipeline stage keys |
| brains | BrainFactory, BrainRunService, etc. | Service wiring |
| execution | ExecutionManager | Service wiring |
| features | FeatureService | Service wiring |
| feedback | FeedbackLoop | Service wiring |
| governance | GovernanceService | Service wiring |
| ledger | Ledger services | Service wiring |
| observability | Observability services | Service wiring |
| parliament | ParliamentService | Service wiring |
| protocol | CommunicationDispatcher | Service wiring |
| risk | RiskEvaluationService | Service wiring |
| state | ControlSnapshotService | Service wiring |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/live_order_sender | EnvironmentConfig, ServiceContainer | DI access |
| runtime/live_cycle | ServiceContainer | Service access |
| apps/engine/ | EnvironmentConfig, ServiceContainer | CLI wiring |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260528-018 | 2026-05-28 | cursor-agent | — | Online_MLP_V1 path defaults cleanup: `ONLINE_BRAIN_PATH` → `None` in path_defaults.py. Brain retired 2026-05-25 (pnl:critical), config file retained for smoke test compatibility. Startup `missing_yaml_entries` warning is informational only (brain_lifecycle_manager.py:899 confirms auto-discovery handles it). | RC-09, RC-11 |
| FIX-20260524-009 | 2026-05-24 | cursor-agent | — | ConfigHotReload YAML support: load() now detects .yaml/.yml suffix and routes to yaml.safe_load(). Previously hardcoded json.loads() causing live_intent_loop's hot_reload on configs/live.yaml to fail every poll cycle. JSON configs (engine_config.json) continue to use json.loads(). | RC-06 |
| FIX-20260523-006 | 2026-05-23 | cursor-agent | — | Day 1 graveyard cleanup: (1) config_hot_reload.load() JSONDecodeError try/except wrapper keeping current config on file corruption; (2) live.yaml brain registry cleared of 5 disabled swing entries (xgboost_d1/m15/m30/h1/h4); (3) 5 swing brain config JSONs moved to archive_deprecated/ | RC-09, RC-06 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260521-001 | 2026-05-21 | cursor-agent | — | High Recall + High Precision: barrier_12bar confidence_threshold 0.45→0.25 (loose upstream recall). MetaFilter as downstream precision gate compensates. Huber vote_weight 0.0→0.8 in brains config. | RC-09 |
| FIX-20260520-027 | 2026-05-20 | cursor-agent | — | Institutional brain→live alignment (Layer 1): structured training_params added to all 14 brain registry entry JSONs — sl_atr_mult, tp_atr_mult, horizon_bars, min_rr_ratio. Parsed from training_contract strings (e.g. survival_barrier_2.0sl_3.5tp_12bar). BrainEntry + BrainRegistry updated. | RC-09 |
| FIX-20260518-027 | 2026-05-18 | cursor-agent | — | Phase 2b: Added DAILY_OPS_WINDOW_HOUR=22 + DAILY_OPS_WINDOW_DURATION_HOURS=1 to core/constants.py for fixed UTC daily_ops scheduling window. | config-drift |
| FIX-20260514-014 | 2026-05-14 | cursor-agent | a4a1005 | 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留 | config-drift |
| FIX-20260516-001 | 2026-05-16 | cursor-agent | — | statarb_dynamic threshold 0.40→0.25: live data shows OU signals at 0.28, 0.40 blocked all | config-drift |
| FIX-20260517-001 | 2026-05-17 | cursor-agent | — | meta_stage2_runtime_59 schema (59-dim) added to SCHEMA_DIMENSIONS and feature name resolver in brain_config_validator.py | config-drift |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor→Executor wiring: scheduler_service governance_eval now chains BrainPromotionEvaluator.evaluate_all() → GovernanceRuleEngine.execute_transitions() instead of calling apply_promotion_decisions() directly. Single Executor eliminates dual-write conflict. | contract-violation |
| FIX-20260524-020 | 2026-05-24 | cursor-agent | — | MEDIUM: Meta_Stage1_Huber_V1 status aligned to probation (was shadow in config, live in comment). Updated configs/brains/meta_stage1_huber_v1.json + configs/live.yaml comment. | RC-09 |
| FIX-20260524-021 | 2026-05-24 | cursor-agent | — | MEDIUM: Online_MLP_V1 allowlist exclusion documented — added comment in live.yaml explaining intentional exclusion (online learner not yet validated for live voting). | RC-09 |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: constants.py (DAILY_OPS_WINDOW) + path_defaults.py (brain switch) + scheduler_service.py (Auditor->Executor). Previously registered as FIX-20260518-027, FIX-20260517-017. | process-violation |
| FIX-20260521-002 | 2026-05-21 | cursor-agent | — | ServiceContainer auto-registration lost enabled flag from live.yaml entries: brain_data loaded from JSON at path but entry['enabled'] not propagated. Added `brain_data["enabled"] = entry.get("enabled", True)` before register(). | RC-09 |
| FIX-20260521-003 | 2026-05-21 | cursor-agent | — | 开单阈值精准化：(1) 禁用5个swing脑(xgboost d1/m15/m30/h1/h4) 100% LONG-only亏损；(2) barrier_12bar min_valid_brains 1→2 + confidence_threshold 0.25→0.45；(3) statarb_dynamic long_bias_discount 0.0→0.10 + hesitation_cycles 2→6 + confidence_threshold 0.20→0.35。 | RC-09 |
| FIX-20260521-004 | 2026-05-21 | cursor-agent | — | Intent进程崩溃循环修复：live_intent_loop.py在multi-brain模式下仍强制加载--brain-entry指定的单一大脑配置文件，默认路径指向已删除的lgb_barrier_12bar配置。修复方案：(1) load_brain_entry()包裹在if not args.multi_brain条件中；(2) path_defaults.py DEFAULT_BRAIN_ENTRY更新为deep_res_mlp_v1.json。 | RC-09 |
| FIX-20260521-005 | 2026-05-21 | cursor-agent | — | 全量类型注解清扫：v9_live_computer.py _returns()返回类型np.ndarray→float；main_v9_shadow.py 15个mypy错误→0(operator/vars-annotated/type-var/index/assignment/dict-item/unused-ignore)；label_builder.py变量trade遮蔽重命名为unlinked_trade。 | RC-02 |
| FIX-20260528-015 | 2026-05-28 | cursor-agent | — | path_defaults.py: DEFAULT_BRAIN_ENTRY updated from deleted deep_res_mlp_v1.json to Meta_Stage1_Binary_Cls_V1.json. Eliminates brain_entry_load_failed at startup. | RC-09 |
| FIX-20260521-006 | 2026-05-21 | cursor-agent | — | 状态清理+artifact修正：(1) governance_state.json清除16个僵尸脑条目(24→8)+27个transition_log条目；(2) live.yaml移除已删除的lightgbm_h1_swing引用；(3) deep_res_mlp_v1.json artifact_path指向现存v2模型。 | RC-09 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `ServiceContainer` is singleton, initialized once at startup | All services | Stable |
| `EnvironmentConfig` determines paths, log levels, and feature flags | All services | Stable |
| `ConfigHotReload` watches for changes, triggers callbacks | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "deployment or config or service" -q
```

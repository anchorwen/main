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
| FIX-20260518-027 | 2026-05-18 | cursor-agent | — | Phase 2b: Added DAILY_OPS_WINDOW_HOUR=22 + DAILY_OPS_WINDOW_DURATION_HOURS=1 to core/constants.py for fixed UTC daily_ops scheduling window. | config-drift |
| FIX-20260514-014 | 2026-05-14 | cursor-agent | a4a1005 | 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留 | config-drift |
| FIX-20260516-001 | 2026-05-16 | cursor-agent | — | statarb_dynamic threshold 0.40→0.25: live data shows OU signals at 0.28, 0.40 blocked all | config-drift |
| FIX-20260517-001 | 2026-05-17 | cursor-agent | — | meta_stage2_runtime_59 schema (59-dim) added to SCHEMA_DIMENSIONS and feature name resolver in brain_config_validator.py | config-drift |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor→Executor wiring: scheduler_service governance_eval now chains BrainPromotionEvaluator.evaluate_all() → GovernanceRuleEngine.execute_transitions() instead of calling apply_promotion_decisions() directly. Single Executor eliminates dual-write conflict. | contract-violation |

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

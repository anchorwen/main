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

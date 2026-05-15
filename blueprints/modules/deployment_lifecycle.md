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
| FIX-20260515-010 | 2026-05-15 | cursor-agent | — | Aggressive data cleanup: removed 2 frozen brain configs + 4 associated model files, 29 orphaned model files, 4 orphaned training NPZs, 2 .bak files, 4 dangling training contracts, 5 April decision directories, 10 frozen governance entries. Cleaned live.yaml disabled entries. | stale-data |
| FIX-20260515-009 | 2026-05-15 | cursor-agent | — | Auto-shadow mechanism: ShadowTracker integrated into governance eval (scheduler_service.py), train.py auto-register in live.yaml + governance_state.json, vote_weight=0.0 for shadow brains. | missing-feature |
| FIX-20260515-008 | 2026-05-15 | cursor-agent | — | Watchdog cleanup: deleted scripts/hourly_watchdog.py (May 5 experiment), data/watchdog.log. Updated ADR-006. Fixed verify.py deleted-file filter. | config-drift |
| FIX-20260515-007 | 2026-05-15 | cursor-agent | a4a1005 | New swing models (5 brain IDs) not registered in governance_state.json. Added all 5 with candidate status for PnL tracking and automated promotion eligibility. | config-drift |

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

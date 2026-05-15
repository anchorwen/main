# Runtime / State

## Purpose
System mode management (NORMAL, LIQUIDATION_ONLY, HALTED, etc.), override management (manual intervention flags), and control snapshot freezing for consistent cycle execution.

## Key Files
| File | Role |
|------|------|
| `core/state/stores/system_mode_store.py` | `SystemModeStore` — in-memory mode with disk persistence, stale reset |
| `core/state/stores/override_store.py` | `OverrideStore` — active overrides with time/symbol/mode scoping |
| `core/state/services/control_snapshot.py` | `ControlSnapshot` — frozen runtime state at a point in time |
| `core/state/services/control_snapshot_service.py` | `ControlSnapshotService` — freezes current state for cycle use |
| `core/constants.py` | `MODE_STALE_SECONDS` (86400 = 24h) |

## Data Flow
```
SystemModeStore.set_mode(new_mode) → disk persistence
         ↓
ControlSnapshotService.freeze() → ControlSnapshot
         ↓
LiveCycle.run_cycle() reads snapshot once per cycle
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | SystemModeState | State data class |
| contracts/enums | SystemMode | Mode enum values |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | SystemModeStore, OverrideStore | Mode-based gating |
| deployment/lifecycle | ControlSnapshotService | State management |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `SystemModeStore.get_mode()` → `SystemMode` | live_cycle, signal_health | Stable |
| `SystemModeStore.set_mode(mode, reason)` → `None` | override_resolver, CLI | Stable |
| `OverrideStore.get_active_overrides(symbol, regime)` → `list[Override]` | live_cycle | Stable |
| Stale mode auto-reset after 24h | SystemModeStore | Stable |

## Verification
```bash
python -m pytest tests/ -k "state or mode or override" -q
```

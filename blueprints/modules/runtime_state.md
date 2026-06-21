# Runtime / State

## Purpose
System mode management (NORMAL, LIQUIDATION_ONLY, HALTED, etc.), override management (manual intervention flags), control snapshot freezing for consistent cycle execution, and **State Governance Protocol (Plan B) — 4-layer defense for all ephemeral state writes**.

## Key Files
| File | Role |
|------|------|
| `core/state/stores/system_mode_store.py` | `SystemModeStore` — in-memory mode with disk persistence, stale reset |
| `core/state/stores/override_store.py` | `OverrideStore` — active overrides with time/symbol/mode scoping |
| `core/state/services/control_snapshot.py` | `ControlSnapshot` — frozen runtime state at a point in time |
| `core/state/services/control_snapshot_service.py` | `ControlSnapshotService` — freezes current state for cycle use |
| `core/state/catalog.py` | **Plan B Layer 1** — `StateArtifact` frozen dataclass + CATALOG (13 registered artifacts) + 10 built-in validators + `ALPHA_ID_SYMBOL_PREFIXES` cross-symbol mapping |
| `core/state/writer.py` | **Plan B Layer 2+3** — `StateWriter` 4-gate pipeline (required fields → schema validation → cross-symbol guard → atomic write via tmp+fsync+os.replace). Factory: `StateWriter.from_state_path()` |
| `core/state/freshness_guard.py` | **Plan B Layer 4** — `check_catalog_freshness()` iterates all artifacts, checks mtime vs TTL, emits CRITICAL for stale/empty, WARNING for missing |
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
|------|------|--------|--------|---------|------------|
| FIX-20260622-001 | 2026-06-22 | cursor-agent | — | **Plan B Phase 1-4: State Governance Protocol — 4-layer defense**. Layer 1: Data Catalog (catalog.py, 13 artifacts + validators). Layer 2: Write Gate (writer.py, 4-gate pipeline). Layer 3: Cross-Symbol Guard (alpha_id prefix registry). Layer 4: Freshness Guard (freshness_guard.py, TTL-based staleness). Purged 16 wild writes across 7 modules. 30/30 tests. | RC-07 (missing-validation — no schema enforcement at write boundary) |
| FIX-20260622-003 | 2026-06-22 | cursor-agent | — | **DQAF-046: XAU dual-track feature pipeline — end 45-day signal vacuum**. BrainSignal API fracture fix + 35-dim feature resolver (_resolve_swing35_feature_vector) + feature router (_route_feature_vector) + 16 brain configs updated. 0/21→11/21 non-neutral. | L2 — missing schema routing contract + dict→dataclass API fracture |

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

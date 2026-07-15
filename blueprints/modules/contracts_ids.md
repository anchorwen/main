# Contracts / IDs

## Purpose
Centralized ID generation for all domain objects. Ensures unique, sortable identifiers with consistent formatting across the system.

## Key Files
| File | Role |
|------|------|
| `core/contracts/ids.py` | `new_snapshot_id()`, `new_proposal_id()`, `new_candidate_id()`, `new_intent_id()`, `new_verdict_id()`, `new_record_id()`, `new_message_id()`, `new_dispatch_id()`, `new_communication_record_id()`, `new_execution_event_id()`, `new_runtime_cycle_id()` |
| `core/contracts/serialization/json_codec.py` | JSON serialization for domain types |
| `core/contracts/strategy_magic.py` | Magic number constants for strategy identification |

## Data Flow
```
Any producer needing an ID → contracts.ids.new_*_id() → uuid4-based string
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Only uses stdlib uuid + datetime |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| ~20 files across all modules | Various new_*_id functions | ID generation for all domain objects |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260715-018 | 2026-07-15 | cursor-agent | — | **L3: `init_magic_mappings()` idempotency guard over-blocked YAML loading**. Auto-init set `_MAPPINGS_INITIALIZED=True` with hardcoded-only fallback → `init_magic_mappings(config_path)` was no-op → 4 BTC multi-TF strategies never mapped → `__UNATTRIBUTED_BRIDGE_DEFAULT__`. Fix: condition changed to `if _MAPPINGS_INITIALIZED and not config_path: return`. ReB: `STRATEGY_MAGIC_IDEMPOTENCY_OVERBLOCKS_YAML`. | L3 — auto-init idempotency guard too aggressive |
| FIX-20260622-059b | 2026-06-22 | cursor-agent | — | **DQAF-059 Production Hardening — UnattributedOrderRejected exception + SENTINEL_UNATTRIBUTED_MAGIC sentinel constant**. See FIX-20260622-059b in FIX_REGISTRY.md. | L2 — missing fail-fast on sentinel magic |
| FIX-20260622-059 | 2026-06-22 | cursor-agent | — | **DQAF-059 Magic Drift Attribution Loss — Complete Repair**. See FIX-20260622-059 in FIX_REGISTRY.md and DQAF-20260622-059 in DQAF_DOCKET_REGISTRY. | L2 — MAGIC_TO_STRATEGY missing entries; L3 — no SSOT for magic↔strategy mapping |
| FIX-20260530-086 | 2026-05-30 | cursor-agent | — | BTC magic: added `btc_swing: 90410` to `strategy_magic.py`. BTC uses isolated 904xx magic range to prevent signal routing conflicts with XAU (900xx). | RC-09 (config-drift) |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| All `new_*_id()` return `str` in format `{prefix}_{uuid_hex[:12]}` | All producers | Highly stable |
| IDs are globally unique (uuid4 based) | All consumers | Highly stable |

## Verification
```bash
python -c "from core.contracts.ids import new_proposal_id; print(new_proposal_id())"
```

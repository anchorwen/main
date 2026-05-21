# Features / Service

## Purpose
Unified feature resolution with tiered fallback: local store → live computation → stub. Manages feature schemas, JSONL-based local storage, incremental update jobs, and feature adapters for different brain types.

## Key Files
| File | Role |
|------|------|
| `core/features/feature_service.py` | `FeatureService` — tiered feature resolution |
| `core/features/local_feature_store.py` | `LocalFeatureStore` — JSONL partitioned feature storage |
| `core/features/feature_snapshot.py` | `StoredFeatureSnapshot` — frozen feature snapshot |
| `core/features/store_contracts.py` | `FeatureQuery`, `FeatureRecord`, `FeatureSchema`, `FeatureStore(Protocol)` |
| `core/features/update_job.py` | `IncrementalFeatureUpdateJob`, `FeatureUpdateResult` |
| `core/features/adapters/v9_feature_adapter.py` | `V9FeatureAdapter` — normalizes V9 40-dim features |
| `core/features/adapters/microstructure_feature_adapter.py` | `MicrostructureFeatureAdapter` — normalizes 9-dim micro features |
| `core/features/computers/v9_live_computer.py` | `V9LiveFeatureComputer` — live MT5 feature computation |
| `core/features/computers/microstructure_computer.py` | `MicrostructureComputer` — 9-dim micro features |
| `core/features/computers/daily_computer.py` | `DailyFeatureComputer` — daily swing features |
| `core/features/computers/live_daily_provider.py` | `LiveDailyProvider` — daily features for swing |
| `core/features/schemas/v9_institutional_schema.py` | `V9_INSTITUTIONAL_40_FEATURES` — 40 feature names |
| `core/features/schemas/microstructure_schema.py` | Microstructure 9-dim schema |
| `core/features/schemas/daily_swing_schema.py` | Daily swing 24-dim schema |
| `core/constants.py` | `FEATURE_FRESHNESS_SLA_SECONDS`, `FEATURE_STORE_RETENTION_DAYS` |

## Data Flow
```
Trigger (symbol/timeframe) → FeatureService.get_snapshot()
    ↓
1. LocalFeatureStore.query() → hit? return
    ↓ (miss)
2. LiveFeatureComputer.compute() → hit? return + store
    ↓ (miss)
3. Stub/default values → return
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/ids | new_snapshot_id | Snapshot identification |
| features/schemas | V9_INSTITUTIONAL_40_FEATURES | Feature name resolution |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_factory | V9FeatureAdapter, MicrostructureFeatureAdapter | Adapter construction |
| runtime/live_cycle | FeatureService | Feature resolution for trading cycle |

## Known Issues
- `_normalize_dt()` strips timezone to naive UTC — consumers must add `tzinfo=UTC` before calling `.timestamp()` (see FIX-20260518-039)

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260518-045 | 2026-05-18 | cursor-agent | — | Commit catch-up: local_feature_store.resolve_version() schema lookup (previously documented under FIX-20260518-024 but never committed due to pre-commit deadlock). | process-violation |
| FIX-20260518-039 | 2026-05-18 | cursor-agent | — | Timezone normalization at freshness check: `feature_ts.timestamp()` on naive datetime interpreted as local time (UTC+8) — 28,800s artificial staleness. Fix: `feature_ts.replace(tzinfo=UTC)` before `.timestamp()` call. Affected FeatureService Tier 1 cache freshness SLA. | timezone-naive |
| FIX-20260518-024 | 2026-05-18 | cursor-agent | — | Phase 1b: Hardcoded schema_version="1.0" → dynamic resolve_version() from registered schemas; write-back skipped gracefully when no matching schema exists. Added LocalFeatureStore.resolve_version(). | hardcoded-value |
| FIX-20260516-005 | 2026-05-16 | cursor-agent | — | _stale=True dead code: pass was no-op, execution fell to raw vector return from same stale record. Fixed by inverting to `if not _stale:` guard with early return, so stale records genuinely fall through to Tier 2/3. | contract-violation |
| FIX-20260517-009 | 2026-05-17 | cursor-agent | — | Zero-vector frozen-confidence defense: Tier 3 now emits brain_alert before returning np.zeros() instead of silent fallback. Cache freshness check exception handler no longer silently swallows errors — now logs warning and forces live recompute (_stale=True) instead of accepting potentially stale cache. | contract-violation |
| FIX-20260519-020 | 2026-05-19 | cursor-agent | — | FeatureService live_compute timeout guard: Tier 2 `compute_all()` now runs in daemon thread with 3s `join()` timeout. On timeout, returns last_known_vector (or zeros) instead of blocking main loop indefinitely. Eliminates "latency slippage" risk where 800ms feature compute delays order dispatch → fill price drifts. Added `feature_compute_timeout` + `feature_compute_duration_ms` diagnostic events. | RC-06 (synchronous-block, latency-slippage) |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: local_feature_store.py resolve_version(). Previously registered as FIX-20260518-045. | process-violation |
| FIX-20260521-002 | 2026-05-21 | cursor-agent | — | FeatureBrainRegistry.list_active_entries() ignored 'enabled' field — only filtered by status. Added `e.get("enabled", True)` check. V3 necrotic brains bypassed enabled:false and voted in parliament. | RC-09 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `FeatureService.get_snapshot(trigger)` → `StoredFeatureSnapshot` | BrainRunService | Stable |
| `LocalFeatureStore.compact(retention_days)` → `int` (records removed) | daily_ops | Stable |
| `V9FeatureAdapter.normalize(raw_vector)` → `np.ndarray` | V9OnnxBrainAdapter | Stable |

## Verification
```bash
python -m pytest tests/ -k "feature" -q
```

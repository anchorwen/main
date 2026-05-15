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

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

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

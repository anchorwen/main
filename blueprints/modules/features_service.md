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
| FIX-20260616-091 | 2026-06-16 | cursor-agent | d5c848d | Dimension cleanup: disabled BTC V4 (37-dim receiving 41-dim input — tensor alignment shift risk). Global rename btc_macro_enhanced_37→btc_macro_enhanced_41 (schema was ALWAYS 41 dims, name was lying). XAU Price_ZScore already correct in latest feature store records. TECH_DEBT-004: V4 41-dim retrain registered. | contract-violation |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染 (C4/C5): BTC 特征静默回退→Fail-Closed RuntimeError; btc_macro_enhanced_37 维度 37→41** | L3 — 静默回退到 XAU 特征 |
| FIX-20260608-004r | 2026-06-08 | cursor-agent | — | **Multi-TF Feature Store ROLLED BACK**: `_resolve_timeframe()` removed — pure M5 stream restored. 40-dim vector already is the multi-TF holographic snapshot. | RC-12 |
| FIX-20260608-004 | 2026-06-08 | cursor-agent | — | **Multi-TF Feature Store (SUPERSEDED)**: Dynamic timeframe labeling added to `produce_from_live_computer()`. | RC-12 |
| FIX-20260604-081 | 2026-06-04 | cursor-agent | — | **BTC 37-dim macro enhanced schema**: `btc_macro_enhanced_schema.py` (AUDJPYc, XAUUSDc, BTC/XAU ratio+ROC). Registered in `_IMPLEMENTED_SCHEMAS`, `SCHEMA_DIMENSIONS`, `feature_assembler`. Physically isolated from XAU. ffill→ROC guard. | RC-06 |
| FIX-20260604-080 | 2026-06-04 | cursor-agent | — | **BTC cross-pair zero-fix**: `build_swing_enhanced_dataset.py --cross-raw-dir` fallback to `data/raw` macro lake. Cross features now non-zero. `ffill()` for 24/7 vs 24/5 weekend gap. | RC-06 |
| FIX-20260531-021 | 2026-05-31 | cursor-agent | — | **Data-driven swing feature assembly**: `assemble_swing_features()` in registry.py + `_derive_xau_indices()` auto-detects XAU-specific feature indices. BTC training removes XAU-only features (6 columns) from 35-dim → 29-dim schema. | RC-06 |
| FIX-20260528-025 | 2026-05-28 | cursor-agent | — | Train-inference feature alignment: dataset builder now uses `DailyFeatureComputer._gather_row()` as SSOT for 24 macro features. Eliminates 12-feature computation discrepancy (~37% model gain) between training (`compute_swing_macro_features()`, TF-bar-based) and inference (`DailyFeatureComputer`, D1-bar-based). Micro features + TF-specific features also aligned to match inference-side computation. | RC-06, RC-09 |
| FIX-20260528-022 | 2026-05-28 | cursor-agent | — | swing_enhanced_35 capability registration: added `swing_enhanced_35` to `_IMPLEMENTED_SCHEMAS` in FeatureService. Without this, `startup_validator` drops Swing_V9 brains at boot because `FeatureService.available_schemas()` doesn't include the 35-dim swing+micro schema. Same class of bug as FIX-20260525-027 (v9_40dim_ou3 missing from capability handshake). | RC-06 |
| FIX-20260528-021 | 2026-05-28 | cursor-agent | — | Swing enhanced schema (35 dims): added `swing_enhanced_35` to SCHEMA_DIMENSIONS + `swing_enhanced_schema.py` with feature name resolution. Combines 24 swing macro (D1/H4/cross-market/calendar) + 9 microstructure + 2 TF-specific (OU_Theta/Hurst). Phase 2 swing revival — M30 and M15 XGBoost v9 brains trained on this schema. | RC-09 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: created `core/features/schemas/registry.py` — single source of truth for all 14 feature schemas (SCHEMA_DIMENSIONS, SCHEMA_ALIASES, get_schema_dimension(), get_schema_feature_names()). Replaced local `_SCHEMA_DIMS` + `_schema_feature_names()` in feature_service.py with registry imports. Eliminated 5+ duplicate SCHEMA_DIMENSIONS copies across the codebase. | RC-06, RC-09 |
| FIX-20260525-027 | 2026-05-25 | cursor-agent | — | `v9_40dim_ou3` schema registration in FeatureService: added to `_IMPLEMENTED_SCHEMAS` (capability handshake for startup_validator Tier 2 check), `_SCHEMA_DIMS` (43 features), and `_schema_feature_names()` (V9 40 + 3 OU). Without this, `startup_validator` dropped MetaLabel brain at boot because `FeatureService.available_schemas()` didn't include the augmented schema. Layer 3 of the MetaLabel 43-dim train-serve fix chain. | RC-06 (contract-violation — missing capability declaration) |
| FIX-20260527-008 | 2026-05-27 | cursor-agent | — | OFI computation in `_compute_tick_features()`: `_ofi_buffer: deque[float]` (maxlen=100, ~8.3h M5 context), per-bar raw OFI from tick volume+flags z-scored and returned as `OFI`. NOT added to FEATURE_NAMES or any ML schema — OFI is a standalone risk signal consumed only by strategy_line toxicity gate, zero train-serve skew risk. | RC-12 |
| FIX-20260527-009 | 2026-05-27 | cursor-agent | — | OFI tick index overflow: `_flgs = np.array([int(t[5]) ...], dtype=np.int32)` read wrong tick field. MT5 COPY_TICKS_ALL returns 8-field tuples (time, bid, ask, last, volume, time_msc, flags, volume_real) — index 5 is time_msc (~1.78e12) which overflows np.int32 (max 2.15e9). Actual flags is at index 6. Fix: `t[5]`→`t[6]` + OFI block wrapped in fail-open try/except (OFI=0.0 on error, gate skipped). | RC-06 (off-by-one index, API-contract-misread) |
| FIX-20260525-009 | 2026-05-25 | cursor-agent | — | MT5 worker refactoring: microstructure_computer.py, v9_live_computer.py, v9_micro_computer.py, live_daily_provider.py — optional mt5_worker param, hardcoded TF constants, _copy_rates helper routes through worker. | RC-04, RC-06 |
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add feature_store_maintenance.py. Mypy fix (1→0 — extract errors_val for isinstance narrowing). Also feature_store_warmer mypy fix (1→0 — float(np.std()) annotation). | RC-02 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260518-045 | 2026-05-18 | cursor-agent | — | Commit catch-up: local_feature_store.resolve_version() schema lookup (previously documented under FIX-20260518-024 but never committed due to pre-commit deadlock). | process-violation |
| FIX-20260518-039 | 2026-05-18 | cursor-agent | — | Timezone normalization at freshness check: `feature_ts.timestamp()` on naive datetime interpreted as local time (UTC+8) — 28,800s artificial staleness. Fix: `feature_ts.replace(tzinfo=UTC)` before `.timestamp()` call. Affected FeatureService Tier 1 cache freshness SLA. | timezone-naive |
| FIX-20260518-024 | 2026-05-18 | cursor-agent | — | Phase 1b: Hardcoded schema_version="1.0" → dynamic resolve_version() from registered schemas; write-back skipped gracefully when no matching schema exists. Added LocalFeatureStore.resolve_version(). | hardcoded-value |
| FIX-20260516-005 | 2026-05-16 | cursor-agent | — | _stale=True dead code: pass was no-op, execution fell to raw vector return from same stale record. Fixed by inverting to `if not _stale:` guard with early return, so stale records genuinely fall through to Tier 2/3. | contract-violation |
| FIX-20260517-009 | 2026-05-17 | cursor-agent | — | Zero-vector frozen-confidence defense: Tier 3 now emits brain_alert before returning np.zeros() instead of silent fallback. Cache freshness check exception handler no longer silently swallows errors — now logs warning and forces live recompute (_stale=True) instead of accepting potentially stale cache. | contract-violation |
| FIX-20260519-020 | 2026-05-19 | cursor-agent | — | FeatureService live_compute timeout guard: Tier 2 `compute_all()` now runs in daemon thread with 3s `join()` timeout. On timeout, returns last_known_vector (or zeros) instead of blocking main loop indefinitely. Eliminates "latency slippage" risk where 800ms feature compute delays order dispatch → fill price drifts. Added `feature_compute_timeout` + `feature_compute_duration_ms` diagnostic events. | RC-06 (synchronous-block, latency-slippage) |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: local_feature_store.py resolve_version(). Previously registered as FIX-20260518-045. | process-violation |
| FIX-20260524-044 | 2026-05-24 | cursor-agent | — | T3-C1: MicrostructureComputer _compute_tick_features now accepts reference_time parameter — backtest passes historical bar timestamp, live defaults to datetime.now(UTC). Prevents look-ahead bias where datetime.now() returned system clock in historical replay. T3-H1: V9MicroComputer NaN sentinel replaced with 0.0 — prevents NaN propagation through feature vectors to model inference. T3-H2: V9FeatureAdapter validates normalization_strategy from model metadata — warns on train/inference mismatch to prevent silent distribution shift. | RC-03, RC-06 |
| FIX-20260521-002 | 2026-05-21 | cursor-agent | — | FeatureBrainRegistry.list_active_entries() ignored 'enabled' field — only filtered by status. Added `e.get("enabled", True)` check. V3 necrotic brains bypassed enabled:false and voted in parliament. | RC-09 |
| FIX-20260530-064 | 2026-05-30 | cursor-agent | — | Strangler Fig #4: _build_meta_feature_vector (121 lines) → core/features/meta_feature_builder.py. live_cycle.py 7100→6665 lines. | RC-08 |
| FIX-20260606-133 | 2026-06-06 | cursor-agent | — | **BTC feature assembler gap documented (Phase 5b Step A/B)**: Found 5/37 (13.5%) feature slots incorrect in live. Root cause of 8.4x confidence std collapse. | RC-06 |
| FIX-20260606-134 | 2026-06-06 | cursor-agent | — | **BTCFeatureAugmenter — Phase 5b Step B.2**: New `btc_feature_augmenter.py` with 3 production safeguards. Fixes [12] XAUUSDc_return, [30] AUDJPYc_return. XAU pipeline frozen. | RC-06 |

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

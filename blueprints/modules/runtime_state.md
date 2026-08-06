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
| FIX-20260806-008 | 2026-08-06 | cursor-agent | f35bb5bc | Scene F 纯测试同步: test_state_writer.py::TestCatalog::test_lookup_known_id 断言 LEADERBOARD ttl_seconds 14400→28800, 对齐 catalog.py:355 (FIX-20260628-156 L3 故意放宽 8h = 6h max_age + 2h buffer), 注释挂载溯源. CI 全绿清偿 (IC 裁决: 破窗效应禁止, 8/19 前 CI 必须 100% Green). 全量 pytest 5003 passed / 0 failed. | boundary-error |
| FIX-20260801-008 | 2026-08-01 | cursor-agent | — | **L3: Freshness Contract 命名空间隔离 — Telemetry vs Batch (DQAF-20260801-008).** `StateArtifact` 新增 `producer_class: ProducerClass = "batch"` 字段; 3 个实时产物 `EXECUTION_STATE`(TTL=30min)/`MT5_BRIDGE_HEALTH`(15min)/`ALERT_COOLING`(2h) 标记 `producer_class="telemetry"`. `validate_freshness_contract()` 跳过 telemetry 产物 — 其刷新由 per-cycle 实时生产者保证, 不受 daily_ops 批量调度器 max_age(6h) 约束. 启动 3 条 Freshness Contract VIOLATION 误报消除. 运行时 `freshness_guard.check_catalog_freshness()` 不变 (telemetry 仍按自身 TTL 检查). ReB: `TELEMETRY_TTL_VS_BATCH_CONTRACT`. | L3 — 分类学错误: 实时遥测产物 TTL 强行套用批处理产物 freshness contract |
| FIX-20260628-156 | 2026-06-28 | cursor-agent | — | **L3 Freshness Contract: catalog TTL 派生化 + validate_freshness_contract()**. All 10 batch-produced artifact TTLs changed from hardcoded `14400` (4h) to `_BATCH_DEFAULT_TTL_S = _BATCH_PRODUCER_MAX_INTERVAL_S + _BATCH_BUFFER_S` (6h + 2h = 8h). Added `validate_freshness_contract(scheduler_max_age_seconds)` → called from live_launcher at startup (fail-fast if TTL < max_age). Guards against DQAF-057 × FIX-149 cross-fix recurrence. | L3 — no producer-consumer freshness contract: catalog TTLs were hardcoded literals with no enforced relationship to scheduler max_age |
| FIX-20260627-154 | 2026-06-27 | cursor-agent | — | **Health Check — Plan B catalog TTL integration**. `scripts/health_check.py` Section [2] reads all 16 Plan B catalog artifacts and compares mtime against TTL from `core/state/catalog.py`, reporting STALE/MISSING/EMPTY/OK. Section [3] cross-references governance state with executing brains from intent log. | RC-14 — ad-hoc-query |
| FIX-20260627-152 | 2026-06-27 | cursor-agent | — | **CALIBRATOR_FEED_STATE catalog registration — Plan B CATALOG_COVERAGE_GAP closure**. `catalog.py`: Added `validate_calibrator_feed_state()` validator + `CALIBRATOR_FEED_STATE` StateArtifact (TTL=4h). Catalog: 15→16 artifacts. `daily_ops.py`: Migrated `_step_calibrator_feed` watermark persist from raw `write_text()` to `StateWriter.write_artifact(lookup("CALIBRATOR_FEED_STATE"), ...)`. | L2 — CATALOG_COVERAGE_GAP: calibrator_feed_state.json outside Plan B perimeter |
| FIX-20260625-132 | 2026-06-25 | cursor-agent | — | **Plan A+E: test-only commit auto-exemption + redundant mypy hook removal**. Added `is_test_only_commit()` to `omega_constants.py` with path normalization (`\\`→`/`) and root `conftest.py` whitelist. Test-only staged files auto-detected as Scene F equivalent. | RC-12 (missing-feature — Scene routing lacked test-only exemption path) |
| FIX-20260624-116 | 2026-06-24 | cursor-agent | 018e58e5 | P1-1+P1-2: Scene F full exemption in omega gate + shared omega_constants.py + pre-flight validator hook | config-drift |
| FIX-20260624-114 | 2026-06-24 | cursor-agent | 14b4c6da | P0-3: unify pre-push with pre-commit framework — extract check_omega_pre_push.py, add ci-mirror-omega hook, create bootstrap-dev-env.sh | config-drift |
| FIX-20260624-112 | 2026-06-24 | cursor-agent | a2c77b03 | P0-2: blueprint baseline mechanism — pre_commit_blueprint.py with baseline-gated validation, mirrors mypy_baseline.json pattern | config-drift |
| FIX-20260622-001 | 2026-06-22 | cursor-agent | — | **Plan B Phase 1-4: State Governance Protocol — 4-layer defense**. Layer 1: Data Catalog (catalog.py, 13 artifacts + validators). Layer 2: Write Gate (writer.py, 4-gate pipeline). Layer 3: Cross-Symbol Guard (alpha_id prefix registry). Layer 4: Freshness Guard (freshness_guard.py, TTL-based staleness). Purged 16 wild writes across 7 modules. 30/30 tests. | RC-07 (missing-validation — no schema enforcement at write boundary) |
| FIX-20260622-003 | 2026-06-22 | cursor-agent | — | **DQAF-046: XAU dual-track feature pipeline — end 45-day signal vacuum**. BrainSignal API fracture fix + 35-dim feature resolver (_resolve_swing35_feature_vector) + feature router (_route_feature_vector) + 16 brain configs updated. 0/21→11/21 non-neutral. | L2 — missing schema routing contract + dict→dataclass API fracture |
| FIX-20260622-005 | 2026-06-22 | cursor-agent | — | **Eliminate CI red-cross root cause — pre-push hook resilience**. `hook_pre_push.py`: ruff now scans only git-tracked files (prevents untracked-script false positives), batch processing for Windows cmdline limits, UTF-8 subprocess encoding, Omega skip-on-empty-range fix. Also skip-worktree on 4 BTC JSONL files to prevent pre-commit stash failure from live process locks. | RC-11 (stale-data — unowned untracked scripts poisoned pre-push gate) |
| FIX-20260622-004 | 2026-06-22 | cursor-agent | — | **DQAF-047: Wire orphaned training_readiness generator into daily_ops pipeline**. (1) Refactored check_training_readiness.py into pure-function engine — new evaluate_training_readiness() returns dict, no I/O. (2) Wild json.dump() replaced with StateWriter gate. (3) Added _step_training_readiness() to daily_ops.py with symbol-isolated contract glob (prefix match prevents cross-symbol contamination). (4) Integrated into run_daily_ops() pipeline. Result: BTC training_readiness 168h stale→healthy, XAU gracefully skipped (no contracts). Freshness Guard CRITICAL→CLEAN. | L2 — generator existed but was never wired to automated pipeline (orphan generator) |
| FIX-20260622-057a | 2026-06-22 | cursor-agent | — | **DQAF-057: CATALOG_COVERAGE_GAP closure — register brain_pnl_ledger.json + alert_cooling.json in State Catalog**. (1) Added `validate_brain_pnl_ledger()` and `validate_alert_cooling()` validators. (2) Registered `BRAIN_PNL_LEDGER` (TTL=14400s/4h) and `ALERT_COOLING` (TTL=7200s/2h) in CATALOG. (3) Tightened 10 TTL values: LEADERBOARD/ALPHA_*/GOVERNANCE_STATE/DAILY_OPS_STATE/DATA_HEALTH_STATE: 24h→4h; EXECUTION_STATE: 1h→30min; MT5_BRIDGE_HEALTH: 1h→15min. (4) Catalog expanded 13→15 artifacts. Freshness Guard now detects BRAIN_PNL_LEDGER staleness (previously invisible — 44.5h BTC, 24.1h XAU). 对标 Goldman Sachs Marquee DQF §4.2 + BlackRock Aladdin Data Integrity Protocol §7.1. | L3 (Architecture Defect) — CATALOG_COVERAGE_GAP: 15.5MB PnL ledger outside governance perimeter since Plan B deployment |

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

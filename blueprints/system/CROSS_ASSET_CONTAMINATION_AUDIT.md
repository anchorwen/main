# XAU/BTC Cross-Asset Contamination — Ω Systematic Audit Report

**Docket ID:** DQAF-20260615-006  
**Date:** 2026-06-15  
**Scope:** Full-codebase audit of all shared modules, configs, state files, feature pipelines  
**Methodology:** 5-agent parallel sweep → dedup → manual spot-verify

---

## Architecture Context

XAU and BTC share **all code modules** but operate as **separate OS processes** via `main.py` subprocess spawning. Differentiation is via config files (`live.yaml` vs `live_btc.yaml`) and CLI args. Process memory isolation prevents runtime state leaks, but **any hardcoded path or incorrect config default** in shared code affects both assets.

**Data directories:** XAU → `data/`, BTC → `data_btc/`  
**Brain config dirs:** XAU → `configs/brains/`, BTC → `configs/brains_btc/`  
**MT5 terminals:** XAU → `D:\exness\MetaTrader 5 EXNESS2\`, BTC → `D:\MetaTrader 5\`

---

## ✅ Already Fixed (Verify Only)

| # | Vector | Fix | Status |
|---|--------|-----|--------|
| F1 | `BTC_Swing_V5` in XAU `live.yaml` | DQAF-20260615-005 — Removed | ✅ |
| F2 | ZMQ ports | XAU=5556/5557, BTC=5558/5559 | ✅ Separated |
| F3 | Data directories | `data/` vs `data_btc/` | ✅ Enforced via `--base-dir` |
| F4 | Process isolation | `main.py` subprocesses | ✅ Correct |
| F5 | `known_open_tickets` | DQAF-20260615-004 — Persisted per-asset | ✅ |
| F6 | BrainRegistry singleton init | `live_intent_loop.py:896-897` — `reset()` + `instance(args.brains_dir)` | ✅ (fragile — see C10) |
| F7 | `_write_meta_exit_telemetry()` hardcoded to `data/` | DQAF-20260615-006 — Fixed | ✅ |
| F8 | BTC brain model_paths → `data_btc/` | 3 JSON files corrected | ✅ |

---

## 🔴 CRITICAL — Active Cross-Contamination (Must Fix Immediately)

### C1 — `strategy_line.py:828`: Brain votes always written to `data/brain_votes/`

```
core/execution/strategy_line.py:828    base_dir="data",
```

**Impact:** BTC process writes ALL brain shadow votes to `data/brain_votes/` (XAU directory). BTC brain vote tracking is completely invisible to BTC governance evaluation. BTC governance reads from `data_btc/brain_votes/` which never receives BTC votes.

**Causal chain:** `StrategyLine.evaluate()` → `record_brain_votes(base_dir="data")` — ignores `self.config.base_dir`. Every strategy evaluation cycle for BTC writes to wrong directory.

**Fix:** Replace `base_dir="data"` with `str(getattr(self.config, "base_dir", "data"))`.

---

### C2 — `scheduler_service.py:165/205/252`: Governance eval always uses `data/`

```
core/deployment/scheduler_service.py:165    tracker = ShadowTracker(base_dir="data")
core/deployment/scheduler_service.py:205    pnl_path = _Path("data") / "brain_pnl_ledger.json"
core/deployment/scheduler_service.py:252    _stream_path = _Path("data") / "ledger_events.jsonl"
```

**Impact:** BTC's 60-second governance evaluation reads XAU's `brain_pnl_ledger.json` and `ledger_events.jsonl`. BTC brain promotion/freeze/retire decisions are based on **XAU trade outcomes**, not BTC ones.

**Causal chain:** `governance_eval()` closure captures `"data"` at definition → every call reads wrong files → BTC brain metrics = XAU brain metrics → wrong brains promoted.

**Fix:** Replace with `str(container.config.base_dir)` — matching the pattern used at lines 375-479 in the same file.

---

### C3 — `live_cycle.py:4099`: MetaFilterGate model_dir hardcoded to `data/models/meta_filter_v3`

```
core/runtime/live_cycle.py:4099    model_dir="data/models/meta_filter_v3",
```

**Impact:** BTC MetaFilterGate loads the XAU meta-filter model. If BTC has its own meta-filter model in `data_btc/models/`, it's ignored. If BTC doesn't have one, it silently uses XAU's model.

**Fix:** `model_dir=f"{config.base_dir}/models/meta_filter_v3"`.

---

### C4 — `feature_assembler.py:211-219`: Silent XAU fallback when BTCFeatureAugmenter fails

```
core/features/feature_assembler.py:211    if btc_augment is not None and len(btc_augment) == 41:
core/features/feature_assembler.py:212        return np.asarray(btc_augment, dtype=np.float64)
core/features/feature_assembler.py:214    # Legacy path (pre-FIX-134): XAU-centric assembly
core/features/feature_assembler.py:218    fv_37 = np.concatenate([fv_35, np.zeros(2, dtype=np.float64)])
```

**Impact:** If `BTCFeatureAugmenter` fails to initialize (MT5 unavailable, feature store down, exception during `augment()`), `_btc_aug` stays `None` and the assembler **silently falls back** to the XAU-centric legacy path:
- Slot [12] = `Cross_Gold_Silver_Ratio` instead of `XAUUSDc_return`
- Slot [30] = `XAGUSDc_return` instead of `AUDJPYc_return`
- Slots [35-36] = hardcoded zeros instead of BTC/XAU ratio + ROC

**BTC brains receive XAU features without any signal rejection.**

**Fix:** When `btc_augment is None` for BTC schema, raise an explicit error or at minimum log a SEVERE alert with `fail_open_guard`. The silent fallback must be eliminated.

---

### C5 — Schema registry dimension mismatch: `btc_macro_enhanced_37` says 37, actual is 41

```
core/features/schemas/registry.py:47    "btc_macro_enhanced_37": 37,
configs/brains_btc/BTC_Swing_V12_H1_Survival.json:85    "n_features": 41,
```

**Impact:** Any code path that queries `SCHEMA_DIMENSIONS["btc_macro_enhanced_37"]` for buffer allocation gets 37 instead of 41. Currently masked because `BTCFeatureAugmenter.augment()` returns a 41-dim vector directly (bypassing the dimension check), but if the augmenter path is skipped or if any other code allocates based on the registry value, it produces a 37-element buffer for a 41-element feature vector.

**Fix:** Update registry to `"btc_macro_enhanced_37": 41` and rename to `btc_macro_enhanced_41`.

---

### C6 — `live_order_sender.py:68-83`: Protection flag resolved to shared XAU path

```
core/execution/live_order_sender.py:68-83
core/runtime/live_cycle.py:99    protection_flag_path: str = "data/live_dispatch_block.flag"
```

**Impact:** `resolve_protection_flag_path()` checks `PROJECT_ROOT / "data/live_dispatch_block.flag"` first. When running BTC with default `protection_flag_path`, it resolves to XAU's flag file. A protection flag set for XAU **also blocks BTC dispatch**.

**Fix:** `resolve_protection_flag_path()` should prefer `base_dir` resolution when the path's first component matches neither XAU nor BTC data dir. Or, change the default to derive from `base_dir`.

---

### C7 — `live_intent_loop.py:1649`: Directional MetaFilter models hardcoded to `data/models/`

```
scripts/live_intent_loop.py:1649    _dir_model_path = str(PROJECT_ROOT / "data" / "models" / f"meta_stage2_{_dir_model}.txt")
```

**Impact:** BTC direction-specific meta filters (long/short) always loaded from `data/models/` (XAU). BTC models in `data_btc/models/` are invisible to the directional filter path.

**Fix:** Use `args.base_dir` instead of hardcoding `"data"`.

---

### C8 — `gate_audit_recorder.py`: Called without `base_dir` — all BTC gate audits go to `data/gate_audit/`

```
core/runtime/strategy_evaluator.py:622    record_gate_block(...)  # NO base_dir passed
core/runtime/gate_audit_recorder.py:34    base_dir: str = "data",
```

**Impact:** Confirmed by directory listing: `data/gate_audit/` exists with files; `data_btc/` has no `gate_audit/` directory. All BTC gate block records are written to XAU's audit trail.

**Fix:** Pass `base_dir` from strategy evaluator config through to `record_gate_block()`.

---

## 🟠 HIGH — Latent Contamination (Should Fix)

### H1 — `live_intent_loop.py:759-760`: Hardcoded XAU daily CSV for BTC process

```
scripts/live_intent_loop.py:759    d1_csv="data/raw/xauusdc_d1_merged.csv",
scripts/live_intent_loop.py:760    h4_csv="data/raw/xauusdc_h4_merged.csv",
```

**Impact:** `LiveDailyFeatureProvider` is initialized with XAU CSV paths even in BTC process. Currently masked because BTC swing strategies don't use daily features, but if a future BTC daily strategy is added, it feeds XAU daily bar data into BTC brains.

**Fix:** Parameterize based on symbol or config.

---

### H2 — `live_intent_loop.py:1095`: MetaExit model hardcoded to `data/models/meta_exit_model.txt`

```
scripts/live_intent_loop.py:1095    model_path=meta_model or "data/models/meta_exit_model.txt"
```

**Impact:** BTC meta_exit_engine loads XAU's exit model. BTC exits evaluated with XAU exit model.

**Fix:** Use `args.base_dir`.

---

### H3 — BTC normalization config is XAU copy (gold ~4500 vs BTC ~74000 scale)

```
configs/brains_btc/v9_institutional_01.normalization.json
```

**Impact:** Currently latent because `normalize=false` everywhere. But if accidentally enabled, BTC features would be z-scored against gold statistics — producing garbage numerical values. No automated guard prevents this.

**Fix:** Add runtime assertion: if `normalize=true` for BTC symbol and mean/scale are clearly XAU-scale, RAISE error with clear message.

---

### H4 — `MicrostructureFeatureComputer` always fetches XAGUSDc (silver) for ALL symbols

```
core/features/computers/microstructure_computer.py:51    CROSS_SYMBOLS = ["XAGUSDc", "EURUSDc", "USDJPYc"]
```

**Impact:** Module-level constant. The BTC cross-asset features should include AUDJPYc (risk appetite proxy for crypto) instead of XAGUSDc (silver, relevant only for gold). Mitigated by `BTCFeatureAugmenter` which replaces slot [30], but if augmenter is bypassed (C4), silver returns leak into BTC features.

**Fix:** Make cross symbols configurable per-asset, not a module-level constant.

---

### H5 — `DailyFeatureComputer` always computes Gold-Silver ratio regardless of symbol

```
core/features/computers/daily_computer.py:596-629
```

**Impact:** Same structural issue as H4. Gold-Silver ratio is XAU-centric. Mitigated by `BTCFeatureAugmenter`.

**Fix:** Make cross-asset computation configurable per symbol.

---

### H6 — `live_intent_loop.py:904-907`: Disabled-brain filter hardcoded XAU fallback

```
scripts/live_intent_loop.py:904    fallback to PROJECT_ROOT / "configs" / "live.yaml"
```

**Impact:** If `--config` is omitted when running BTC, disabled-brain filtering reads XAU's live.yaml and filters BTC brains against XAU registry entries.

**Fix:** Default to `args.config` if set, otherwise raise error for BTC.

---

### H7 — `brain_registry_service.py:95/100`: Allowlist matching hardcodes `configs/brains/`

```
core/brains/services/brain_registry_service.py:95    cfg_path = self._project_root / "configs" / "brains" / f"{cfg_brain_id}.json"
core/brains/services/brain_registry_service.py:100   rel = f"configs/brains/{cfg_brain_id}.json"
```

**Impact:** When BTC uses explicit `registry_entries` (allowlist) in live_btc.yaml, the path matching constructs XAU paths. Allowlist entries for BTC brains won't match because the constructed path uses `configs/brains/` instead of `configs/brains_btc/`.

**Fix:** `_filtered_by_allowlist()` must use `BrainRegistry`'s known `brains_dir` instead of hardcoding `configs/brains/`.

---

### H8 — Multiple scripts/apps hardcode `configs/brains/` for brain lookup

| File | Line | Hardcoded Path |
|------|------|---------------|
| `scripts/live_daily_recap.py` | 217 | `Path("configs/brains")` |
| `apps/monitor/live_trading_dashboard.py` | 2038 | `Path(f"configs/brains/{brain_id}.json")` |
| `apps/monitor/live_trading_dashboard.py` | 2297 | `Path("configs/brains")` |
| `scripts/paper_trade_simulator.py` | 97-98 | `PROJECT_ROOT / "configs" / "brains"` |
| `core/feedback/param_optimizer.py` | 91 | `Path("configs/brains")` |
| `scripts/shadow_pnl_loop.py` | 218 | `default="configs/brains"` |

**Impact:** Each of these tools/scripts is XAU-only. They cannot be used for BTC without code changes. The dashboard cannot display BTC brain data.

---

### H9 — Scheduler service tasks silently fail for BTC

```
core/deployment/scheduler_service.py:375-479 (governance_eval uses hardcoded data/)
core/deployment/scheduler_service.py:165 (ShadowTracker uses hardcoded data/)
```

**Impact:** In addition to C2, the ShadowTracker reads from `data/brain_votes/` which for BTC only contains votes from the hardcoded C1 path. This creates a partial contamination: some BTC votes go to `data/brain_votes/` (from C1), while BTC's own `data_btc/brain_votes/` is incomplete.

---

## 🟡 MEDIUM — Fragility / Technical Debt

### M1 — `event_writer.py`: Singleton pins path on first call

```
core/data/event_writer.py:105-119    _writer singleton, base_dir="data" default
```

**Impact:** If XAU and BTC ever share a process, the first `get_event_writer()` call locks in the path forever. Currently safe due to process isolation, but the API is footgun-shaped.

**Fix:** Key the singleton by `base_dir`.

---

### M2 — `path_defaults.py`: All module-level constants are XAU

```
core/deployment/path_defaults.py — all defaults assume XAUUSDc
```

**Impact:** Any code importing these constants without overriding gets XAU paths. The docstring acknowledges this, but nothing enforces it.

---

### M3 — BTC archive brains point to `data/` not `data_btc/`

```
configs/brains_btc/archive/BTC_Swing_V11_H1_Directional.json:9    "model_path": "data/models/btc_directional_h1/..."
configs/brains_btc/archive/BTC_Swing_V11_M15_Directional.json:9  "model_path": "data/models/btc_directional_m15/..."
configs/brains_btc/archive/BTC_Swing_V3.json:9                    "model_path": "data\\models\\btc_swing_fresh/..."
configs/brains_btc/archive/BTC_Swing_V5.json:9                    "model_path": "data\\models\\swing/..."
```

**Impact:** Archived brains only, but inconsistent convention. If resurrected without path correction, they'd load from wrong directory.

---

### M4 — Meta Stage2 Filters V2/V3 use XAU V9 schema for BTC

```
configs/brains_btc/meta_stage2_filter_v2.json:7    "feature_schema": "v9_institutional_40"
configs/brains_btc/meta_stage2_filter_v3.json:7    "feature_schema": "v9_institutional_40"
```

**Impact:** V2/V3 are XAU V9-based filters applied to BTC. They produce output (V9 technicals are symbol-agnostic), but miss BTC-specific signal (BTC/XAU ratio, AUDJPY return). Degraded but not actively wrong.

---

### M5 — Missing BTC strategies in cooldown lookup

```
core/execution/pre_trade_guards.py:627-639    _STRATEGY_TIMEFRAME_SEC
```

**Impact:** `btc_swing` and `btc_swing_h1` are missing from the strategy-to-timeframe mapping. Would default to 300s (M5) for cooldown — incorrect for H1 strategies.

---

### M6 — ZMQ defaults are XAU (5556/5557)

```
scripts/mt5_bridge_worker.py:62/67    default endpoints tcp://127.0.0.1:5556/5557
core/deployment/service_container.py:287    fallback to tcp://127.0.0.1:5556
```

**Impact:** Only if bridge/container is run manually without YAML overrides. Launcher always passes correct ports.

---

### M7 — `live_intent_loop.py` default symbol is `XAUUSDc`

```
scripts/live_intent_loop.py:92    default="XAUUSDc"
```

**Impact:** Only if the intent loop is run directly without `--symbol`. Launcher always overrides.

---

### M8 — `LiveCycleConfig` defaults are XAU-tuned

```
core/runtime/live_cycle.py:82-170    symbol="XAUUSDc", base_dir="data", market_type="forex_24_5", exit_min_step=0.15
```

**Impact:** BTC should have `market_type="crypto_24_7"`, `exit_min_step=1.0`. Currently overridden by launcher from YAML, but constructing `LiveCycleConfig()` without args would get XAU defaults.

---

### M9 — `TrainingRegistry` default SQLite path

```
core/training/training_registry.py:91    db_path = "data/training/registry.db"
```

**Impact:** If BTC training uses defaults, XAU and BTC training records mix in the same SQLite DB.

---

### M10 — `experience_replay.py` / `online_feedback_hook.py` defaults to `data/`

```
core/feedback/experience_replay.py:38    state_path: str = "data/experience_replay_state.json"
core/feedback/online_feedback_hook.py:42-46    journal_path, feature_store_dir, last_processed_path all "data/..."
```

**Impact:** Default parameters point to XAU data directory. Callers must override.

---

### M11 — `live_intent_loop.py:1775`: `full_cfg` potentially undefined

```
scripts/live_intent_loop.py:323 (defined inside if args.config:)
scripts/live_intent_loop.py:1775 (referenced unconditionally)
```

**Impact:** If `--config` is not passed, `NameError` at runtime. Launcher always passes `--config`, so this is latent.

---

### M12 — Feature schema registry computes XAU indices at import time for BTC process

```
core/features/schemas/registry.py:169-178    _XAU_35_INDICES computed at import
```

**Impact:** Waste, not contamination. BTC process computes XAU-specific index sets it never uses. Pure CPU/memory waste.

---

## 🟢 LOW — Cosmetic / Note Only

### L1 — Various smoke tests default to `XAUUSDc`

```
core/features/computers/v9_live_computer.py:313
core/features/computers/microstructure_computer.py:549
```

### L2 — Docstring examples reference `data/` paths

```
core/data/wap.py:11, core/data/projections.py:20-21, core/training/model_card.py:15/21
core/brains/services/brain_leaderboard.py:8-9, core/brains/adapters/meta_filter_adapter.py:13-14
```

### L3 — Error message in `brain_registration_gate.py:271` says "configs/brains/" regardless of context

---

## ✅ SAFE — Verified Correct

| Item | Why Safe |
|------|----------|
| ZMQ port separation (5556/5557 vs 5558/5559) | launcher enforces from YAML |
| MT5 terminal path separation (EXNESS2 vs standard) | launcher reads from per-asset YAML |
| Feature store (per-symbol partitioning) | `{records_dir}/symbol={symbol}/timeframe={tf}/features.jsonl` |
| State files under `data/` and `data_btc/` (19 matching files) | `--base-dir` correctly applied |
| Log files (`logs/alert_audit.jsonl`) | Separated per data dir |
| Lock files (`locks/live_intent_loop.lock`) | Per data dir |
| `BrainLifecycleManager` | Caller passes per-asset paths |
| `exit_watchdog.py` | Uses `self.data_dir` from config |
| `golden_master.py` | Accepts `data_dir` parameter |
| `position_manager.py` (except C1) | `save_state(save_path)` takes explicit path |
| `daily_ops.py` | Uses `base` parameter |
| `brain_pnl_ledger.py` | `save(path)` / `load(path)` take explicit paths |
| `execution_state.py` | Takes `save_path` parameter |
| `live_alert_hub.py` | `base_dir` passed from CLI |
| `EventWriter` singleton | Per-process, initialized with correct `base_dir` at line 817 |
| `BrainRegistry` singleton | Explicitly reset + re-initialized at lines 896-897 |
| All singleton patterns (BrainQualityEngine, BrainConfigValidator, etc.) | Per-process isolated |
| Module-level caches keyed by schema name (not asset) | Correct by design |
| `CONTRACT_GROUPS`, `ENSEMBLE_GROUPS` | Per-process, populated from BrainRegistry |
| Golden Master env vars | Per-process, writes to `{data_dir}/golden_master.jsonl` |
| `data_health_service.py` | Uses `self._base_dir`, has `"btc" in base_dir` heuristic |

---

## 🔢 Severity Summary

| Severity | Count | Fix Priority |
|----------|-------|-------------|
| CRITICAL | 8 | Immediate (this session) |
| HIGH | 9 | This week |
| MEDIUM | 12 | When touching affected modules |
| LOW | 3 | Note only |
| SAFE | 22 | Verified — no action |

**Total findings: 54** (32 actionable, 22 verified safe)

---

## 🎯 Recommended Remediation Order

### Batch 1 — Immediate (this session): Fix C1-C8

| # | File | Change | Lines |
|---|------|--------|-------|
| C1 | `core/execution/strategy_line.py` | `base_dir="data"` → `str(getattr(self.config, "base_dir", "data"))` | 828 |
| C2 | `core/deployment/scheduler_service.py` | 3x hardcoded `"data"` → `str(container.config.base_dir)` | 165, 205, 252 |
| C3 | `core/runtime/live_cycle.py` | `model_dir="data/models/meta_filter_v3"` → `f"{config.base_dir}/models/meta_filter_v3"` | 4099 |
| C4 | `core/features/feature_assembler.py` | Replace silent fallback with `fail_open_guard` + SEVERE alert | 211-219 |
| C5 | `core/features/schemas/registry.py` | `"btc_macro_enhanced_37": 37` → `41` | 47 |
| C6 | `core/execution/live_order_sender.py` | Fix `resolve_protection_flag_path()` to prefer `base_dir` | 68-83 |
| C7 | `scripts/live_intent_loop.py` | `PROJECT_ROOT / "data" / "models"` → `Path(args.base_dir) / "models"` | 1649 |
| C8 | `core/runtime/strategy_evaluator.py` | Pass `base_dir` to `record_gate_block()` | 622-627 |

### Batch 2 — This week: Fix H1-H9

| # | File | Change |
|---|------|--------|
| H1 | `live_intent_loop.py:759-760` | Parameterize daily CSV paths by symbol |
| H2 | `live_intent_loop.py:1095` | Use `args.base_dir` for meta_exit model |
| H3 | Normalization configs | Add automated guard assertion |
| H4 | `microstructure_computer.py:51` | Per-asset cross symbols |
| H5 | `daily_computer.py:596-629` | Per-asset cross features |
| H6 | `live_intent_loop.py:904-907` | Eliminate XAU fallback for BTC |
| H7 | `brain_registry_service.py:95/100` | Use `BrainRegistry`'s `brains_dir` |
| H8 | 6 scripts with hardcoded `configs/brains/` | Add `--brains-dir` CLI arg |
| H9 | `scheduler_service.py` ShadowTracker | Covered by C2 fix |

### Batch 3 — When touching modules: Fix M1-M12

These are defaults, conventions, and fragility issues. Fix them when you next modify the affected file.

---

## 📊 Cross-Reference: Contamination by Subsystem

```
Feature Pipeline    ████████████ C4, C5, H1, H4, H5
Brain Registry       ██████ H7, H8, M3
State/Log Files     ████████████ C1, C2, C6, C8, M1
Scheduler/Governance ██████ C2, H9, M9
Meta Filters        ██████████ C3, C7, H2, M4
Config Defaults     ████████ H3, H6, M2, M5, M6, M7, M8, M10, M11
Training Scripts    ███ M3, M9
```

---

## Appendix: Full File Index of Contaminated Paths

### Hardcoded `"data/"` (not parameterized)

| File | Line(s) | String |
|------|---------|--------|
| `core/execution/strategy_line.py` | 828 | `base_dir="data"` |
| `core/deployment/scheduler_service.py` | 165 | `ShadowTracker(base_dir="data")` |
| `core/deployment/scheduler_service.py` | 205 | `_Path("data") / "brain_pnl_ledger.json"` |
| `core/deployment/scheduler_service.py` | 252 | `_Path("data") / "ledger_events.jsonl"` |
| `core/runtime/live_cycle.py` | 4099 | `model_dir="data/models/meta_filter_v3"` |
| `core/runtime/live_cycle.py` | 385 | `DAILY_OPS_STATE_PATH = "data/state/daily_ops_state.json"` |
| `core/runtime/fault_handler.py` | 54, 83 | `Path("data/state/last_good_state.json")` |
| `core/protocol/event_bar_sync.py` | 691 | `Path("data") / "reports" / "bar_sync_events.jsonl"` |
| `core/observability/mlflow_bridge.py` | 37 | `Path("data/mlflow_artifacts")` |
| `scripts/live_intent_loop.py` | 1649 | `PROJECT_ROOT / "data" / "models" / ...` |
| `scripts/live_intent_loop.py` | 1095 | `"data/models/meta_exit_model.txt"` |
| `scripts/live_intent_loop.py` | 759-760 | `"data/raw/xauusdc_d1_merged.csv"` |
| `scripts/audit_brain_fleet.py` | 31 | `Path("data/models")` |
| `scripts/validate_brain_before_deploy.py` | 34 | `Path("data/feature_store/records/symbol=XAUUSDc/...)` |

### Hardcoded `"configs/brains/"` (not parameterized)

| File | Line(s) |
|------|---------|
| `core/brains/services/brain_registry_service.py` | 95, 100 |
| `scripts/live_daily_recap.py` | 217 |
| `apps/monitor/live_trading_dashboard.py` | 2038, 2297 |
| `scripts/paper_trade_simulator.py` | 97-98 |
| `core/feedback/param_optimizer.py` | 91 |
| `scripts/shadow_pnl_loop.py` | 218 |

### Hardcoded `"configs/live.yaml"` fallback (not `live_btc.yaml`)

| File | Line(s) |
|------|---------|
| `scripts/live_intent_loop.py` | 907, 1704 |
| `main.py` | 244, 834 |

---

*End of Audit Report. Next action: IC review → approve remediation batches.*

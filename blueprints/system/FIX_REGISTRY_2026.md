# Fix Registry — 2026

> Parent index: [FIX_REGISTRY.md](FIX_REGISTRY.md) — Fix ID format, root cause categories, and global Fix Index.

## Fix Details

### FIX-20260516-003
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: analysis / documentation
- **Module**: multi-module (runtime-live, execution-guards, brains-services)
- **Files**: blueprints/modules/runtime_live.md, blueprints/modules/execution_guards.md
- **Description**: Comprehensive data-driven strategy parameter analysis using 3 data sources:
  1. brain_votes (7,216 records, 2026-05-15): Per-strategy signal distributions, per-brain directionality, consensus dilution analysis
  2. live_trade_journal (1,230 entries, Apr 29-May 16): Exit reason effectiveness, SL:TP hit ratio, per-strategy PnL, historical loss attribution
  3. brain_pnl_ledger (773 trades): Per-brain live performance vs training metrics

  Key findings documented in blueprints:
  - CRITICAL: Both LightGBM brains produce FROZEN confidence (0.5519/0.6120 identical every cycle). ML inference pipeline broken - constant/zero feature vectors. Explains 100% LONG bias, negative live PnL, 8.3 Sharpe gap (train 8.21 vs live -0.10)
  - Only non-ML brain (OU_Params_V6_Sniper) works: 49.7% win rate, +119.91 bps
  - 7/8 barrier_12bar brains 100% neutral - no directional signals
  - SL:TP hit ratio 4.6:1 - per-trade R:R adequate but frequency mismatch fatal
  - magic=90004 (unregistered, May 5-7) = 79% of all journal PnL losses - already removed
  - 60% exits have unknown reason, 34% closes lack PnL - journal completeness gaps
- **Root Cause**: RC-06 - contract-violation (ML inference pipeline not delivering valid feature vectors to LightGBM adapters; journal not capturing exit reasons/PnL for 34-60% of trades)
- **Prevention**: All parameter changes must reference Strategy Parameter Reference in runtime_live.md. ML brain deployment must validate confidence variance within first 100 cycles. Journal should enforce mandatory exit_reason and pnl on close records.
- **Dependents Checked**: runtime_live.md, execution_guards.md blueprints updated. All active strategy parameters documented with data justification.

### FIX-20260516-004
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: brains-adapters, brains-services, apps-engine
- **Files**: `core/brains/adapters/lightgbm_brain_adapter.py`, `core/brains/services/brain_run_service.py`, `apps/engine/runtime_loop.py`, `configs/brains/lightgbm_v1.json`, `configs/brains/lightgbm_h1_swing_lightgbm_v1_20260514_165620.json`, `tests/engine/test_brain_loading_shadow.py`
- **Description**: LightGBM inference pipeline frozen confidence root-cause fix. Four structural defects identified and repaired:

  1. **Hardcoded schema imports removed**: LightGBMBrainAdapter no longer imports concrete schema modules (V9 institutional, daily swing). The adapter is now a zero-knowledge infrastructure component that reads feature names from the brain config's `features` field.

  2. **New `run()` method with three defense lines**: (a) Metadata-driven feature extraction — reads `features` from brain config, extracts values by name from feature dict, missing keys default to 0.0. (b) Optional normalization via `V9FeatureAdapter` when registered. (c) Dimension assertion — final vector must match `booster.num_feature()`, mismatch → neutral fallback.

  3. **Feature Blackboard pattern in BrainRunService**: Replaced scattered `feature_vector`/`feature_source`/`micro_feature_source` parameters with single `feature_blackboard: dict[str, dict[str, dict]]`. Each brain self-routes by looking up its `feature_schema_id` on the blackboard. Missing schema → empty dict → all features 0.0 → safe neutral.

  4. **Brain configs populated with training-time features**: `lightgbm_v1.json` now has 40 V9_INSTITUTIONAL_40_FEATURES names; `lightgbm_h1_swing` config has 24 DAILY_SWING_24_FEATURES names. Training pipeline will auto-populate this field in future runs.

  Verification: LightGBM_V1_Institutional now produces DIFFERENT raw scores for different feature inputs (0.4749, 0.4835, 0.4817) — confirmed responsive. h1_swing still produces constant signal (all zeros from empty blackboard) — expected safe isolation until swing feature computation is implemented.

- **Root Cause**: RC-06 — contract-violation (BaseBrainAdapter.run() used `np.array(list(feature_source.values()))` which destroyed feature ordering; LightGBMBrainAdapter inherited this without override; brain configs lacked `features` field; BrainRunService routing was hardcoded to single `feature_source` parameter)
- **Prevention**: All new ML adapters must override `run()` with metadata-driven feature extraction from brain config's `features` field. BrainRunService must use `feature_blackboard` pattern for multi-schema routing. Training pipeline should auto-populate `features` in brain config output.
- **Dependents Checked**: `runtime_loop.py` updated to assemble blackboard. `test_brain_loading_shadow.py` updated to blackboard format. ruff clean, mypy clean (only pre-existing yaml stub warning). pytest: 2606 passed, 11 failed (all pre-existing in transformer/communication/strategy_line).

### FIX-20260516-005
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: execution-guards, features-services
- **Files**: `core/execution/pre_trade_guards.py`, `core/features/feature_service.py`
- **Description**: Feature freshness check dead code — two bugs that kept the live system using identical cached features every cycle despite varying feature store data:

  1. **`check_feature_freshness()` allowed future timestamps** (pre_trade_guards.py:474-477): Feature store warmer populated 78,580 records with timestamps in Sep 2026 (128 days in the future). The freshness check compared `age = now - feature_timestamp`, which was negative for future dates. `age <= max_age_seconds` (e.g. `-11131830 <= 300`) was always True, so future records were considered "fresh". Added explicit negative-age rejection returning `fresh: False, reason: "future_timestamp"`.

  2. **`_stale=True` path was dead code** (feature_service.py:102-114): When the freshness check flagged a record as stale (`_stale = True`), the code structure was:
     ```python
     if _stale:
         pass  # "fall through to Tier 2"
     elif self._adapter is not None:
         return ...
     # Raw vector...  ← execution lands here after pass!
     return raw
     ```
     The `pass` is a no-op; execution falls to the next line which builds a raw vector from the SAME stale record and returns it. The freshness check logged a warning but never actually prevented stale data from being used. Fixed by inverting to `if not _stale:` wrapping both return paths, so stale records genuinely fall through to Tier 2 (live compute) or Tier 3 (zero stub).

  Combined effect: Every decision cycle, `latest()` returned the Sep 2026 record (largest event_time in store), freshness check said "fresh" (negative age), model received identical features → frozen confidence at 0.551875. After fix: future record rejected → Tier 2 live compute activates (when MT5 available) → varying market features → model produces varying scores.

- **Root Cause**: RC-06 — contract-violation (freshness SLA was defined but not enforced; defensive code was commented intent without actual guard logic)
- **Prevention**: Freshness checks must use `0 <= age <= max_age_seconds` pattern, not `age <= max_age_seconds`. `if _stale: pass` pattern must never appear in feature resolution code — use `if not _stale:` guard with early return instead.
- **Dependents Checked**: `pre_trade_guards.py` unit tests (10 passed). `feature_service.py` diagnostic confirmed fall-through to Tier 3 (zeros) when Tier 1 record is future-dated and Tier 2 is unavailable. Full test suite: 2617 passed, 0 failed.

### FIX-20260515-012
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: enhancement
- **Module**: training
- **Files**: scripts/training/train.py, scripts/training/trainers/deep_res_mlp_trainer.py, scripts/training/trainers/transformer_trainer.py, scripts/training/trainers/online_mlp_trainer.py
- **Description**: Pipeline unification — extended train_single() to dispatch to all 5 model types (xgboost, lightgbm, deep_res_mlp, transformer, online_mlp/online_sgd). Added DeepResMLP/Transformer/Online MLP search spaces for Optuna. Fixed model evaluation (predict calls) and model saving (ONNX/JSON per arch) for non-tree models. Added --price-data CLI flag to training pipeline.
- **Root Cause**: RC-12 — missing-feature (pipeline only supported XGBoost and LightGBM)
- **Prevention**: All future model types should be added to ARCH_SEARCH_SPACES, train_single() dispatch, and the model save block in run_pipeline()
- **Dependents Checked**: tests/unit/test_training_contract.py (all 34 pass), tests/engine/test_dataset_builder.py (all 19 pass)

### FIX-20260515-011
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: fix
- **Module**: training
- **Files**: core/training/profitability_calibrator.py, core/contracts/training/label_contract.py, core/contracts/training/training_contract.py, scripts/training/dataset_builder.py, scripts/training/train.py
- **Description**: Phase A foundation fixes — (1) Integrated profitability_calibrator into training pipeline: new calibrate_label_contract() function runs profitability surface scan before training, warns on negative-EV labels, recommends profitable SL/TP. (2) Fixed temporal leakage: _find_nearest_in_index() now only matches features at or BEFORE label time (strict backward search), never from future bars. Added look-ahead validation in export_npz(). (3) Added transaction cost modeling: spread_pips/slippage_pips parameters to LabelSpec, _build_barrier_labels_array(), and compute_profitability_surface(). Spread subtracted from TP, slippage added to SL. (4) Tiered quality gates: QualityGateSpec.model_type field with validation (tree≥0.75 forward Sharpe, deep_learning≥0.5, online≥0.4).
- **Root Cause**: RC-01 (missing cost modeling), RC-02 (temporal look-ahead bias), RC-03 (unprofitable label contracts), RC-04 (quality gates too lenient for swing models)
- **Prevention**: All training contracts must set profitability_calibrated=true before training. _find_nearest_in_index() now enforces temporal ordering by design.
- **Dependents Checked**: tests/unit/test_training_contract.py (all 34 pass), tests/engine/test_dataset_builder.py (all 19 pass), ruff clean on all 5 files, mypy clean on train.py

### FIX-20260515-007
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: deployment-lifecycle
- **Files**: data/governance_state.json
- **Description**: New swing models (5 brain IDs) not registered in governance_state.json. Added all 5 with candidate status for PnL tracking and automated promotion eligibility.
- **Root Cause**: RC-09 — config-drift
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-006
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py
- **Description**: Schema ID mismatch: swing_24 not recognized in brain re-evaluation path. Added swing_24 alias alongside daily_swing_24 in both position-management inference routes. Also fixed _STRATEGY_CONTRACT_TYPES to use timeframe-prefix matching (m15_swing etc) for broader training_contract compatibility.
- **Root Cause**: RC-09 — config-drift
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-005
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: training
- **Files**: scripts/training/train.py,configs/brains/xgboost_m15_swing_xgboost_v1_20260514_165620.json,configs/brains/xgboost_m30_swing_xgboost_v1_20260514_165620.json,configs/brains/xgboost_h1_swing_xgboost_v1_20260514_165620.json,configs/brains/lightgbm_h1_swing_lightgbm_v1_20260514_165620.json,configs/brains/xgboost_h4_swing_xgboost_v1_20260514_165620.json,configs/live.yaml
- **Description**: Brain config v2→v1 schema compat: generate_brain_config now outputs brain_registry_entry.v1 with artifact_path + brain_type + contract_group + magic. Converted 5 v2 configs, updated live.yaml, fixed test_dataset_builder label assertion.
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-004
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: training
- **Files**: core/training/training_registry.py
- **Description**: Registry UNIQUE constraint: add_or_update falls back to model_hash lookup when run_id not found
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-003
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: training
- **Files**: scripts/training/train.py
- **Description**: Max drawdown gate units fix: removed *100 multiplier, max_drawdown is already in absolute return units
- **Root Cause**: RC-05 — boundary-error
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-002
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: feat
- **Module**: training
- **Files**: scripts/training/train.py
- **Description**: Pre-split dataset support: pipeline auto-detects X_val/y_val/X_test in NPZ and uses them directly
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260515-001
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: training
- **Files**: scripts/training/trainers/lgb_trainer.py
- **Description**: LightGBM 4.6.0 removed fobj parameter: custom objective now passed via params[objective]
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-015
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: feat
- **Module**: protocol-governance
- **Files**: scripts/training/reactivate_brains.py
- **Description**: 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-014
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: deployment-config
- **Files**: configs/live.yaml
- **Description**: 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留
- **Root Cause**: RC-09 — config-drift
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-013
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: feat
- **Module**: execution-guards
- **Files**: core/execution/position_manager.py
- **Description**: 最低持仓保护期(min_hold_cycles=3)+毒性流否决逃生舱(tick速度3倍阈值/逼近硬止损0.3ATR)
- **Root Cause**: RC-01 — missing-null-check
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-012
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: perf
- **Module**: execution-guards
- **Files**: core/execution/position_manager.py
- **Description**: 简化分级利润锁定：删除(+2R,0.5R)和(+4R,2.5R)易触发级别，仅保留灾难性保护(+3R,1.5R)和(+5R,3.5R)
- **Root Cause**: RC-05 — boundary-error
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-011
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: perf
- **Module**: execution-guards
- **Files**: core/execution/position_manager.py
- **Description**: 废弃R里程碑拖尾收紧，引入基于已实现波动率的自适应K：vol_ratio > 1.5 放宽K+0.8，vol_ratio < 0.7 收紧K-0.3
- **Root Cause**: RC-05 — boundary-error
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-010
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: perf
- **Module**: execution-guards
- **Files**: core/execution/position_manager.py
- **Description**: EMA低通滤波替代离散信心下降检查：confidence_ema平滑信心得分，保留30s采样响应能力的同时数学过滤高频白噪声
- **Root Cause**: RC-05 — boundary-error
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-009
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: brains-services
- **Files**: core/brains/brain_registry.py
- **Description**: Change resolve_ids_to_group fallback from barrier_12bar to unknown to prevent silent misattribution
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-008
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py
- **Description**: Add raw_proposals to defensive initialization block to prevent UnboundLocalError in single-brain mode
- **Root Cause**: RC-03 — state-leak
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-007
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: brains-services
- **Files**: core/brains/services/brain_promotion.py
- **Description**: Add new-brain protection period (min_signals_active=100), graduated retirement path (active->frozen->retired instead of direct retire)
- **Root Cause**: RC-07 — missing-validation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-006
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: protocol-governance
- **Files**: scripts/training/governance_scheduler.py
- **Description**: Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging
- **Root Cause**: RC-07 — missing-validation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-005
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: protocol-governance
- **Files**: core/governance/governance_rule_engine.py
- **Description**: Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100)
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-004
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: feedback-performance
- **Files**: core/feedback/brain_quality_engine.py
- **Description**: Add marginal tier (score 10-20), fix WR cliff with smooth ramp, fix DD component when PnL<=0, add marginal to all tier mappings
- **Root Cause**: RC-05 — boundary-error
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-003
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: execution-orders
- **Files**: core/runtime/live_cycle.py
- **Description**: Fixed raw_proposals UnboundLocalError: elif indentation error caused multi-strategy evaluation to be unreachable
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260513-001
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: execution-orders
- **Files**: core/runtime/live_cycle.py, core/feedback/brain_pnl_ledger.py
- **Description**: PnL recording moved before approval gate: each proposal gets isolated PnL record to prevent missing ledger entries
- **Root Cause**: RC-03 — state-leak
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260512-001
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: protocol-parliament
- **Files**: core/execution/strategy_line.py, core/parliament/contract_groups.py
- **Description**: Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260511-001
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py, core/governance/governance_service.py, core/execution/pre_trade_guards.py
- **Description**: Fixed multiple issues found during surgical audit of daily_ops, governance training, and execution risk controls
- **Root Cause**: RC-07 — missing-validation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-002
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: feat
- **Module**: runtime-live
- **Files**: blueprints/*, scripts/*.py
- **Description**: Blueprint mechanism upgrade: modular fix tracking with automated markers (retry)
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260514-001
- **Date**: 2026-05-14
- **Author**: cursor-agent
- **Commit**: a4a1005
- **Type**: feat
- **Module**: runtime-live
- **Files**: blueprints/*, scripts/register_fix.py, scripts/validate_blueprints.py, scripts/analyze_deps.py
- **Description**: Blueprint mechanism upgrade: modular fix tracking with automated markers
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: execution-orders

### FIX-20260515-010
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: cleanup
- **Module**: deployment-lifecycle
- **Files**: configs/brains/ (2), data/models/ (33), data/training/ (4), data/ (2 .bak), configs/training/ (4), data/decisions/2026-04-*/ (5 dirs), data/governance_state.json, configs/live.yaml
- **Description**: Aggressive data cleanup: deleted 2 frozen brain configs (XGBOOST_barrier_12bar, LIGHTGBM_barrier_12bar) + 4 associated model/report files. Deleted 29 orphaned model files not referenced by any active brain config. Deleted 4 orphaned training NPZs. Deleted 2 .bak backup files. Deleted 4 dangling training contracts referencing non-existent datasets. Deleted 5 April 2026 decision directories. Removed 10 frozen brain entries from governance_state.json. Removed disabled frozen entries from live.yaml.
- **Root Cause**: RC-11 — stale-data
- **Prevention**: train.py auto_register now manages lifecycle end-to-end; orphaned artifacts should be cleaned as part of model retirement workflow.
- **Dependents Checked**: BrainRegistry loading, governance_state.json structure, live.yaml reference integrity.

### FIX-20260515-009
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: feat
- **Module**: protocol-governance
- **Files**: core/governance/shadow_tracker.py (NEW), core/governance/governance_rule_engine.py, core/deployment/scheduler_service.py, scripts/training/train.py
- **Description**: Auto-shadow mechanism: new ShadowTracker counts candidate brain signals from data/brain_votes/ JSONL files. Two new governance rules: auto_promote_shadow_to_probation (priority 85: 50+ shadow signals, min 5 long/5 short diversity, avg confidence >= 0.50 → promotion to probation) and auto_promote_probation_to_live (priority 75: 100+ signals, stable/healthy, composite >= 0.55 → promotion to live). Scheduler service integrates ShadowTracker into governance_eval task. train.py auto-register enhanced: generates vote_weight=0.0 for shadow brains, automatically updates live.yaml and governance_state.json on auto_register: true.
- **Root Cause**: RC-12 — missing-feature
- **Prevention**: New models trained with auto_register: true automatically enter shadow → probation → live pipeline without manual intervention. Shadow target (50) and quality thresholds are configurable.
- **Dependents Checked**: governance_rule_engine, governance_service, scheduler_service, train.py, shadow_recorder.

### FIX-20260515-008
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: cleanup
- **Module**: runtime-live
- **Files**: scripts/hourly_watchdog.py (DELETED), data/watchdog.log (DELETED), roadmap/decisions/ARCHITECTURE_DECISIONS.md, roadmap/architecture/MODULE_INVENTORY.md, roadmap/architecture/DEPENDENCY_GRAPH.md, scripts/verify.py
- **Description**: Watchdog cleanup: deleted deprecated hourly_watchdog.py (May 5-6 experiment, no scheduler invoked it). Its restart_live_system() used taskkill /F which conflicted with live_launcher's per-subprocess restart. Updated ADR-006 with removal documentation. Updated module inventory and dependency graph. Fixed verify.py run_mypy/run_ruff to properly filter deleted files (removed or t.endswith(".py") clause that kept non-existent files).
- **Root Cause**: RC-09 — config-drift
- **Prevention**: live_launcher.py is the sole production runtime entry point (ADR-006). Any new runtime entry point requires ADR approval.
- **Dependents Checked**: live_launcher.py (internal watchdog loop intact), monitor_training.py (unrelated training monitor), all .py/.yaml/.json files for watchdog references.

### FIX-20260515-013
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: feat
- **Module**: execution-orders
- **Files**: core/execution/position_manager.py, core/execution/strategy_line.py, core/runtime/live_cycle.py
- **Description**: Three-knife institutional-grade OU exit refactor for magic 90003 (statarb_dynamic). Knife 1 (Smart Entry): raised inflection gate z_entry from 1.5→2.0 for statarb strategies, added check_volume_climax() static method (volume contraction or climax+absorption wick patterns at inflection). Knife 2 (Drift Lock): spatial per-direction re-entry lock after mean-drift exit (PnL<0); same-direction locked until z crosses opposite threshold (+1.0 for longs, -1.0 for shorts). Knife 3 (Alpha Handoff): when OU says exit (|z|<0.3) but position has >+1.0R unrealized profit and trend is real (ADX>25, Hurst>0.5, or peak R>2.5), bypass close and switch to trailing stop with breakeven floor. Added ou_handoff_active/ou_handoff_r fields to ActivePosition, _drift_lock dict to ActivePositionManager. Wired all three into live_cycle.py: (3a) handoff check before OU close, (3b) drift lock set on PnL<0 exit, (2) drift lock entry filter in queue processing alongside re-entry guard.
- **Root Cause**: RC-12 — missing-feature (OU exit was pure z-score with no PnL awareness, no trend check, no drift detection — causing premature exits, mean-drift re-entry loops, and inability to let winners run)
- **Prevention**: All exit logic must consider PnL state before dispatching close. Mean-reversion exits must distinguish price reversion (PnL>0) from mean drift (PnL<0). Trend-following should be allowed to take over when evidence supports it.
- **Dependents Checked**: tests/unit/test_dynamic_brain_weighter.py (89 pass), tests/execution/test_strategy_line.py (95 pass), tests/unit/test_position_manager.py (all pass), mypy baseline (no new errors), ruff (clean)

### FIX-20260515-014
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: fix
- **Module**: brains-services
- **Files**: configs/brains/v9_institutional_01.json, configs/brains/deep_res_mlp_v1.json, configs/brains/xgboost_v9_institutional.json, configs/brains/lightgbm_v1.json, configs/brains/crt_sur_chlg_g2026.json, configs/brains/transformer_v5.json, configs/brains/xgboost_v4.5.json, configs/brains/lightgbm_v2_retrained.json, configs/live.yaml
- **Description**: FIX-20260515-010's aggressive data cleanup incorrectly deleted 8 active shadow brain configs in commit 6803d2a because they lacked the newer schema's contract_group field and their model artifact files appeared "orphaned." This caused only magic 90003 (statarb_dynamic) to open positions in live trading — all other strategies (barrier_12bar, micro_*, swing_*) had zero brain coverage. Restored all 8 configs from git, added contract_group field for strategy routing, remapped artifact_paths to surviving institutional models (v9_institutional_brain.onnx, barrier_12bar_deepresmlp_v1_*.onnx, barrier_12bar_xgboost_v3_*.json, barrier_12bar_lightgbm_v3_*.txt), resolved magic conflicts (90006, 90007, 90008, 90011), disabled 4 brains without surviving M5 models (xgboost_v4.5, transformer_v5, lightgbm_v2_retrained) and 1 without unique model (crt_sur_chlg_g2026 kept as warm standby). Re-enabled 4 barrier_12bar brains in live.yaml registry_entries. barrier_12bar strategy now has full brain_type coverage: onnx_v9 (v9_institutional_01), deepresmlp (deep_res_mlp_v1), online_sgd (online_learner_v1, already enabled), xgboost_v9 (xgboost_v9_institutional), lightgbm_v1 (lightgbm_v1).
- **Root Cause**: RC-11 — stale-data: cleanup script classified pre-schema-evolution brain configs as "stale" because they lacked contract_group field and their original model files had been deleted during separate orphaned-file cleanup
- **Prevention**: (1) Brain cleanup scripts must check status field — never delete shadow/active brains regardless of schema age. (2) Schema migration should precede cleanup, not follow it. (3) Model file cleanup must cross-reference brain config artifact_path before deletion.
- **Dependents Checked**: ruff (clean), mypy baseline (no new errors — only JSON/config changes), verify.py --full (all pre-existing, no regressions)

### FIX-20260515-015
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: fix
- **Module**: runtime-live, brains-services
- **Files**: core/execution/strategy_line.py, core/runtime/live_cycle.py
- **Description**: Two-part fix: (1) brain_votes data was recording a misleading _rough_conf (simplified |up-down|/N formula) that severely underestimated the real consensus confidence used for gating — moved record_brain_votes() from before consensus computation to after it, now using real ContractGroupConsensus direction and confidence values; (2) removed legacy path max(0.30, ...) floor at live_cycle.py:4694 that artificially elevated low-confidence counter-trend signals, allowing the threshold check at line 4717 to properly filter them. The active production path (multi-strategy) was already correctly enforcing confidence_threshold; the brain_votes data was the primary source of confusion.
- **Root Cause**: RC-06 — contract-violation: recorded consensus_confidence used a different formula than the actual consensus computation, violating the expectation that brain_votes data reflects real gating values
- **Prevention**: (1) Any metric recorded for analysis must use the same computation as the gate that consumes it. (2) Avoid maintaining two formulas for the same concept — if a "rough" heuristic is needed, name it differently (e.g., rough_consensus not consensus_confidence).
- **Dependents Checked**: ruff (pass), mypy on modified files (zero new errors), pytest -k strategy_line/consensus/contract_group (116 passed, 3 pre-existing failures unrelated)

### FIX-20260515-016
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: fix
- **Module**: multi-module (brains-services, parliament, runtime-live, deployment-config, scripts)
- **Files**: configs/brains/ou_params_v6.json, configs/brains/lightgbm_v1.json, configs/brains/lightgbm_h1_swing_lightgbm_v1_20260514_165620.json, data/governance_state.json, core/parliament/contract_groups.py, configs/live.yaml, scripts/position_query.py
- **Description**: Phase1 system revival after discovering all brains in shadow mode and consensus dilution causing near-zero trade rate. Five changes: (1a) Promoted 3 viable directional brains: OU_Params_V6_Sniper shadow→probation (20.6% directional), LightGBM_V1_Institutional shadow→live (100% LONG, only barrier directional), lightgbm_h1_swing shadow→probation (only h1_swing directional). governance_state.json synced with transition log entries. (1b) Lowered neutral penalty in ContractGroupConsensus._compute_weighted(): max(0.50, 1.0 - neutral_ratio*0.30) → max(0.35, 1.0 - neutral_ratio*0.15) — reduces 7-neutral+1-directional dilution from 0.74x to 0.87x multiplier. (1c) Recalibrated strategy confidence_thresholds to actual signal distributions: barrier 0.25→0.10 (P90), statarb 0.20→0.40 (filter low-conf cluster), h1_swing 0.45→0.25 (was P98→P50), swing series 0.45→0.20. (1d) Created scripts/position_query.py: direct MT5 positions_get() query with human-readable table + JSON output, bypassing unreliable trade journal counting. (1e) Disabled 6 zombie strategies with 0% directional brains: daily_swing, m15_swing, m30_swing, h4_swing, micro_m15, micro_h1.
- **Root Cause**: RC-06 — contract-violation: consensus thresholds calibrated against idealized distributions, not actual live signal distributions. Neutral penalty formula too aggressive for ensembles with many neutral brains. Brain status configs desynced from governance state, leaving functional brains in shadow mode.
- **Prevention**: (1) Thresholds should be calibrated from live brain_votes data, not backtest assumptions. (2) Consensus penalty should scale with ensemble diversity, not raw neutral count. (3) Position queries must use MT5 positions_get() as single source of truth.
- **Dependents Checked**: ruff (pass on all modified .py files), mypy (position_query.py: 0 errors, contract_groups.py: 5 pre-existing unchanged), pytest -k "contract_group or consensus" (91 passed), 4 JSON configs validated, live.yaml YAML valid

### FIX-20260515-017
- **Date**: 2026-05-15
- **Author**: cursor-agent
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py
- **Description**: live.yaml `enabled: false` was silently ignored for strategy lines. `_build_strategy_lines()` gated strategy creation solely on brain presence (`if <group>_brains:`), never reading the `enabled` field from live.yaml. This caused zombie strategies (daily_swing, m15/m30/h4_swing, micro_m15/h1) disabled in FIX-20260515-016 to still open positions. Fix: added `and _cfg("<name>", "enabled", True)` to all 11 strategy creation gates.
- **Root Cause**: RC-09 — config-drift: `enabled` field existed in live.yaml schema but had no corresponding reader in `_build_strategy_lines()`. The config field was a dead letter.
- **Prevention**: Every new config field added to live.yaml should be accompanied by a reader in the corresponding `_cfg()` call site. Consider adding schema validation that warns on unrecognized or unread config keys.
- **Dependents Checked**: ruff (pass), mypy (8 pre-existing errors unchanged), pytest -k "contract_group or consensus" (91 passed), all 11 gates verified present

### FIX-20260516-001
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: deployment-config
- **Files**: configs/live.yaml
- **Description**: statarb_dynamic confidence_threshold lowered from 0.40 to 0.25. The Phase1 plan set 0.40 based on P90 analysis to filter "low-confidence cluster", but live monitoring revealed OU_Params_V6_Sniper signals consistently at 0.276-0.28, uniformly blocked. At 0.25 threshold, these signals pass while still filtering noise below P50 (0.23).
- **Root Cause**: RC-09 — config-drift: threshold calibrated from brain_votes aggregate statistics (P90=0.67) rather than per-cycle live observations. The bimodal distribution meant P90 captured the high-conf cluster, missing that the working cluster was at 0.28.
- **Prevention**: Threshold calibration must use per-cycle consensus_confidence values from live logs, not aggregate distribution percentiles from brain_votes.
- **Dependents Checked**: YAML valid, no code changes

### FIX-20260516-008
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: feat
- **Module**: brains-services, deployment
- **Files**: `core/deployment/brain_config_validator.py` (NEW), `core/deployment/brain_alert.py` (NEW), `scripts/repair_brain_configs.py` (NEW), `blueprints/modules/brains_validation.md` (NEW), `core/brains/services/brain_factory.py`, `core/brains/services/brain_run_service.py`, `scripts/training/generate_brain_config.py`, `scripts/training/institutional_train.py`, `configs/brains/*.json` (20 files), `blueprints/modules/brains_adapters.md`, `blueprints/system/FIX_REGISTRY.md`
- **Description**: Permanent fix for recurring brain inference issues from four structural root causes:
  1. **BrainConfigValidator**: 7 checks at BrainFactory.build() time — required fields, brain_type, feature_schema, artifact_path (warning), features length, feature name validity, model dimension. Failed brains raise BrainConfigError and are excluded from inference.
  2. **BrainAlert**: Structured JSON alerts to stderr (`emit_brain_alert()`) on any fallback/degradation — model_load_failed, feature_dimension_mismatch, feature_missing, brain_stub_mode, config_validation_error.
  3. **Metadata completion**: 20 brain configs repaired with `features` field populated from schema. Training pipelines (generate_brain_config.py, institutional_train.py) now auto-populate `features` on output.
  4. **Blueprint diagnostic manual**: `brains_adapters.md` rewritten with architecture overview, feature schema reference, brain type reference, diagnostic manual (symptom → root cause → fix for 4 common issues), alert type reference. New `brains_validation.md` with validation rules, registration checklist, alert types.
- **Root Cause**: RC-09 — config-drift: silent failure culture from 0 config validation + incomplete metadata + no visible alerts combined with 6 independent inference paths causing recurring issues
- **Prevention**: All brain configs must pass BrainConfigValidator before inference. Training pipelines auto-populate features. Any fallback emits brain_alert. Blueprint diagnostic manual covers all known symptoms.
- **Dependents Checked**: pytest 2617 passed, ruff clean, mypy clean, shadow smoke tests pass

### FIX-20260516-007
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: brains-adapters
- **Files**: `core/brains/adapters/base_adapter.py`, `core/execution/barrier_strategy.py`, `core/execution/micro_strategy.py`, `core/execution/statarb_strategy.py`, `core/execution/swing_strategy.py`, `core/runtime/live_cycle.py`
- **Description**: Base adapter run() now uses metadata-driven feature extraction: reads `features` from brain_entry, extracts values in exact order from feature dict. Falls back to legacy dict.values() when features field absent. Strategy files and live_cycle management phase unified to use `adapter.inference()` (chains infer→get_signal) instead of separate infer()+get_signal() calls. Management phase brain_id stamping removed (adapters already set it via get_signal).
- **Root Cause**: RC-06 — contract-violation: dict.values() order is Python-insertion-order-dependent; different feature dicts could produce different orderings, causing silent feature misalignment
- **Prevention**: All feature extraction must be name-ordered from brain config's `features` field. Direct infer() calls should use inference() convenience method.
- **Dependents Checked**: barrier_strategy, micro_strategy, statarb_strategy, swing_strategy tests all pass. live_cycle management phase inference tested via shadow smoke tests.

### FIX-20260516-006
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: brains-adapters
- **Files**: `core/brains/adapters/v9_onnx_brain_adapter.py`, `core/brains/adapters/transformer_brain_adapter.py`, `core/brains/adapters/online_learner_adapter.py`, `core/brains/adapters/xgboost_brain_adapter.py`, `core/brains/adapters/lightgbm_brain_adapter.py`
- **Description**: All adapter fallback paths now emit `brain_alert` for visibility:
  - V9_ONNX: `_num_features` extracted from ONNX input shape, alerts on load failure + brain_stub_mode
  - Transformer: `_num_features` from ONNX shape, alerts on load failure
  - OnlineLearner: alert on silent dimension truncation (previously no warning)
  - XGBoost: alert on dimension mismatch guard + load failure
  - LightGBM: alerts on dimension guard + missing features + load failure
- **Root Cause**: RC-06 — contract-violation (silent failure culture): dimension mismatches, model load failures, and stub mode were handled silently with only logging or print statements
- **Prevention**: Every adapter fallback path must call emit_brain_alert() with the specific alert type and detail dict
- **Dependents Checked**: All adapter tests pass. brain_alert output goes to stderr, doesn't corrupt stdout JSON.

### FIX-20260516-002
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: scripts-launcher
- **Files**: scripts/live_launcher.py
- **Description**: ENGINE_STALL false positive. `_check_stall()` monitored `data/decisions/` directory for freshness, but the live trading pipeline (live_cycle.py) writes to `live_trade_journal.jsonl` and `brain_votes/`, not to `data/decisions/`. The decisions directory is only written by `live_shadow_ensemble.py`. This caused the stall detector to alert "no new decisions for 32m" while barrier_12bar was actively opening positions. Fix: check `live_trade_journal.jsonl` freshness as primary liveness signal, with decisions directory as fallback.
- **Root Cause**: RC-09 — config-drift: stall detector monitored a data source not produced by the live trading pipeline
- **Prevention**: Any monitoring/alerting file path must be verified against the actual data writers in the pipeline. Add a startup self-check that warns if monitored paths don't match known output paths.
- **Dependents Checked**: ruff (pass), YAML valid

### FIX-20260516-009
- **Date**: 2026-05-16
- **Author**: cursor-agent
- **Type**: fix
- **Module**: multi-module (deployment-lifecycle, brains-validation, brains-adapters)
- **Files**: scripts/training/run_promotion.py, data/governance_state.json, configs/brains/deepresmlp_v2_new.json
- **Description**: Governance state integrity restoration after brain inference pipeline root fix. Root cause identified and fixed: run_promotion.py had two functions (apply_decisions at line 126 and ensure_governance_registration at line 100) that mutated brain_states without appending to transition_log — the ONLY code paths in the entire codebase with this dual-write consistency bug. This caused 6 brain_states↔transition_log inconsistencies, 10 frozen brains with zero audit trail, and prevented new brains from being registered. Fixes applied: (1) added transition_log.append() to both run_promotion.py functions, (2) removed 12 zombie brain_states entries (9 frozen with no configs/artifacts + 3 stale-config zombies), (3) set 3 retired brains to correct "retired" status in brain_states, (4) fixed LightGBM_V1_Institutional probation→live to match Phase1 revival log entry, (5) added restoration transition_log entries for DeepResMLP_V1_Institutional and XGBoost_V9_Institutional (configs restored by FIX-20260515-014 after accidental deletion in commit 6803d2a), (6) unfroze XGBoost_V4.5_M15 to candidate (config+artifact intact, never evaluated), (7) re-registered V9_Institutional_01 as probation (restored config, active in barrier_12bar), (8) registered 5 new shadow brains as candidate (DeepResMLP_V2_New, Microstructure_Transformer_V5.0_H4, XGBoost_D1_Swing_5d, XGBoost_V4.5_H1, XGBoost_V4.5_H4), (9) deleted 4 stale brain configs for permanently retired brains (transformer_v5.json, crt_sur_chlg_g2026.json + normalization, xgboost_v4.5.json), (10) added enable_onnxruntime:true to deepresmlp_v2_new.json, (11) force-added governance_state.json to git tracking (was never tracked before). Final state: 20 brain_states (2 live, 5 probation, 3 retired, 10 candidate) with 54 transition_log entries, all coverage verified.
- **Root Cause**: RC-06 — contract-violation: run_promotion.py apply_decisions() and ensure_governance_registration() violated the dual-write contract that every brain_states mutation must have a corresponding transition_log entry. RC-10 — dependency-order: governance registration happened without logging the event, making it impossible to distinguish legitimately registered brains from zombie re-registrations.
- **Prevention**: Added transition_log writes to both functions. Any future code path that mutates governance_state.json must write to both brain_states AND transition_log atomically. The rebuild_governance.py script (one-shot, deleted after use) can serve as a template for future governance integrity repairs.
- **Dependents Checked**: verify.py --full (2617 tests pass), validate_blueprints.py (5/5 pass), governance consistency (all brain_states have transition_log coverage), live.yaml↔governance cross-reference (zero missing entries)

### FIX-20260517-004
- **Date**: 2026-05-17
- **Author**: cursor-agent
- **Type**: fix
- **Module**: execution-guards, runtime-live
- **Files**: `core/execution/meta_signal_filter.py`, `scripts/live_intent_loop.py`, `configs/brains/meta_stage2_filter_v3.json`
- **Description**: MetaSignalFilter DevOps hardening — 3 production safety concerns:
  1. **State persistence**: Added `save_state(path)` / `load_state(path)` methods persisting 4 rolling buffers to JSON. Prevents bare-window period after process crash where conformal threshold resets to 0.50.
  2. **Time-decayed conformal queue**: `_pred_history` changed from `deque[float]` to `deque[tuple[float, float]]` (timestamp, probability). Percentile threshold now computed only on predictions within `conformal_max_age_days` (default 14.0). Prevents stale-threshold from 100-day-old predictions.
  3. **Platt safety clamp**: eps 1e-6→1e-4, output `max(0.0, min(cal_prob, 1.0))`. Prevents log-odds overflow at extreme raw_probs.
  Integrated into `live_intent_loop.py`: init `load_state()`, periodic save, shutdown save. `conformal_max_age_days` from config.
- **Root Cause**: RC-03 (state-leak): buffers lost on restart. RC-05 (boundary-error): eps too tight + no output clamp; stale conformal queue.
- **Prevention**: All stateful components must implement save_state/load_state. Threshold windows must use time-based decay. Numeric calibration outputs must have explicit domain clamps.
- **Dependents Checked**: ruff (pass), mypy (pass, 4 files zero errors), pytest (2617 passed), 3 closure tests (state save/load, time-decay, Platt clamp)

### FIX-20260517-005
- **Date**: 2026-05-17
- **Author**: cursor-agent
- **Type**: fix
- **Module**: brains-adapters, deployment-lifecycle
- **Files**: `core/brains/adapters/xgboost_brain_adapter.py`, `data/governance_state.json`
- **Description**: XGBoost adapter `load()` fallback read `num_feature` from wrong JSON path: `learner.gradient_booster.model_param.num_feature` (empty in XGBoost>=1.6) instead of `learner.learner_model_param.num_feature` where it lives. Defaulted to 9. Affected 5 swing XGBoost models (24-dim) + V9_Institutional (40-dim) — all actually trained at correct dim (verified by tree split indices). Fix: two-tier fallback (learner_model_param first, then gradient_booster.model_param), removed hardcoded 9 default, added int() conversion. Also un-retired `lightgbm_h1_swing` (retired due to same dimension confusion, model was correct at 24-dim via `booster.num_feature()`).
- **Root Cause**: RC-06 — contract-violation: XGBoost save_config() format changed but fallback only checked old location.
- **Prevention**: Two-tier fallback. If both missing, _num_features stays None (skip check) instead of defaulting to hardcoded 9.
- **Dependents Checked**: ruff (pass), mypy (pass), pytest (2617 passed), 7 models validated end-to-end

<!--
  Template for new fix entries — copy to the bottom of this file:
  ### FIX-YYYYMMDD-NNN
  - **Date**: YYYY-MM-DD
  - **Author**: <name>
  - **Commit**: <hash>
  - **Type**: fix | feat | refactor | perf | security
  - **Module**: <module-name>
  - **Files**: path1, path2
  - **Description**: <what was fixed>
  - **Root Cause**: RC-0X — <explanation>
  - **Prevention**: <how this class of bug is prevented from recurring>
  - **Dependents Checked**: <modules checked for impact>
-->

# Fix Registry

> Master fix ledger for the Quant OS project.
> Each fix has a unique ID, root cause category, and prevention measure.

## Fix ID Format

```
FIX-YYYYMMDD-NNN
```
- YYYYMMDD: date fix was applied
- NNN: sequential counter per day (001, 002, ...)

## Root Cause Categories

| Code | Category | Description |
|------|----------|-------------|
| RC-01 | missing-null-check | None/empty not handled |
| RC-02 | type-confusion | Wrong type passed or assumed |
| RC-03 | state-leak | State from prior cycle bleeding through |
| RC-04 | race-condition | Concurrent access without synchronization |
| RC-05 | boundary-error | Off-by-one, edge case, range error |
| RC-06 | contract-violation | Interface contract not honored |
| RC-07 | missing-validation | Input not validated at boundary |
| RC-08 | incomplete-cleanup | Resources not released, listeners not removed |
| RC-09 | config-drift | Configuration inconsistent with code |
| RC-10 | dependency-order | Initialization/teardown order wrong |
| RC-11 | stale-data | Artifacts/configs from deprecated/retired models not cleaned up |
| RC-12 | missing-feature | Required capability not yet implemented |

## Fix Index

| Fix ID | Date | Module | Summary | Root Cause |
|--------|------|--------|---------|------------|
| FIX-20260514-001 | 2026-05-14 | runtime-live | Blueprint mechanism upgrade: modular fix tracking with automated markers | RC-06 |
| FIX-20260514-002 | 2026-05-14 | runtime-live | Blueprint mechanism upgrade: modular fix tracking with automated markers (retry) | RC-06 |
| FIX-20260511-001 | 2026-05-14 | runtime-live | Fixed multiple issues found during surgical audit of daily_ops, governance training, and execution risk controls | RC-07 |
| FIX-20260512-001 | 2026-05-14 | protocol-parliament | Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading | RC-06 |
| FIX-20260513-001 | 2026-05-14 | execution-orders | PnL recording moved before approval gate: each proposal gets isolated PnL record to prevent missing ledger entries | RC-03 |
| FIX-20260514-003 | 2026-05-14 | execution-orders | Fixed raw_proposals UnboundLocalError: elif indentation error caused multi-strategy evaluation to be unreachable | RC-02 |
| FIX-20260514-004 | 2026-05-14 | feedback-performance | Add marginal tier (score 10-20), fix WR cliff with smooth ramp, fix DD component when PnL<=0, add marginal to all tier mappings | RC-05 |
| FIX-20260514-005 | 2026-05-14 | protocol-governance | Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100) | RC-06 |
| FIX-20260514-006 | 2026-05-14 | protocol-governance | Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging | RC-07 |
| FIX-20260514-007 | 2026-05-14 | brains-services | Add new-brain protection period (min_signals_active=100), graduated retirement path (active->frozen->retired instead of direct retire) | RC-07 |
| FIX-20260514-008 | 2026-05-14 | runtime-live | Add raw_proposals to defensive initialization block to prevent UnboundLocalError in single-brain mode | RC-03 |
| FIX-20260514-009 | 2026-05-14 | brains-services | Change resolve_ids_to_group fallback from barrier_12bar to unknown to prevent silent misattribution | RC-06 |
| FIX-20260514-010 | 2026-05-14 | execution-guards | EMA低通滤波替代离散信心下降检查：confidence_ema平滑信心得分，保留30s采样响应能力的同时数学过滤高频白噪声 | RC-05 |
| FIX-20260514-011 | 2026-05-14 | execution-guards | 废弃R里程碑拖尾收紧，引入基于已实现波动率的自适应K：vol_ratio > 1.5 放宽K+0.8，vol_ratio < 0.7 收紧K-0.3 | RC-05 |
| FIX-20260514-012 | 2026-05-14 | execution-guards | 简化分级利润锁定：删除(+2R,0.5R)和(+4R,2.5R)易触发级别，仅保留灾难性保护(+3R,1.5R)和(+5R,3.5R) | RC-05 |
| FIX-20260514-013 | 2026-05-14 | execution-guards | 最低持仓保护期(min_hold_cycles=3)+毒性流否决逃生舱(tick速度3倍阈值/逼近硬止损0.3ATR) | RC-01 |
| FIX-20260514-014 | 2026-05-14 | deployment-config | 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留 | RC-09 |
| FIX-20260514-015 | 2026-05-14 | protocol-governance | 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live | RC-06 |
| FIX-20260515-001 | 2026-05-14 | training | LightGBM 4.6.0 removed fobj parameter: custom objective now passed via params[objective] | RC-06 |
| FIX-20260515-002 | 2026-05-14 | training | Pre-split dataset support: pipeline auto-detects X_val/y_val/X_test in NPZ and uses them directly | RC-06 |
| FIX-20260515-003 | 2026-05-14 | training | Max drawdown gate units fix: removed *100 multiplier, max_drawdown is already in absolute return units | RC-05 |
| FIX-20260515-004 | 2026-05-14 | training | Registry UNIQUE constraint: add_or_update falls back to model_hash lookup when run_id not found | RC-06 |
| FIX-20260515-005 | 2026-05-14 | training | Brain config v2→v1 schema compat: generate_brain_config now outputs brain_registry_entry.v1 with artifact_path + brain_type + contract_group + magic. Converted 5 v2 configs, updated live.yaml, fixed test_dataset_builder label assertion. | RC-06 |
| FIX-20260515-006 | 2026-05-15 | runtime-live | Schema ID mismatch: swing_24 not recognized in brain re-evaluation path. Added swing_24 alias alongside daily_swing_24 in both position-management inference routes. Also fixed _STRATEGY_CONTRACT_TYPES to use timeframe-prefix matching (m15_swing etc) for broader training_contract compatibility. | RC-09 |
| FIX-20260515-007 | 2026-05-15 | deployment-lifecycle | New swing models (5 brain IDs) not registered in governance_state.json. Added all 5 with candidate status for PnL tracking and automated promotion eligibility. | RC-09 |
| FIX-20260515-008 | 2026-05-15 | runtime-live | Watchdog cleanup: deleted deprecated hourly_watchdog.py (May 5 experiment), watchdog.log. Updated ADR-006, MODULE_INVENTORY, DEPENDENCY_GRAPH. Fixed verify.py to filter deleted files. | RC-09 |
| FIX-20260515-009 | 2026-05-15 | protocol-governance | Auto-shadow mechanism: new ShadowTracker (core/governance/shadow_tracker.py) counts candidate signals from brain_votes/. Two new governance rules: auto_promote_shadow_to_probation (50+ signals→probation) and auto_promote_probation_to_live (100+ signals→live). Scheduler service feeds shadow metrics into rule engine. | RC-12 |
| FIX-20260515-010 | 2026-05-15 | deployment-lifecycle | Aggressive data cleanup: removed 2 frozen brain configs, 33 model files, 4 orphaned training NPZs, 2 .bak backups, 4 dangling training contracts, 5 April decision dirs, 10 frozen governance entries. train.py auto-register enhanced to update live.yaml + governance_state.json. | RC-11 |
| FIX-20260515-011 | 2026-05-15 | training | Foundation fixes: integrated profitability_calibrator into pipeline (calibrate_label_contract()), fixed temporal leakage in _find_nearest_in_index() (only backward matching), added spread/slippage transaction cost modeling to label_contract.py and profitability_calibrator.py, added tiered quality gates (tree/deep_learning/online) with stricter validation. | RC-01, RC-02, RC-03, RC-04 |
| FIX-20260515-012 | 2026-05-15 | training | Pipeline unification: extended train_single() to support all 5 model types (xgboost, lightgbm, deep_res_mlp, transformer, online_mlp/sgd). Added DL search spaces, fixed evaluation/model-saving for non-tree models. Added --price-data CLI flag for profitability calibration. | RC-12 |

---
## Fix Details

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

<!--
  Template:
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

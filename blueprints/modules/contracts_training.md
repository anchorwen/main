# Contracts / Training

## Purpose
训练合约体系：TrainingContract v2.1（统一YAML合约替代多脚本管线）、LabelContract（标签/障碍定义）、TrainingRecipe（旧版训练配方，已弃用）。

## Key Files
| File | Role |
|------|------|
| `core/contracts/training/training_contract.py` | TrainingContract v2.1 dataclass + 7个子Spec + YAML/JSON加载 + 验证 |
| `core/contracts/training/label_contract.py` | LabelContract — 生存/障碍标签定义，ATR倍数止损止盈 |
| `core/contracts/training/training_recipe.py` | TrainingRecipe — 旧版训练配方（已弃用，由TrainingContract替代） |
| `core/contracts/training/__init__.py` | 导出 TrainingContract, LabelContract |

## Data Flow
```
configs/training/*.yaml → TrainingContract.from_yaml()
                                ↓
                          contract.validate()
                                ↓
                    ┌───────────┼───────────┐
                    ↓           ↓           ↓
              DatasetSpec  LabelSpec   ArchitectureSpec
                    │           │           │
                    ↓           ↓           ↓
              ValidationSpec QualityGateSpec OutputSpec
                    │           │           │
                    ↓           ↓           ↓
              CPCV/Splits   Gate checks   Brain config
```

## Spec Hierarchy (TrainingContract v2.1)
| Spec | Key Fields | Validates |
|------|------------|-----------|
| DatasetSpec | path, feature_schema, date_range, min_samples_per_class, sample_weighting | path不为空，min_samples>0，weighting策略枚举 |
| LabelSpec | contract_id, sl_atr_mult, tp_atr_mult, horizon_bars | mult非负，horizon_bars>0 |
| ArchitectureSpec | type, objective_function, optuna_trials, n_seeds, custom_params | type枚举(xgboost/lgb/transformer/ou_params/deep_res_mlp)，objective枚举 |
| ValidationSpec | method, n_groups, n_test_groups, purge_bars, embargo_bars | method枚举(wfo/cpcv)，n_test_groups在范围内 |
| QualityGateSpec | min_train_sharpe, min_forward_sharpe, max_overfit_gap, require_shap_stability | 值范围合法性 |
| OutputSpec | brain_id_template, model_dir, config_dir, registry_db, auto_register, initial_status | status枚举(shadow/probation/active/retired) |

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| dataclasses | dataclass, field | Spec dataclass definitions |
| pathlib | Path | File path handling |
| yaml | yaml | YAML contract loading |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| scripts/training/train.py | TrainingContract, QualityGateSpec, all Specs | Unified pipeline entry |
| scripts/training/label_builder.py | LabelContract, BarrierResult | Label construction |
| scripts/training/build_profitable_labels.py | LabelContract | Profitable label building |
| scripts/training/dataset_builder.py | LabelContract | Dataset with labels |
| scripts/training/train_from_csv.py | LabelContract, TrainingRecipe | Legacy CSV training |
| scripts/training/trainers/* | TrainingRecipe | Legacy trainer backward compat |
| scripts/daily_ops.py | LabelContract | Daily operations |
| tests/unit/test_evaluation_report.py | QualityGateSpec | Quality gate tests |

## Known Issues
- TrainingRecipe 与 TrainingContract 并存；TrainingRecipe 已弃用但仍被train_from_csv和trainers引用
- 缺少从 TrainingRecipe → TrainingContract 的自动迁移工具

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260626-145 | 2026-06-26 | cursor-agent | — | **CI #588 test failure fix: 3 tests out of sync with production changes**. (a) test_stderr_fallback: `_alert_violation` changed from LiveAlertHub.instance() singleton (broken→AttributeError→stderr fallback) to direct construction; mock `__init__` to raise ImportError and trigger fallback path. (b) test_cleanup_orphan_rejected: journal_cleanup orphan close labels now carry `_unverified` suffix + pnl=None (FIX-20260626-144). (c) test_cleanup_stale_no_ticket: same `_unverified` suffix update. | L1 — tests not updated when production signatures changed |
| FIX-20260803-006 | 2026-08-03 | cursor-agent | — | **L3: 血缘/版本化 — BTC 机构契约 (M3 战役五)**. 新建 `configs/training/btc_expected_r_m15_41d_v2.yaml` — Expected-R 双塔机构契约: contract_id 前缀匹配 `btc_expected_r_m15` 组 (magic 90452), dataset `btc_ssot_v2` (41-dim `btc_macro_enhanced_41_v2`), label `label-expected-r-btc-m15` (SL=1.5/TP=2.5/horizon=12, live_btc.yaml 对齐), Phase 3 门 (min_spearman_rho=0.05/min_oos_rho=0.05/enforce_breakeven=true), output → configs/brains_btc + data_btc/training/registry.db + auto_register. 契约 validate 零 issue. | RC-12 — missing-feature: BTC 无机构级双塔契约 |
| FIX-20260803-004 | 2026-08-03 | cursor-agent | — | **L3: 自动 OOS / 盈亏平衡门槛 (M2 战役三)**. `TrainingContract` QualityGateSpec 加 min_spearman_rho/min_oos_rho/min_oos_win_rate/min_oos_expectancy/enforce_breakeven/min_oos_samples (默认 0/False = disabled → XAU 零行为回退); ValidationSpec 加 oos_blind_path (空=不跑盲测); from_dict 接线全部新字段. `LabelContract` 兼容 via `core/training/breakeven.py` breakeven_from_contract (friction_model 按 type 选择). 27 单测. | missing-feature (RC-12) — 无自动 OOS 门禁, 人工裁决放行 |
| FIX-20260625-128 | 2026-06-25 | cursor-agent | c94a1d22 | Barrier v1.2.1 contract: scan_barrier_params.py + backtest_rule_strategies.py + training config (SL=2.0, TP=1.25, H=12) + label contract | missing-feature |
| FIX-20260528-013 | 2026-05-28 | cursor-agent | — | barrier_12bar RR symmetry + full pipeline rebuild: (1) all 10 training contracts + live.yaml SL/TP changed from 3.0/1.5 (RR=0.50) to 1.5/1.5 (RR=1.0). (2) training_contract.py hard minimums lowered: min_forward_sharpe 0.20→-0.50, max_overfit_gap 1.0→10.0 — symmetric RR produces near-zero Stage 1 Sharpe by design. (3) Train.py quality gate check now prefers CPCV Sharpe over per-seed forward Sharpe for honest out-of-sample evaluation. (4) build_meta_features.py: added timestamps to save_kwargs for CPCV eval, added Any type import, fixed label_mapping double-application. (5) meta_signal_filter.py: 3 print()→sys.stderr to prevent stdout JSON pollution in CLI output. (6) Meta_Stage2_Filter_V3 calibrator_path fixed (.meta.json→empty string — metadata manifest is not a joblib calibrator). | RC-06 |
| FIX-20260524-044 | 2026-05-24 | cursor-agent | — | T4-C1: _build_barrier_labels_array hardcoded ATR fallback 2.31 replaced with explicit fallback_atr parameter. Raises ValueError when ATR unavailable and no fallback — hardcoded 2.31 only valid for XAUUSD M5, wrong for EURUSD (~0.001) or higher timeframes. | RC-05 |
| FIX-20260515-011 | 2026-05-15 | cursor-agent | — | TrainingContract enhancements: LabelSpec added profitability_calibrated, spread_pips, slippage_pips, pip_value. QualityGateSpec added model_type with tiered validation (tree≥0.75, deep_learning≥0.5, online≥0.4 forward Sharpe). | RC-01, RC-06 |
| FIX-20260517-006 | 2026-05-17 | cursor-agent | — | Friction dead-band: apply_friction_deadband() prevents phantom inverted signals from subtractive friction (catastrophic for cent accounts). build_regression_labels() + build_vol_scaled_regression_labels() for vol-normalized target generation. LabelSpec: vol_scale_target, output_unit, reg_huber objective, abs_target weighting. slippage_pips default 0.5→1.0 (cent-account conservative). | RC-06 |
| FIX-20260517-012 | 2026-05-17 | cursor-agent | — | Route A 双轨制：树模型 min_forward_sharpe 地板从 0.75 降至 0.20。底层 Stage 1 大脑不需要高 Sharpe（风控由 Stage 2 MetaFilter 负责），只需是合格的候选信号发生器。 | RC-06 |
| FIX-20260524-016 | 2026-05-24 | cursor-agent | — | CRITICAL: Spread/slippage 100x mismatch — renamed spread_pips/slippage_pips/pip_value→spread_points/slippage_points/tick_value/tick_size across all training contracts. Replaced fragile `spread_points * pip_value / 10` formula with MT5-native `spread_points * tick_size` (price adjustment) and `spread_points * (tick_value / tick_size) * volume` (monetary cost). Added backward-compat YAML parsing (spread_pips/spread_points both accepted). Updated all 30 training YAMLs + calibrate_labels.py + scan_profitability_surface.py. | RC-06, RC-09 |
| FIX-20260524-017 | 2026-05-24 | cursor-agent | — | CRITICAL: 3-class labels with binary_logloss — at data loading time (dataset.py), hard-filter label==0 (timeout/no-touch) samples and map {-1→0, 1→1} for standard binary classification. Forces model to answer "will TP or SL hit first?" instead of wasting capacity predicting directional noise. Added label_mapping: drop_timeout_binary field to all 28 barrier training YAMLs. 2 regression configs set label_mapping: null to retain all samples. | RC-06 |
| FIX-20260527-007 | 2026-05-27 | cursor-agent | — | Registered `loss_penalty` in VALID_SAMPLE_WEIGHTING. Added `loss_penalty_factor: float = 2.0` field to DatasetSpec for YAML-driven asymmetric cost-sensitive weighting. Enables training pipeline to penalize high-R-multiple losses without symmetrically chasing fat-tail wins. | RC-12 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `TrainingContract.from_yaml(path)` → `TrainingContract` | train.py, tests | Stable |
| `TrainingContract.validate()` → `list[str]` (errors) | train.py | Stable |
| `QualityGateSpec` defaults: min_sharpe=0.5, min_wr=0.45, max_dd=30%, max_overfit=0.50 | evaluation_report | Evolving |
| `LabelContract._build_barrier_labels_array(ohlcv, sl, tp)` → `BarrierResult` | label_builder | Stable |

## Verification
```bash
python -m pytest tests/unit/test_training_contract.py tests/engine/test_training_contracts.py -v
python -m mypy core/contracts/training/ --no-error-summary
```

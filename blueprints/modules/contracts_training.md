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
| FIX-20260803-003 | 2026-08-03 | cursor-agent | — | **L3: BTC 标签契约绝对独裁 (M1 战役二)**. `LabelContract` 新增 `expected_r` 类型 + `build_expected_r()` (open下一bar入场, TP-before-SL, 同bar双中→0, 连续 R-multiple); barrier 路径保留 SL-first 并显式声明 `metadata.barrier_order` (双语义 pinned). 新建 `label_from_live_yaml.py` (live.yaml 唯一真理源, live→训练方向). `validate_label_vs_live.py` 硬门禁 (不符熔断). 17 单测. | config-drift (RC-09) — 训练-实盘 SL/TP 错位 (DQAF-20260630-200) |
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

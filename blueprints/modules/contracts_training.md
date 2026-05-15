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
| FIX-20260515-011 | 2026-05-15 | cursor-agent | — | TrainingContract enhancements: LabelSpec added profitability_calibrated, spread_pips, slippage_pips, pip_value. QualityGateSpec added model_type with tiered validation (tree≥0.75, deep_learning≥0.5, online≥0.4 forward Sharpe). | RC-01, RC-06 |
| — | 2026-05-14 | cursor-agent | — | Phase E S2 新建 TrainingContract v2.1，无历史修复 | — |

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

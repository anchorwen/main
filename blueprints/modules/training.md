# Training

## Purpose
训练基础设施：数据集管理、CPCV交叉验证、Sharpe对齐自定义目标函数、模型哈希、SQLite训练注册库、标准化评估报告（含SHAP可解释性）、统一训练管线入口。

## Key Files
| File | Role |
|------|------|
| `core/training/dataset.py` | TrainingDataset 加载/验证，时序划分，walk-forward splits |
| `core/training/cpcv.py` | Combinatorial Purged Cross-Validation（De Prado 2018） |
| `core/training/custom_objectives.py` | Sharpe对齐损失函数（XGBoost/LightGBM），样本加权，利润因子近似 |
| `core/training/evaluation_report.py` | TrainingEvalReport 统一评估报告 + 金融指标 + SHAP分析 + 质量门禁自检 |
| `core/training/model_hashing.py` | SHA256 模型文件哈希，支持单模型和集成 |
| `core/training/training_registry.py` | SQLite + SQLAlchemy 训练注册库（ACID） |
| `core/training/trainer_protocol.py` | TrainerProtocol 抽象接口 + TRAINER_REGISTRY |
| `core/training/checkpoint.py` | CheckpointManager 检查点保存/恢复 |
| `core/training/model_card.py` | ModelCard / ModelCardGenerator 模型卡片 |
| `core/training/experiment_tracker.py` | ExperimentTracker 实验追踪 |
| `core/training/registries.py` | LOSS_REGISTRY, METRIC_REGISTRY, OPTIMIZER_REGISTRY, SCHEDULER_REGISTRY |
| `core/training/profitability_calibrator.py` | 标签盈利性校准 |

## Data Flow
```
TrainingContract (YAML)
        ↓
   train.py (unified pipeline)
        ↓
   ┌────┼────┬──────────┬─────────────┐
   ↓    ↓    ↓          ↓             ↓
Dataset CPCV CustomObj  Trainer    EvaluationReport
   │    │      │          │             │
   │    │      ↓          ↓             ↓
   │    │  Sharpe-aligned  Model    Quality Gates
   │    │  gradient        │        (auto-check)
   │    └──────┬───────────┘             │
   │           ↓                         ↓
   │     Multi-seed Training    ┌────────────────┐
   │           ↓                │ SHAP Analysis   │
   └───── CPCV Evaluation       │ (optional)      │
              ↓                 └────────────────┘
         CPCVResult                     ↓
              ↓                  SHAPReport
         ┌────┴────┐
         ↓         ↓
    Model Hash   Registry
    (SHA256)     (SQLite)
         ↓         ↓
    Brain Config JSON
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/training | TrainingContract, QualityGateSpec | Pipeline dispatch + quality gate check |
| numpy | ndarray | Feature/label arrays |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| scripts/training/train.py | Dataset, CPCV, custom_objectives, evaluation_report, model_hashing, registry | Unified pipeline |
| scripts/training/trainers/* | custom_objectives, trainer_protocol | Trainer implementation |
| scripts/training/dataset_builder.py | dataset, profitability_calibrator | Dataset construction |
| scripts/daily_ops.py | label_contract (via contracts/training) | Daily operations |
| scripts/training/e2e_pipeline_validation.py | label_contract | E2E validation |

## Known Issues
- `verify.py --full` mypy [FAIL] 来自预存错误（mt5_spread_probe, environment_config 等），不是训练模块引入
- SHAP 分析需要 `shap` 包；未安装时 `run_shap_analysis()` 返回 None，`require_shap_stability` 门禁自动跳过

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260524-010 | 2026-05-24 | cursor-agent | — | Torch trainer mypy cleanup: fixed 33 errors across deep_res_mlp_trainer.py (17→0), transformer_trainer.py (15→0), xgb_trainer.py (1→0). Used nn.Module annotations at model construction sites — zero runtime changes. | RC-02 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors | type-confusion |
| FIX-20260521-008 | 2026-05-21 | cursor-agent | — | Meta labeling dataset + filter training pipeline: build_meta_labeling_dataset.py (OU-parameterized entry/exit), scan_profitability_surface.py (upstream×meta threshold grid search), train_meta_filter.py (Stage 2 LGB+MLP binary classifier), backtest scripts for high-recall/precision validation | RC-06 |
| FIX-20260515-005 | 2026-05-14 | cursor-agent | a4a1005 | Brain config v2→v1 schema compat: generate_brain_config now outputs brain_registry_entry.v1 with artifact_path + brain_type + contract_group + magic. Converted 5 v2 configs, updated live.yaml, fixed test_dataset_builder label assertion. | contract-violation |
| FIX-20260515-004 | 2026-05-14 | cursor-agent | a4a1005 | Registry UNIQUE constraint: add_or_update falls back to model_hash lookup when run_id not found | contract-violation |
| FIX-20260515-003 | 2026-05-14 | cursor-agent | a4a1005 | Max drawdown gate units fix: removed *100 multiplier, max_drawdown is already in absolute return units | boundary-error |
| FIX-20260515-002 | 2026-05-14 | cursor-agent | a4a1005 | Pre-split dataset support: pipeline auto-detects X_val/y_val/X_test in NPZ and uses them directly | contract-violation |
| FIX-20260515-001 | 2026-05-14 | cursor-agent | a4a1005 | LightGBM 4.6.0 removed fobj parameter: custom objective now passed via params[objective] | contract-violation |
| FIX-20260515-011 | 2026-05-15 | cursor-agent | — | Foundation fixes: integrated profitability_calibrator into pipeline (calibrate_label_contract()), fixed temporal leakage in _find_nearest_in_index() (only backward matching), added spread/slippage transaction cost modeling to label_contract.py and profitability_calibrator.py, added tiered quality gates (tree/deep_learning/online) with stricter validation. | RC-01, RC-02, RC-03, RC-04 |
| FIX-20260515-012 | 2026-05-15 | cursor-agent | — | Pipeline unification: extended train_single() to support all 5 model types (xgboost, lightgbm, deep_res_mlp, transformer, online_mlp/online_sgd). Added DL search spaces (DeepResMLP, Transformer, OnlineMLP). Fixed evaluation and model saving for non-tree models. Added --price-data CLI flag for profitability calibration. | RC-05 |
| — | 2026-05-14 | cursor-agent | — | Phase E S1-S3 新建模块，无历史修复 | — |
| FIX-20260517-001 | 2026-05-17 | cursor-agent | — | Route C+ Protocol 1: PiT OOF generator replacing cross_val_predict with row-by-row deque loop. Cross-fold deque clearance. Stage 2 LGB+MLP training scripts on PiT features. meta_stage2_runtime_59 schema registered. LGB Val Sharpe 30.6, MLP 18.8. | config-drift |
| FIX-20260520-029 | 2026-05-20 | cursor-agent | — | Future-data leak in build_v9_micro_dataset.py: np.abs(micro_ts - ts) allowed matching future micro features to past V9 bars. Fix: backward-only matching (micro_ts <= ts) with future_leak_prevented diagnostic counter. Same class of bug as FIX-20260515-011 (_find_nearest_in_index) but in the micro→V9 merge path. | RC-03 (state-leak) |
| FIX-20260521-005 | 2026-05-21 | cursor-agent | — | label_builder.py变量遮蔽修复：unlinked循环中trade变量重命名为unlinked_trade，消除与linked循环trade变量的同名遮蔽。 | RC-02 |
| FIX-20260520-030 | 2026-05-20 | cursor-agent | — | Regression training target: --target regression flag added to institutional_train.py. Uses y_reg (PnL values) from NPZ with reg:squarederror (XGBoost) / regression (LightGBM) objectives. RMSE/R² metrics instead of Sharpe/WR/PF. Clean dataset v9_micro_49_clean.npz built (42710 samples, 0 future leaks). | RC-12 (missing-feature) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `combinatorial_purged_cv(timestamps, n_groups, n_test_groups)` → `list[CPCVFold]` | train.py | Stable |
| `make_xgb_sharpe_obj(pnl)` → `(grad, hess)` callable | xgb_trainer | Stable |
| `lightgbm_sharpe_obj(y_true, y_pred, pnl)` → `(grad, hess)` | lgb_trainer | Stable |
| `compute_financial_metrics(y_true, y_pred, pnl)` → `dict` | evaluation_report | Stable |
| `TrainingEvalReport.check_quality_gates(spec)` → `(passed, results)` | train.py | Stable |
| `hash_model_file(path)` → `str` (SHA256) | registry, train.py | Stable |
| `TrainingRegistry.add_run(record)` → `None` | train.py | Stable |

## Verification
```bash
python -m pytest tests/unit/test_evaluation_report.py tests/unit/test_cpcv.py tests/unit/test_custom_objectives.py tests/unit/test_model_hashing.py tests/unit/test_training_registry.py -v
python -m mypy core/training/ --no-error-summary
python -m ruff check core/training/
```

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
| FIX-20260530-073 | 2026-05-30 | cursor-agent | — | Barrier brain restoration: recovered deleted configs from git, registered, but both failed schema validation (Macro_Gold_Silver_Spread renamed to Macro1_Corr). Retrained Barrier_V9_12B_V1 on M5 swing_enhanced_35 schema. Brain_Rev disproven (PF=0.46 with correct eval). | RC-06, RC-09 |
| FIX-20260530-072 | 2026-05-30 | cursor-agent | — | Training pipeline fixes: (1) compute_metrics evaluation skew — hardcoded ±1.5 → reads sl/tp from meta.json. Brain_Trend PF corrected 2.10→3.50. (2) meta.json hardcoded 1.5/1.5 → uses actual params. (3) barrier_12bar + M5 support in train_swing_v9.py + build_swing_enhanced_dataset.py. | RC-06 |
| FIX-20260530-067 | 2026-05-30 | cursor-agent | — | Dual-track asymmetric labels: label-trend-1.0.0 (sl=1.5/tp=2.5) + label-reversion-1.0.0 (sl=2.5/tp=0.7). Modified build_swing_enhanced_dataset.py (sl_atr_mult/tp_atr_mult + --label-contract). Trained Brain_Trend_M30_V1 (Test WR 67.7%/PF 2.10) + Brain_Rev_M30_V1 (Test WR 62.3%/PF 1.65). | RC-09 |
| FIX-20260530-063 | 2026-05-30 | cursor-agent | — | MetaExit model retrained: 819 paired trades (up from 59), 229 wins, WR=27.96%. Engine quality gates passed. Phase C闸门 #2 complete. | RC-09 |
| FIX-20260529-033 | 2026-05-29 | cursor-agent | — | Swing_V9 V2 full-cycle retrain: rebuilt M15+M30 datasets with purge-gap (FIX-20260529-029), trained XGBoost V2 models with embedded feature_names (FIX-20260529-027) + artifact_hash integrity. M30_V2: Test WR 62.9%, PF 1.70, Sharpe 29.49. M15_V2: Test WR 53.5%, PF 1.15, Sharpe 7.67. V1 models disabled in live.yaml (superseded). `train_swing_v9.py` now auto-computes SHA256 artifact_hash and bumps brain_id to V2. | RC-06, RC-09 |
| FIX-20260528-025 | 2026-05-28 | cursor-agent | — | Train-inference feature skew: dataset builder replaced `compute_swing_macro_features()` (TF-bar-based) with `DailyFeatureComputer._gather_row()` (D1-bar-based, SSOT). Micro features changed from N-bar aggregation to single M5 snapshot. TF-specific OU/Hurst changed from TF closes to M5 closes. 12 of 24 macro features were computed differently — ~37% model gain affected, causing systematic wrong-direction predictions. | RC-06, RC-09 |
| FIX-20260529-027 | 2026-05-29 | cursor-agent | — | XGBoost feature name embedding: `train_swing_v9.py` — all `xgb.DMatrix()` constructors now pass `feature_names` from dataset. Enables downstream adapter to validate column order at load time (fail-fast on mismatch). Previously the booster saved without embedded names (`feature_names=None`), leaving zero defense against train-serve column-order skew. | RC-06 |
| FIX-20260529-029 | 2026-05-29 | cursor-agent | — | Purge gap in swing dataset chronological split: labels look ahead `horizon` bars but train/val/test were split with zero gap. Last training sample's label window overlapped first `horizon` validation bars (M30: 12 bars, M15: 24 bars), causing label leakage and inflated val/test metrics. Fixed with `purge_bars = horizon` gap between splits, matching existing pattern in `dataset_builder_d1.py`. Metadata now records `purge_bars`, `n_train_init`, `n_val_init`, `n_test_init`. | RC-03 |
| FIX-20260528-023 | 2026-05-28 | cursor-agent | — | `train_swing_v9.py` — added `schema_version`, `magic`, `artifact_path`, `training_horizon` fields to generated brain configs. Swing_V9 brain configs were missing `schema_version: "brain_registry_entry.v1"` — `_load_brain_entries_from_dir()` at live_intent_loop.py:166 silently skips files without this field. Also added strategy-aware magic numbers (90310/90320/90330/90340/90301) and training horizons (24/12/48/192/5). Fixed existing configs retroactively. | RC-09 |
| FIX-20260528-022 | 2026-05-28 | cursor-agent | — | `train_swing_v9.py` — added `contract_group` field to generated brain configs. Swing_V9 brains were missing this field, causing them to be sorted into `_unknown_brains` instead of being assigned to m30_swing/m15_swing strategies. Also fixed both existing brain configs retroactively. | RC-09 |
| FIX-20260528-021 | 2026-05-28 | cursor-agent | — | Phase 2 swing revival: (1) `build_swing_enhanced_dataset.py` — 35-dim swing+micro dataset builder with 24 swing macro + 9 microstructure + 2 TF-specific features, symmetric SL=TP=1.5×ATR barrier labels, chronological split. (2) `train_swing_v9.py` — XGBoost multi-class trainer with class-balancing weights, simulated PnL metrics. (3) M30 2499 samples, M15 4999 samples. (4) Swing_V9_M30_V1 (Test WR 62%, PF 1.65) + Swing_V9_M15_V1 (Test WR 62%, PF 1.65). | RC-09 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: replaced local `_resolve_features_for_schema()` in generate_brain_config.py and institutional_train.py with registry lookup via `get_schema_feature_names()`. Training pipeline now imports schema data from canonical SSOT instead of maintaining its own copy. | RC-09 |
| FIX-20260528-013 | 2026-05-28 | cursor-agent | — | barrier_12bar full pipeline rebuild follow-up: build_meta_features.py — added timestamps to save_kwargs for CPCV eval, added Any type import, fixed label_mapping double-application bug. train.py — quality gate check now prefers CPCV Sharpe over per-seed forward Sharpe for honest out-of-sample evaluation. | RC-06 |
| FIX-20260524-044 | 2026-05-24 | cursor-agent | — | T4-H1: walk_forward() now delegates to purged_walk_forward() with default purge gap ≥ max(10, n//splits*5) — prevents multi-bar label leakage across train/test boundary. T4-H2: split(method='random') emits FutureWarning — shuffling financial time-series leaks future samples into training. T4-H3: custom Sharpe objective now zeros out NaN PnL samples before gradient computation — prevents NaN gradients silently corrupting boosting. | RC-03, RC-06 |
| FIX-20260524-012 | 2026-05-24 | cursor-agent | — | Training scripts mypy cleanup: eval_regime.py cast(np.ndarray) for np.percentile (9 err), label_builder_d1.py widened h4 type annotation (2 err), train_from_csv.py numpy scalar annotations (4 err), train_online_init.py r type annotation (1 err), build_profitable_labels.py timestamp arg-type ignore (1 err). 17 errors → 0. | RC-02 |
| FIX-20260524-013 | 2026-05-24 | cursor-agent | — | Backtest mypy cleanup: backtest_dynamic_exit.py (22→0). Two root causes: _detect_toxic_flow_m5 called with int direction instead of str side; heterogeneous strategies dict with pnl_aware_z extra key "mean_drifts" causing mypy to infer all values as object. Fixed with side: str conversion + dict[str, dict] type annotations + MODULE_SOURCE_MAP entry. | RC-02 |
| FIX-20260524-015 | 2026-05-24 | cursor-agent | — | Test mypy cleanup (partial): test_eval_alignment (annotated recs), test_order_state_machine (removed 7 stale ignores). Part of cross-module Batch H — all mypy errors cleared (19→0). | RC-02 |
| FIX-20260524-011 | 2026-05-24 | cursor-agent | — | Variable shadowing fix in calibrate_sl_tp.py: renamed r→res in two result-printing loops. Mypy inferred r as int from earlier enumerate/range, breaking dict indexing. | RC-02 |
| FIX-20260527-007 | 2026-05-27 | cursor-agent | — | Asymmetric R-multiple cost-sensitive sample weighting: new `loss_penalty` method in `compute_sample_weights()` — loss samples `weight = 1.0 + |pnl| × penalty_factor` (default 2.0, clip 8.0), win samples weight=1.0. Prevents model from chasing fat-tail wins; forces gradient to focus on catastrophic-loss avoidance. | RC-12 |
| FIX-20260524-010 | 2026-05-24 | cursor-agent | — | Torch trainer mypy cleanup: fixed 33 errors across deep_res_mlp_trainer.py (17→0), transformer_trainer.py (15→0), xgb_trainer.py (1→0). Used nn.Module annotations at model construction sites — zero runtime changes. | RC-02 |
| FIX-20260524-016 | 2026-05-24 | cursor-agent | — | CRITICAL: Spread/slippage 100x mismatch in profitability_calibrator.py — renamed params, MT5-native cost formula (tick_value/tick_size). Updated calibrate_labels.py + scan_profitability_surface.py. | RC-06, RC-09 |
| FIX-20260524-017 | 2026-05-24 | cursor-agent | — | CRITICAL: 3-class → 2-class label mapping — dataset.py hard-filters timeout samples, remaps {-1→0, 1→1} for binary_logloss. Added label_mapping field to 30 training YAMLs. | RC-06 |
| FIX-20260524-018 | 2026-05-24 | cursor-agent | — | Added calmar_ratio (annualized_return / abs(max_drawdown)) to compute_financial_metrics(). Previously checked in quality gates but never computed — default -999.0 always passed gates. | RC-12 |
| FIX-20260524-019 | 2026-05-24 | cursor-agent | — | HIGH: MLP bypasses quality gates — verified already resolved by FIX-20260515-011 (tiered quality gates). No code changes needed. | RC-06 |
| FIX-20260524-022 | 2026-05-24 | cursor-agent | — | Added profitability_calibrated: false to 11 training configs missing the field. Explicit is better than implicit for pipeline behavior consistency. | RC-09 |
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
| FIX-20260524-030 | 2026-05-24 | cursor-agent | — | Meta-Labeling Pivot: build_meta_labeling_dataset.py — barrier label mode (SL=3.0/TP=1.5, 12-bar horizon), PIT feature alignment (entry_idx-1 to prevent look-ahead), OU process features (ou_z_score/half_life/theta), deprecated parallel universe sampling (data leakage from overlapping windows). 675 OU signals → 445 binary after drop-timeout. Guardrails 1-3 all PASSED. | RC-03, RC-06 |
| FIX-20260524-031 | 2026-05-24 | cursor-agent | — | Meta-Labeling Binary Classifier: training contract barrier_12bar_meta_binary_cls.yaml with extreme regularization (max_depth=2, num_leaves=7, lambda_l1/l2=1.0, min_data_in_leaf=30) for 445-sample training. Brain config meta_stage1_metalabel_binary_v1.json (magic=90013, shadow). Model: train_sharpe=13.7, fwd_sharpe=8.1, CPCV=12.9. OOF calibration verified. | RC-06 |
| FIX-20260524-032 | 2026-05-24 | cursor-agent | — | Contract group barrier_12bar_meta registered in live.yaml (strategy line magic=90014, regime_map all 5 regimes) + governance_state.json (Meta_Stage1_Binary_Cls_V1 shelved, Meta_Stage1_MetaLabel_Binary_V1 shadow). Future: OU signal engine integration needed for live voting. | RC-09 |
| FIX-20260526-037 | 2026-05-26 | cursor-agent | — | Full Pipeline Rebuild: build_calibrated_dataset.py — fixed H1-first (alphabetical sort) feature order to canonical M5-first V9_INSTITUTIONAL_40_FEATURES. Same class of bug as FIX-20260525-026 and FIX-20260526-028 (train/serve feature skew). | RC-03 |
| FIX-20260526-038 | 2026-05-26 | cursor-agent | — | Full Pipeline Rebuild: build_meta_features.py — binary mode class-imbalance fix (added scale_pos_weight); rebuilt with regression mode + full 53K sample dataset (including timeouts). OOF predictions via purged walk-forward PiT CV with deque-based feature computation. | RC-03, RC-06 |
| FIX-20260526-039 | 2026-05-26 | cursor-agent | — | Full Pipeline Rebuild: train.py compute_financial_metrics — class-prior threshold replacing fixed 0.5 (extreme imbalance artifact fix); degenerate model detection (prob_range < 0.01 & prob_std < 0.005 → -999 Sharpe); baseline Sharpe subtraction (excess Sharpe isolates model skill); ModelQualityException hard veto blocks garbage deployment. | RC-01, RC-04 |
| FIX-20260526-040 | 2026-05-26 | cursor-agent | — | Full Pipeline Rebuild: meta_stage2_runtime_48 schema registered in brain_config_validator.py (40 V9 + 8 meta: oof_pred, oof_pred_zscore_20, atr_percentile_100, vol_zscore, hurst_m5, session_sin, session_cos, rolling_hit_rate_20). meta_stage2_filter_v3.json updated: single LGB model, Train Sharpe 4.78, Forward Sharpe 1.30. | RC-09, RC-12 |

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

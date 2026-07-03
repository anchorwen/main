# Training / Pipeline

## Purpose
Institutional ML training infrastructure: unified dataset abstraction, Combinatorial Purged Cross-Validation (CPCV), custom trading-aligned loss functions, standardized evaluation reports, model cards for deployment awareness, and a SQLite-backed training registry with cryptographic model hashing. Supports 8 trainer architectures (XGBoost, LightGBM, DeepResMLP, OnlineMLP, Transformer, Arb, Survival, MatrixNet).

## Key Files
| File | Role |
|------|------|
| `core/training/dataset.py` | Unified Training Dataset — NPZ/Parquet abstraction with deterministic split and walk-forward temporal split |
| `core/training/cpcv.py` | Combinatorial Purged Cross-Validation (De Prado 2018, Ch.12) |
| `core/training/custom_objectives.py` | Differentiable trading metrics (Sharpe, profit factor approximations) for XGBoost/LightGBM |
| `core/training/trainer_protocol.py` | `train()` interface + `TRAINER_REGISTRY` — all trainers conform to this protocol |
| `core/training/training_registry.py` | SQLite-backed training run registry with ACID guarantees and SHA256 hashing |
| `core/training/brain_config.py` | `BrainConfigBuilder` — single source of truth for brain config generation |
| `core/training/evaluation_report.py` | Standardized training evaluation with financial metrics, stability, regime breakdown, SHAP |
| `core/training/model_card.py` | Model card generator — records feature order, preprocessing, provenance, performance |
| `core/training/model_hashing.py` | SHA256 model artifact fingerprinting for tamper-proof audit |
| `core/training/profitability_calibrator.py` | Barrier (SL, TP) surface EV computation and selection |
| `core/training/experiment_tracker.py` | JSONL-based experiment log — zero-cost bridge to MLflow/W&B |
| `core/training/checkpoint.py` | Checkpoint manager — save/resume for long-running training jobs |
| `core/training/registries.py` | Loss/metric/optimizer/scheduler registries (decorator pattern) |
| `core/training/utils.py` | Shared utilities: git metadata, time helpers |

## Training Flow
```
TrainingRecipe (YAML/JSON)
        ↓
BrainConfigBuilder → brain_registry_entry.v1 JSON
        ↓
Dataset.load() → CPCV split → N train/test folds
        ↓
TRAINER_REGISTRY[architecture].train() → model artifact
        ↓
EvaluationReport (Sharpe, Sortino, Calmar, win rate, profit factor, regime breakdown)
        ↓
ModelCard (feature_order, preprocessing, provenance, performance)
        ↓
model_hashing.sha256() → TrainingRegistry.register()
        ↓
Promotion pipeline (shadow → live)
```

## Trainer Architectures (8 registered)
| Architecture | Trainer File | Typical Use |
|-------------|-------------|-------------|
| xgboost | `scripts/training/trainers/xgboost_trainer.py` | Barrier, swing prediction |
| lightgbm | `scripts/training/trainers/lightgbm_trainer.py` | Meta-labeling, multi-class |
| deep_res_mlp | `scripts/training/trainers/deep_res_mlp_trainer.py` | Deep residual MLP |
| online_mlp | `scripts/training/trainers/online_mlp_trainer.py` | Online SGD adaptation |
| transformer | `scripts/training/trainers/transformer_trainer.py` | Micro-barrier, attention-based |
| arb | `scripts/training/trainers/arb_trainer.py` | Stat arb (OU process) |
| survival | `scripts/training/trainers/survival_trainer.py` | Survival analysis (time-to-event) |
| matrixnet | `scripts/training/trainers/matrixnet_trainer.py` | MatrixNet ensemble |

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/schema_versions | Schema version constants | Config versioning |
| contracts/training | Label contracts, training recipes | Type-safe configs |
| metrics/financial_metrics | Sharpe, Sortino, Calmar, etc. | Evaluation reports |
| features/schemas | Feature schema registry | Dataset feature selection |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| scripts/training/train.py | TrainerProtocol, TrainingRegistry, BrainConfigBuilder | Main training CLI |
| scripts/training/builders/* | dataset.py, cpcv.py | Dataset construction |
| scripts/training/trainers/* | trainer_protocol.py, custom_objectives.py | Trainer implementations |
| deployment/brain_lifecycle_manager | model_card.py, model_hashing.py | Deployment validation |

## Known Issues
- CPCV is computationally expensive (C(N, N_test) combinations) — for large N, training time scales combinatorially.
- Custom objectives (Sharpe, profit factor) are differentiable approximations — gradient quality varies by model architecture.
- Training recipes and live configs are maintained in separate files (config drift risk — RC-09).

## Fix History
| Fix ID | Date | Summary | Root Cause |
|--------|------|---------|------------|
| FIX-20260704-001 | 2026-07-04 | **L3: Label Generation Pipeline — symmetric directional profitability labels** (DQAF-20260703-062). `compute_labels()` in `train_btc_swing_v9.py` added `side` parameter (long/short/None). Multiclass path now computes LONG and SHORT outcomes independently then merges into proper 3-class directional labels. Old behavior preserved via side=None for binary training. | L3 — label encoding (barrier hit) ≠ model output semantics (directional signal); 3 compounding biases destroyed directional information |
| FIX-20260628-164 | 2026-06-28 | **P0: XAU Swing_V9_M30_V2 模型回训 (L1)**. V2 模型文件丢失 (artifact_path 指向不存在文件), 使用同架构 (35-dim xgboost_v9, 300 trees, LR=0.03, SL=3.0/TP=1.5) 完整回训: build_swing_enhanced_dataset.py → train_swing_v9.py。新模型指标全面超越原版: Test WR 42.2%→45.9%, PF 2.16→2.28, Sharpe 33.06→36.70。配置已更新 (artifact_hash, training_metrics, objective=multi:softprob, label_contract). | L1 — model file lost on disk, config/architecture preserved |
| FIX-20260628-160 | 2026-06-28 | **XAU 训练就绪合约 + check_training_readiness.py 泛化 (L2)**. 创建 training_pipeline_xau_swing_v3.json (35-dim, v9_institutional_40 Feature Store, build_swing_enhanced_dataset.py builder). check_training_readiness.py 符号检测 / 模式处理 / builder 调用全部合约驱动 (builder_script, builder_args, builder_output_arg). XAU training_readiness.json 已生成, audit_state 12/12. | RC-12 — missing-feature: XAU 无训练流水线合约 |
| FIX-20260617-003 | 2026-06-17 | TECH_DEBT-004 RESOLVED — BTC 41-dim retraining complete | — |
| FIX-20260617-100 | 2026-06-17 | MetaFilter V3 — 102 samples, 47-dim | — |
| FIX-20260616-003 | 2026-06-16 | Directional Balance Filter (Tactic A) | RC-05 |
| FIX-20260610-003 | 2026-06-10 | Training pipeline CPCV integration | — |
| FIX-20260602-078 | 2026-06-02 | V9 institutional schema 41-dim training | — |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `TrainerProtocol.train(dataset, config, checkpoint)` | All trainers | Stable |
| `BrainConfigBuilder.build(recipe)` → dict | train.py, deployment | Stable |
| `TrainingRegistry.register(model_path, card, hash)` | train.py, promotion | Stable |
| `Dataset.load(path, split)` → (X_train, y_train, X_test, y_test) | All training scripts | Stable |

## Data Flow
See [Training Flow](#training-flow) above — the architecture diagram and 8-stage pipeline description serve as this module's Data Flow documentation.

## Verification
```bash
python -m pytest tests/ -k "training" -q
```

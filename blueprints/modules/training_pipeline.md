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
| `scripts/training/train_swing_binary_directional.py` | Binary directional XGBoost swing trainer — filters NEUTRAL labels, binary:logistic, SL/TP PnL simulation |

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
| P3-VANGUARD | 2026-08-23 | **P3 XAU Micro Scaler V1 双轨战役 (IC 雷霆开火令 The LightGBM Vanguard, DQAF-AUDIT-20260823-P3)**: 新增 `scripts/training/train_micro_scaler_v1.py` (Option A 真实标签基线 + Option B 伪标签敏感性诊断). **TECH_DEBT-023 (The Schema Mutagenesis) 建档** — v9_institutional_40 静默列漂移, 训练锚定 current-gen 列族 (Price_ZScore family) 规避 train/serve skew. Option A: 576 current-gen 真实标签 × 40 特征, LightGBM (depth<=3/min_child=20/leaves=8/lr=0.03, scale_pos_weight=2.045), 60/20/20 TS-Split + Purge±300min (purged=16) → **OOS 诚实 FAIL** (ρ=−0.020, MCC=0.000, PR-AUC=0.619) — 小样本不泛化, 标签非平稳 (base 0.328→0.562). Option B: 7,834 行前向 3-bar 伪标签 → OOS ρ=0.466, PR-AUC=0.757, MCC=0.430 (M5 信息熵存在, 合成标签+自相关审慎). **零实盘代码触碰** (Shadow Mandate) — 工件落 data/training/micro_scaler_v1/. 复用权威 builder asof_join (Iron Law #1.1 不散布). 待 Shadow 冷却 + current-gen 样本积累后再议. | 数据规模 (576 真实标签不泛化) + TECH_DEBT-023 schema 世代分叉 (治理靶点) | readiness 度量分母对齐 builder SSOT. 22.3% 是假象 (复刻+builder 自证双轨吻合): builder join 宇宙 = 去重含 PnL 1262, 实配 1046 = **82.9% ≥ 80%**, 被原始 closed 条目 (4697) 分母放大 3.72×. 修复: ① builder 落 `*.report.json` 边车 (valid_trades_count=1262 SSOT) ② readiness asof_join_rate 读 SSOT (report 缺失 → 本地 distinct 回退) ③ 阶段2 去重宇宙排除 manual_close/orphan → pnl_completeness 0/1238=0.0%. XAU 就绪评估转绿 (82.9% / 0.0% / 1661 / 1046 / 标签 39.2%). 回归锁 +3. | RC-06 metric-semantics (度量分母=原始事件条目非业务实体) |
| FIX-20260821-006 | 2026-08-21 | **TECH_DEBT-020 清偿 (Phase 3 P1 首优 The Vanguard, IC 三位一体防御裁决 — The Empty NPZ)**: `training_pipeline_xau_metafilter_v1.json` stage_3 缺 `builder_args` → builder 默认 symbol=BTCUSDc 空转 → 静默早退 rc=0 → validator np.load 空文件 EOFError (check_training_readiness.py:722). 三腿: ① 契约补 builder_args ② builder 静默早退→`_fail()` sys.exit(1) ③ reader np.load 空文件守卫 + (EOFError,ValueError,OSError,pickle.UnpicklingError,zipfile.BadZipFile) 捕获 → FAIL verdict. XAU 训练就绪评估首次真实生效 (1046 样本/40 维), 残余 FAIL=诚实数据信号. 回归锁 7. | RC-12 missing-feature (契约字段缺失→默认值空转) + RC-06 (静默 rc=0 + 读取零容错) |
| FIX-20260803-004 | 2026-08-03 | **L3: 自动 OOS / 盈亏平衡门槛 (M2 战役三 / IC 最高批准)**. 硬性消灭"ρ=0.0445 靠人工裁决上线": `core/training/breakeven.py` 唯一盈亏平衡实现 (compute_breakeven WR=1/(1+RR), 摩擦对齐 label_contract: win=TP−spread/loss=SL+slippage, expected_r 双轨); `scripts/training/oos_blind_test.py` 盲测 CLI (ρ/WR/expectancy/breakeven_WR/verdict, INSUFFICIENT_OOS=警告); `core/training/utils.py` 共享 spearman_rho; `train.py` check_quality_gates 加 train_spearman + 质量门后写 registry 前跑盲测, FAIL → ModelQualityException 硬否决 (fail-closed). 27 单测. | missing-feature (RC-12) — 无自动 OOS 门禁, 人工裁决放行 |
| FIX-20260803-002 | 2026-08-03 | **L3: BTC 特征计算图绝对统一 (M1 战役一 / IC 最高批准)**. `btc_feature_augmenter.py` 状态剥离 → 纯函数 `_assemble_41`/`assemble_41_vector`/`assemble_41_series`; `core/training/feature_replay.py` 历史回放唯一入口 (OHLC → 组件 → 共享装配, bit-identical); `scripts/training/build_btc_dataset_from_ssot.py` 取代 ad-hoc 41-dim 手搓实现 (修 XAU 缺省假比值). `tests/training/test_feature_bit_identical.py` 9 硬断言. | state-leak (RC-03) — 训练/实盘特征装配状态分叉 (历史 8+ 次: FIX-20260625-137/-091/-022) |
| FIX-20260704-001 | 2026-07-04 | **L3: Label Generation Pipeline — symmetric directional profitability labels** (DQAF-20260703-062). `compute_labels()` in `train_btc_swing_v9.py` added `side` parameter (long/short/None). Multiclass path now computes LONG and SHORT outcomes independently then merges into proper 3-class directional labels. Old behavior preserved via side=None for binary training. | L3 — label encoding (barrier hit) ≠ model output semantics (directional signal); 3 compounding biases destroyed directional information |
| FIX-20260706-024 | 2026-07-06 | **L2: Per-bar real spread → barrier label computation**. `compute_labels()` gains `spreads` parameter. CSV spread data (median $18) replaces hardcoded $10 constant — 44% underestimation fixed. Per-bar spread captures volatility-regime-dependent friction. | RC-09 — config-drift: training used $10 spread constant; actual BTC spread $10–$160 |
| FIX-20260706-025 | 2026-07-06 | **L3: Symmetric binary directional v2 — brain config upgrade**. Baseline (WR=51.1%)→symmetric retrain (WR=54.2%, +3.1pp). Real 41-dim features + per-bar real spread + curation. Brain config `BTC_Swing_M5_Binary.json` upgraded with new artifact hash + training metrics. Institutional mandate: accept native 64.6% LONG (regime-honest, not structural). Shadow/probation deployment. | RC-09 — zero-fill features + constant spread understated achievable WR by 3.1pp |
| FIX-20260628-164 | 2026-06-28 | **P0: XAU Swing_V9_M30_V2 模型回训 (L1)**. V2 模型文件丢失 (artifact_path 指向不存在文件), 使用同架构 (35-dim xgboost_v9, 300 trees, LR=0.03, SL=3.0/TP=1.5) 完整回训: build_swing_enhanced_dataset.py → train_swing_v9.py。新模型指标全面超越原版: Test WR 42.2%→45.9%, PF 2.16→2.28, Sharpe 33.06→36.70。配置已更新 (artifact_hash, training_metrics, objective=multi:softprob, label_contract). | L1 — model file lost on disk, config/architecture preserved |
| FIX-20260628-160 | 2026-06-28 | **XAU 训练就绪合约 + check_training_readiness.py 泛化 (L2)**. 创建 training_pipeline_xau_swing_v3.json (35-dim, v9_institutional_40 Feature Store, build_swing_enhanced_dataset.py builder). check_training_readiness.py 符号检测 / 模式处理 / builder 调用全部合约驱动 (builder_script, builder_args, builder_output_arg). XAU training_readiness.json 已生成, audit_state 12/12. | RC-12 — missing-feature: XAU 无训练流水线合约 |
| FIX-20260617-003 | 2026-06-17 | TECH_DEBT-004 RESOLVED — BTC 41-dim retraining complete | — |
| FIX-20260617-100 | 2026-06-17 | MetaFilter V3 — 102 samples, 47-dim | — |
| FIX-20260616-003 | 2026-06-16 | Directional Balance Filter (Tactic A) | RC-05 |
| FIX-20260610-003 | 2026-06-10 | Training pipeline CPCV integration | — |
| FIX-20260728-001 | 2026-07-28 | **L3: M30 Swing binary_directional V3 + generic training script.** Closed gap: M15/H1/H4 already converted to binary_directional (FIX-20260726-007/008/009), M30 was last 3-class holdout. New `train_swing_binary_directional.py`: filters NEUTRAL labels, trains binary:logistic with SL/TP PnL simulation. M30 V3: WR=63.5%, PF=2.62 (vs 3-class WR=36.5%/PF=1.77). H1 V4 (horizon=48) rejected: regime overfitting (22.4σ feature drift, label rupture). | RC-12 — missing-feature |
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

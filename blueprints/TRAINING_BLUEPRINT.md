# Training Blueprint: 从临时脚本到机构级训练方法论

> "告诉我你怎么打标签，我就知道你的策略赚什么钱。告诉我你的 Recipe，我就能复现你的模型。"

版本: 1.0.0 | 创建: 2026-05-04 | 状态: Phase 1 实施中

---

## 1. 设计哲学

### 三个核心原则

**原则 1: Label Contract 是策略的数学定义**

Label Contract 不只是参数文件。它是交易逻辑的形式化规范：
- 一个 contract = 一个可证伪的假设（"如果在 M5 上 2×ATR SL + 3.5×ATR TP 不能赚钱，则策略无效"）
- 模型只是这个假设的统计近似器
- contract 版本化 → 模型可复现 → 策略可审计

**原则 2: 所有量在"波动率空间"中定义**

价格是时间的函数。波动率是价格不确定性的函数。模型应学习后者。
- 距离/阈值 = ATR 倍率，而非绝对点数
- 特征 normalization 按 ATR 分位数分层评估
- 训练数据增强基于波动率缩放

**原则 3: Recipe 是训练的唯一入口**

不是 `python train.py --lr 0.001 --epochs 200`，而是一个结构化 JSON。
- 一个 Recipe = 一次可复现的完整训练
- 两个 recipe 之间的差异 = 两个模型之间的差异（消融实验）
- 零 CLI hack：所有参数在 recipe 中显式声明

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                   TRAINING BLUEPRINT                        │
│                                                             │
│  blueprints/                      core/contracts/training/  │
│  ├── schemas/                     ├── label_contract.py     │
│  │   ├── label_contract.v1.schema └── training_recipe.py    │
│  │   └── training_recipe.v1.schema                          │
│  ├── contracts/                   scripts/training/         │
│  │   └── label-survival-barrier-1.0.0.json                  │
│  ├── recipes/                     ├── label_builder.py      │
│  │   └── sur-g2026.1-recipe-001.json                        │
│  └── TRAINING_BLUEPRINT.md        ├── dataset_builder.py    │
│                                   ├── generate_batch_plan.py│
│                                   └── trainers/             │
│  ├── label_builder.py     ← journal 或 price → labels       │
│  ├── dataset_builder.py   ← labels + features → train.npz   │
│  ├── generate_batch_plan.py  ← recipe → manifests           │
│  └── trainers/                                             │
│      ├── sur_trainer.py   ← --recipe 参数支持               │
│      └── xgb_trainer.py   ← 从 NPZ 训练 XGBoost             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 四层数据流

```
Layer 1: Price Data (OHLC CSV / MT5 history)
    ↓  label_builder.py --label-contract --price-data
Layer 2: Label Contract → Barrier Labels (JSONL)
    ↓  dataset_builder.py
Layer 3: Feature Store → Feature-Label Pairs (NPZ/Parquet)
    ↓  xgb_trainer.py / sur_trainer.py --recipe
Layer 4: Training Recipe → Trained Model (ONNX/JSON booster)
```

---

## 3. Label Contract 系统

### 概念

Label Contract 定义了"模型要预测什么"。它是训练标签的生成规范。

### 类型

| 类型 | 描述 | 适用 Lane |
|------|------|-----------|
| `survival_barrier` | 在 horizon 内 TP 先于 SL 触发？ | sur, xgbinrepo |
| `regression` | 未来 N-bar 收益率 | mtx |
| `binary_class` | 涨/跌方向 | 通用 baseline |

### Barrier Label 算法

```
输入: OHLC 数组, entry_idx, side (long/short), contract
输出: {label, hit_bar_index, hit_price, sl_price, tp_price, atr}

算法:
  1. 在 entry_idx 计算 ATR(14)
  2. SL = entry ± sl_atr_mult × ATR (long: -, short: +)
  3. TP = entry ± tp_atr_mult × ATR (long: +, short: -)
  4. 从 entry_idx+1 到 entry_idx+horizon_bars:
     a. 若 low ≤ SL (long) 或 high ≥ SL (short) → "sl_hit_first"
     b. 若 high ≥ TP (long) 或 low ≤ TP (short) → "tp_hit_first"
  5. 若 horizon 内都未触发 → "timeout"
```

### 第一个具体 Contract

`blueprints/contracts/label-survival-barrier-1.0.0.json`:
- **SL**: 2.0×M5_ATR(14)
- **TP**: 3.5×M5_ATR(14)
- **Horizon**: 12 bars (60 分钟)
- **原理**: 与实盘 SL/TP 一致，确保训练标签和实盘执行逻辑对齐
- **盈亏比**: 3.5/2.0 = 1.75:1，方向准确率 > 36.4% 时期望为正

### 关键洞察：为什么用 ATR 而非固定点数？

```
训练集 (2020-2025):  黄金 ~$1800, M5_ATR(14) 均值 = 2.31
实盘 (2026-05):      黄金 ~$4560, M5_ATR(14) 范围 = 5-15

固定 SL=$15: 训练时为 6.5×ATR (极宽), 实盘为 1-3×ATR (正常)
→ 训练数据中几乎不会触发 SL → 标签分布严重偏差

ATR 倍率 SL=2.0×ATR: 训练时为 $4.62, 实盘为 $10-30
→ 标签分布跨时间一致 → 模型学到的模式可迁移
```

---

## 4. Training Recipe 系统

### 四段结构

```
recipe
├── model_identity    ← 什么模型？(lane, role, generation, feature_contract)
├── label_contract_ref ← 预测什么？(引用 Label Contract)
├── data              ← 用什么数据？(切片、日期范围、normalization)
├── training          ← 怎么训练？(架构、损失、优化器、超参)
└── evaluation        ← 如何评估？(per-regime 指标、稳定性检查)
```

### 字段说明

#### model_identity

| 字段 | 类型 | 说明 |
|------|------|------|
| `lane` | string | 模型通道: sur, mtx, arb, xgbinrepo |
| `role` | enum | prd (生产), chlg (挑战者), cabl (金丝雀), stub (占位) |
| `generation` | string | gYYYY.N 格式，如 g2026.1 |
| `feature_contract_id` | string | 特征 Schema 标识 |

#### data

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `normalization_strategy` | enum | fixed | fixed / rolling_ewma / rank |
| `normalization_halflife_days` | int | 63 | EWMA 半衰期 (仅 rolling_ewma) |
| `data_augmentation.enabled` | bool | false | 是否启用波动率缩放增强 |
| `data_augmentation.volatility_scaling` | float[] | [0.7, 0.85, 1.0, 1.15, 1.3] | 缩放因子 |

#### training

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `architecture` | enum | mlp_multihead | 骨干架构 |
| `hidden_dims` | int[] | [128, 64, 32] | 隐藏层维度 |
| `dropout` | float | 0.3 | Dropout 率 |
| `loss_weights` | dict | {dir:1.0, risk:0.5, vol:0.3} | 多任务损失权重 |
| `optimizer` | enum | adam | 优化器 |
| `learning_rate` | float | 0.001 | 学习率 |
| `seeds` | int[] | [42] | 训练种子（ensemble 用多个） |

#### evaluation

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `metrics` | enum[] | [accuracy, f1, sharpe_ratio] | 评估指标 |
| `regime_splits` | dict | ATR 分位数 | 按波动率分层评估 |
| `stability_checks` | enum[] | [walk_forward, seed_sensitivity] | 鲁棒性检查 |

### 第一个具体 Recipe

`blueprints/recipes/sur-g2026.1-recipe-001.json`:
- **Lane**: sur, **Role**: chlg, **Architecture**: mlp_multihead
- **Features**: 40-dim V9 Institutional
- **Label**: survival_barrier (SL=2.0×ATR, TP=3.5×ATR, 12-bar horizon)
- **Training**: adam, lr=0.001, batch=256, epochs=200, 5 seeds
- **Evaluation**: per-ATR-regime metrics + walk_forward + seed_sensitivity

---

## 5. 训练流程（端到端）

### 5.1 标签生成

```bash
# 模式 1: 从交易日志生成标签（在线闭环）
python scripts/training/label_builder.py \
  --journal data/live_trade_journal.jsonl \
  --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json \
  --output data/labels/live_labels.jsonl

# 模式 2: 从价格历史生成 barrier 标签（离线批处理）
python scripts/training/label_builder.py \
  --price-data data/raw/XAUUSD_M5_2024.csv \
  --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json \
  --output data/labels/barrier_labels.jsonl \
  --side long,short
```

### 5.2 数据集构建

```bash
python scripts/training/dataset_builder.py \
  --labels data/labels/barrier_labels.jsonl \
  --feature-store-dir data/feature_store \
  --output-dir data/training \
  --format npz
```

### 5.3 批量计划生成

```bash
python scripts/training/generate_batch_plan.py \
  --generation g2026.1 \
  --lanes sur
```

### 5.4 训练执行

```bash
# ONNX (sur lane via D:\ai Survival_V9 forge)
python scripts/training/trainers/sur_trainer.py \
  --manifest-path batch_plans/g2026.1/manifests/CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0.s42.json \
  --artifact-path data/models/sur_g2026.1_s42.onnx \
  --result-json-path data/models/sur_g2026.1_s42.result.json \
  --recipe blueprints/recipes/sur-g2026.1-recipe-001.json

# XGBoost (xgbinrepo lane, fully in-repo)
python scripts/training/trainers/xgb_trainer.py \
  --data data/training/train.npz \
  --output-model data/models/xgb_booster.json \
  --output-result data/models/xgb_result.json
```

---

## 6. 评估体系

### Per-Regime 指标矩阵

不只看全局准确率。按 ATR 分位数分层评估：

| Regime | ATR 分位数 | 重点指标 | 含义 |
|--------|-----------|----------|------|
| Low Vol | 0-33% | direction_bias, precision | 盘整市不瞎动 |
| Normal | 33-67% | sharpe_ratio, profit_factor | 正常市稳赚 |
| High Vol | 67-100% | max_drawdown, sortino_ratio | 黑天鹅不死 |

### 稳定性检查

| 检查项 | 方法 | 通过标准 |
|--------|------|----------|
| walk_forward | 滚动窗口重训练 | 各窗口 sharpe 无显著衰减 |
| seed_sensitivity | 多 seed ensemble | 指标 std < 均值的 30% |
| regime_consistency | 各 regime 评测 | 至少 2/3 regime 利润因子 > 1 |
| data_augmentation_robustness | 增强数据评测 | 增强前后 F1 降幅 < 5% |

---

## 7. 机构参考

| 机构 | 借鉴点 | 我们的实现 |
|------|--------|-----------|
| Two Sigma | Research Environment — alpha 形式化为可测试假设 | Recipe = 假设文档 |
| WorldQuant | Alpha Factory — 特征 rank-normalization | normalization: rank |
| Citadel | Regime-conditional evaluation | regime_splits + per-regime metrics |
| XTX Markets | Online adaptation — 滚动 normalization | normalization: rolling_ewma |
| Man AHL | Barrier Labels — 屏障标签作为策略定义 | Label Contract survival_barrier |
| DeepMind | 数据增强 — volatility scaling | data_augmentation.volatility_scaling |

---

## 8. 与现有系统的集成

### 在线闭环（自进化）

```
live_intent_loop → journal.jsonl → label_builder → labels.jsonl
                                         ↓
registry ← register_brain ← trainer ← dataset_builder
    ↓
governance → runtime (live/shadow deployment)
```

### 离线研究（假设驱动）

```
Label Contract (新假设) → barrier labels → 训练 → 回测 → 评估
    ↓ (如果通过)
  新 Recipe → 新 chlg model → 在线 shadow → governance 升级
```

### 命名一致性

- Label Contract ID: `label-{type}-{semver}` (e.g. `label-survival-barrier-1.0.0`)
- Recipe ID: `{lane}-{generation}-recipe-{NNN}` (e.g. `sur-g2026.1-recipe-001`)
- Model ID (CRT): `CRT.{lane}.{role}.{generation}@{feature_contract}.s{seed}`

---

## 9. 下一步

### Phase 1 (当前 — 已完成)
- [x] JSON Schema: label_contract.v1 + training_recipe.v1
- [x] Pydantic 模型: LabelContract + TrainingRecipe
- [x] 第一个 Label Contract: survival_barrier 1.0.0
- [x] 第一个 Recipe: sur-g2026.1-recipe-001
- [x] label_builder barrier label 支持
- [x] sur_trainer --recipe 参数支持

### Phase 2 (待办)
- [ ] Regression Label Contract for mtx lane
- [ ] 在线 normalization (rolling_ewma) 实现
- [ ] 数据增强 pipeline (volatility scaling)
- [ ] Per-regime 评估报告自动生成
- [ ] recipe diff 工具 (消融实验对比)
- [ ] Label Contract 版本迁移工具

### Phase 3 (规划)
- [ ] 多合约集成测试 (同一价格数据, 不同 contract → 不同 label 分布验证)
- [ ] 自动 recipe 搜索 (Optuna 超参优化)
- [ ] 训练数据质量门禁 (CI 检查 label 分布、特征覆盖率)

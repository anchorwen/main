# Model Evolution Blueprint — 模型进化蓝图

> 从静态批量训练 → 在线自适应 → 强化学习自主进化

## Context

当前 QUANT OS 训练管线产出 4 种静态模型（MLP ONNX / XGBoost / Transformer ONNX / OU Params），
全部依赖离线批量训练。实盘交易数据积累缓慢（~10笔），但有 67K+ 无标签特征记录。
需要引入**不需要大量实盘标签**的进化型模型，逐步实现从"人驱动训练"到"数据驱动进化"的转变。

## Priority 1: Online/Streaming Learning (在线自适应学习)

### 为什么是第一优先

- 每完成一笔交易即可增量更新，**不需要等待积累大量标签**
- 天然适应市场制度变化（概念漂移），不会像批训练模型"过期"
- 利用 67K 无标签特征做 barrier-label 初始训练，实盘标签做微调
- 实现复杂度最低，可复用现有 V9FeatureAdapter + 40维特征管线

### 架构设计

```
实盘特征流 (V9LiveFeatureComputer, 每30s)
    │
    ▼
OnlineLearnerAdapter (SGDClassifier partial_fit)
    │
    ├── infer(features) → BrainDecisionProposal (方向+置信度)
    │
    └── on_trade_closed(features, pnl_label)
        ├── win  → partial_fit(features, +1)
        ├── loss → partial_fit(features, -1)
        └── breakeven → partial_fit(features, 0)
            │
            ▼
        持久化权重到 data/models/online_learner_weights.json
```

### 技术选型

| 算法 | 优点 | 缺点 |
|------|------|------|
| **SGDClassifier (log loss)** | sklearn 内置，无新依赖，概率输出 | 线性决策边界 |
| Passive-Aggressive | 更激进的在线更新 | 无概率校准 |
| River (Hoeffding Tree) | 专为流式数据设计 | 需要新依赖 |

**选用 SGDClassifier** — 零新依赖，概率输出直接映射到 confidence，`partial_fit` 成熟稳定。

### 关键文件

| 文件 | 用途 |
|------|------|
| `core/brains/adapters/online_learner_adapter.py` | 在线学习适配器（NEW） |
| `core/brains/adapters/__init__.py` | 注册 `online_sgd` 适配器 |
| `scripts/training/train_online_init.py` | 初始训练（barrier labels → 初始权重） |
| `core/feedback/online_feedback_hook.py` | 交易结果 → partial_fit 回调 |
| `configs/brains/online_learner_v1.json` | 大脑注册配置 |

### 集成点

1. `BrainFactory` — 添加 `online_sgd` → `OnlineLearnerAdapter` 映射
2. `BrainRunService` — 与其他大脑并行运行推理
3. `FeedbackLoop` 或 `BrainPerformanceTracker` — 交易平仓时触发 `partial_fit`
4. `ParliamentService` — 参与议会投票

---

## Priority 2: Reinforcement Learning Agent (强化学习智能体)

### 为什么是第二优先

- 天然匹配"状态→行动→奖励"的交易环境闭环
- 可学习**仓位管理**和**择时**，而不只是方向预测
- 长期来看最接近"自主进化"目标
- **需要积累 100+ 笔实盘交易后才能有效训练** — 排在在线学习之后

### 技术选型

| 算法 | 适用场景 |
|------|---------|
| **Contextual Bandit** | 信号稀疏环境（日交易量少），比完整 RL 更稳定 |
| **PPO (Proximal Policy Optimization)** | 连续动作空间（仓位大小），数据效率高 |
| **DQN / Double DQN** | 离散动作空间（多/空/观望 + 仓位档位） |

推荐路径：先 Contextual Bandit（低风险）→ 积累数据 → PPO（完整 RL）

### 状态空间

- 40维 V9 机构特征（已有）
- 当前持仓状态（仓位、浮动盈亏）
- 市场制度标签（RegimeDetector 输出）
- 近期交易表现（胜率、连续亏损次数）

### 动作空间

- `{LONG, SHORT, FLAT}` × `{0.01, 0.02, 0.05}` 仓位
- 或连续动作：[−1, +1] 映射到方向和仓位

### 奖励函数

- 主要：已实现 PnL（美元）
- 辅助：夏普比率分量（惩罚高波动）、最大回撤惩罚

### 关键文件（规划中）

| 文件 | 用途 |
|------|------|
| `core/brains/adapters/rl_agent_adapter.py` | RL 智能体适配器 |
| `scripts/training/train_rl_agent.py` | RL 离线预训练（历史回放） |
| `core/feedback/rl_reward_hook.py` | PnL → 奖励信号转换 |

---

## Priority 3: Meta-Learner / Ensemble Weighting (元学习器)

### 为什么是第三优先

- 系统已有 3+ 个大脑在投票，元学习器直接利用现有推理输出
- **零额外训练数据需求** — 只需各大脑的历史预测 + 结果
- 可立即产生价值（动态调整投票权重），但需要多个活跃大脑先运行
- 排在 RL 之后因为 RL 本身的集成价值更高

### 技术方案

#### 阶段 3a: 制度条件加权 (Regime-Conditioned Weighting)

- 立即可用：根据 `RegimeDetector` 输出（低/正常/高波动率）调整各大脑权重
- 例如高波动时给 OU 均值回归模型更高权重，强趋势时给 V9 趋势模型更高权重
- 实现：修改 `ParliamentService` 的投票权重计算

#### 阶段 3b: 在线加权多数 (Online Weighted Majority)

- 每笔交易完成后按各大脑的预测正确性更新权重
- 错误的大脑降权，正确的大脑加权
- 实现：`MetaWeightTracker` 在 `BrainPerformanceTracker` 基础上增加权重追踪

#### 阶段 3c: Stacking 元模型

- 用各大脑的预测输出作为特征，训练一个浅层网络做最终决策
- 需要较多数据（建议 200+ 交易后启动）

### 关键文件（规划中）

| 文件 | 用途 |
|------|------|
| `core/parliament/regime_weighted_voter.py` | 制度条件加权投票 |
| `core/feedback/meta_weight_tracker.py` | 在线权重追踪 |
| `core/brains/adapters/meta_stacking_adapter.py` | Stacking 元模型 |

---

## 实施路线图

```
现在 (2026-05)    →  Priority 1: Online SGD Learner
                  ├─ online_learner_adapter.py
                  ├─ train_online_init.py (barrier labels 初始训练)
                  └─ online_feedback_hook.py (实盘标签增量更新)

1-2周后 (~30笔交易) →  Priority 1 微调 + Priority 3a: Regime Weighted Voting
                    ├─ 验证在线学习权重更新正确
                    └─ regime_weighted_voter.py

1个月后 (~100笔交易) →  Priority 2: Contextual Bandit RL
                     ├─ rl_agent_adapter.py (Bandit 模式)
                     └─ 离线回放验证

3个月后 (~500笔交易) →  Priority 2: PPO RL + Priority 3c: Stacking
                      ├─ 完整 PPO 智能体
                      └─ 元模型集成
```

## 模型类型注册扩展

当前 `BRAIN_TYPE_MAP` → `ADAPTER_REGISTRY` 映射：

| brain_type | 适配器 | 状态 |
|------------|--------|------|
| `onnx_v9` | V9OnnxBrainAdapter | active |
| `xgboost_v4.5` | XGBoostBrainAdapter | active |
| `ou_params_v6` | ParamsBrainAdapter | active |
| **`online_sgd`** | **OnlineLearnerAdapter** | **implementing** |
| `rl_bandit` | RLAgentAdapter (Bandit) | planned |
| `rl_ppo` | RLAgentAdapter (PPO) | planned |
| `meta_stacking` | MetaStackingAdapter | planned |

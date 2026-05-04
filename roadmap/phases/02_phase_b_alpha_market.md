# 方案B：Alpha 市场期（1.5 - 3 年）

> **状态**: 规划中 (40% 组件已存在)
> **目标**: 实现策略的优胜劣汰与资金动态分配。20-50个Alpha并行竞争，系统自动分配资金。
> **前置条件**: 方案A完成。

---

## 核心理念：从单一策略到策略生态

方案A实现了"一个模型产生一个信号"的管道。方案B升级为：
- **多个模型并行运行**（每个是一个Alpha）
- **议会投票机制**融合多信号
- **市场机制**根据表现自动分配资金
- **独立风控Agent**主动生成对冲指令

---

## 已有基础设施

| 组件 | 路径 | 方案B用途 |
|------|------|----------|
| `ParliamentService` | `core/parliament/` | 多信号投票融合 |
| `BrainRegistryService` | `core/features/` | 模型注册和加载 |
| `BrainRunService` | `core/brains/services/` | 并行运行多个Brain |
| `PortfolioAllocator` | `core/alpha/` | 动态资金分配 |
| `PerformanceStore` | `core/alpha/` | Alpha表现记录 |
| `PromotionGate` | `core/alpha/` | Alpha晋升/降级门禁 |
| `BrainPerformanceTracker` | `core/feedback/` | 模型表现追踪 |
| `GovernanceRuleEngine` | `core/governance/` | 规则门禁 |
| `RiskEvaluationService` | `core/risk/` | 风控评估 |

---

## 四阶段实施计划

### 阶段 B-1：Brain并行化

**预期耗时**: 6-8周
**里程碑**: [B1] 多Alpha并行 — 至少10个模型（覆盖 ONNX / XGBoost JSON / OU参数）同时运行

**架构**:
```
brain_entries.json (10-20个条目, brain_type 多样化)
        │
        ▼
BrainRegistryService.load_all()
        │
        ├──→ BrainRunService[mtx_transformer.onnx]   → DecisionCandidate{side: LONG, confidence: 0.82}
        ├──→ BrainRunService[mtx_xgboost.json]       → DecisionCandidate{side: LONG, confidence: 0.79}
        ├──→ BrainRunService[sur_institutional.onnx] → DecisionCandidate{side: LONG, confidence: 0.65}
        ├──→ BrainRunService[arb_ou.params.json]     → DecisionCandidate{side: SHORT, confidence: 0.73}
        ├──→ BrainRunService[trend_h1.onnx]          → DecisionCandidate{side: FLAT, confidence: 0.45}
        ├──→ ...
        │
        ▼
ParliamentService.vote()  ← 多信号融合（统一 DecisionCandidate 接口）
```

**实施步骤**:

1. 扩展 `brain_entries.json` 至10+条目
   ```json
   {
     "brains": [
       {"brain_id": "mtx_transformer_v2", "brain_type": "onnx_v9", "model_path": "models/mtx_v2.onnx", "weight": 1.0, "enabled": true},
       {"brain_id": "mtx_xgboost_v2", "brain_type": "xgboost_json", "model_path": "models/mtx_v2.xgb.json", "weight": 0.9, "enabled": true},
       {"brain_id": "sur_institutional_v3", "brain_type": "onnx_v9", "model_path": "models/sur_v3.onnx", "weight": 1.0, "enabled": true},
       {"brain_id": "arb_ou_stat_v1", "brain_type": "ou_params_json", "model_path": "models/arb_v1.params.json", "weight": 0.8, "enabled": true},
       {"brain_id": "trend_h1_v1", "brain_type": "onnx_v9", "model_path": "models/trend_h1.onnx", "weight": 0.7, "enabled": true},
       {"brain_id": "meanrev_m15_v1", "brain_type": "onnx_v9", "model_path": "models/meanrev_m15.onnx", "weight": 0.7, "enabled": true},
       {"brain_id": "xgboost_momentum_v1", "brain_type": "xgboost_json", "model_path": "models/momentum_v1.xgb.json", "weight": 0.6, "enabled": true},
       {"brain_id": "lgbm_volatility_v1", "brain_type": "lightgbm_txt", "model_path": "models/volatility_v1.lgbm.txt", "weight": 0.5, "enabled": true}
     ]
   }
   ```


2. 实现 `BrainRunService` 的并行调度（asyncio 或 线程池）
3. 每个Brain的输出独立记录到 `BrainPerformanceTracker`
4. 性能基准：10个Brain在1秒内完成推理

---

### 阶段 B-2：议会治理

**预期耗时**: 8-10周
**里程碑**: [B2] 议会治理上线 — ParliamentService 取代单一策略

**投票机制设计**:

```
输入: N个 Brain 的 DecisionCandidate
         │
         ▼
    ┌─────────────────────────────────────┐
    │         ParliamentService            │
    │                                      │
    │  权重计算:                            │
    │    base_weight (注册表配置)            │
    │    × performance_weight (近期夏普)     │
    │    × correlation_penalty (与其它信号共线则降权) │
    │                                      │
    │  投票规则:                            │
    │    如果 LONG 票加权和 > 阈值 → LONG   │
    │    如果 SHORT 票加权和 > 阈值 → SHORT │
    │    否则 → FLAT (不交易)               │
    │                                      │
    │  法定人数:                            │
    │    至少 60% 的Brain产生有效信号       │
    │    否则不产生任何决策                  │
    └─────────────────────────────────────┘
         │
         ▼
    最终 DecisionIntent
```

**实施步骤**:

1. 实现加权投票算法（基于夏普比率和近期表现）
2. 实现相关性惩罚（两个Brain信号高度共线时降低两者权重）
3. 法定人数检查（足够多的Brain参与才投票）
4. 投票历史记录到 ledger 供回溯分析

---

### 阶段 B-3：Alpha市场机制

**预期耗时**: 10-14周
**里程碑**: [B3] Alpha市场机制 — PortfolioAllocator 每周自动重分配资金

**资金分配算法**:

```
每周评估:
  对每个 Alpha:
    1. 计算近30日夏普比率 (Sharpe_30d)
    2. 计算近30日最大回撤 (MaxDD_30d)
    3. 计算与其他Alpha的平均相关性 (AvgCorr)
    
  分配权重 = f(Sharpe_30d, -MaxDD_30d, -AvgCorr)

  f() 可以是:
    - Kelly Criterion 变体
    - 风险平价 (Risk Parity)
    - 等权重 + 止损淘汰
    - 强化学习 (LLM/RL) 动态决策
```

**实施步骤**:

1. 实现 `PerformanceStore` 持久化每个Alpha的日收益
2. 实现 `PortfolioAllocator.weekly_rebalance()`
3. 分配结果写入 `engine_config.json`，由 `ConfigHotReload` 生效
4. 分配边界保护（任何单一Alpha不超过总资金的30%）
5. 资金分配决策记录到 ledger 供审计

---

### 阶段 B-4：独立风控Agent

**预期耗时**: 12-16周
**里程碑**: 独立风控上线

**独立风控 = 不仅仅是信号门禁**:

当前 `RiskEvaluationService` 只是"拒绝/通过"信号的过滤器。独立风控Agent需要：

1. **主动监控相关性异动**
   ```
   检测: 黄金 vs 美元指数的20日滚动相关性
   如果: 相关性发生超过 2σ 的偏离
   动作: 生成对冲指令（如买入黄金看跌期权或做空美元指数）
   ```

2. **尾部风险对冲**
   ```
   检测: VIX 或隐含波动率超过阈值
   动作: 自动减少总敞口至50%
   ```

3. **时段风控**
   ```
   重大新闻前 (FOMC, NFP): 自动减仓或平仓
   流动性枯竭时段: 禁止开新仓
   ```

4. **与Alpha市场联动**
   ```
   当某Alpha在某品种上连续亏损3笔:
     → 该Alpha在该品种上的权重临时降为0
     → 3日后自动恢复观察
   ```

---

## 方案B完成标准

- [ ] 至少10个模型（覆盖 ≥3 种 brain_type）在实盘环境并行运行
- [ ] ParliamentService 加权投票产生最终决策
- [ ] PortfolioAllocator 每周自动重分配资金
- [ ] 独立风控Agent能主动生成对冲指令
- [ ] Dashboard 展示各Alpha资金分配和整体敞口
- [ ] 单Alpha故障不影响其它Alpha运行

---

## 关键风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 多模型并行推理延迟 | 中 | 高 | 批量推理 + GPU加速；XGBoost 和参数型推理天然<1ms |
| 议会投票法定人数不足（信号稀疏）| 中 | 中 | 放宽法定人数阈值 + 补充简单规则Agent |
| Alpha间高度共线 | 高 | 中 | 组合优化中加入相关性惩罚项 |
| 风控Agent误判对冲需求 | 中 | 高 | 对冲指令也需经过风控门禁 |
| 资金分配过于激进 | 中 | 高 | 宪法第一条 + 分配边界保护 |

---

> **最后更新**: 2026-05-01
> **关联 ADR**: 待记录
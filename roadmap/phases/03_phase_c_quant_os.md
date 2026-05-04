# 方案C：自驱动 Quant OS（3 - 5 年）

> **状态**: 规划中 (10% 组件已存在)
> **目标**: 机器自我繁衍策略，多模态多智能体系统，最高宪法约束一切。
> **前置条件**: 方案B完成。

---

## 核心理念：从人工设计策略到机器自我演化

方案B实现了"多Alpha竞争、议会投票"。方案C升华为：
- **基因池**: 机器自动组合特征、修改超参、生成新策略
- **沙盒进化**: 新策略自动回测 → 纸质交易 → 实盘晋升
- **动态执行**: 深度学习预测微观订单簿，滑点变套利
- **最高宪法**: 你只设定不可逾越的边界，其余全部自治

---

## 已有基础设施

| 组件 | 路径 | 方案C用途 |
|------|------|----------|
| `your_trainer.py` | `scripts/training/` | 策略训练模板（基因基础） |
| `run_train_batch.py` | `scripts/training/` | 批量训练调度 |
| `lane_trainers.json` | `scripts/training/` | Trainer注册表（含 model_type 多样性） |
| `ModelArtifactAdapter` | `core/brains/adapters/base_adapter.py` | 模型蓝本抽象层（方案A产出） |
| `BrainFactory` | `core/brains/services/brain_factory.py` | 按 brain_type 路由到正确 Adapter |
| `PromotionGate` | `core/alpha/` | 自动晋升评估 |
| `ShadowLiveIntentProducer` | `scripts/` | 沙盒环境 |
| `PerformanceStore` | `core/alpha/` | 策略表现持久化 |
| `GovernanceRuleEngine` | `core/governance/` | 宪法执行引擎 |
| `SystemModeState` | `core/state/` | 模式管理 |

---

## 四阶段实施计划

### 阶段 C-1：基因池与策略自动繁衍

**预期耗时**: 16-24周
**里程碑**: [C1] 自动策略繁衍

**基因池设计**:
```
策略模板库 (基于现有 trainer 架构, model_type 是核心基因维度)：
  ├── 模型类型基因 (决定产出物格式 → 决定实盘 Adapter):
  │     onnx_v9         → 产出 .onnx       → V9OnnxBrainAdapter
  │     xgboost_json    → 产出 .xgb.json    → XGBoostBrainAdapter
  │     ou_params_json  → 产出 .params.json → OUParamsBrainAdapter
  │     lightgbm_txt    → 产出 .lgbm.txt    → LightGBMBrainAdapter (NEW)
  │     pytorch_jit     → 产出 .pt          → TorchJITBrainAdapter (NEW)
  │   
  ├── 特征组合基因:
  │     price_action, ema_cross, macd_divergence, adx_breakout, bb_squeeze, ...
  │   
  ├── 模型架构基因:
  │     LSTM, Transformer, GRU, LightGBM, XGBoost, CatBoost, ...
  │   
  ├── 超参基因:
  │     lookback_window: [20, 50, 100, 200]
  │     learning_rate: [1e-3, 1e-4, 1e-5]
  │     layers: [2, 3, 4]
  │     dropout: [0.1, 0.2, 0.3]
  │   
  └── 时间框架基因:
        H1, H4, D1, W1
```

**繁衍算法**:

1. **变异 (Mutation)**
   ```
   从现有表现最好的策略中随机修改:
     - 替换一个特征
     - 修改一个超参 (±20%)
     - 改变时间框架
     - 切换模型类型 (如 ONNX → XGBoost, 或反之)
       注意: 模型类型切换意味着产出物格式变化，需同步切换实盘 Adapter
   ```

2. **交叉 (Crossover)**
   ```
   取两个表现好的策略:
     - 取A的特征集合 + B的模型架构 + C的模型类型
     - 取A的超参 + B的时间框架
     交叉产生的模型类型必须存在于 ModelArtifactAdapter 注册表中
   ```

3. **LLM辅助生成**
   ```
   输入: 市场微观结构描述 + 最近另类数据特征 + 可用 Adapter 清单
   LLM: 生成新的特征工程代码 + 策略逻辑 + 对应的 brain_type
   ```

**实施步骤**:
1. 定义策略基因编码格式，**model_type 为必填字段**（JSON/YAML）
2. 实现 `GenePool` 类管理所有策略基因
3. 实现变异和交叉算子（含 model_type 切换逻辑）
4. 自动生成 `trainer` 配置（含 model_type → 产出物映射）并提交 `run_train_batch.py`
5. 训练完成后自动注册到 `brain_entries.json`（含正确的 brain_type）
6. 若新 model_type 尚无对应 Adapter，自动创建 Adapter 骨架（基于 `base_adapter.py` 模板）

---

### 阶段 C-2：沙盒进化流水线

**预期耗时**: 12-16周
**里程碑**: [C2] 沙盒自动晋升

**完整进化流水线**:
```
新策略诞生 (C-1)
        │
        ▼
┌──────────────────────────┐
│ 阶段1: 回测验证 (7天)      │
│   - 历史数据回测            │
│   - 夏普 > 0.5 通过        │
│   - 最大回撤 < 宪法阈值    │
└──────────────────────────┘
        │ (通过)
        ▼
┌──────────────────────────┐
│ 阶段2: 沙盒观察 (90天)     │
│   - ShadowLive 纸质交易    │
│   - 每日对账               │
│   - 记录所有信号和模拟成交  │
└──────────────────────────┘
        │ (90天后)
        ▼
┌──────────────────────────┐
│ 阶段3: PromotionGate评估   │
│   - 信息比率 > 阈值        │
│   - 90%交易日有信号        │
│   - 无单日回撤超限          │
└──────────────────────────┘
        │ (通过)
        ▼
┌──────────────────────────┐
│ 阶段4: 小资金实盘 (30天)   │
│   - 初始资金: 总资金的1%   │
│   - 每日监控               │
│   - 任何回撤超限→退回沙盒  │
└──────────────────────────┘
        │ (通过)
        ▼
┌──────────────────────────┐
│ 阶段5: 正式Alpha           │
│   - 加入 Alpha市场 (方案B) │
│   - 参与议会投票           │
│   - PortfolioAllocator分配│
└──────────────────────────┘
```

**实施步骤**:
1. 扩展 `PromotionGate` 支持多阶段评估
2. 实现自动化回测调度（基于现有 `run_train_batch.py`）
3. ShadowLive 环境自动化（无需人工启动）
4. 晋升/降级决策记录到 ledger
5. 小资金实盘阶段有熔断保护

---

### 阶段 C-3：动态流动性捕获

**预期耗时**: 20-28周
**里程碑**: 动态执行上线

**从执行到套利**:

当前执行层只负责"按信号下单"。方案C的执行层需要：

1. **订单簿预测模型**
   ```
   输入: L2订单簿快照 (最近N个tick)
   输出: 未来K秒的价格方向和订单簿失衡方向
   模型: 轻量级 CNN/LSTM (推理<1ms)
   ```

2. **智能挂单策略**
   ```
   如果预测价格将上涨:
     → 主动吃单 (Market Order) 而非等待限价单成交
   如果预测订单簿将失衡到买方:
     → 提前挂限价买单在较优位置
   ```

3. **滑点捕获**
   ```
   将原本的被动滑点成本 (-2 pips)
   转化为主动的套利利润 (+1~2 pips)
   通过预测微秒级方向在taker/maker之间切换
   ```

4. **TWAP/VWAP 自适应**
   ```
   根据实时订单簿深度动态调整拆单节奏
   而非固定的时间间隔
   ```

**实施步骤**:
1. 收集L2订单簿历史数据
2. 训练订单簿预测模型（轻量级，推理<1ms）
3. 集成到 `ExecutionManager` 中
4. 在 `StubAdapter` 上验证预测准确率
5. 实盘以最小手数测试

---

### 阶段 C-4：最高宪法自治 + K8s部署

**预期耗时**: 12-16周
**里程碑**: 全自治系统上线

**最高宪法的技术落地**:

```
main.py 中硬编码的不可变约束:
┌────────────────────────────────────────┐
│ 宪法参数 (修改需要物理重启)              │
│                                        │
│ MAX_TOTAL_DRAWDOWN = 0.25              │
│ MAX_SINGLE_POSITION_PCT = 0.10         │
│ MAX_CORRELATED_EXPOSURE_PCT = 0.30     │
│ MIN_SHARPE_FOR_PROMOTION = 0.5         │
│ SHADOW_OBSERVATION_DAYS = 90           │
│ EMERGENCY_LIQUIDATION_TIMEOUT = 30     │
│                                        │
│ 运行时参数 (可热加载):                  │
│   engine_config.json ← ConfigHotReload │
└────────────────────────────────────────┘
```

**K8s 集群架构**:
```
┌─────────────────────────────────────────────┐
│              Kubernetes Cluster              │
│                                              │
│  ┌─────────────┐  ┌─────────────┐           │
│  │ main.py     │  │ ShadowLive  │           │
│  │ (中枢引擎)   │  │ (沙盒Pod)   │           │
│  │ replica: 1  │  │ replica: N  │           │
│  └─────────────┘  └─────────────┘           │
│                                              │
│  ┌─────────────┐  ┌─────────────┐           │
│  │ Training    │  │ Feature     │           │
│  │ Job (Cron)  │  │ Update Job  │           │
│  │             │  │ (Cron)      │           │
│  └─────────────┘  └─────────────┘           │
│                                              │
│  ┌─────────────┐  ┌─────────────┐           │
│  │ MT5 Bridge  │  │ Dashboard   │           │
│  │ (sidecar)   │  │ (Web UI)    │           │
│  └─────────────┘  └─────────────┘           │
│                                              │
│  ┌─────────────────────────────┐            │
│  │   Persistent Volume (Ledger) │            │
│  └─────────────────────────────┘            │
└─────────────────────────────────────────────┘
```

---

## 方案C完成标准

- [ ] 系统能自动组合特征 + 模型类型 生成新策略并提交训练
- [ ] 新策略自动经历回测→沙盒→小资金→正式Alpha全流程
- [ ] PromotionGate 在90天观察期后自动评估晋升/降级
- [ ] 执行层能预测订单簿方向并优化执行
- [ ] 任何自动决策不违反最高宪法
- [ ] K8s集群管理所有Pod
- [ ] 人类只需设定宪法参数，其余全自治

---

## 关键风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 自动繁衍的策略过拟合 | 高 | 高 | 严格的沙盒观察期 + 小资金入门 + 表现跟踪 |
| 订单簿预测模型实盘失效 | 高 | 中 | 预测仅作为执行优化，不改变信号方向 |
| 系统自治过度 | 中 | 极高 | 宪法硬编码 + EMERGENCY熔断 + 人类最终控制权 |
| 基因池搜索空间爆炸（含模型类型多样性）| 中 | 中 | 基于贝叶斯优化的搜索 + 淘汰低效基因；model_type 限制在已有 Adapter 范围内 |
| K8s 运维复杂度 | 中 | 中 | 渐进式迁移，先单机后集群 |

---

> **最后更新**: 2026-05-01
> **关联 ADR**: 待记录
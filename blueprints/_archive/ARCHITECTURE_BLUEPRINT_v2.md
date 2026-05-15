# Quant OS — 架构蓝图 v3.1 (2026-05-13)

> **快照时间**: 2026-05-13T03:00:00Z
> **基准提交**: Phase D + P0-P3 + Phase E (5.12 ping-pong fix) + Phase F (大脑可插拔解耦) + PnP 记录前置修复 + 数据质量修复 + 特性存储满仓修复 + Online_MLP_V1 治理恢复
> **测试**: 2452 passed, 0 failed
> **维护**: Team + Agent

---

## 1. 系统拓扑

```
┌─────────────────────────────────────────────────────────────────────┐
│                        main.py (CLI中枢)                             │
│  live | train | status | dashboard | recap | daily | shadow | ...   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  Live Launch  │  │  Training CLI │  │  Ops & Recap  │
│ live_launcher │  │ batch_train   │  │ daily_ops     │
│ intent_loop   │  │ recipe_search │  │ cost_report   │
│ bridge_worker │  │ register_brain│  │ healthcheck   │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                  │
        ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      ServiceContainer (DI)                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ Brain    │ │ Feature  │ │ Execution│ │ Risk     │ │ Governance│ │
│  │ Factory  │ │ Service  │ │ Manager  │ │ Service  │ │ Service   │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │Parliament│ │ Feedback │ │ Alpha    │ │Deployment│ │Observab. │  │
│  │ Service  │ │ Loop     │ │Registry  │ │ Pipeline │ │ Stack    │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Ledger (Event Store)                           │
│  decisions │ communications │ runtime_evidence │ replays │ journals  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    MT5 Bridge (Outbox Pattern)                        │
│  outbox/ → bridge_worker → MT5 → receipts/ → journal                │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. 模块清单 (19 core packages + 30+ scripts)

### Core 包

| # | Package | 行数(估) | 核心职责 | 测试覆盖 |
|---|---------|---------|---------|---------|
| 1 | `alpha` | ~2,000 | 策略生命周期、资本分配、风控预算 | ✅ 6 test files |
| 2 | `brains` | ~4,000 | 6 种模型适配器 + 工厂 + 权重 + 归因 | ✅ 3 test files |
| 3 | `contracts` | ~1,500 | 50+ 领域数据类 + 枚举 + 序列化 | — (shared) |
| 4 | `deployment` | ~6,000 | CI/CD 门禁、合规审计、SLO、发布流水线 | ✅ 15+ test files |
| 5 | `execution` | ~10,000 | 订单执行、策略线、组合风控、质量分析、Kelly | ✅ 8 test files |
| 6 | `features` | ~3,000 | 特征计算、存储、归一化、数据增强 | ✅ 3 test files |
| 7 | `feedback` | ~2,500 | 反馈循环、PnL 账本、性能追踪、贝叶斯校准 | ✅ 2 test files |
| 8 | `governance` | ~1,500 | 大脑治理规则引擎 (5 规则) | ✅ 2 test files |
| 9 | `ledger` | ~4,000 | JSONL 事件存储 + 20+ 读写服务 | ✅ 5 test files |
| 10 | `market` | ~500 | 持仓追踪、信号过滤 | — |
| 11 | `metrics` | ~500 | Sharpe/Sortino/Calmar/Omega 等 8 指标 | — |
| 12 | `observability` | ~2,000 | 事件总线、告警、审计、SLO、追踪 | ✅ 2 test files |
| 13 | `parliament` | ~1,000 | 多脑决策聚合 + 合约分组 | ✅ 3 test files |
| 14 | `protocol` | ~3,000 | 通信适配器、调度器、消息构建 | ✅ 5 test files |
| 15 | `risk` | ~1,000 | 风控策略评估 (6 policies) | ✅ 1 test file |
| 16 | `runtime` | ~5,500 | 实时循环、执行管道、信号健康、审批链、回放 | ✅ 8 test files |
| 17 | `state` | ~500 | 系统模式 + 覆写存储 | — |
| 18 | `strategies` | ~500 | 策略插件协议 + 注册表 | ✅ 1 test file |
| 19 | `training` | ~1,500 | 数据集、协议、检查点、实验追踪、模型卡 | — (infrastructure) |

### Scripts (30+)

| 类别 | 脚本数 | 说明 |
|------|--------|------|
| 实盘操作 | 12 | live_intent_loop, live_launcher, bridge_worker, send_live_order, ... |
| 监控诊断 | 8 | live_monitor, healthcheck, stack_diagnostic, data_quality, ... |
| 训练 | 22 | 8 trainers + batch_train + label_builder + dataset_builder + champion_challenger + ... |
| 运维 | 5 | daily_ops, cost_report, market_calendar, feature_warmer, ... |
| 验证 | 4 | smoke_test_e2e, verify_all_brains, journal_validator, feature_quality_validator |

### 大脑矩阵 (17 registered brains, all shadow — 2026-05-12)

| Brain ID | Type | 特征维度 | 推理引擎 | 方向信号 |
|----------|------|---------|---------|---------|
| V9_Institutional_01 | onnx_v9 | 40 | ONNX | ✅ |
| CRT.sur.chlg.g2026.1 | onnx_v9 | 40 | ONNX | ✅ |
| DeepResMLP_V1_Institutional | onnx_v9 | 40 | ONNX | ✅ |
| LightGBM_V1_Institutional | lightgbm_v1 | 40 | .txt | ✅ |
| LightGBM_V2_Retrained | lightgbm_v1 | 40 | .txt | ✅ |
| XGBoost_V9_Institutional | xgboost_v9 | 40 | JSON | ✅ |
| XGBoost_V10_Retrained | xgboost_v9 | 40 | JSON | ✅ |
| XGBoost_V4.5_Microstructure | xgboost_v4.5 | 9 (micro) | JSON | ✅ |
| Microstructure_Transformer_V5.0 | transformer_v4.3 | 9×32seq | ONNX | ✅ 96.4% |
| Online_MLP_V1 | online_sgd | 40 | numpy | ✅ 80% (live, governance restored 05-13) |
| OU_Params_V6_Sniper | ou_params_v6 | price seq | Kalman | 极端偏离 |
| SurvivalAlpha_Ensemble | (group) | — | 多脑融合 | ✅ |
| TreeAlpha_Ensemble | (group) | — | LGB+XGB | ✅ |
| Online_SGD_V1 (legacy) | online_sgd | 40 | sklearn | ✅ |

---

## 3. 数据流全景 (当前架构)

```
MT5 Terminal ──► V9FeatureComputer ──► LocalFeatureStore
                  MicroFeatureComputer ─┘
                        │
                        ▼
              FeatureService.build_vector()
                        │
                        ▼
              BrainFactory ──► 17 adapters.infer()
                        │
                        ▼
              StrategyLine.evaluate()  ◄── 3-5 条策略线并行
                ├─ _build_strategy_lines() (按 contract_group 自动分组)
                ├─ DynamicBrainWeighter (实盘P&L加权)
                ├─ ── PnP LEDGER RECORDING (审批门前) ──
                │   └─ BrainPnLStore.record_signal() 逐 proposal try/except
                ├─ ContractGroupConsensus (加权/Union投票 + 中性处理)
                ├─ BrainRegistry.instance() (统一查询 brain_id→contract_group)
                ├─ RegimeGate (M5→H1→H4→D1 多周期)
                ├─ compute_dynamic_sl_tp() (波动率自适应)
                └─ _compute_volume() (vol-targeted 手数)
                        │
                        ▼
              PortfolioRiskController.check()
                ├─ 总敞口 / 净敞口 / 同向集中度
                ├─ VaR / CVaR (历史模拟法)
                └─ 策略间相关性惩罚
                        │
                        ▼
              ExecutionQueue.flush() (优先级错开)
                        │
                        ▼
              dispatch_live_open_order() ──► entry_context → Journal
                        │
                        ▼
              SignalHealthMonitor ──► 数据新鲜度/ATR异常/预测漂移/点差异常
                        │
                        ▼
              Reconciliation ──► Per-Strategy SL Streak + VaR Buffer Update
                        │
                        ▼
              BrainPnLStore ←── FeedbackLoop
                        │
                        ▼
              DynamicBrainWeighter ← GovernanceService
```

---

## 4. 已完成的机构化升级 (P0-P3: 基础设施)

| 层级 | 能力 | 文件 |
|------|------|------|
| P0 | 周末/节假日清仓 (T-30/T-5) | market_calendar.py, live_cycle.py |
| P0 | SystemMode 持久化 (24h stale) | system_mode_store.py |
| P1 | Bridge 重试 (3x, transient) | mt5_bridge_worker.py |
| P1 | 成交后仓位验证 | mt5_bridge_worker.py |
| P2 | 仓位状态完整存取 | position_manager.py |
| P2 | Config 热加载 | config_hot_reload.py |
| P2 | 优雅退出保存所有状态 | live_intent_loop.py |
| P3 | 会话感知交易 (7 sessions) | pre_trade_guards.py |
| P3 | VaR 检查 + Tick/FV 质量守卫 | pre_trade_guards.py |
| P3 | 执行成本日报 | daily_cost_report.py |
| P3 | 滑点时间序列追踪 | quality_analyzer.py |
| P3 | 连续止损熔断 + 日志预扫描 | live_cycle.py |
| P3 | 执行队列错开发送 | execution_queue.py |

---

## 5. Phase D: 模型-实盘对齐升级 (D1-D7)

| 层级 | 能力 | 文件 |
|------|------|------|
| D1 🔴 | Micro Barrier 合约 (1.5×ATR SL / 2.5×ATR TP / 5bar) | contracts/label-micro-barrier-1.0.0.json |
| D1 | Micro SL/TP 参数更新 (configs) | configs/live.yaml |
| D2 🔴 | 动态 SL/TP 范围收窄 [1.2, 3.0] + TP 独立上限 | dynamic_sl_tp.py |
| D2 | SL/TP 分布包络警告日志 | dynamic_sl_tp.py |
| D3 🔴 | 脑结果记录方向修复 (硬编码→实际方向) | live_cycle.py |
| D4 🟡 | H4/D1 多周期趋势检测 (48 M5→1 H4, 6 H4→1 D1) | regime_gate.py |
| D4 | 多周期门控: macro_regime + H4 counter-trend | regime_gate.py, strategy_line.py |
| D5 🟡 | Meta Exit R轨迹评分 + 指数时间衰减 | meta_exit_engine.py |
| D5 | Meta Exit 启发式权重重新校准 | configs/live.yaml |
| D6 🟢 | Micro 部分止盈 (50% @ 1.0R, SL→breakeven) | configs/live.yaml, position_manager.py |
| D7 🟢 | 大脑训练合约兼容性检查 | live_cycle.py |

---

## 6. P0-P2: 实时交易深度审计修复 (2026-05-09)

### P0.1 — Vol-Targeted 仓位系统对接

**问题**: `compute_position_size()` (vol-targeted) 和 `_compute_volume()` (乘法链) 两套手数系统独立运行，互不知晓。

**修复**: 打通 `risk_budget_usd` 参数链:
```
configs/live.yaml → execute_live_cycle() → _evaluate_strategy_lines()
    → StrategyLine.evaluate(risk_budget_usd) → _compute_volume(risk_budget_usd)
        → compute_position_size(risk_budget_usd, atr, sl_atr_mult, ...)
```

| 文件 | 操作 |
|------|------|
| `core/execution/strategy_line.py` | `evaluate()` 新增 `risk_budget_usd` 参数; `StrategyDecision` 新增 `entry_context` 字段; `_compute_volume()` 支持 vol-targeted 基础手数 |
| `core/runtime/live_cycle.py` | 从 config 传递 `risk_budget_usd` 到策略评估 |

### P0.2 — DynamicBrainWeighter 接入策略线

**问题**: `DynamicBrainWeighter` (基于实盘 Sharpe/胜率 映射权重 0.0-3.0) 仅在旧 Parliament 路径生效，新 StrategyLine 路径未调用。

**修复**: 在 `StrategyLine.evaluate()` 推理后、共识前调用 `DynamicBrainWeighter.apply_weights(proposals)`。

| 文件 | 操作 |
|------|------|
| `core/execution/strategy_line.py` | `evaluate()` 中 `_run_inference()` 后调用 DynamicBrainWeighter |

### P1.1 — Journal Entry Context 透传

**问题**: Journal 中无法进行事后分析——缺少 ATR、regime、brain predictions 等上下文。

**修复**: `StrategyDecision.entry_context` 携带 ATR、regime、trend、macro_regime、brain_predictions，通过 `dispatch_live_open_order()` → `execution_payload` → bridge → journal 完整链路透传。

| 文件 | 操作 |
|------|------|
| `core/execution/strategy_line.py` | `evaluate()` 构建 entry_context; `StrategyDecision` 新增字段 |
| `core/execution/live_order_sender.py` | `dispatch_live_open_order()` 新增 `entry_context` 参数 |
| `core/execution/execution_queue.py` | `flush()` 传递 entry_context 到 dispatch_fn |

### P1.2 — Per-Strategy SL Streak 追踪与阻断

**问题**: 全局单一计数器——micro 小额止损会错误阻断 barrier 策略线。

**修复**: 改为每策略独立计数器 + 全局覆盖 (3+ 策略各 ≥2 SL → 系统性事件全阻断)。

```
LiveCycleState:
  consecutive_sl_hits: dict[str, int]       # per-strategy counter
  sl_streak_blocked_until: dict[str, float]  # per-strategy block expiry
  sl_streak_blocked_all_until: float         # global block (systemic)
```

| 文件 | 操作 |
|------|------|
| `core/runtime/live_cycle.py` | `LiveCycleState` 字典化; `_strategy_from_brain_ids()` 映射; 对账循环按策略追踪; 评估循环按策略检查阻断 |
| `core/runtime/order_dispatch.py` | `_check_recent_sl_streak()` 新增 `strategy_name` 参数; 迁入 `_strategy_from_brain_ids()` (消除循环导入) |

### P2.1 — 组合风险 VaR/CVaR + 相关性惩罚

**问题**: 仅检查总敞口/净敞口/同向集中度，缺少 VaR 和策略间相关性分析。

**修复**: `PortfolioRiskController` 新增:
- 滚动收益缓冲区 (按策略) + `update_returns()` 供对账后喂入
- `compute_var()` / `compute_cvar()` — 历史模拟法, 95% confidence
- `compute_correlation()` / `compute_correlation_matrix()` — Pearson
- `check()` 中相关性惩罚: 当两策略同向且相关系数 >0.70，手数 ×0.50
- VaR/CVaR 诊断 (非阻塞 warning)
- 控制器持久化在 `LiveCycleState` 中跨周期保留状态

| 文件 | 操作 |
|------|------|
| `core/execution/portfolio_risk.py` | 新增 VaR/CVaR/相关性计算方法; `RiskResult` 新增诊断字段; `check()` 新增 VaR warning + 相关性惩罚 |
| `core/runtime/live_cycle.py` | `LiveCycleState` 新增 `portfolio_risk_controller`; 对账后 `update_returns()` 喂入已实现 P&L |

### P2.2 — 信号功能健康检查

**问题**: 无数据质量监控——数据延迟、预测漂移、ATR 异常、点差扩大可能静默侵蚀策略表现。

**修复**: 新建 `SignalHealthMonitor` (滚动统计 + 4 项检查):
1. **数据新鲜度**: 特征快照年龄 >120s 告警
2. **ATR 异常**: 当前 ATR 超出历史 IQR 3 倍范围告警
3. **预测漂移**: 脑预测 up_prob 均值偏移 >0.30 或 confidence 崩溃 <0.35 告警
4. **点差扩大**: 当前点差% 超出历史 IQR 3 倍范围告警

所有检查为非阻塞 — 仅产生结构化日志事件 `signal_health_warning`。

| 文件 | 操作 |
|------|------|
| `core/runtime/signal_health.py` | **新建** — SignalHealthMonitor + run_signal_health_checks() |
| `core/runtime/live_cycle.py` | `LiveCycleState` 新增 `signal_health_monitor`; 每周期喂入 ATR/点差/预测; 每 20 周期运行完整检查 |

### P2.3 — 共识 `>=` 偏差修复

**问题**: `_compute_consensus()` 中 `if weighted_up >= weighted_down: direction = "long"` — 当两方权重完全相等时偏向做多。

**修复**: 改为严格三分支:
```python
if weighted_up > weighted_down:      direction = "long"
elif weighted_down > weighted_up:    direction = "short"
else:                                return "neutral"  # 无方向优势
```

| 文件 | 操作 |
|------|------|
| `core/execution/strategy_line.py` | `_compute_consensus()` L430: `>=` → `>` + else→neutral |

### P2.6 — 信号健康 → 自主行动闭环

**问题**: SignalHealthMonitor 只检测不行动 — 检测到预测漂移/点差异常后无自动响应。

**修复**: `_derive_actions()` 将健康检查结果映射为4类自主行动:
1. **freeze_lowest_performing_brain** — 预测漂移+confidence崩溃 → 冻结表现最差大脑
2. **reduce_all_position_sizes** — up_prob偏移 → 全局手数×0.70
3. **reduce_new_position_sizes** — 点差扩大 → 分级缩量 (0.40/0.60/0.80)
4. **skip_new_positions** — 数据过期 → 跳过本周期

去重逻辑: 同类 reduce 动作只保留最严格乘数。`LiveCycleState._last_health_volume_mult` 存储并在策略评估时应用。

| 文件 | 操作 |
|------|------|
| `core/runtime/signal_health.py` | 新增 `_derive_actions()` — 4类自主行动映射 + 去重 |
| `core/runtime/live_cycle.py` | `_execute_management_phase()` 处理 health actions; `_evaluate_strategy_lines()` 应用 `health_volume_mult` |

### P2.7 — Dead Capital Allocator → Live P&L 桥接

**问题**: `capital_allocator.compute_optimal_group_weights()` 存在但从未接入实盘数据流 — 是一个"死"模块。

**修复**: `PortfolioRiskController.compute_optimal_allocation()` 桥接方法，从滚动 P&L 缓冲区计算 risk_parity / min_variance / max_sharpe 权重，供 `GovernanceService` 或 manual review 使用。

| 文件 | 操作 |
|------|------|
| `core/execution/portfolio_risk.py` | 新增 `compute_optimal_allocation()` — 调用 `compute_optimal_group_weights()` 从 live P&L 计算权重 |

### P2.8 — 分级连败响应 (0.9^n)

**问题**: 策略在 `max_consecutive_losses` 之前无任何手数缩减 — binary on/off 过于粗暴。

**修复**: `StrategyBudget.get_streak_multiplier()` — 每笔连续亏损后手数×0.90，下限 0.30。硬暂停仍作为最终安全阀在 max_consecutive_losses 触发。`StrategyLine._compute_volume()` 在乘法链末尾应用 streak_mult。

| 文件 | 操作 |
|------|------|
| `core/execution/strategy_budget.py` | 新增 `get_streak_multiplier()` — 0.90^n_losses, floor 0.30 |
| `core/execution/strategy_line.py` | `_compute_volume()` 末尾应用 `budget.get_streak_multiplier()` |

### P2.9 — 动态追踪止盈 + 管理阶段接入

**问题**: 止盈距离固定于入口 ATR — 当波动率收缩时，原 TP 变得过远不切实际。

**修复**: `PositionManager.compute_trail_tp()` — 当当前 ATR 收缩至 <80% 入口 ATR 时，收紧 TP 以匹配当前波动率（TP 只向内移动）。在 `_execute_management_phase()` step 5.2 中调用，与 Chandelier 追踪止损对称。

| 文件 | 操作 |
|------|------|
| `core/execution/position_manager.py` | 新增 `compute_trail_tp()` — ATR 收缩时动态收紧 TP |
| `core/runtime/live_cycle.py` | `_execute_management_phase()` step 5.2 — 调用 trail TP + dispatch modify |

---

## 6b. P3: 高级自适应修复 (2026-05-09)

### P3-10 — Kelly Criterion 仓位优化

**问题**: 仓位大小由 vol-targeted 公式决定，但未考虑策略的实际统计优势（胜率+盈亏比）。

**修复**: `compute_kelly_fraction()` 和 `compute_kelly_risk_budget()` — 从实盘统计计算最优风险比例:
- `f* = (bp - q) / b` — Kelly 最优分数
- 默认使用 half-Kelly (更保守)，上限 max_fraction=0.25
- `compute_kelly_risk_budget()` 组合 Kelly 比例与账户权益计算 USD 风险预算

| 文件 | 操作 |
|------|------|
| `core/execution/pre_trade_guards.py` | 新增 `compute_kelly_fraction()` + `compute_kelly_risk_budget()` |

### P3-11 — Adam 自适应学习率

**问题**: 在线 SGD 使用固定衰减 `lr = 0.01 / (1 + 0.0001*n)` — 对所有参数方向一视同仁。

**修复**: Adam 优化器状态追踪梯度范数的第一/第二矩，为每个 SGD 更新计算自适应学习率:
- `m_t = β₁·m_{t-1} + (1-β₁)·g_t`
- `v_t = β₂·v_{t-1} + (1-β₂)·g_t²`
- `lr_effective = α · m̂_t / (√v̂_t + ε)`
- 默认: α=0.001, β₁=0.9, β₂=0.999, ε=1e-8

梯度范数从前后权重的 Frobenius 范数差 / 学习率估算。

| 文件 | 操作 |
|------|------|
| `core/brains/adapters/online_learner_adapter.py` | `partial_fit()` SGD 路径: Adam 状态变量 + 自适应 lr 替换固定衰减 |

### P3-12 — 贝叶斯阈值校准

**问题**: `BrainPnLStore._assess_health()` 使用固定阈值 (sharpe<-1.0→critical, sharpe<-0.5→degraded…) — 不随市场状态自适应。

**修复**: `calibrate_thresholds()` 从跨大脑分布计算百分位阈值:
- bottom 20% Sharpe/胜率 → "critical", bottom 40% → "degraded"
- top 30% → "healthy"
- `assess_health_calibrated()` 使用校准后阈值（<30 样本回退到固定阈值）
- `get_metrics_calibrated()` — 使用校准阈值的完整健康评估

| 文件 | 操作 |
|------|------|
| `core/feedback/brain_pnl_ledger.py` | 新增 `calibrate_thresholds()` + `assess_health_calibrated()` + `get_metrics_calibrated()`; `FIXED_THRESHOLDS` 类常量 |

### P3-13 — 治理路径补全: probation→frozen + auto-retire

**问题**: 治理规则仅 auto_freeze_critical 和 auto_demote_degraded→probation。缺失:
1. Probation 大脑持续表现差 → 应降级到 frozen
2. 反复冻结的大脑 → 应永久退休

**修复**: 新增两条规则:
- `auto_demote_probation_to_frozen` (priority=80): probation + health∈{critical,degraded} + samples≥20 → frozen
- `auto_retire_repeated_frozen` (priority=100): freeze_count≥3 或 (freeze_count≥2 + critical) → retired

GovernanceService 已原生支持 `VALID_TRANSITIONS["probation"] = {"live", "frozen", "retired"}` 和 `freeze_count` 追踪。

| 文件 | 操作 |
|------|------|
| `core/governance/governance_rule_engine.py` | `with_default_rules()` 新增 2 条规则 + 闭包条件函数 |

---

## Phase E: 5.12 策略互殴修复 (2026-05-12)

### 根因

`barrier_12bar`（趋势跟踪）和 `statarb_dynamic`（均值回归）每5分钟发出相反信号，`net_out` 机制强制相互平仓，全天15次往返≈$4.5纯点差损耗。

### E.1 — net_out → allow_coexist

**问题**: 两条策略相反方向信号 → net_out 强制平仓 → 立即对方再开 → 循环

**修复**: `PortfolioRiskController` 默认 `netting_mode` 从 `"net_out"` 改为 `"allow_coexist"`。两个策略可同时持有相反仓位（对冲），各自按独立SL/TP逻辑退出。

| 文件 | 操作 |
|------|------|
| `core/execution/portfolio_risk.py:66` | `netting_mode` 默认值改为 `"allow_coexist"` |

### E.2 — 最小持仓时间 min_hold_cycles

**问题**: 新开仓位立即被 net_out 机制平掉

**修复**: 新增 `min_hold_cycles=6`（30min@M5），position dict 新增 `entry_cycle` 字段。net_out 逻辑跳过未满最小持仓周期的仓位。

| 文件 | 操作 |
|------|------|
| `core/execution/portfolio_risk.py:67,290-300` | `min_hold_cycles` 参数 + check() 中过滤未满期仓位 |
| `core/runtime/live_cycle.py:2756-2800,1859-1864` | position dict 新增 `entry_cycle` 字段 |

### E.3 — 退役大脑过滤 (已存在)

`_apply_governance_filter()` 已在启动时过滤 retired/frozen 大脑，但旧进程需重启以加载新治理状态。

### E.4 — SL修改日志 (已正确)

`_dispatch_modify_trail` 已使用 `action: "modify_sltp"`，bridge worker 正确记录到 journal。

---

## Phase F: Phase 0 大脑可插拔解耦 (2026-05-12)

**目标**: 消除所有硬编码 `brain_type` 引用，新增/替换大脑只需 1 个 JSON + 1 个 adapter 文件。

### 5 层解耦架构

```
Layer 5: Governance (大脑生命周期管理)
         ↓ 只依赖 BrainQualityVerdict，不直接读大脑配置
Layer 4: Voting (投票权重)
         ↓ 只依赖 vote_weight 数字，不关心大脑类型
Layer 3: Strategy Line (策略线)
         ↓ 只依赖 contract_group 分组，不关心具体大脑
Layer 2: Brain Adapter (模型适配器)
         ↓ 实现统一接口: infer(features) → BrainDecisionProposal
Layer 1: Brain Config (大脑配置 JSON)
         ↓ 声明: brain_id, brain_type, contract_group, training_horizon, feature_schema
```

### F.1 — BrainRegistry 中枢注册表

**新建** `core/brains/brain_registry.py`:
- 从 `configs/brains/*.json` 加载所有大脑元数据
- 三级索引: `brain_id` → `BrainEntry`, `brain_type` → `BrainEntry`, `contract_group` → `list[BrainEntry]`
- 单例访问: `BrainRegistry.instance()`
- 17 个大脑，6 个 contract_group

### F.2 — 消除的硬编码映射

| 已删除 | 原位置 | 改为 |
|--------|-------|------|
| `_HORIZON_BY_TYPE` (24行字典) | `live_cycle.py:67-90` | 读 brain JSON `training_horizon` 字段 |
| `_TYPE_TO_GROUP` 外部引用 | `strategy_line.py:502`, `live_cycle.py` | `get_group_for_contract_group(strategy_name)` |
| `_strategy_from_brain_ids()` 子字符串匹配 | `order_dispatch.py:40-59` | `BrainRegistry.resolve_ids_to_group(brain_ids)` |
| `_build_strategy_lines()` `if brain_type in SET` | `live_cycle.py:1518-1540` | 按 `contract_group` 字段自动分组，未知组跳过+告警 |
| `group_proposals()` 未知类型→barrier 静默路由 | `contract_groups.py:562-577` | 跳过未知 contract_group，打印 JSON 告警 |

### F.3 — Adapter 统一属性

`BaseBrainAdapter` 新增属性（均从 brain JSON 读取）:
- `brain_id` — 大脑唯一标识
- `contract_group` — 归属的策略组 (e.g. 'barrier_12bar')
- `training_horizon` — 训练周期数 (e.g. 12)
- `feature_schema` — 特征类型 (e.g. 'v9_40dim')

### F.4 — Contract Groups 新公共接口

`contract_groups.py` 新增:
- `_GROUP_BY_NAME` — contract_group 名字 → 组定义字典
- `get_group_for_contract_group(name)` → 组定义或 None
- `get_group_for_proposal()` 更新为优先查 `brain_id → BrainRegistry → contract_group`，后 fallback legacy brain_type

### F.5 — 解耦验证

| 检查项 | 状态 |
|--------|------|
| `if brain_type == "xxx"` 硬编码分支 | 仅剩 `brain_factory.py:43` (online_sgd 特殊路径 — adapter 基础设施层正当使用) |
| `brain_type in {set}` 硬编码分支 | **已清零** |
| 新增大脑类型不修改 voting/risk/governance/strategy_line | ✅ |
| 移除大脑不残留硬编码引用 | ✅ |
| 2452 tests passed | ✅ |

---

## Phase G: 实盘数据质量修复 + 治理恢复 (2026-05-13)

### G.1 — PnP 反事实记录前置 (昨日修复)

**问题**: `StrategyLine.evaluate()` 中 PnP 记录位于所有审批门之后。当策略因中性共识、低置信度、逆势阻断等原因提前返回时，StrategyDecision 携带 brain_ids 写入 journal，但 PnP 记录代码从未执行。Online_MLP_V1 始终预测 `short` 成为 barrier_12bar 组的唯一空头，96 次 journal 出现但 0 条 PnP 记录。

**修复 (2 文件)**:
- `core/execution/strategy_line.py`: PnP 记录从审批门之后移至 proposals 收集后、审批门前；逐 proposal try/except 隔离
- `core/runtime/live_cycle.py`: 旧版 multi-brain 路径同样改为逐 proposal try/except

### G.2 — `modify_sltp` 数据质量

**问题**: Journal 校验器 `VALID_ACTIONS = {"open", "close", "modify"}` 缺少 `"modify_sltp"`，所有 SL/TP 修改被标记为 schema 非法。标签生成器 `("close", "modify")` 过滤同样遗漏 `modify_sltp`，导致带 `close_price` 的平仓记录无法生成训练标签。

**修复 (2 文件)**:
- `scripts/validators/journal_validator.py:42`: `VALID_ACTIONS` 添加 `"modify_sltp"`
- `scripts/training/label_builder.py:205`: close 过滤补充 `"modify_sltp"`

### G.3 — `position_not_found` 异常吞没

**问题**: Guard 2 (`live_cycle.py:617-618`) `except Exception: pass` 在 MT5 IPC 异常时沉默跳过，允许对已关闭仓位继续派发 `modify_sltp`。5/12 共 49/50 次拒绝为 modify_sltp 对已平仓 ticket 重试。

**修复**: `except Exception: pass` → 打印 `position_manager_mt5_unreachable` 事件并 `return False`，阻断不可验证仓位的派发。

### G.4 — 特性存储满仓停更

**问题**: `execute_live_cycle()` 中特性计算和 FeatureStore 写入在仓位限制检查之后。满仓时早退跳过所有特性计算，FeatureStore 完全停止更新。盯盘确认 17 个周期无新记录。

**修复**: 特性计算 + FeatureStore 持久化 + 特性新鲜度检查全部移至仓位限制检查之前。

### G.5 — Online_MLP_V1 治理误伤恢复

**数据**:

| Brain | Win | Loss | BE | WinRate | AvgScore | 状态 |
|-------|-----|------|-----|---------|----------|------|
| DeepResMLP_V1_Inst | 0 | 83 | 8 | 0.0% | 0.285 | live |
| LightGBM_V1_Inst | 10 | 73 | 8 | 11.0% | 0.388 | live |
| XGBoost_V9_Inst | 10 | 73 | 8 | 11.0% | 0.388 | live |
| **Online_MLP_V1** | **10** | **73** | **8** | **11.0%** | **0.372** | live ✅ |

**结论**: Online_MLP_V1 表现与 LightGBM_V1/XGBoost_V9 完全相同，显著优于 DeepResMLP_V1。自 5/10 降为 probation 后再无重新评估——其他 3 个 brain 在此期间经历了 2-5 次降级→复活循环。根因是 PnP Ledger 缺失（G.1），治理系统无法看到 PnP 改善数据。此外其唯一 `short` 偏好在 barrier_12bar 组提供方向多元化对冲价值。

**修复 (1 文件)**:
- `data/governance_state.json`: `probation` → `live`，移除 `exposure_limited`，添加完整 transition log

---

## 7. 三条策略线对比

| 维度 | barrier_12bar | micro_3bar | statarb_dynamic |
|------|:---:|:---:|:---:|
| 大脑数 | 5-7 institutional | 2-3 microstructure | 1 OU Kalman |
| 训练合约 | survival_barrier (2.0sl/3.5tp/12bar) | survival_barrier (1.5sl/2.5tp/5bar) | ou_params |
| 时间窗口 | 60 min | 25 min | 动态 (OU half-life) |
| SL/TP 模式 | 动态 (ATR自适应) [1.2, 3.0] | 动态 (ATR自适应) [1.2, 3.0] | OU 边界 |
| 部分止盈 | false | 50% @ 1.0R → BE | false |
| 手数基准 | vol-targeted (risk_budget) | vol-targeted (risk_budget) | 固定 × vol_factor |
| 趋势门控 | H1+H4 counter-trend | 仅强趋势阻挡 | 永不阻挡 |
| macro_regime | risk_off → vol×0.7 | 不限制 | 不限制 |
| 大脑权重 | DynamicBrainWeighter | DynamicBrainWeighter | 固定 |

---

## 8. 北极星原则

1. 先活下来，再跑得快 — 风控优先
2. 先可解释，再加复杂度 — 每个决策可追溯
3. 先稳主链路，再扩多模型 — 执行链路零容忍故障
4. 所有进化必须可回滚 — 增量、幂等、不破坏现有
5. 用真实运行数据驱动下一次迭代 — 数据 > 假设

---

## 9. Live/Shadow 大脑投票架构审计 (2026-05-12)

### 9.1 现状诊断

**当前状态**: 全部 17 个大脑 `status: shadow`，`default_mode: shadow`。

**用户期望模式**: "live 里的 A 模型应该只能由 shadow 里面的同类型 BCD 模型共同投票决定开单/闭单等行为"

**实际运行模式**: 所有大脑（无论 status）在同一 contract_group 内平等投票 → 共识 → RegimeGate 决定是否实盘执行。

```
实际流程:
  Brain1 (shadow) ─┐
  Brain2 (shadow) ─┤
  Brain3 (shadow) ─┼─→ ContractGroupConsensus ─→ StrategyDecision
  Brain4 (shadow) ─┤         │
  Brain5 (shadow) ─┘         ▼
                       RegimeGate
                     ┌───────┴───────┐
                     │               │
                 shadow          live
              (volume=0)     (volume=real)
              (venue=shadow)  (venue=live)
```

### 9.2 与用户期望模式的差异分析

| 维度 | 用户期望 | 当前实际 | 评估 |
|------|---------|---------|------|
| 谁投票 | 同类型 shadow BCD 投票给 live A | 同 contract_group 内所有大脑平等投票 | ✅ 当前更优：更多大脑→更稳健信号 |
| 谁决定交易 | live A 独立决策 | contract_group 共识 + RegimeGate | ✅ 当前更优：共识比单脑决策更可靠 |
| live/shadow 区分 | 按 brain_id 区分 | 按 RegimeGate 区分 | ⚠️ 存在差距：brain status 未用于投票门控 |
| 权重分配 | 固定？ | DynamicBrainWeighter (实盘P&L驱动) | ✅ 当前更优：自适应权重 |
| 大脑退役/frozen | governance 自动处理 | `_apply_governance_filter()` 启动时过滤 | ⚠️ 运行时未重新检查 |

### 9.3 结论：当前模式合理，但有一个缺口

**当前架构是合理的，且比用户期望的 "Live A + Shadow B/C/D" 模式更优**。理由：

1. **多样性 > 单点**: 同一 contract_group 内 5-8 个大脑共同投票，产生的信号比单个 "live A" 决策更稳健
2. **可插拔**: Phase 0 解耦后，任何大脑可随时加入/退出 contract_group，无需修改投票逻辑
3. **权重自适应**: DynamicBrainWeighter 根据实盘P&L动态调整权重，表现好的大脑自动获得更多话语权
4. **RegimeGate 作为最终开关**: 是否实盘执行由 RegimeGate 决定，不依赖大脑的 live/shadow 标签

**存在的缺口**:

| # | 问题 | 严重度 | 修复 |
|---|------|--------|------|
| 1 | `_build_strategy_lines()` 未按 `status` 过滤大脑 — frozen/retired 大脑仍参与投票 | 🔴 高 | 在分区前过滤 `status not in ("frozen", "retired")` |
| 2 | 17 个大脑中仅 6 个 live — 治理已分化 live/probation/retired 三态 | 🟢 已解决 | 治理引擎持续评估；Online_MLP_V1 于 05-13 从 probation 恢复为 live |
| 3 | RegimeGate 控制所有策略线同步切换 shadow↔live — 无法按策略粒度独立控制 | 🟡 中 | RegimeGate 已支持 `strategy_activation` 按策略粒度调节，只需配置 |
| 4 | `default_mode: shadow` — 系统不会自主进入 live 模式，需手动切换 | 🟢 低 | 现状是最安全默认值；待大脑通过 shadow PnL 验证后手动开启 |

### 9.4 设计建议：正确的 live/shadow 投票模式

推荐的演化路径（Plan Phase 1-5 实施后）：

```
未来架构:
  ┌─────────────────────────────────────────────┐
  │           contract_group: barrier_12bar       │
  │                                               │
  │  live brains (governance promoted):           │
  │    DeepResMLP_V1_Institutional (status: live)      │
  │    LightGBM_V1_Institutional   (status: live)      │
  │    Online_MLP_V1               (status: live, restored 05-13) │
  │    OU_Params_V6_Sniper         (status: live)      │
  │    XGBoost_V9_Institutional    (status: live)      │
  │                                               │
  │  shadow/retired brains:                         │
  │    CRT.sur.chlg.g2026.1       (status: retired)  │
  │    LightGBM_V2_Retrained      (status: retired)  │
  │    LightGBM_V3_New            (status: retired)  │
  │    V9_Institutional_01        (status: retired)  │
  │    XGBoost_V10_Retrained      (status: retired)  │
  │    XGBoost_V11_New            (status: retired)  │
  │    XGBoost_V4.5_Microstructure(status: retired)  │
  │                                               │
  │  所有 active 大脑 (live + shadow) 平等投票    │
  │  → ContractGroupConsensus                     │
  │  → DynamicBrainWeighter (P&L 加权)            │
  │  → RegimeGate (至少1个live brain时允许live)   │
  └─────────────────────────────────────────────┘
```

**核心原则**:
1. **投票权 = 同 contract_group + 非 retired/frozen** — live 和 shadow 大脑平等投票
2. **执行权 = RegimeGate + 至少 N 个 live brain 在组内** — 防止未经证明的大脑驱动实盘
3. **权重 = DynamicBrainWeighter (实盘 P&L 驱动)** — live 大脑自然获得更高权重
4. **晋升 = BrainQualityEngine → governance promote** — shadow 大脑在 shadow PnL 验证成功后晋升为 live
5. **退役 = governance retire/freeze** — 持续表现差的大脑自动退出投票

### 9.5 关键发现：contract_group 分组正确性验证

```
barrier_12bar (8 brains, 全部 V9 40-dim):
  CRT.sur.chlg.g2026.1, DeepResMLP_V1, LightGBM_V1/V2,
  Online_MLP_V1, V9_Institutional_01, XGBoost_V9/V10
  → 全部训练于 survival_barrier 合约 (2.0sl/3.5tp/12bar)
  → 信号可通约 ✅

micro_3bar (2 brains, 9-dim micro):
  XGBoost_V4.5_Microstructure, Microstructure_Transformer_V5.0
  → 全部训练于 label-micro-barrier 合约 (1.5sl/2.5tp/5bar)
  → 信号可通约 ✅

micro_m15 (2 brains):
  XGBoost_V4.5_M15, Microstructure_Transformer_V5.0_M15
  → M15 时间框架
  → 信号可通约 ✅

micro_h1 (2 brains): gate-only, 不产生独立信号
micro_h4 (2 brains): trend gate only, 不产生独立信号

statarb_dynamic (1 brain):
  OU_Params_V6_Sniper
  → OU 均值回归，与其他组信号不可通约
  → 独立合约组 ✅
```

**分组验证结论**: 所有大脑的 contract_group 与其训练合约一致，组内信号可通约，组间信号正确隔离。Phase 0 解耦后，分组由 brain JSON 的 `contract_group` 字段声明式驱动，不再依赖硬编码。

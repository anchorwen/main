# Quant OS — 架构蓝图 v2 (2026-05-09)

> **快照时间**: 2026-05-09T08:00:00Z
> **基准提交**: 516cf2c (P0-P3 机构化实盘加固)
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
| 5 | `execution` | ~8,000 | 订单执行、策略线、风控、质量分析 | ✅ 8 test files |
| 6 | `features` | ~3,000 | 特征计算、存储、归一化、数据增强 | ✅ 3 test files |
| 7 | `feedback` | ~2,000 | 反馈循环、PnL 账本、性能追踪 | ✅ 2 test files |
| 8 | `governance` | ~1,000 | 大脑治理规则引擎 | ✅ 2 test files |
| 9 | `ledger` | ~4,000 | JSONL 事件存储 + 20+ 读写服务 | ✅ 5 test files |
| 10 | `market` | ~500 | 持仓追踪、信号过滤 | — |
| 11 | `metrics` | ~500 | Sharpe/Sortino/Calmar/Omega 等 8 指标 | — |
| 12 | `observability` | ~2,000 | 事件总线、告警、审计、SLO、追踪 | ✅ 2 test files |
| 13 | `parliament` | ~1,000 | 多脑决策聚合 + 合约分组 | ✅ 3 test files |
| 14 | `protocol` | ~3,000 | 通信适配器、调度器、消息构建 | ✅ 5 test files |
| 15 | `risk` | ~1,000 | 风控策略评估 (6 policies) | ✅ 1 test file |
| 16 | `runtime` | ~5,000 | 实时循环、执行管道、审批链、回放 | ✅ 8 test files |
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

### 大脑矩阵 (14 registered brains)

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
| Online_MLP_V1 | online_sgd | 40 | numpy | ✅ 80% |
| OU_Params_V6_Sniper | ou_params_v6 | price seq | Kalman | 极端偏离 |
| SurvivalAlpha_Ensemble | (group) | — | 多脑融合 | ✅ |
| TreeAlpha_Ensemble | (group) | — | LGB+XGB | ✅ |
| Online_SGD_V1 (legacy) | online_sgd | 40 | sklearn | ✅ |

---

## 3. 数据流全景

```
MT5 Terminal ──► V9FeatureComputer ──► LocalFeatureStore
                  MicroFeatureComputer ─┘
                        │
                        ▼
              FeatureService.build_vector()
                        │
                        ▼
              BrainFactory ──► 14 adapters.infer()
                        │
                        ▼
              Parliament/ContractGroups
                        │
                        ▼
              RiskEvaluation + PreTradeGuards
                        │
                        ▼
              ExecutionQueue ──► MT5 Bridge ──► Journal
                        │
                        ▼
              BrainPnLStore ←── FeedbackLoop ←── Reconciliation
                        │
                        ▼
              DynamicBrainWeighter ← GovernanceService
```

---

## 4. 已完成的机构化升级 (P0-P3)

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

## 5. 北极星原则

1. 先活下来，再跑得快 — 风控优先
2. 先可解释，再加复杂度 — 每个决策可追溯
3. 先稳主链路，再扩多模型 — 执行链路零容忍故障
4. 所有进化必须可回滚 — 增量、幂等、不破坏现有
5. 用真实运行数据驱动下一次迭代 — 数据 > 假设

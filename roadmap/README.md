# 量化构建系统路线图（Quant OS Roadmap）

> **任何人类或软件的前提读物。** 本文档说明项目的方向、阶段和架构宪法，是所有设计决策的最高参考。

---

## 目录

- [快速导航](#快速导航)
- [三阶段概览](#三阶段概览)
- [当前状态](#当前状态)
- [如何使用本路线图](#如何使用本路线图)
- [自动化更新机制](#自动化更新机制)

---

## 快速导航

| 你想做什么 | 看这里 |
|-----------|--------|
| 了解终极目标 | [constitution.md](constitution.md) |
| 查看当前进度 | [roadmap.json](roadmap.json)（机器可读）或下方[当前状态](#当前状态) |
| 了解方案A细节 | [phases/01_phase_a_hub_agent.md](phases/01_phase_a_hub_agent.md) |
| 了解方案B细节 | [phases/02_phase_b_alpha_market.md](phases/02_phase_b_alpha_market.md) |
| 了解方案C细节 | [phases/03_phase_c_quant_os.md](phases/03_phase_c_quant_os.md) |
| 查看架构决策记录 | [decisions/ARCHITECTURE_DECISIONS.md](decisions/ARCHITECTURE_DECISIONS.md) |
| 查看改动历史 | [changelog/CHANGELOG.md](changelog/CHANGELOG.md) |

---

## 三阶段概览

```
方案A（0-1.5年）          方案B（1.5-3年）           方案C（3-5年）
━━━━━━━━━━━━━━━━━━━━    ━━━━━━━━━━━━━━━━━━━━     ━━━━━━━━━━━━━━━━━━━━
│ 中枢加冕               │  Alpha 市场              │  自驱动 Quant OS
│                        │                          │
│ • main.py 中枢启动 ✅  │  • 4/50个Alpha并行 🔄    │  • 基因池与策略变异
│ • 多模型管线打通 ✅    │  • 议会多信号投票 ✅     │  • 沙盒自动晋升 🔄
│ • Feature Store流式 ✅ │  • 动态资金分配 🔄      │  • 深度学习执行
│ • MT5 实盘网关 ✅     │  • 独立风控Agent         │  • 最高宪法约束
│ • BrokerAdapter 预留 ✅│  • LLM/RL 延后至Phase C  │  • K8s集群部署
│ • 云端24/5 ⏸️         │                          │
│                        │                          │
│ 状态：90%完成          │  状态：65%完成           │  状态：15%完成
└────────────────────────┴──────────────────────────┴──────────────────────────
```

---

## 当前状态

- **阶段**: 方案A 收尾 → 方案B 早期（组件大量前置构建）
- **生产架构**: `main.py live` → `live_launcher.py` → `live_intent_loop.py` → `core/runtime/live_cycle.py`
- **备用架构**: `ServiceContainer` → `RuntimeLoop` → `DecisionCycleOrchestrator`（测试/影子场景）
- **当前里程碑**: A1/A2/A3 完成，B2 完成；A4/A5/TWAP-VWAP 延后（接口预留），B1/B3 推进中
- **核心决策 (2026-05-06)**: live_intent_loop 为规范生产路径（ADR-006），LLM/RL 延后至 Phase C（ADR-007）
- **已有基础设施完成度**:
  - ✅ `main.py` — 中枢入口（live/daily-ops/status/train 四命令）
  - ✅ `LiveCycle` — 核心周期逻辑（core/runtime/live_cycle.py, 1008行）
  - ✅ `BrokerAdapter` — 执行抽象接口（为FIX/云端预留）
  - ✅ `MT5BrokerAdapter` — MT5实现
  - ✅ `BrainFactory` — 5种adapter工厂（ONNX/XGBoost/OU/OnlineSGD/Base）
  - ✅ `ParliamentService` — 多脑加权投票（B2完成）
  - ✅ `GovernanceRuleEngine` — 4条自动规则
  - ✅ `DynamicBrainWeighter` — 表现→投票权重
  - ✅ `BrainPerformanceTracker` — 100窗口滚动评分
  - ✅ `BrainPnLStore` — 反事实 P&L 账本（独立核算每脑盈亏，23 tests pass）
  - ✅ `FeedbackLoop` — 反馈闭环（含OnlineFeedbackHook partial_fit）
  - ✅ `RiskEvaluationService` — 5条风控策略 + RegimeDetector
  - ✅ `FeatureStoreMaintenance` — 独立调度批量增量更新（5min周期）+ compact 去重（A3完成）
  - ✅ `ServiceContainer` — DI容器（备用架构）
  - ✅ `RuntimeLoop` — 决策周期（备用架构）
  - ✅ `PortfolioAllocator` — 合约完整（未投产）
  - ⏸️ FIX Gateway — BrokerAdapter接口已预留，延后至云端阶段
  - ⏸️ Docker/K8s — BrokerAdapter接口已预留，延后至用户指示
  - ⏸️ TWAP/VWAP — 延后至交易量需要时
  - ❌ LLM/RL 资产分配 — 有意延后至Phase C（ADR-007）

---

## 如何使用本路线图

### 对于新加入的开发者

1. 首先阅读 [constitution.md](constitution.md) — 理解不可逾越的约束
2. 阅读 [phases/01_phase_a_hub_agent.md](phases/01_phase_a_hub_agent.md) — 理解当前阶段做什么
3. 查看 [roadmap.json](roadmap.json) — 了解已完成和待完成的任务

### 对于AI/自动化工具

直接解析 `roadmap.json`，从中获取：
- `current_phase` — 当前阶段标识
- `milestones` — 里程碑列表及完成状态
- `dependencies` — 各模块之间的依赖关系
- `module_status` — 各模块的完成百分比

### 对于架构决策

任何重大架构变更必须在 [decisions/ARCHITECTURE_DECISIONS.md](decisions/ARCHITECTURE_DECISIONS.md) 中记录为 ADR（Architecture Decision Record），格式：
- 标题
- 日期
- 状态（提议/已接受/已废弃）
- 背景
- 决策
- 后果

---

## 自动化更新机制

```bash
# 每次提交后自动更新路线图状态
python scripts/update_roadmap.py

# 或手动运行
python D:\future\roadmap\scripts\update_roadmap.py
```

自动化脚本会：
1. 扫描 `core/` 目录检测新模块
2. 对比 `roadmap.json` 中的预期里程碑
3. 自动标记已完成项
4. 追加 `changelog/CHANGELOG.md`

建议将其挂载为 Git `post-commit` hook。

---

> **最后更新**: 2026-05-06
> **维护者**: 量化构建系统团队
> **许可证**: 私有
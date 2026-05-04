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
│ • main.py 中枢启动     │  • 20-50个Alpha并行      │  • 基因池与策略变异
│ • ONNX模型接入实盘     │  • 议会多信号投票         │  • 沙盒自动晋升
│ • Feature Store流式    │  • 动态资金分配           │  • 深度学习执行
│ • FIX/MT5 实盘网关     │  • 独立风控Agent          │  • 最高宪法约束
│ • 云端24/5稳定运行     │  • 基金经理视角Dashboard  │  • K8s集群部署
│                        │                          │
│ 状态：基础设施70%完成   │  状态：组件40%已存在      │  状态：规划中
└────────────────────────┴──────────────────────────┴──────────────────────────
```

---

## 当前状态

- **阶段**: 方案A — 中枢加冕期
- **当前里程碑**: 创建 main.py 中枢，打通训练→实盘模型管线
- **核心发现**: `core/` 层已包含完整的 ServiceContainer、RuntimeLoop、BrainFactory、DecisionCycleOrchestrator，但生产链路（`scripts/`）从未使用它们
- **已有基础设施完成度**:
  - ✅ `ServiceContainer` — 30+服务DI容器
  - ✅ `BrainFactory` — ONNX v9模型工厂
  - ✅ `BrainRegistryService` — 模型注册表
  - ✅ `BrainRunService` — 模型运行服务
  - ✅ `RuntimeLoop` — 推理→决策→派发循环
  - ✅ `DecisionCycleOrchestrator` — 编排器
  - ✅ `ParliamentService` — 多信号投票
  - ✅ `GovernanceRuleEngine` — 规则门禁
  - ✅ `RiskEvaluationService` — 风控评估
  - ✅ `FeedbackLoop` — 反馈闭环
  - ✅ `ConfigHotReload` — 运行时配置热加载
  - ✅ `SystemModeState` — NORMAL/SAFE/EMERGENCY模式切换
  - ❌ `main.py` — 中枢入口（缺失）
  - ❌ 训练产出的ONNX模型接入实盘（断链）
  - ❌ 真实行情Feature流（当前仅有占位价格差策略）

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

> **最后更新**: 2026-05-01
> **维护者**: 量化构建系统团队
> **许可证**: 私有
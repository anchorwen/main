# EVOLUTION PLAN - Quant OS 路线主文档

最后更新(UTC): 2026-04-30T01:39:41Z  
维护人: Team + Agent

---

## 1) 我们现在所处阶段（当前工程现状）

我们已从“链路不通”进入“可运行闭环”阶段，核心事实如下：

- 实盘执行闭环已打通：`live_intent_loop -> mt5_outbox -> mt5_bridge_worker -> MT5 -> receipts -> live_trade_journal`
- 已定位并修复关键阻塞：`Invalid "comment" argument`（去除下单请求 `comment` 字段后已通过）
- 已出现真实成交成功：`ack_status=accepted`，`retcode=10009`
- `live_dispatch_policy` 可正常恢复放行（`dispatch_blocked=false`），并可通过闸口策略保护运行
- 运行侧能力已具备：自动监督、日志、回执、日常巡检、保护旗标机制

结论：系统已跨过从 0 到 1，进入“稳定运行 + 数据积累 + 受控进化”期。

---

## 2) 方向共识（长期不偏航）

主方向：先稳定实盘部署，持续收集真实数据并分析迭代；在主链路稳定前提下并行推进多模型联合运行，最终收敛到方案C终极形态。

一句话目标：

> 构建一个可自我观测、自我约束、自我进化的自驱动 Quant OS。

---

## 3) 方案C分阶段推进图

## Phase A - 稳定实盘底座（当前进行中）

目标：稳定、可控、可回放、可审计。

- 保持执行链路稳定运行，降低异常率与人工干预频率
- 强化风控闸口（rejection、spread、calendar、回退机制）
- 保证 journal / receipt / report 一致性与可追溯性
- 固化日常巡检与故障应急SOP

通过标准（全部满足）：

- 连续多日无阻断级故障
- 拒单率、异常重启、手工救火次数持续下降
- 每日核心报告完整且可复盘

## Phase B - 数据驱动进化

目标：让每一次实盘运行都沉淀为可训练、可评估、可优化的数据资产。

- 建立高质量特征与标签流水线
- 固化训练导出 manifest 与版本管理
- 将线上表现与离线评估对齐，减少“回测好、实盘差”

通过标准：

- 训练数据连续、可复现、可审计
- 模型迭代有明确收益证据（风险调整后）

## Phase C - 多模型联合与自治编排（终极形态）

目标：从单策略执行升级为“多模型协同 + 治理驱动 + 自主优化”。

- Shadow / Ensemble / Champion-Challenger 并行
- 在线评估、动态权重、自动降级与回滚
- 策略、执行、风控、运营的一体化自治编排

通过标准：

- 多模型协同收益稳定优于单模型
- 系统可在风险约束内自主演化
- 关键治理指标长期稳定达标

---

## 4) 接下来 30 天优先级（按顺序执行）

1. 稳住实盘主链路（默认保守参数，不追求高频触发）
2. 固化数据质量（journal/receipt/report 自动校验）
3. 建立每日复盘闭环（收益、风险、执行质量、异常归因）
4. 开始多模型 shadow 并行（不直接控盘）
5. 达标后再放开联合决策权重

---

## 5) 每 24 小时固定更新制度（必须执行）

更新时间窗口（建议）：

- 每天 UTC 00:10 - 00:40（固定窗口）

自动化（推荐每日执行一次）：

- 模板追加：`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update_evolution_plan.ps1`
- 自动汇总当日 journal + 阻断旗标 + 更新文档头部时间戳：`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update_evolution_plan_daily.ps1`

说明：`update_evolution_plan_daily.ps1` 若距离上次修改 `EVOLUTION_PLAN.md` 超过 24 小时，会先备份同目录 `EVOLUTION_PLAN.backup.<UTC时间戳>.md`，再追加 `Daily Update (auto-filled)`。

更新动作（每天都做）：

1. 更新“昨日运行摘要”（成交数、accepted/rejected、主要异常）
2. 更新“风险与闸口状态”（是否触发 block、原因、修复动作）
3. 更新“路线进度”（Phase A/B/C 当前里程碑完成度）
4. 更新“明日唯一优先事项”（只保留 1-3 个最关键动作）

文档规则：

- 只追加，不覆盖历史关键结论
- 每次更新必须带 UTC 时间戳
- 所有决策必须能追溯到日志/回执/报告

---

## 6) 每日更新模板（直接复制使用）

```md
### Daily Update - <UTC时间>

- 运行状态: <稳定/告警/阻断>
- 核心统计: <accepted x / rejected y / rejection_rate z>
- 关键事件: <最多3条>
- 根因与修复: <最多3条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3条>
```

---

## 7) 北极星原则（长期约束）

1. 先活下来，再跑得快  
2. 先可解释，再加复杂度  
3. 先稳主链路，再扩多模型  
4. 所有进化必须可回滚  
5. 用真实运行数据驱动下一次迭代

---

## 8) 当前结论（本次基线）

我们正在正确地走向方案C，但仍处于“稳定底座 + 数据积累”的关键窗口。  
当前最优策略是：稳实盘、强治理、快复盘、慢放权。  
只要严格执行“每日24小时更新制度”，路线不会迷失。

### Daily Update - 2026-04-29T16:50:23Z (auto-filled)

- date_key_utc: 2026-04-29
- run_state: BLOCKED(flag_present)
- stats: accepted=2 rejected=3 acknowledged=0 other=0 total=5 rejection_rate=0.6
- live_dispatch_block.flag: present
- notes_events: <manual max 3; ops_logs / bridge_supervisor / p1_daily_run>
- notes_root_cause_fix: <manual max 3>
- phase_progress: <Phase A/B/C checkpoint>
- tomorrow_priority: <1-3 items>

### Daily Update - 2026-04-29T16:53:17Z (auto-filled)

- date_key_utc: 2026-04-29
- run_state: BLOCKED(flag_present)
- stats: accepted=2 rejected=3 acknowledged=0 other=0 total=5 rejection_rate=0.6
- live_dispatch_block.flag: present
- notes_events: <manual max 3; ops_logs / bridge_supervisor / p1_daily_run>
- notes_root_cause_fix: <manual max 3>
- phase_progress: <Phase A/B/C checkpoint>
- tomorrow_priority: <1-3 items>

### Daily Update - 2026-04-30T01:39:41Z（自动生成）

- 日期键(UTC): 2026-04-30
- 运行状态: 稳定
- 核心统计: 接受=1 拒绝=0 确认=0 其他=0 合计=1 拒单率=0
- live_dispatch_block.flag: 不存在
- 关键事件: <手动最多 3 条；可从 ops_logs / bridge_supervisor / p1_daily_run 摘抄>
- 根因与修复: <手动最多 3 条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3 条>

### Daily Update - 2026-05-01T14:45:38Z（自动生成）

- 日期键(UTC): 2026-05-01
- 运行状态: 静默（当日无交易记录）
- 核心统计: 接受=0 拒绝=0 确认=0 其他=0 合计=0 拒单率=0.0
- 数据质量: 交叉校验问题=0 outbox超时=0
- live_dispatch_block.flag: 不存在
- 关键事件: <手动最多 3 条>
- 根因与修复: <手动最多 3 条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3 条>

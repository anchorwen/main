# DQAF Docket Registry — 案卷总账

> **标准参考**: IEC 62740:2015 (Root Cause Analysis), ISO 31000:2018 (Risk Management), NTSB Party System (49 CFR Part 831.11)
> **用途**: 记录每次正式诊断的元数据总览。症状简述不超过 60 字，详细内容引用 CCT_LEDGER.md 条目。
> **格式约定**: 表格格式（仅元数据，字段简短）。因果链详细内容见 `CCT_LEDGER.md`，模式签名见 `ReB_PATTERN_INDEX.md`。

## 严重等级定义

| 等级 | 定义 | 适用流程 |
|------|------|---------|
| Sev 1 | 交易阻断、数据丢失、资金风险 | 完整 Agentic DQAF（DA/AR 双轨 + IC 裁决） |
| Sev 2 | 输出偏差、信号退化、实盘质量下降 | 完整 Agentic DQAF（DA/AR 双轨 + IC 裁决） |
| Sev 3 | 告警噪音、技术债务、非关键异常 | 简化流程（单人 DA，无需 AR 对抗） |
| Sev 4 | 外观、文档、非功能性 | 现有 Iron Law #8 协议即可 |

## 案卷索引

| Docket ID | 日期 | 严重等级 | 症状简述 | IC 裁决 | 关联 FIX |
|-----------|------|---------|---------|--------|----------|
| DQAF-20260609-002-UPDATE | 2026-06-09 | Sev 2 | 剥洋葱排查: BTC MetaFilter模型文件根本不存在(configs/brains_btc/),静默退化到rolling_wr(0.48),跨品种盲点模式 | **APPROVED → CLOSED** — FIX-002-UPDATE (Hard Floor Defense) + FIX-002-BTC (Path A: BTC MetaFilter V1训练, 47-dim LGB, val WR 70.9%). Path B (≥200笔实盘重训) 搁置. ReB: CROSS_SYMBOL_METAFILTER_BLINDSPOT | FIX-20260609-002-UPDATE, FIX-20260609-002-BTC |
| DQAF-20260609-002 | 2026-06-09 | Sev 2 | XAU 2日交易退化: WR=20%, h1/h4 p_win=0.41(rolling_wr)绕过MetaFilter, 三swing同向同时开仓, BrainSignal合约断裂37次/9天 | **APPROVED → CLOSED** — FIX-20260609-002: (1) h1/h4接入MetaFilter, (2) Kelly低RR保本线兜底, (3) Family同周期乐观锁, (4) BrainSignal兼容. ReB: ReB-20260609-002 | FIX-20260609-002 |
| DQAF-20260609-001 | 2026-06-09 | Sev 2 | BTC 23h 零开仓: reentry guard hesitation 类别同时缺少 _MAX_THRESHOLD 天花板和 TTL 硬解锁 → 阈值 0.9168 超过模型 P99(~0.685) → 数学死锁, 148 信号全拦截 | **APPROVED → CLOSED** — FIX-20260609-001: (1) `min(max(exit_conf+0.15, 0.70), _MAX_THRESHOLD)` 天花板, (2) TTL 硬解锁 2h 后仅需 confidence>0.50. ReB: ReB-20260609-001 | FIX-20260609-001 |
| DQAF-20260608-003 | 2026-06-08 | Sev 2 | 熔断器碎片化 trip 路径：6条独立路径×3种计数器，auto-reset仅清除1种→stale counter泄漏→立即重新trip→死亡螺旋。系统May 31重启110次。 | **APPROVED → CLOSED** — FIX-20260608-009: (1) 5条trip路径全部记录 `trip_reason`，(2) auto-reset统一清除全部3种counter，(3) 持久化补齐全部counter+trip_reason，(4) restore防幽灵breaker。ReB: `FRAGMENTED_BREAKER_TRIP_PATHS_WITH_STALE_COUNTER_LEAK` | FIX-20260608-009 |
| DQAF-20260608-001 | 2026-06-08 | Sev 1 (复合) | 全量数据异常审计: 熔断器永久卡死(Sev 1) + calibrator时间戳损坏(Sev 2) + MetaFilter静默失效(Sev 2) + Alpha空跑(Sev 2) + Golden Master仅3天(Sev 3) — 10项异常收敛为5个独立根因 | **APPROVED → CLOSED** — FIX-20260608-003: (1) 熔断器冷却制统一自愈, (2) MetaFilter模型路径修复, (3) calibrator时间戳字段-值错配修复. ReB: CIRCUIT_BREAKER_RESET_ASYM, ORPHAN_SUBSYSTEM_DETECTION | FIX-20260608-003 |
| DQAF-20260606-004 | 2026-06-06 | Sev 2 | 6+ 小时零开仓——p_win=0.44 与 breakeven=0.45 之间的 0.01 死锁带，双闸门交替拦截 (p_win + bleed_stop) | **APPROVED → CLOSED** — UCB 弹性地板 (FIX-139) 填平死锁带。置信度推导 p_win=0.482，Kelly 自动微仓探索。方案三优于方案一二。 | FIX-20260606-139 |
| DQAF-20260606-003 | 2026-06-06 | Sev 3 | 重启后立即开单——排查是否 FIX-137 引入回归 | **APPROVED → CONFIRMED** — 老问题重现 (RC-03 state-leak)，cooldown 清理非致因。关联已知存量模式 `state_leak_across_restart` | — |
| DQAF-20260607-005 | 2026-06-07 | Sev 1 | UnboundLocalError 导致 dispatch 崩溃 → 孤儿持仓 → 系统数小时只开仓不平仓 (Fail-Open) | **APPROVED → CLOSED** — 三层防线: FIX-140 (Fail-Closed dispatch), FIX-141 (孤儿富化), FIX-142 (兜底网关) | FIX-20260607-140, FIX-20260607-141, FIX-20260607-142 |
| DQAF-20260607-007 | 2026-06-07 | Sev 3 | 架构师提案: 趋势衰竭 Confidence Decay + V型反转非对称出场。诊断确认: 拒绝N笔交易计数衰减，接受 Kalman+Hurst 状态驱动仓位缩放 + Kalman 速度翻转快速出口。 | **APPROVED → CLOSED** — `trend_maturity_discount()` + `evaluate_brain_exit()` Kalman velocity flip 接线完成。纯增量，两个信号已计算仅未消费。 | FIX-20260607-143 |
| DQAF-20260608-002 | 2026-06-08 | Sev 2 | XAUUSDc meta_exit 平仓无钉钉通知: `dispatch_managed_close()` 遗漏 `notify_trade` 调用, 所有受管平仓静默 | **APPROVED → CLOSED** — FIX-20260608-005: 在 `dispatch_managed_close()` 尾部注入 fire-and-forget `notify_trade(action="close", ...)`. 覆盖全部 7+ 受管退出路径. ReB: MISSING_NOTIFY_IN_MANAGED_CLOSE | FIX-20260608-005 |
| DQAF-20260606-002 | 2026-06-06 | Sev 2 | BTC swing WR=14.29% PnL=-$813, brain_flip_extreme_100pct 假阳性出场 | **APPROVED → CLOSED** — 根因 RC-06: live_cycle.py:1424 `_l2_supporting=[]` 在 neutral 平票时产生 100% 假翻转 | FIX-20260606-137 |

## 裁决状态说明

- `AWAITING_IC` — 已提交 [DQAF_REPORT]，等待人类 IC 审批
- `APPROVED` — IC 批准，进入修复阶段
- `REJECTED` — IC 驳回，需重新诊断（注明驳回原因）
- `TIERBREAKER` — DA 与 AR 结论矛盾，IC 要求第三轮诊断
- `CLOSED` — 修复完成并验证通过

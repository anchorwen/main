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
| DQAF-20260610-001 | 2026-06-10 | Sev 2 | BTC 移动止损修改前后对比: 0%胜率伪影→数据证伪→亏损来自改前旧仓位(trail卡死)，改后trail激活(+768pts)保本防守。根因非trail而是微生命周期+全long逆势+遥测盲区。 | **APPROVED → CLOSED** — IC Mandate: 冻结trail参数(衰减曲线已验证正确)，立即修复MIA管道PnL缺失(close_accepted/breakeven无PnL)。ReB: TRAIL_TELEMETRY_BLINDSPOT + MICRO_LIFESPAN_COUNTER_TREND | FIX-20260610-004, FIX-20260610-006, FIX-20260610-007 |
| DQAF-20260612-001 | 2026-06-12 | Sev 3 | data_health CRITICAL: journal PnL null 17.6% + dupes 89 + trail label 0 + calibrator cold-stalled + 位置脆弱性 .values() 5站点. 6源证据→5根因→5 FIX | **APPROVED → CLOSED** — FIX-002 (位置脆弱性→命名投影), FIX-003 (P0洪水锁+P1 trail标签), FIX-004 (P2 bridge PnL+P4 MIA重试), FIX-005 (P5 calibrator cold_started过渡). ReB: PHANTOM_CLOSE_FLOOD + TRAIL_LABEL_BLINDSPOT + PNL_BACKFILL_GAP + CALIBRATOR_COLD_STALLED + POSITIONAL_FRAGILITY | FIX-20260612-002, FIX-003, FIX-004, FIX-005 |
| DQAF-20260612-002 | 2026-06-12 | Sev 1 | no_live_brains 全交易阻塞: FIX-20260610-001 退役 V5 三处残留(status=retired, vote_weight=0, enabled=false)与 governance 升为 live 冲突 → _live_count=0 → 所有周期降级。三重修复+Cut 4 SSOT重构。 | **APPROVED → CLOSED** — IC Mandate: 三步同步 (registry→live + weight→1.0 + yaml enabled→true) + strategy_evaluator Cut 4 decision.brain_ids SSOT 替换 strategy.brains 嵌套 dict. ReB: TRIPLE_BOOKKEEPING_RESIDUAL + GOVERNANCE_BRAIN_SOURCE_MISMATCH | FIX-20260612-006 |
| DQAF-20260612-004 | 2026-06-12 | Sev 2 | KI-004 收口: resolve_p_win_from_brains() 三条静默 fallback 路径全部返回 0.40, 无日志无告警. p_win=0.40 在 journal 中占比 0% (已有隐式安全网) | **APPROVED → CLOSED** — FIX-20260612-001: Phase 0 可观测性注入. BLE001→fail_open_guard + 3条结构化告警 + p_win_source/p_win_degraded 透传至 journal. ReB: SILENT_FALLBACK_ZERO_OBSERVABILITY | FIX-20260612-001 |
| DQAF-20260610-002 | 2026-06-10 | Sev 2 | 入场/出场全量审计: V9/V10 Survival brain SL/TP训练-部署偏差, BTC_Swing_V5退休后残留在XAU配置, kalman_velocity/meta_exit等10+出场模式未分类→标签中毒 | **APPROVED → CLOSED** — 4项修复: (1) XAU配置清理V5, (2) V9/V10补全label_contract(SL=3.0/TP=2.0生存模式,需专属策略线), (3) verify.py新增`_check_config_consistency()`跨品种污染+退役大脑+contract缺口静态闸门, (4) `_classify_exit_reason()`补全14种出场模式. 虚拟沙盒推迟(独立项目). ReB: CONFIG_SYMMETRY_DRIFT | FIX-20260610-008 |
| DQAF-20260609-012 | 2026-06-09 | Sev 2 | BTC 大脑盈利能力诊断: V5 test_PF=1.81→live_PF=0.73, 归一化器XAU复制品, M5/12bar信号≈随机. 全TF网格搜索→BTC不支持R:R≥1.0. M15 SL=3.0/TP=2.0 EV=+0.456R 全场最佳. | **APPROVED → CLOSED** — FIX-20260609-012: B1审计→B2/B3重训管线→V9 H1 (EV+0.38R) + V10 M15 (EV+0.46R) shadow注册. ReB: BTC_SURVIVAL_ALPHA | FIX-20260609-012 |
| DQAF-20260609-011 | 2026-06-09 | Sev 2 | 大脑治理真空: 4 candidate brains 零 live, 全票权开单, 0.1 lot 裸奔, PnL ~-$30/day. candidate 倒挂 (全票权) vs probation (0.5×). | **APPROVED → CLOSED** — FIX-20260609-011: (1) candidate vote_weight×0.5, (2) governance_state per-cycle 管线, (3) strategy_evaluator 无 live 降级 (conf<0.50→blocked, vol→0.01). ReB: GOVERNANCE_VACUUM_CADET_BRAINS | FIX-20260609-011 |
| DQAF-20260609-002-UPDATE | 2026-06-09 | Sev 2 | 剥洋葱排查: BTC MetaFilter模型文件根本不存在(configs/brains_btc/),静默退化到rolling_wr(0.48),跨品种盲点模式 | **APPROVED → CLOSED** — FIX-002-UPDATE (Hard Floor Defense) + FIX-002-BTC (Path A: BTC MetaFilter V1训练, 47-dim LGB, val WR 70.9%). Path B (≥200笔实盘重训) 搁置. ReB: CROSS_SYMBOL_METAFILTER_BLINDSPOT | FIX-20260609-002-UPDATE, FIX-20260609-002-BTC |
| DQAF-20260609-002 | 2026-06-09 | Sev 2 | XAU 2日交易退化: WR=20%, h1/h4 p_win=0.41(rolling_wr)绕过MetaFilter, 三swing同向同时开仓, BrainSignal合约断裂37次/9天 | **APPROVED → CLOSED** — FIX-20260609-002: (1) h1/h4接入MetaFilter, (2) Kelly低RR保本线兜底, (3) Family同周期乐观锁, (4) BrainSignal兼容. ReB: ReB-20260609-002 | FIX-20260609-002 |
| DQAF-20260609-001 | 2026-06-09 | Sev 2 | BTC 23h 零开仓 + 全栈健康检查: (A) hesitation reentry 数学死锁 150 cycles, (B) budget 每 cycle 归零 → 全部累计风控失效, (C) 大脑治理真空, (D) MIA/孤儿/仓位放大 | **APPROVED → CLOSED** — FIX-20260609-001: `_MAX_THRESHOLD` 天花板 + TTL 硬解锁. FIX-20260609-010: (1) budget 每 cycle 从磁盘恢复, (2) hesitation margin +0.15→+0.08, floor 0.70→0.65. ReB: Cap-Output Mismatch Deadlock + Budget Reconstruction Amnesia | FIX-20260609-001, FIX-20260609-010 |
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

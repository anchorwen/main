# 📋 8/19 后清偿路线图（Phase 2）— 草案

> 依据：TECH_DEBT_REGISTRY（8/19 冻结期已解除）、ACTIVE_TASKS_REGISTRY、memory 活跃 Deferred。
> 8/19 清偿序列 Phase 1（010→008→017→009→019）已全绿，本路线图为 **Phase 2 开放窗口**。

---

## 0. 前置原则（承继 8/19 纪律）

1. **同模块聚批** — 同一文件域一次改动到位，禁止反复触碰（Iron Law Iterability）。
2. **先实盘噪音，后潜伏** — 有每日可感知影响的优先；潜伏项排后。
3. **每项独立闭环** — DQAF → 蓝图 → FIX 注册 → 回归锁 → 三步归档，一债一 commit（沿用非巨型 commit 分批原则）。
4. **核心路径变更必带回归锁 + 历史回放** — 涉 live_cycle / intent_loop / watchdog 的改动，回归锁需含休市期/降级路径场景。
5. **零行为变化门禁** — 每项改动声明"受影响策略/路径"，不改动的路径零变化（min_rr=0 式门禁复用）。

---

## 1. 清偿序列总览（建议执行顺序）

| 序 | 项 | 模块域 | 级别 | 规模 | 用户可感知影响 | 依赖 |
|:--|:--|:--|:--|:--|:--|:--|
| **P1** | journal_freeze_gate Win 路径 bug | 工具 | L2 | S | 无（环境变量退役） | 无 |
| **P2** ✅ | TECH_DEBT-013 watchdog 休市误杀 | runtime-live | L3 | XL | **每日 11-14 次硬杀重启 + 假告警** | 无 |
| **P3** | TECH_DEBT-014 背景击杀 + 漂移单杀 | runtime-live | L2 | M-L | 每日 1-3 次静默重启 | 随 P2 取证 |
| **P4** | TECH_DEBT-011 DCI Auditor 休市盲区 | scripts/audit | L2 | M | 周末必误报退化 BLOCKED | 复用 P2 日历工具 |
| **P5** | TECH_DEBT-012 Feature Writer 休市重写 | features | L3 | S-M | 无（纯防御） | 复用日历 |
| **P6** | TECH_DEBT-007 close label 三路单源 | runtime-live | L3 | L | 出场归因/p_win 校准完整性 | 无 |
| **P7** | TECH_DEBT-018 META_FILTER_WIRED_STALE | observability | L3 | S-M | 崩溃循环期假 WARN | 017 已清，触发已弱 |
| **P8** | TECH_DEBT-016 supervisor 白名单 | deployment | L2 | S | 无（潜伏双开风险） | 无 |
| **P9** | TECH_DEBT-015 launcher 心跳 supervisor | deployment | L2 | L | 停机 5.8h 空窗 | 无 |

---

## 2. 各项详述

### P1 — journal_freeze_gate Win 路径 bug（先行小手术）
- **内容**：修复 freeze-gate 在 Windows 路径的缺陷；修复后 **退役 `JOURNAL_FREEZE_BYPASS`** 环境变量（[journal_freeze_gate.py:10](scripts/journal_freeze_gate.py#L10)）。
- **流程**：Scene A，L2 修复，回归锁 + 现有 freeze 测试。
- **Done**：`JOURNAL_FREEZE_BYPASS` 从代码与 memory 移除；freeze-gate 在 Win 路径正常判定。

### P2 — TECH_DEBT-013 watchdog 休市误杀（核心项，最大噪音）✅ **CLOSED**
- **根因**：`bar_sync` 超时 360s > watchdog 硬杀 300s → 21:00-22:00Z 休市窗每日 11-14 次硬杀（全史 905 条击杀中 57% 集中该窗）。
- **修复方案（三选一，DQAF 时 IC 裁决）**：
  1. **intent market_closed 感知**（首选）：复用 `pre_trade_guards.py:46-47` 市场日历 → 休市期跳过 bar_sync 等待，低功耗 idle，不触发 watchdog。
  2. bar_sync 超时降至 watchdog 之下（结构对齐，intent 自行优雅超时）。
  3. watchdog 加休市豁免窗（重开后复位）。
- **IC 裁决 (2026-08-19 雷霆裁决)**: **方案 1+2 组合 (The Resilient Pulse)** 绝对批准 — ① heartbeat_refresh 脉冲穿透 (heartbeat delegation) + ② 超时倒置 (360→240 双路对齐) + ③ degraded deadline 结构化. 否决方案 3. pre_trade_guards caution tier 零语义漂移.
- **风险**：触碰 live_cycle / intent_loop / watchdog 核心路径 → 需 **DQAF Sev 2** + 休市期回放回归锁（21:00-21:55 窗口 + 周五收市场景）。
- **Done (commit aff05b85, 2026-08-20)**: 休市窗零硬杀 (心跳脉冲保活, 结构性不可能再误杀); `bar_sync_timeout` 双路 240 对齐; 回归锁 8 测试 (休市阻塞期无硬杀 + **BTC 24/7 对照**). 三步归档: DQAF-20260820-001 / CCT-20260820-001 / ReB-20260820-MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK. `JOURNAL_PNL_NULL_RATE_HIGH` 假告警消失 (击杀簇根除). **注意: 生效需重启实盘进程** (现行 PID 16196 仍带 --bar-sync-timeout 360 旧参, 下次自然重启即接管).

### P3 — TECH_DEBT-014 背景零星击杀 + 漂移单杀
- **内容**：逐条击杀时刻 × intent 阻塞点关联取证（Iron Law #11 脚本先行），确认 386/905 背景击杀同源与否；定位 12:15→13:00 逐日 +5min 漂移单杀来源（连续 12 天精确规律）。
- **流程**：取证脚本 `_audit_` 前缀 → 定性后并入 P2 修复或独立 FIX。
- **Done**：背景击杀根因定性；漂移来源定位；修复落地。

### P4 — TECH_DEBT-011 DCI Auditor 日历感知
- **内容**：`audit_data_chain_integrity.py` 停滞阈值 12h 无日历感知 → 按资产日历（forex_24_5 / crypto_24_7）计算最近有效收盘，休市期阈值放宽锚定收盘；或 `--now` 锚定周五收盘。
- **复用**：与 P2 共用市场日历工具（收敛单一实现，避免两处日历逻辑）。
- **Done**：周六 `--baseline-read` 零假阳性；BTC/XAU 日历类型分别验证。

### P5 — TECH_DEBT-012 Feature Writer 休市重写抑制
- **内容**：特征写入侧 `market_closed → 跳过落盘` 守卫（或 last-value 指纹幂等去重）。
- **零行为变化**：休市期不再落重复行；工作日落盘逻辑不变。
- **Done**：周六零重复特征记录；工作日计数回归不变。

### P6 — TECH_DEBT-007 close label 三路单源
- **内容**：提取单一 `resolve_close_label(deal_reason, deal_comment, trail_active)` 纯函数为 SSOT，`position_close_adapter` / `reconciliation` / `mia_close` 三路共同消费（DQAF-20260806-001 Option C 记账项）。
- **回归锁**：三路对同一 deal 输出一致 label；既有 trail label 契约（sl_hit_trailed）不漂移。
- **Done**：label 决策逻辑单源；现有 81 笔 sl_hit_first 分类复算一致。

### P7 — TECH_DEBT-018 META_FILTER_WIRED_STALE 假阳性
- **内容**：health check 崩溃循环下回退读 launcher log `[intent]` 行（跨崩溃恢复 wired 时间戳），或引入 `meta_pipeline_wired` 独立持久化事件文件。
- **注**：017 已清后触发条件已弱化，纯防御，可最后做。
- **Done**：崩溃循环期不再误报；正常期行为不变。

### P8 — TECH_DEBT-016 supervisor 白名单
- **内容**：`one_click_supervisor.ps1` 匹配白名单化 — 只匹配 `D:\cursor\scripts\live_intent_loop.py` 完整路径，不跨 RepoRoot 通配。
- **Done**：跨系统误匹配消除，双开风险解除。

### P9 — TECH_DEBT-015 launcher 心跳 supervisor
- **内容**：独立 schtasks 探针（5min）+ 原子拉起 + **双开防护锁**（防探针与手动重启竞态双实例）。
- **风险**：新增常驻机制 → 需最严格双开验证（与 E 盘 watchdog 接线方式区分）。
- **Done**：SIGINT 停机后 ≤5min 自动恢复；双实例防护验证通过。

---

## 3. Parked — 触发条件式（不主动动）

| 项 | 触发条件 |
|:--|:--|
| TECH_DEBT-001 MIA 幽灵默认值 | 新增调用方未传 symbol |
| TECH_DEBT-002 Journal O(N) 扫描 | journal > 10,000 行 |
| TECH_DEBT-003 三层去重过度设计 | 下次 MIA 大版本重构 |
| MetaExit 门禁待复查 | 累计 500+ ExitFeatureSnapshot |
| bars_held 重启连续性 | 动 hesitation / 重启重建路径时 |
| breakeven 意图锁→成交锁 | 触发条件式 |
| V6 schema 剪枝 37→31 维 | Phase D 后 |
| 退役旧 brain_pnl_ledger.json | 事件流稳定后 |
| DQAF-057 Phase 3b/3c | label_coverage<90% 持续 3 天 |
| P2 进场点差摩擦对齐 | 下次触碰门禁链时 |
| T19 rr_below_minimum | MONITORING（30/50，rr=0，未达标不动作） |
| T22 V4 confidence 校准 | MONITORING |

---

## 4. 纪律约束

- **每项开工前**：DQAF 握手（P2/P3/P6 预计 Sev 2；其余 Sev 3-4）→ IC 批准 → 蓝图 + FIX_REGISTRY 检索 → verify --full 全绿 → FIX 注册 → 三步归档。
- **禁止**：跨项并行触碰同一文件域；一债未收口不并开下一债。
- **8/19 已终审项**（Flow46 双塔 OOS）不得借本路线图回卷。
- 涉 E 盘实盘（Dual_Assassin）与 F 盘工作区事项不在本路线图范围。

---

**预估节奏**：P1 (半小时) → P2-P3 (主战役，取证 + 修复 + 回放回归) → P4-P5 (同日历批) → P6 (重构批) → P7-P9 (运维收尾批)。

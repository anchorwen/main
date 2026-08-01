# CCT Ledger — 因果链账本

> **标准参考**: IEC 62740:2015 §6.2 "Causal Factor Charting", NTSB Form 6120.1 "Sequence of Events"
> **用途**: 记录每个 Docket 的完整因果链。症状 → 中间异常 → 根因，每环节标注证据引用和置信度。
> **格式约定**: **强制使用三级标题块格式**（禁止 Markdown 表格，因果链文本较长会被水平拉爆不可读）。

## 格式模板

```markdown
### CCT-YYYYMMDD-NNN
- **Docket ID**: DQAF-YYYYMMDD-NNN
- **日期**: YYYY-MM-DD
- **置信度**: confirmed / hypothesis / speculative
- **因果链**:
  - [Layer 1 — 症状]: 具体可观测现象 + 证据引用（日志行号/文件路径）
  - [Layer 2 — 中间异常]: 异常状态/变量 + 证据引用
  - [Layer 3 — 根因]: 根因类别（RC-XX）+ 根因陈述
- **证据引用**:
  - Source 1: [日志/Journal/Golden Master/State/Receipts] — 具体位置
  - Source 2: [独立第二源] — 具体位置
  - Source 3 (if root cause): [跨品种验证源] — 具体位置
- **是否被推翻**: 否 / 被 CCT-YYYYMMDD-NNN 取代
- **关联 ReB Pattern**: ReB-YYYYMMDD-NNN
```

## 因果链条目

---

### CCT-20260726-012
- **Docket ID**: DQAF-20260726-012
- **日期**: 2026-07-26
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 在 downtime 中于 MT5 侧平仓的仓位（SL/TP/手动）跨重启后仍出现在分析脚本的「活跃仓位」列表中。3871727437（6/10 开仓）50+ 天后仍被报告为活跃。
  - [Layer 2 — 中间异常]: execution_state.json 恢复 `known_open_tickets` → `state.known_open_tickets` 非空 → `live_cycle.py:1447` 条件 `not state.known_open_tickets` 为 False → `bootstrap_known_open_from_journal()` 被跳过 → 未播种幽灵仓位 → 启动 reconciliation 仅检查 restored state 中的票号 → 幽灵仓位永不检测。
  - [Layer 3 — 根因]: L2 — DQAF-20260710-003 的 belt-and-suspenders 设计为互斥而非互补。Journal bootstrap（suspenders）被 execution_state restore（belt）静默抑制。
- **证据引用**:
  - Source 1: `data_btc/state/execution_state.json` — 仅含 4308533605，不含 3871727437
  - Source 2: `data_btc/live_trade_journal.jsonl:327-328` — 3871727437 的 OPEN 与 3871726916 的 CLOSE（message_id 匹配但 ticket 不匹配），确认 journal bootstrap 通过 message_id 正确排除此记录
  - Source 3: `core/runtime/live_cycle.py:1447` — `not state.known_open_tickets` 守卫条件
- **是否被推翻**: 否
- **关联 ReB Pattern**: GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION

---

### CCT-20260724-001
- **Docket ID**: DQAF-20260724-001
- **日期**: 2026-07-24
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 实盘所有 swing/trend 策略被 vol_zscore_hard_block 持续熔断（~48h）。Golden master 显示每个 cycle 所有非 micro 策略的 reason 均为 `vol_zscore_hard_block:m5_vol_zscore_-3.NN_lt_-3.0`。Feature store 39,714 条 M5_Vol_ZScore 记录（2026-05-05 至 2026-07-24）94% 非正值，仅最大值 -1.34 — 从未达到正值。
  - [Layer 2 — 中间异常]: `v9_live_computer._vol_zscore()` 算法结构正确（price_zscore 同算法 49/51 pos/neg 健康分布证明），但输入数据源 CFD tick_volume 具有 burst-decay 分布 + 连续相同值频发（→std=0→zscore=0）。`_vol_zscore` 使用 inclusive window（`volume[-lookback:]` 含当前 bar），当前 bar 加入 μ/σ 计算导致均值拖拽（Mean Drag）—— zscore 系统性地为负或零。
  - [Layer 3 — 根因]: L3 架构缺陷 — 电路断路器（circuit breaker）锚定于合成 CFD 伪指标（tick_volume）而非真实价格行为（ATR）。CFD broker 的 tick_volume 是人工合成的、充满噪声的代理变量，不应作为保护实盘资本的物理熔断器的唯一信号源。Inclusive-window z-score 的 Mean Drag 效应是次要因素——即使改用 exclusive window，CFD tick_volume 的 burst-decay 分布仍会持续产生大量负 zscore。
- **证据引用**:
  - Source 1: `data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` — 39,714 条 Vol_ZScore 记录，94% 非正值
  - Source 2: `core/features/computers/v9_live_computer.py:116-125` — `_vol_zscore()` 算法审计（inclusive window + tick_volume 输入）
  - Source 3: `core/features/computers/v9_live_computer.py:180-189` — `_price_zscore()` 对照验证（同算法，48.9% 正值，证明算法正确）
  - Source 4: `data/golden_master.jsonl` — 实时 golden master 输出显示每 cycle vol_zscore_hard_block
  - Source 5: `core/runtime/strategy_evaluator.py:484-527` — 旧 Vol_ZScore 硬闸门代码位置
  - Source 6: `data/regime_detector_state.json` — buffer_sample (50-bar ATR buffer, 3.38-4.31 range) 用于新 ATR 比率闸门
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260724-CIRCUIT_BREAKER_ANCHORED_TO_SYNTHETIC_CFD_PSEUDO_METRIC
- **关联 FIX**: FIX-20260724-001
- **状态**: **CLOSED** — 摘除 Vol_ZScore 硬闸门，替换为 ATR 比率闸门 (atr_ratio < 0.5)；阈值 0.5 为临时热修复，历史回测校准 Deferred

### CCT-20260718-001
- **Docket ID**: DQAF-20260718-001
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `cleanup_orphan_opens()` 写入 `label="auto_orphan_*"` 合成 close 条目以配平无 close 的孤儿 open。当 `compact_journal()` 按 age 剪枝旧的 rejected open 时，配对的合成 close 未被级联删除 → 孤儿 close 永久残留在 journal 中。
  - [Layer 2 — 中间异常]: `compact_journal()` 单条目压缩逻辑逐行扫描 journal，仅根据单条记录自身的 age 决定保留/剪枝 — 无跨条目关联感知。
  - [Layer 3 — 根因]: L3 架构缺陷 — journal compaction 缺少级联删除语义。合成 close 通过 `open_message_id` 外键关联父 open，但 compact 无级联逻辑。
- **证据引用**:
  - Source 1: `core/ledger/services/journal_cleanup.py:compact_journal()` — 单 pass 剪枝逻辑
  - Source 2: `core/ledger/services/journal_cleanup.py:cleanup_orphan_opens()` — auto_orphan_* 合成 close 写入
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260718-ORPHAN_CASCADE_DELETE_MISSING
- **关联 FIX**: FIX-20260718-001
- **状态**: **CLOSED** — two-pass cascade: Pass 1 收集 pruned open IDs → Pass 2 级联删除匹配的 auto_orphan_* close

### CCT-20260718-002
- **Docket ID**: DQAF-20260718-002
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC governance 定时对账不工作。`daily_ops.py` 每夜调用 `cmd_reconcile()` 但路径全部硬编码为 XAU (`data/brains`, `live.yaml`) → BTC governance_state 漂移未被 cron 检测。
  - [Layer 2 — 中间异常]: `brain.py:cmd_reconcile()` 所有路径推导硬编码 — `brains_dir = project_root / "configs" / "brains"`, `data_path = project_root / "data"` 等 — 无资产参数化。
  - [Layer 3 — 根因]: L3 架构缺陷 — 单资产硬编码架构。`daily_ops.py` 调用 `cmd_reconcile()` 时未传递资产上下文（`base_dir` 已有资产信息但未利用）。
- **证据引用**:
  - Source 1: `scripts/brain.py:cmd_reconcile()` — hardcoded path derivation
  - Source 2: `scripts/daily_ops.py` — reconcile call site without --data-dir
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260718-SINGLE_ASSET_HARDCODED_PATHS
- **关联 FIX**: FIX-20260718-002
- **状态**: **CLOSED** — cmd_reconcile() 参数化 + daily_ops.py 从 base_dir 契约派生双资产路径

### CCT-20260718-003
- **Docket ID**: DQAF-20260718-003
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU statarb 策略不受 ConformalOUGate 约束。OU gate 查找不到已归档的 OU brain 配置 → 静默 passthrough，无任何日志或指标。
  - [Layer 2 — 中间异常]: XAU OU_Params_V6_Sniper 和 OU_Params_V7_M15 均已被 FIX-20260625-136 退役（统计显著亏损）。ConformalOUGate 加载时找不到任何 XAU OU 配置 → `_ou_configs_by_strategy` 为空 → `filter()` 走 passthrough 路径。
  - [Layer 3 — 根因]: L3 架构缺陷 — gate bypass 路径零可观测性。passthrough 分支无 logging、无 metrics、无 describe() 暴露 → 无法感知 statarb 信号未受门禁约束。
- **证据引用**:
  - Source 1: `core/execution/conformal_ou_gate.py:filter()` — passthrough path with no logging
  - Source 2: `configs/brains/archive_deprecated/OU_Params_V6_Sniper.json` — retired OU config
- **是否被推翻**: 否 (AR 拒绝恢复已归档配置 — 统计显著亏损)
- **关联 ReB Pattern**: ReB-20260718-SILENT_GATE_BYPASS_ZERO_OBSERVABILITY
- **关联 FIX**: FIX-20260718-003
- **状态**: **CLOSED** — 节流 WARNING + passthrough 计数器 + describe() 诊断

### CCT-20260710-001
- **Docket ID**: DQAF-20260710-001
- **日期**: 2026-07-10
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU h4_swing ticket 4108944294 (SHORT@4107.272) 从未盈利, 但 ATR 收缩 (entry_atr=4.88→current_atr≈1.6, atr_ratio=0.33) 触发 25 次 TP 收紧 (3866.73→4060.36), 每次以 `comment='tp'` modify_sltp。SL 固定 4252.08, SL:TP 比从 [(4252-4107)/(4107-3866)]≈0.60 恶化至 [(4252-4107)/(4107-4060)]≈3.09 (TP 距 entry 仅 47 点, 冒 145 博 47)。
  - [Layer 2 — 中间异常]: `compute_trail_tp()` gate `atr_ratio=current_atr/pos.entry_atr<0.80` 通过, `tp_distance=trail_mult×current_atr×1.75×tf_scale` 重算更近 TP。门禁仅检查 ATR 收缩, 无盈亏前提 — position_manager.py:1696-1712。
  - [Layer 3 — 根因]: L3 设计不对称 — SL trail (compute_trail_stop) 有 `trail_activation_atr` 盈亏水位线 (FIX-20260603-064, trail_stop_engine.py:217-223), TP trail (compute_trail_tp) 无对等保护。两个 trail 机制同出一源 (Chandelier 体系) 但保护不对称: SL 侧要求 ≥1.0×ATR 盈利才激活, TP 侧零盈亏感知。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` ticket 4108944294 — 25 modify_sltp actions with comment='tp', tp 3866.73→4060.36
  - Source 2: `data/state/active_position.json` — entry_price=4107.272, cycles_held=14, breakeven_triggered=false
  - Source 3 (机制): `core/execution/position_manager.py:1696-1699` atr_ratio-only gate; `core/execution/trail_stop_engine.py:217-223` trail_activation_atr check in compute_trail_stop (对比)
  - Source 4 (golden_master): price path 4107.27→4136.30, never below entry (never profitable)
- **AR 对抗反驳**: 反假设(a)"收紧是对的 — 低 ATR 意味着原 TP 太远"→ **推翻**: 此逻辑仅对盈利持仓成立, 亏损持仓收紧 TP 让回升更难止盈; (b)"这是个例"→ **部分推翻**: XAU TP:SL=1:3.8 (总体 TP 命中率仅 7.1%), 系统性证据; (c)"SL trail 的 trail_activation_atr 已覆盖"→ **推翻**: trail_activation_atr 仅保护 SL trail, TP trail 独立运作无交叉保护。
- **是否被推翻**: 否 (存活假设; 三反假设均证伪)
- **关联 ReB Pattern**: ReB-20260710-TP_TRAIL_NO_PROFITABILITY_GATE

### CCT-20260709-003
- **Docket ID**: DQAF-20260709-003
- **日期**: 2026-07-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: live XAU h4_swing 4103318355 (SHORT) 的 TP 被 `comment='tp'` 逐周期从开仓 3823.46 (距 entry 232 点=3.5×H4_ATR, RR 1.66) 拉到 4044.8 (距 entry 11 点, RR 0.08, 冒 140 博 11)。影响半径: h4_swing 162/436(37.2%) + h1_swing 80/625(12.8%) 快照 RR<0.5。
  - [Layer 2 — 中间异常]: `position_manager.compute_trail_tp` gate `atr_ratio=current_atr/entry_atr`(均 M5, 3.40/4.61=0.738≤0.80) 触发, 然后 `tp_distance=trail_mult(2.0)×current_atr(M5,3.40)×1.75=11.9` 以 M5 小尺度覆盖 H4 大尺度开仓 TP (candidate=4055.844−11.9=4043.9≈实测 4044.8)。SL 全程不动 (H4 尺度 139.9)。
  - [Layer 3 — 根因]: RC-05 (boundary-error) L3 — per-TF ATR **半迁移**: FIX-20260706-027 把 per-TF ATR 注入**开仓定尺** (dynamic_sl_tp), 但 pos.entry_atr 仍存 M5 base (position_registration:198) 且管理期 current_atr 仍 M5 → 所有 bracket-relative 消费者 (compute_trail_tp / R 度量 / ratchet) 在错误尺度运算。跨 entry→management 交接的尺度边界未携带。
- **证据引用**:
  - Source 1: `scripts/verify_xau_post_restart_20260709.py` + ad-hoc RR 扫描 stdout — h4 37.2% / h1 12.8% RR<0.5 (Iron Law #11)
  - Source 2: `data/live_trade_journal.jsonl` 4103318355 open tp=3823.46 → modify tp=4044.8; `position_snapshots.jsonl` entry_atr=4.61/current_atr=3.40 (M5) 而 SL 距 139.9=2.0×H4_ATR
  - Source 3 (机制): `core/execution/position_manager.py:1691` `tp_distance=mult×current_atr×1.75`; `core/execution/dynamic_sl_tp.py:148` per-TF 定尺 vs `core/runtime/position_registration.py:198` `entry_atr=current_atr`(M5)
- **AR 对抗反驳**: 反假设(a)"SL 用 H4/TP 用 M5 开仓即异尺"→ **推翻** (open bracket 正是 1.66 RR); (b)"H4 ATR 真收缩到 3.4"→ **推翻** (若 H4 尺度 tp_distance≈210, 实测 11→反推 M5); (c)"entry_atr(4.61) 是定尺 ATR"→ **推翻** (SL 139.9≠2×4.61)。存活假设=compute_trail_tp 在 M5 尺度运算并覆盖 H4 bracket。
- **是否被推翻**: 否 (存活假设; 三反假设均证伪)
- **关联 ReB Pattern**: ReB-20260709-R_UNIT_MISMATCH_CROSS_TIMEFRAME (PER_TF_ATR_HALF_MIGRATION)
- **关联 FIX**: FIX-20260709-004
- **状态**: **CLOSED (终态)** — TP 侧 FIX-20260709-004 + 几何余项 FIX-20260709-006 (bracket_atr 换锚 breakeven/Chandelier/graduated_lock/max_lock)。激活端 (A1-A3): watermark/threshold/candidate 全切 bracket_atr with entry_atr fallback。锁定端 (L1-L2): graduated_lock_levels (3.0,1.5)/(5.0,3.5)→(1.0,0.5)/(2.0,1.0), max_lock_atr 4.0→2.0, bracket_atr 单位。Ratchet floor 故意未动 (entry_atr 稳定标尺, 双轨制)。反事实回测 96.1% XAU / 94.3% BTC breakeven death 存活。最终阈值标 SHADOW_TUNING_PENDING。proximity(Sev 4) + R 度量(observational) 仍 Deferred。

### CCT-20260708-004
- **Docket ID**: DQAF-20260708-004
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 仓位冲到 +1.4R~+6.3R MFE 后回撤, 被 signal_close 在 ~保本处市价平仓实现 ~$0, 或进一步回撤打 SL。give-back cohort (MFE≥1R 却 pnl≤0): BTC 121 / XAU 74。样本 4090084166 (short +1.44R, **0 改单**, signal_close breakeven); 样本 4067021409 (XAU long +6.30R, 27 改单 26 拒, tp@close 未置 0)。
  - [Layer 2 — 中间异常]: 唯一锁利机制=trailing SL, 但 87-89% 从未把 SL 推过 entry (SL@close 锁定 ≤0R)。三种失效: (a) `compute_trail_stop` 候选没推进就返回 None → 完全无底线; (b) 候选=extreme±mult×**current_atr**, 波动放大时 goalpost 移动, +1~1.5R 激活即锁负; (c) breakeven 楼层依赖 `breakeven_triggered`, 但 trail_dispatch.py:117 无条件置 True (意图锁, feasibility-skip/reject 也 latch)。主拒绝码 10025 NO_CHANGES (BTC 35/XAU 109)=重发同 SL。graduated_lock 首档 +3R 留 +1~3R 死区。
  - [Layer 3 — 根因]: RC-12 (missing-capability) L3 — 系统缺少一条**能抵达券商、抗改单失败、单调的硬利润棘轮**, 且模型出场(signal_close)在无底线时于保本处实现 $0。bracket 反转 (FIX-009) 仅 MODE_D 2.5-4% 尾部, **原 DQAF-004 假设误把尾部当主因, 被生命周期脚本推翻**。
- **证据引用**:
  - Source 1: `scripts/_diagnose_giveback_lifecycle.py` stdout — BTC MODE_B 105/121(86.8%) MODE_C 101/121(83.5%) MODE_D 3(2.5%); XAU MODE_B 66/74(89.2%) MODE_C 59(79.7%) MODE_D 3(4.1%); reject retcodes {10025,10006,10016}
  - Source 2: `data_btc/position_snapshots.jsonl` + `live_trade_journal.jsonl` ticket 4090084166 (+1.44R, 0 modify, SL@close=62831 在 entry 62651 之上=锁负)
  - Source 3 (对照): `data/` ticket 4067021409 (+6.30R, 27 modify/26 rej, tp@close=4186 未释放 → FIX-009 未生效)
  - Source 4 (机制): `core/execution/trail_stop_engine.py:131` compute_trail_stop (返 None 无底线) + `core/runtime/trail_dispatch.py:117` (breakeven_triggered 意图锁) + `scripts/mt5_bridge_worker.py:440` (10025 不在 _TRANSIENT_RETCODES)
- **AR 对抗反驳**: 反假设"bracket 反转 (SL 越 TP → FIX-009 释放 TP=0) 是主因"→ **被推翻**: MODE_D 仅 2.5-4%; FIX-009 本尊 ticket 4067021409 的 tp@close 仍=4186 (未置 0, FIX-009 未生效); BTC 侧主拒绝码是 10025 NO_CHANGES 非 10016 INVALID_STOPS。存活假设=trail 从未锁正底线 (MODE_B 87-89%)。
- **是否被推翻**: 否 (存活假设; 原 bracket-反转假设已被 AR 证伪并降级为 2.5-4% 尾部)
- **关联 ReB Pattern**: ReB-20260708-PROFIT_RATCHET_NEVER_REACHES_BROKER
- **关联 FIX**: FIX-20260708-004
- **状态**: **CLOSED** — Profit Ratchet Floor: peak r_max(entry_atr)≥arm_r 强制 SL 锁 ≥max(0.1R,r_max−1.0R), 折入候选即使 Chandelier 返 None 也托底, 独立于意图锁, 单调抑 NO_CHANGES。broker-bound 楼层封顶回撤 → R1 结构性吸收 MODE_C。意图锁 [[deferred_breakeven_intent_latch_20260708]] Deferred。

### CCT-20260708-003
- **Docket ID**: DQAF-20260708-003
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 用户实盘观察 BTC 到/超止盈却不平仓, 回撤后打止损; journal 却记为 breakeven ($0)。地面真相 ticket 3947528377: close 记录 close_price=63514.66 == entry_price 精确相等, pnl=0.0, reason=signal_close, label=breakeven; 而下一单 3 分钟后 @64598.99 开仓 → 真实市场 ~64599, ~+1084 点被记为 $0。
  - [Layer 2 — 中间异常]: `position_close_adapter._build_event` 取 `_new_deals = [d for d in deals if d.ticket > _cursor]` 后 `_deal = _new_deals[0]` (最早 deal)。adapter 每周期经 reconcile_and_record_closes() 新建实例 → `self._last_deal_id` 恒空 → cursor 恒 0 → `_new_deals` 含全部 deal → `[0]` = 最早 = DEAL_ENTRY_IN 入场 deal (price=入场价, profit=0, reason=3 signal) → close_price=入场价, pnl=0, label=breakeven。
  - [Layer 3 — 根因]: RC-06 (contract-violation) L3 — MT5 deal 模型知识在三处独立实现 (adapter 错取 deals[0]; reconciliation.py:118 与 mia_close.py:120 正确过滤 entry==1)。上游从未强制"一个 close 必须取自 DEAL_ENTRY_OUT 出场 deal"不变量, 允许分叉 → adapter 分支违约。同类模式 FIX-20260601-046 (label_builder 盲取 closes[0])。
- **证据引用**:
  - Source 1: `core/runtime/position_close_adapter.py` (pre-fix `_new_deals[0]` 无 entry 过滤)
  - Source 2: `data_btc/live_trade_journal.jsonl` ticket 3947528377 (close==entry==63514.66, pnl=0) + 次单 @64598.99
  - Source 3 (跨品种): `scripts/backfill_fabricated_breakeven.py` dry-run — BTC 14 (data_btc) + XAU 1 (data, ticket 4059439852) 同签名
  - Source 4 (对照正确路径): `core/runtime/reconciliation.py:118` + `core/runtime/mia_close.py:120` 均 `entry==1` 出场过滤
- **AR 对抗反驳**: 反假设"close==entry 是真实瞬时平仓(真 breakeven)"→ 被推翻: 次单 @64598.99 (相差 1084 点) 证明 3 分钟内市场已远离入场价, 若真在入场价平仓不可能在千点外重新开仓; pnl=0 与 reason=signal 是入场 deal 固有特征而非平仓结果。
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260708-BLIND_DEAL_INDEX_FABRICATES_BREAKEVEN

### CCT-20260628-062
- **Docket ID**: DQAF-20260628-062
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU governance_state.json 仅含 18 大脑条目，但 configs/brains/ 下存在 ~49 个 brain_registry_entry.v1 配置。PnLStore 有 49 大脑的 144K settled 记录，但 governance leaderboard 仅显示 18 大脑有指标注入。`governance_service.py:146-148` — `set_performance_metrics()` 对未注册大脑静默跳过（`_brain_states.get(brain_id)` 返回 None → 无操作，无日志，无告警）。
  - [Layer 2 — 中间异常]: Config→Governance 仅在 `_load_or_create_governance()` 首次创建时同步一次 (`daily_ops.py:117-144`)。后续新增 config 文件不会触发 governance 注册。配置状态变更 (candidate→live) 只在首次注册时写入 governance，之后 governance 独立演变 → 双轨漂移。
  - [Layer 3 — 根因]: RC-12 (missing-feature) + RC-09 (config-drift) — 缺少自动化 Config→Governance 对齐管道。FIX-20260613-076 确立 "governance owns lifecycle" 契约（正确），但未补充 "config defines existence" 的匹配机制。两者共同导致：config 定义大脑存在，governance 不知道自己需要管理它们。
- **证据引用**:
  - Source 1: `governance_service.py:146-148` — `self._brain_states.get(brain_id)` 静默跳过
  - Source 2: `daily_ops.py:117-144` — 首次创建时一次性同步，无后续对齐
  - Source 3: `daily_ops.py:3048-3056` — cmd_reconcile 已存在但仅处理 PnL ledger
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT

### CCT-20260628-063
- **Docket ID**: DQAF-20260628-063
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC circuit_breaker_active (management_only, consecutive_degraded=3) → 0 live brains → 全部 BTC 策略 should_trade=false → DQAF-059 ZERO LIVE brains p_win=0.40 fallback。`governance_state.json` transition_log 显示 V4 在 3 小时内被降级 3 次 (06:51/09:04/09:34)，每次 SSOT reconciliation (08:55/09:29/10:20) 恢复后下一 governance cycle 再次降级。governance state 从 3 膨胀到 16 brains（12 个幽灵重新注册）。
  - [Layer 2a — 幽灵注册]: `governance_scheduler.py:300` — `pnl_store.get_all_metrics()` 返回 PnL ledger 中全部 13 个 brain（含 12 个已归档 brain: V1/V2/V3/V5/V6/V7/V8/V9/V10/LGB_V1/V11/V12_H1_Survival）。`governance_scheduler.py:357-358` — `current_state is None → governance.register_brain(brain_id, "candidate")` 每次 cycle 将这些幽灵重新注册为 candidate。
  - [Layer 2b — 评分过严]: Quality Engine V4 评分 27.69→"degraded" tier→probation。Legacy 路径 WR=35.5% < WR_PROBATION_THRESHOLD=45%→probation。RR-adjusted 通道 (FIX-20260627-152) PF=1.15 < PF_RR_ADJUSTED_MIN=1.3→blocked。V4 有 298 trades/+42.4R/PF=1.15 但三条路径全部通向 probation。
  - [Layer 2c — Last-live guard 绕过]: FIX-20260628-162 在 `governance_rule_engine.py:201-210` 添加 last-live guard — 但实际降级走 `governance_scheduler.py:462` → `GovernanceService.transition()` 直接调用，绕过 rule engine 的 `evaluate()` 路径。Guard 从未被检查。
  - [Layer 3 — 架构根因]: RC-11 (stale-data) + RC-06 (contract-violation) — PnL ledger 无生命周期 GC 机制，已归档 brain 的历史 PnL 数据永久残留成为幽灵注册数据源。双轨降级路径（quality_engine + legacy threshold）均绕过 rule engine 的 last-live guard → 单一 live brain 无任何防护。
- **证据引用**:
  - Source 1: `governance_state.json` transition_log — V4 3 次降级 (06:51/09:04/09:34)，3 次 SSOT 恢复 (08:55/09:29/10:20)，13 次幽灵注册
  - Source 2: `governance_scheduler.py:300` — `pnl_store.get_all_metrics()` 返回 13 个 brain；`:357-358` — 无条件 `register_brain(bid, "candidate")`
  - Source 3: `governance_scheduler.py:462` — `governance.transition(brain_id, target_status)` 直接调用，绕过 rule engine
  - Source 4: `live_trade_journal.jsonl` — ticket=4006314705 V4 trade (09:40 OPEN, 10:04 TP close, +$1.38)
  - Source 5: `brain_pnl_ledger.json` — settled 表含 13 个 brain（含 12 个已归档）
  - Source 6: `brain_quality_engine.py:323-357` — V4 score=27.69, tier="degraded"
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-PING_PONG_DEMOTE

### CCT-20260628-061
- **Docket ID**: DQAF-20260628-061
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 49 XAU 大脑在 BrainPnLStore 中有指标数据 (144K+ settled 记录) 但 governance_state 中仅 18 大脑有非空 `performance_metrics`。31 大脑的指标完全不可见于 downstream (leaderboard, weighter, strategy_evaluator)。
  - [Layer 2 — 中间异常]: (A) `governance_scheduler.py:347-350` — 循环遍历 `all_metrics`（来自 PnLStore 的 49 大脑），调用 `governance.set_performance_metrics()`，但 `governance_service.py:146-148` 因大脑未注册而静默跳过。(B) `scheduler_service.py:298-317` — MT5 调度器 purge 逻辑检查 `source` 字段清除 backtest 指标，但 `daily_ops` → `governance_scheduler` 使用 `_data_source` 字段 → 字段名不匹配 → 合法 daily_ops 注入指标被作为 stale backtest 清除。(C) Journal-based metrics 因 80% XAU entries 缺少 `position_ticket` (有别于 BTC 的 `event` 字段) → `compute_journal_brain_metrics()` 跳过无 ticket 条目 → journal 无法为缺少大脑提供 fallback。
  - [Layer 3 — 根因]: RC-06 (contract-violation) + RC-09 (config-drift) — `set_performance_metrics()` 的静默跳过是合约违规：调用方期望指标被注入，实现方因未注册而吞没数据。`_data_source` vs `source` 字段名分裂是配置漂移：两个独立演进子系统约定不同的字典键名。
- **证据引用**:
  - Source 1: `governance_service.py:146-148` — 静默跳过逻辑
  - Source 2: `scheduler_service.py:298-301` — purge 仅检查 `source` 不检查 `_data_source`
  - Source 3: `governance_scheduler.py:377-379` — daily_ops 使用 `_data_source` 键名
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-GOVERNANCE_REGISTRATION_SILENT_SKIP

### CCT-20260626-001
- **Docket ID**: DQAF-20260626-001
- **日期**: 2026-06-26
- **置信度**: confirmed (git diff confirmed BLE001 narrowing + evidence from 6 sources)
- **因果链**:
  - [Layer 1 — 症状]: `data_btc/golden_master.jsonl` 06-20~06-24 连续 5 天零记录 (正常每天 ~150 周期)。`data_btc/regime_snapshots.jsonl` 06-21 起跟随停止 (cascade: build_regime_snapshots.py 以 GM 为主数据源)。所有下游 regime 分析工具盲化。
  - [Layer 2 — 中间异常]: (A) BLE001 Phase 3a (FIX-20260619-057) 将 live_cycle.py golden_master 异常处理从 `except Exception` 收窄为 `except (ValueError, TypeError, OSError)`。`record_cycle_inputs()` 内部 `regime_info.get()` 对非 dict 类型抛 `AttributeError` — 不在收窄元组内 → 静默逃逸。(B) `golden_master.py:170` `except OSError: pass` 为盲 catch 零日志吞没 — 即使同文件内的 I/O 错误也完全不可观测。
  - [Layer 3 — 根因]: L3 架构缺陷 — 非阻塞 telemetry 路径异常处理契约脆弱。(a) `record_cycle_inputs()` 无内部防御深度 — 依赖调用方猜对异常类型。(b) BLE001 收窄时未审计调用链完整异常剖面。(c) golden_master 失败无告警/监控/自动恢复 — 5 天盲区。(d) build_regime_snapshots 单一数据源依赖 — GM 故障直接级联。
- **证据引用**:
  - Source 1: `data_btc/golden_master.jsonl` — 06-19 cycle 153 停止, 06-20~06-24 零条, 06-25 仅 6 条
  - Source 2: `git diff 0002ea83..49e46a4c -- core/runtime/live_cycle.py` — except Exception → except (ValueError, TypeError, OSError)
  - Source 3: `core/runtime/golden_master.py:76-77` — regime_info.get() 无防御
  - Source 4: `core/runtime/golden_master.py:170` — except OSError: pass 盲 catch
  - Source 5: `scripts/build_regime_snapshots.py:27-29` — golden_master.jsonl 为主数据源
  - Source 6: XAU `data/golden_master.jsonl` — 5,890 条持续至 06-25 (同一 period 正常), 证实 BTC-only 问题
- **是否被推翻**: 否 — AR 5 条假设全被推翻 (GOLDEN_MASTER_RECORD=0 / base_dir 变化 / 系统宕机 / block_new_entries / 收窄类型足够)
- **关联 ReB Pattern**: ReB-20260626-001
- **关联 FIX**: FIX-20260626-001

### CCT-20260622-060
- **Docket ID**: DQAF-20260622-060
- **日期**: 2026-06-22
- **置信度**: confirmed (5 工程契约 × 双模 PSI × 实测验证)
- **因果链**:
  - [Layer 1 — 症状]: PSI 在 raw 特征空间 36/40 特征 Sev1 (mean_PSI=2.73, max_PSI=8.28)。归一化后降至 38/40 Sev1 (mean_PSI=3.42, max_PSI=12.43) — 不降反升确认真阳性 regime change。3 个独立 PSI 实现 (等频/等宽/合并分箱) 互不一致。
  - [Layer 2 — 中间异常]: (A) `--compute-baseline` flag 定义但从未实现 — baseline 不可复现。(B) PSI 在 raw 特征空间计算, 树模型 (`normalize: false`) 对尺度变换不敏感 — PSI 高 ≠ 模型退化。(C) 阈值 0.10/0.25 从归一化场景校准, 在 raw 空间不适配。(D) 无 model-performance correlation 验证框架 — PSI 信号不可操作。
  - [Layer 3 — 根因]: L3 架构缺陷 — PSI 监控缺乏 (1) 归一化策略 (训练 μ/σ vs 滚动 μ/σ), (2) 双模解耦 (regime vs anomaly), (3) 工程保护 (零方差/对数发散/窗口隔离/样本非对称)。`stability_monitor.compute_psi()` 使用等宽分箱 (合并数据), 而 `monitor_feature_drift._compute_psi()` 使用固定 baseline 分箱 — 两个"PSI"不可比。
- **证据引用**:
  - Source 1: `scripts/monitor_feature_drift.py:1-712` — 完整重写 (DQAF-060), 287→712 lines
  - Source 2: `core/brains/services/stability_monitor.py:31-87` — `compute_psi()` @deprecated
  - Source 3: `data/training/balanced_v1/feature_baseline_v9_normalized_20260622.json` — 新 baseline (160,138 samples, 40 features, norm μ/σ 内嵌)
  - Source 4: CLI 实测 — Mode A: mean_PSI=3.42, Mode B: mean_PSI=9.0
  - Cross-symbol: BTC confirmed regime-changed. XAU PSI pending empirical scaler.
- **是否被推翻**: 否 — AR 假设 (归一化后 PSI 应降) 被实测推翻: PSI 反升 2.73→3.42, 证伪"raw 特征导致假阳性"假设, 确认"BTC 真的 regime-changed"
- **关联 ReB Pattern**: ReB-20260622-060
- **关联 FIX**: FIX-20260622-060

### CCT-20260622-058-bis
- **Docket ID**: DQAF-20260622-058-bis
- **日期**: 2026-06-22
- **置信度**: confirmed (code audit × 3 sites verified × runtime confirmation)
- **因果链**:
  - [Layer 1 — 症状]: DQAF-058 部署后 `micro_scaler_loaded: false` 持续。健康检查 `MICRO_SCALER_NOT_LOADED` 警告未消除。MetaFilter 仍然在 raw features 上运行。
  - [Layer 2 — 中间异常]: DQAF-054 修复了 3 个 `MicrostructureFeatureAdapter` 实例化站点, DQAF-055 补齐了其余 2 个 — 但 `meta_signal_filter.py:135` 使用 `self._micro_scaler = joblib.load(micro_scaler_path)` 直接加载, 完全绕过 adapter 的 `_load_scaler_json()`。`live_intent_loop.py:1512` 和 `bootstrap_v9.py:91` 缺少 `resolve_scaler_path()` 回退。
  - [Layer 3 — 根因]: L2 逻辑缺陷 — `MetaSignalFilter` 是 `MicrostructureFeatureAdapter` 的**消费者**而非子类, 其 scaler 加载是独立实现。DQAF-054 的模式搜索 (`grep joblib.load`) 遗漏了此站点因为此处不是 adapter 实例化而是**直接消费**。
- **证据引用**:
  - Source 1: `core/execution/meta_signal_filter.py:135` — `joblib.load(micro_scaler_path)` (修复前)
  - Source 2: `scripts/live_intent_loop.py:1512-1520` — `resolve_scaler_path()` 回退 (新增)
  - Source 3: `apps/engine/bootstrap_v9.py:91-99` — `resolve_scaler_path()` 回退 (新增)
  - Cross-symbol: 仅 BTC 受影响 (XAU 尚无 MetaFilter 配置)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-058-bis
- **关联 FIX**: FIX-20260622-058-bis

### CCT-20260622-058
- **Docket ID**: DQAF-20260622-058
- **日期**: 2026-06-22
- **置信度**: confirmed (6 源 ECoL + DA/AR 双轨 + 跨品种验证)
- **因果链**:
  - [Layer 1 — 症状]: BTC PSI 38/40 特征 Sev1, `micro_scaler_loaded: false` 持续 23 天。`MICRO_SCALER_NOT_LOADED` 警告从未触发（健康检查缺失此检查项）。
  - [Layer 2 — 中间异常]: (A) `MicrostructureFeatureAdapter.resolve_scaler_path()` 硬编码 `btc_micro_scaler.json` → XAU 永远找不到 scaler。(B) `require_scaler=True` + 无 scaler → `DataIntegrityError` 阻断 XAU 启动。(C) 健康检查 `check_meta_filter_state` 不提取 `micro_scaler_loaded` → 运维盲区。
  - [Layer 3 — 根因]: L3 架构缺陷 — DQAF-054 引入的 JSON scaler 加载替换了 joblib, 但部署激活是独立步骤: 需要 (1) 生成 JSON scaler 文件, (2) 配置 `micro_scaler_path`, (3) 健康检查验证。这三个步骤均缺失。冷启动路径 (无 Feature Store 的新品种/新环境) 从未被设计 — 系统要求 scaler 必须存在, 但没有"不存在时怎么办"的答案。
- **证据引用**:
  - Source 1: `core/features/adapters/microstructure_feature_adapter.py:resolve_scaler_path()` — 修复前硬编码 `btc_micro_scaler.json`
  - Source 2: `core/observability/health_checks.py:check_meta_filter_state()` — 修复前不检查 `micro_scaler_loaded`
  - Source 3: `scripts/generate_micro_scaler.py` — 新建多品种 scaler 生成脚本
  - Source 4: `data_xau/models/xau_micro_scaler.json` — XAU 冷启动 identity scaler
  - Source 5: `data_btc/models/btc_micro_scaler.json` — BTC 实证 scaler (前序 DQAF-054 产出)
  - Cross-symbol: XAU 受阻断（启动熔断）, BTC 受静默退化（raw features）
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-058
- **关联 FIX**: FIX-20260622-058

### CCT-20260622-057
- **Docket ID**: DQAF-20260622-057
- **日期**: 2026-06-22
- **置信度**: confirmed (Layer 1-2: confirmed by code review + ledger data; Layer 3: confirmed by DQAF-033/034 external-close evidence)
- **因果链**:
  - [Layer 1 — 症状]: Label coverage 从 85%→65% (XAU), 67%→40% (BTC)。Timestamp inversions 从 336→365 (XAU), 22→52 (BTC)。Evidence: `audit_data_exhaustive.py:216-222`, `live_labels.jsonl` per-symbol counts。
  - [Layer 2 — 中间异常]: (A) `build_trade_records()` 依赖 close_price 计算 PnL — 当 close_price 缺失时 PnL=None → label="unlabeled"。无 label_contract defense layer 可回退至 SL/TP barrier 分类。(B) `live_cycle.py:1338` 使用 `.locks` 锁目录而所有其他 writer 使用 `locks` — 跨进程 FileLock 协调失效。(C) `_merge_overflow_files` 零锁写入共享 journal。
  - [Layer 3 — 根因]: (A) DEAL_REASON_SIGNAL 外部平仓比例 66% (DQAF-033/034) → journal ingestion 盲区。(B) 多进程 journal 写入架构 + 锁命名空间碎裂 (L3 architecture defect)。(C) label pipeline 无 defense layer (L2 logic defect with L3 contributory)。
- **证据引用**:
  - Source 1: `label_builder.py:176-307` — `build_trade_records()` matching logic, `_classify_label(pnl)` vs `_classify_barrier_label()`
  - Source 2: `live_cycle.py:1338` — `.locks` lock directory (active bug)
  - Source 3: `mt5_bridge_worker.py:220` — `_merge_overflow_files` zero-lock write
  - Source 4: `daily_ops.py:2049` — `_step_label_builder` called without `contract_path`
  - Source 5: DQAF-20260621-033/034 — 66% external close evidence
  - Source 6: `audit_data_exhaustive.py:216-222` — coverage computation logic (LONG-only denominator)
  - Cross-symbol: Both XAU and BTC affected — rules out symbol-specific code asymmetry
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-LABEL_COVERAGE_DEGRADATION, ReB-20260613-JOURNAL_LOCK_NAMESPACE_FRAGMENTATION

### CCT-20260621-042
- **Docket ID**: DQAF-20260621-042
- **日期**: 2026-06-21
- **置信度**: confirmed (10 项发现 × 8 项坐实 × 双源交叉验证: journal + state files + code audit)
- **因果链**:
  - [Layer 1 — 症状 (视图静默损坏)]: Leaderboard 崩溃 (Sev 1) — `brain_performance` 维度全部为空, `leaderboard.json` 产出空排行榜, `daily_ops` Poison Pill 阻断整个管线。Journal vs Labels 38% 缺口 (Sev 2) — 38% 的交易在 journal 中存在但在 label 导出中缺失。治理含 3140 笔回测数据 (Sev 2) — governance_state.json 被 backtest 时代的 V12_H1 历史数据污染。校准器 p_win 退化至 0.5 (Sev 2), Alpha 数据不一致 (Sev 2), 健康报告自相矛盾 (Sev 2), golden_master 未排序 (Sev 3)。**修复被实盘进程覆盖** (Sev 1 新发现) — 人工修复的 state JSON 被 live 进程在下一 cycle 覆写回损坏版本。
    - 证据: `data_btc/reports/leaderboard.json` — brains=[]; `data_btc/reports/live_labels.jsonl` — 38% 缺口; `data_btc/governance_state.json` — V12_H1 3140 trades
  - [Layer 2 — 中间异常 (生成器数据计算残缺 + 鸭子类型无防备)]: (A) `compute_journal_brain_metrics()` 产出缺少 `sharpe_ratio` / `cumulative_pnl` 等关键字段的字典 → `BrainLeaderboard._validate_metrics()` 在缺少字段时未抛异常（使用 `dict.get(key, default)` 贴纸）, 下游静默产出空排行榜。(B) `daily_ops.py` `_step_retraining_check()` 中 `leaderboard.get("total_decisions", 0) == 0 and _gov_states` → `DataIntegrityError` → 管线 Poison Pill 阻断 — 这是正确的 Fail-Closed 行为, 但暴露了上游 generator 数据不完整的事实。(C) governance_state.json 中 `total_trades` 字段包含 backtest 时期的 3140 笔交易 — governance_service 未按 `is_live` 字段过滤历史数据。(D) 人工直接修改 state JSON → live 进程在下一 cycle 从 ledger 重建时覆盖修复 — 两套写入器 (人工 + live 进程) 对同一物化视图的竞态覆写。
    - 证据: `core/brain_leaderboard.py:_validate_metrics()` — 修复前 `dict.get(key, default)` 贴纸; `scripts/daily_ops.py:_step_retraining_check()` — Poison Pill 触发逻辑; `data_btc/governance_state.json` — V12_H1 is_live=false 但 total_trades=3140
  - [Layer 3 — 根因 (架构坍塌 — 混淆不可变账本与物化视图)]: **RC-11 (architecture-violation)** — `IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION` 架构模式被系统性违反。系统的物理设计是 Event Sourcing (ledger → generator → view), 但日常运维中人工绕过生成器直接修改物化视图——混淆了 `append-only immutable journal` 与 `regenerated ephemeral view` 两个本体论范畴。这导致: (a) 人工修复与实盘进程的互斥覆写竞态, (b) 修复无法持久 (下一 cycle 被覆盖), (c) 根因从未被触及 (因为 generator code 中的 bug 被"直接修 JSON" 的运维惯性永久掩盖)。同类根因在 DQAF-20260615-011 (退役大脑幽灵霸占排行榜 — 视图未过滤活性)、DQAF-20260615-012 (orphan 合成条目污染告警 — 视图消费者未区分合成/真实) 中反复出现。
    - 证据: `CLAUDE.md` 2. AGENT BEHAVIORAL RESTRICTIONS — 4 条 RED 禁令显式编码了正确的架构关系; `.gitignore` — 24 条 ephemeral state 模式物理隔离; `tests/test_state_reconstruction.py` — 26 契约测试强制 ledger→view 重建可复现性
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 385+ 条记录, 38% label 缺口
  - Source 2 (State Files): `data_btc/reports/leaderboard.json` — brains=[]; `data_btc/governance_state.json` — 3140 backtest trades; `data_btc/calibrator_feed_state.json` — p_win=0.5
  - Source 3 (Code Audit): `core/feedback/live_journal_metrics.py`, `core/brains/services/brain_leaderboard.py`, `scripts/daily_ops.py` — 完整 generator 链路追踪
  - Source 4 (Git History): 5 commits (7d448ae → e8fe77c5) — 四防线全生命周期
  - Source 5 (Cross-symbol): `data/live_trade_journal.jsonl` — XAU 同架构, 确认非品种特化
- **是否被推翻**: 否 — AR 反向假设 (单文件损坏, 修复 JSON 即可) 被 10 项发现中 8 项坐实推翻: 问题是架构级而非数据级
- **关联 ReB Pattern**: ReB-20260621-042 (`IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION`)
- **关联 FIX**: FIX-20260621-042

### CCT-20260621-043

- **Docket**: DQAF-20260621-043
- **Confidence**: confirmed (7 源交叉验证 × 实测复现)
- **Refutation**: 否 — AR 反向假设 (purge 已运行后被覆盖) 经 `_step_governance` 实测推翻，确认治理周期从未成功执行

**Layer 1 — 症状 (视图静默损坏)**:
  - 治理状态含 14 brains 回测数据 (BTC V12: 3140 trades, Sharpe -16.66; XAU 13 brains >1000 trades)
  - `_step_governance` 每次都返回 `{'status': 'error', 'error': "'dict' object has no attribute 'win_rate'"}`
  - 证据: `data_btc/governance_state.json` — V12_H1 total_trades=3140; `daily_ops._step_governance('data_btc', dry_run=True)` 实测 crash
  - 置信度: confirmed

**Layer 2 — 中间异常 (类型管线的隐式断裂 + 静默吞没反模式)**:
  - FIX-20260621-032 在 `governance_scheduler.py:264` 添加 `all_metrics[_bid] = _jm` — 将 journal dict 直接赋值给期望 BrainPnLMetrics dataclass 的集合
  - `compute_journal_brain_metrics()` 返回 `dict[str, dict]` — 键访问 (`.get()`)
  - `pnl_store.get_all_metrics()` 返回 `dict[str, BrainPnLMetrics]` — **dataclass 属性访问** (`.win_rate`)
  - 下游 `metrics.win_rate` (line 301) 在 dict 上触发 `AttributeError`
  - `daily_ops._step_governance` 的 `except Exception` (line 530) 静默吞没此错误 — 返回 `{"status": "error"}` 但无日志无告警
  - 证据: `governance_scheduler.py:264` — 赋值语句; `daily_ops.py:530-532` — except Exception; 实测 dry_run crash 输出
  - 置信度: confirmed

**Layer 3 — 根因 (架构缺陷: 跨模块边界缺乏类型强制)**:
  - L2 逻辑缺陷: 单一赋值语句的类型不匹配 (dict vs dataclass) 导致全治理周期静默崩溃
  - L3 架构缺陷: IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION 的数据管道缺乏端到端类型约束 —
    Journal (SSOT, dict) → governance_scheduler (期望 dataclass) → governance_state.json (projection)
    中间没有任何 Schema 校验或类型转换层
  - 反模式 (ReB-043): `BOUNDARY_TYPE_ENFORCEMENT_AND_EXPLICIT_CATCH` — 
    跨核心子系统边界禁止原生 dict 裸奔; 顶层调度器禁止无类型断言的 `except Exception`
  - 证据: 完整代码追踪 governance_scheduler.py:250-307 + daily_ops.py:493-532 + brain_pnl_ledger.py:BrainPnLMetrics dataclass
  - 置信度: confirmed

**修复验证**:
  - `_step_governance('data_btc', dry_run=True)` → `Status: ok, Brains assessed: 14` (修复前: Status: error, crash)
  - `_step_governance('data', dry_run=True)` → `Status: ok, Brains assessed: 50` (修复前: crash)
  - 3/3 合约测试 PASSED
  - purge: BTC 1 brain 修正 / XAU 13 brains 修正

**交叉引用**: ReB-20260621-043 (`BOUNDARY_TYPE_ENFORCEMENT_AND_EXPLICIT_CATCH`), FIX-20260621-043, DQAF-20260621-042 (上游)

---

### CCT-20260620-002
- **Docket ID**: DQAF-20260620-002
- **日期**: 2026-06-20
- **置信度**: confirmed (3 源确认: code audit + git history + cross-file trace)
- **因果链**:
  - [Layer 1 — 症状]: XAU budget_breached 误触发 — 单笔 -$5 亏损被计为 -500% 日 PnL，导致熔断器错误断开。budget.daily_pnl_pct 累积值远超 -3% 限制，但实际 USD 亏损仅 ~$5。断路器误触发后系统停止交易。
  - [Layer 2 — 中间异常]: 三条独立代码路径将 raw USD 值传递给 `StrategyBudget.record_trade(pnl_pct, is_win)`，该参数期望的是 decimal fraction (如 0.005 = 0.5%) 而非 USD 绝对值。(A) `live_cycle.py:2348-2358` MIA close 路径 — `_mia_pnl` 为 raw USD，直接传入 record_trade；(B) `managed_close.py:317` — `_pnl_pct = float(pnl) / 1000.0` 硬编码 divisor；(C) `position_close_adapter.py:255-260` — `_notify_budget` 回退路径未经 USD→pct 转换。唯一正确的路径是 `live_cycle.py:1617` reconciliation — `_pnl_pct = _evt.pnl / _eq`。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — `pnl_pct` 参数名的语义契约仅存在于变量名中，未经类型系统强制执行。`float` 类型接受任何数值，USD 与 percentage 在类型层面不可区分。这是 L3 架构缺陷: 量纲安全依赖人工审查而非编译器闸门。同类模式已出现于 DQAF-20260615-011 (pnl_r ↔ pnl_per_unit 量纲混乱) 和 DQAF-20260607-007 (策略盈亏 USD vs R-multiple 标签错位)。
- **证据引用**:
  - Source 1: `core/execution/strategy_budget.py:record_trade()` — pnl_pct 参数 docstring 明确期望 decimal fraction
  - Source 2: `core/runtime/live_cycle.py:2348-2358` (pre-fix) — MIA 路径 raw USD 直接传入
  - Source 3: `core/execution/managed_close.py:317` (pre-fix) — 硬编码 `/1000.0` divisor
  - Source 4: `core/runtime/position_close_adapter.py:255-260` (pre-fix) — `_notify_budget` 回退路径未转换
  - Source 5: `core/runtime/live_cycle.py:1617` — reconciliation 路径正确转换 (正面控制)
- **是否被推翻**: 否 — AR 反向假设 (budget 计算正确, 实际亏损确实超限) 被 journal 逐笔去重统计推翻: 实际 PnL ≈ -$5, 远低于 -$30 daily limit
- **关联 ReB Pattern**: ReB-20260620-002 (PNL_UNIT_MIXING)
- **关联 FIX**: FIX-20260620-003

### CCT-20260615-012
- **Docket ID**: DQAF-20260615-012
- **日期**: 2026-06-15
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 钉钉告警显示胜率 0.91% (7/767)，断路器断开。当日盈亏仅 $0.39，但窗口内"交易数"高达 767。
  - [Layer 2 — 中间异常]: 767 笔"交易"中 752 笔是 `auto_orphan_rejected` 合成 close 条目 (pnl=0, position_ticket=None)。这些条目由 `cleanup_orphan_opens()` 在启动时生成，为历史 rejected open 写 synthetic close。由于没有 ticket，绕过了告警上下文的去重逻辑 (`if _pos_tkt is not None`)。pnl=0 被计为"亏损"→752:7 的比例将真实胜率从 46.67% 稀释至 0.91%。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — 告警上下文构建器未区分 `auto_orphan_*` 合成条目与真实交易。`cleanup_orphan_opens()` 生成的 synthetic close 是合法审计记录，但不应参与实时告警统计。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` — 2671 orphan close, 752 today
  - Source 2: `core/ledger/services/journal_cleanup.py:275-318` — cleanup_orphan_opens() 生成逻辑
  - Source 3: `core/runtime/live_cycle.py:903-959` — 告警上下文 journal 扫描逻辑
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260615-012 (ORPHAN_ENTRY_ALERT_POLLUTION)

### CCT-20260615-011
- **Docket ID**: DQAF-20260615-011
- **日期**: 2026-06-15
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 钉钉告警显示"最差大脑"为 `BTC_Swing_V11_M15_Directional`，累积PnL(R) -1452.68，胜率 0.1579。但该大脑已归档禁用（`enabled=False`），不在 `governance_state.json` 活跃列表中。告警"最差大脑"指标与实盘日记账 $4.61/日 无法对账。
  - [Layer 2 — 中间异常]: (A) `get_all_metrics()` 返回所有大脑（含退役/归档），min(cumulative_pnl) 选中的永远是历史最长的退役大脑。(B) `load_from_stream()` 将事件流的 `pnl_r`（R-multiple, 相对值）直接赋值给 `pnl_per_unit`（美元/单位, 绝对值）→ 同一字段承载两种不可比量纲 → 累积求和无数学意义。
  - [Layer 3 — 根因]: RC-06 (contract-violation) + RC-11 (stale-data)。Event Sourcing 迁移中 `pnl_r ↔ pnl_per_unit` 的序列化契约未定义单位转换。退役大脑的历史数据未从告警评比中排除——"幸存者偏差"的逆向版本：尸体统治排行榜。
- **证据引用**:
  - Source 1: `data_btc/ledger_events.jsonl` — 1227事件(仅live+migration)中 V11 pnl_r 累积=-1452.69
  - Source 2: `configs/live_btc.yaml` — V11路径在 `archive/` 下, `enabled=False`
  - Source 3: `data_btc/governance_state.json` — V11不在 brain_states 中
  - Source 4: `brain_pnl_ledger.py:872` — `"pnl_per_unit": event.pnl_r` 单位错配
  - Source 5: `brain_pnl_ledger.py:904` — `_t.get("pnl", 0)` 读取不存在的字段名
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260615-011 (ARCHIVED_BRAIN_ALERT_POLLUTION)

### CCT-20260608-001a
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `execution_state.json` 显示 `circuit_breaker_tripped: true` 但 `consecutive_degraded: 0`。备份文件（6/6）显示 `false` → 熔断器在 6/6~6/7 间触发且从未自愈
  - [Layer 2 — 中间异常]: 熔断器有 3 种触发路径（bridge_silence/cycle_stall×3/ExecutionQueueFatalError），但自愈逻辑仅覆盖 cycle_stall 路径。bridge_silence 和 FatalError 不递增 `consecutive_degraded` → 自愈条件 `_consecutive_degraded_cycles > 0` 永久为 False
  - [Layer 3 — 根因]: RC-06 状态机非对称陷阱 (Asymmetric State Machine Trap) — 多路径触发 vs 单路径自愈的不完备状态转换表。`live_cycle.py:2771` 自愈条件与 `live_cycle.py:2634` bridge_silence 触发路径不兼容
- **证据引用**:
  - Source 1: `data_btc/state/execution_state.json:24` — `circuit_breaker_tripped: true, consecutive_degraded: 0`
  - Source 2: `data_btc/state/execution_state.json.bak:31` — 6/6 03:54 仍为 `false`
  - Source 3: `core/runtime/live_cycle.py:2634-2648` + `2771-2784` — 触发与自愈代码源
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-001

### CCT-20260608-001b
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `meta_filter_state.json` 所有缓冲区为空（pred_history=0, pred_buffer=0, atr_buffer=0, micro_spread_buffer=0）
  - [Layer 2 — 中间异常]: `MetaFilterGate(model_dir=f"{base_dir}/models/meta_filter_v3")` → `data_btc/models/meta_filter_v3/` 不存在 → `_mg.load()` 抛出 FileNotFoundError → `except Exception` 静默吞噬 → `_mg.is_loaded=False` → `state._meta_filter_gate` 从未赋值
  - [Layer 3 — 根因]: RC-09 config-drift — BTC 品种迁移到 `data_btc/` 时，静态模型文件留在 `data/models/`，路径构造盲目使用 `config.base_dir` 导致断裂
- **证据引用**:
  - Source 1: `core/runtime/live_cycle.py:3900` — `model_dir=f"{config.base_dir}/models/meta_filter_v3"`
  - Source 2: `data/models/meta_filter_v3/` 存在（4个文件） vs `data_btc/models/meta_filter_v3/` 不存在
  - Source 3: `meta_filter_state.json` — 全空缓冲区
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-002

### CCT-20260608-001c
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `calibrator_feed_state.json` 中 `"updated_utc": "35"` — 值 "35" 不是 ISO 时间戳
  - [Layer 2 — 中间异常]: `scripts/daily_ops.py:379` 代码 `"updated_utc": str(cal.describe().get("sample_count", "?"))` — 字段名期望时间戳但实际读取 `sample_count` 键（整数 35）
  - [Layer 3 — 根因]: RC-06 contract-violation — `cal.describe()` 不返回 `updated_utc` 字段，开发者使用错误键名回退到 `sample_count`
- **证据引用**:
  - Source 1: `data_btc/calibrator_feed_state.json:2` — `"updated_utc": "35"`
  - Source 2: `scripts/daily_ops.py:379` — 源码行确认为字段-值错配
  - Source 3: `core/execution/conformal_calibrator.py:337` — `describe()` 返回键名确认为 `sample_count`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-003
- **Docket ID**: DQAF-20260607-005
- **日期**: 2026-06-07
- **置信度**: confirmed (3 源确认)
- **因果链**:
  - [Layer 1 — 症状]: BTC 持仓 ticket=3807675970 数小时未平仓，系统持续开新仓 (vol=0.09, 113 周期)，但 exit watchdog 未管理任何持仓。日志仅 3 个 management_phase 事件 vs 113 个 multi_strategy_eval 事件。
    - 证据: `intent_20260606T134832Z.log` — `cycle_error` 事件 (13:54:45) + `orphan_position_adopted` 事件 (13:59:44) + 仅 3 个 management 事件 vs 113 周期
  - [Layer 2 — 中间异常]: `execution_queue.py:350` 中 `_close_result` 变量未初始化即被引用 (`UnboundLocalError`)，导致 `flush()` 崩溃。调用方 `live_intent_loop.py:1902` 的 `except Exception` 仅打印日志但未熔断，系统继续新开仓循环。
    - 证据: `execution_queue.py` git diff 显示 line 194 的 `_close_result = None` 初始化是后加补丁；traceback 确认崩溃点在 `flush()` 内部
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 反模式)** — 派发管道的致命异常被通用 `except Exception` 吞噬，未触发 circuit_breaker。孤儿收养逻辑仅存储 `source + adopted_at` 元数据，exit watchdog 无足够信息接管。
    - 证据: `live_cycle.py:2608-2644` 孤儿收养代码仅写入 2 字段；`live_intent_loop.py:1902-1916` 异常处理未区分 fatal vs transient
- **证据引用**:
  - Source 1: `data_btc/logs/intent_20260606T134832Z.log` — cycle_error traceback (line 350)
  - Source 2: `core/execution/execution_queue.py` git history — `_close_result` init added in 3dbeeb4
  - Source 3: `core/runtime/live_cycle.py:2608-2644` — orphan adoption minimal metadata
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-003

---

### CCT-20260608-002
- **Docket ID**: DQAF-20260608-002
- **日期**: 2026-06-08
- **置信度**: confirmed (双源确认)
- **因果链**:
  - [Layer 1 — 症状]: XAUUSDc LONG 平仓 (ticket=3818953854, 07:09:43, exit_watchdog:meta_exit, PnL=+0.03) 无钉钉通知。操作员仅收到开仓提醒，完全不知道仓位已平。
    - 证据: `data/live_trade_journal.jsonl` — close action at 07:09:43, PnL=0.03
    - 证据: `data/logs/alert_audit.jsonl` — 无 trade_close 条目, 最后一条 XAU 记录是 06:49:44 的 trade_open
  - [Layer 2 — 中间异常]: `dispatch_managed_close()` (managed_close.py) — 所有受管平仓的统一入口 (meta_exit/SL/TP/hesitation/time_decay/brain_flip/drawdown_kill) — 覆盖了重入守卫、ghost-volume 审计、PnL 追踪, 但**从未调用 `notify_trade()`**。
    - 证据: `core/execution/managed_close.py:298-318` — pre-fix 代码包含 `known_open_tickets.pop()`, `_pending_budget_records.append()`, `_pending_sl_records.append()`, 但没有 `notify_trade` 调用
  - [Layer 3 — 根因]: RC-06 contract-violation — FIX-20260608-002 创建了 `_emit_close_notification()` 作为平仓通知的统一 helper, 但仅接线 MIA 路径 (live_cycle.py:3807) 和执行队列 net_out 路径 (live_cycle.py:5186)。`dispatch_managed_close()` (FIX-20260530-071 从 live_cycle.py 通过 Strangler Fig 提取) 早于通知系统 (FIX-20260606-138-Phase3) 的出现, 从未被 retrofitted。本质是"事件总线缺失综合征"——横切关注点 (通知) 通过手动调用耦合到每个退出路径, 而非通过发布/订阅自动覆盖。
    - 证据: `core/runtime/live_cycle.py` git history — `_emit_close_notification` 在 88112bf 中添加, 仅 2 个调用点 (MIA + net_out)。`managed_close.py` 零调用。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` — XAUUSDc LONG close at 07:09:43, ticket=3819448262, PnL=+0.03
  - Source 2: `data/logs/alert_audit.jsonl` — 无对应 trade_close 条目
  - Source 3: `core/execution/managed_close.py` — `dispatch_managed_close()` pre-fix 代码中无 `notify_trade` 调用
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-003

---

## 置信度标记说明

| 标记 | 定义 | 要求 |
|------|------|------|
| `confirmed` | 双源确认 | 至少 2 个独立数据源支撑 |
| `hypothesis` | 单源推断 | 仅 1 个数据源支撑，需补充验证 |
| `speculative` | 纯逻辑推理 | 无数据源支撑，仅逻辑推断 |
| `refuted` | 已证伪 | 后续证据推翻了该环节 |

---

### CCT-20260606-001
- **Docket ID**: DQAF-20260606-002
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: BTC swing 策略 Win Rate = 14.29%，PnL = -$813.49，24h 内 6 次 brain_flip_extreme_100pct 紧急出场，18 次 reentry_persistent_block 告警
    - 证据: Journal (Source 1) 6 条 brain_flip 出场记录 + Alert Audit (Source 2) 44 条 strategy_degradation + 18 条 reentry_block
  - [Layer 2 — 中间异常]: Exit Watchdog 在双脑（V4+V5）投票出现 neutral 平票时，将 `_l2_supporting=[]`（空集）传入 `evaluate_brain_exit()`，导致 flip 计算 `flipped = entry_ids - {}` = 100% 假阳性
    - 证据: live_cycle.py:1424 `_l2_supporting = []` + position_manager.py:716 `flip_ratio = len(flipped)/len(entry_ids)` = 2/2 = 1.0
  - [Layer 3 — 根因]: RC-06（contract-violation）— `_l2_supporting` 的语义在 neutral 分支（`[]` = "组内无一致方向"）与 directional 分支（`brain_ids` = "组内全部 brain"）之间存在契约断裂。`position_manager.py` 将 `[]` 错误解释为"入场 brain 全部消失"
    - 证据: contract_groups.py:385 `brain_ids=brain_ids`（全部brain）vs live_cycle.py:1424 `_l2_supporting = []`（空）
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 6 条 brain_flip_extreme_100pct
  - Source 2 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — strategy_degradation PnL=-813.49 WR=14.29%
  - Source 3 (Source Code): `core/runtime/live_cycle.py:1424`, `core/execution/position_manager.py:713-746`, `core/parliament/contract_groups.py:443-458`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-001
- **关联 FIX**: FIX-20260606-137

### CCT-20260606-002
- **Docket ID**: DQAF-20260606-003
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: **重启后 1 秒即在第一个周期开仓** btc_swing short（system_online 05:21:13 → open 05:21:14, conf=0.6718, p_win=0.47）。18 分钟后 brain_flip 出场，MT5 断连导致 9 次重试全部 REJECTED（retcode 10031），仓位卡死。
    - 证据: Alert Audit `05:21:13 system_online` + Journal `05:21:14 open` + Journal `05:39-05:45 9 次 rejected close`
  - [Layer 2 — 中间异常 — 门禁三重失效]: Cooldown deadline 重启时已过期（03:49:44 vs 05:21:13, >1.5h）→ Cut 1 通过。Family spacing 无冲突 → Cut 2 通过。Reentry guard 理论应拦截（前次出场=brain_flip, exit_conf=0.6889, 阈值=0.7389, 新 conf=0.6718 < 0.7389）但实际未拦截 → Cut 3 失效。
    - 证据: `execution_state.json.bak` cooldown deadline=03:49:44 + `reentry_guard.py:114-151` brain_flip 判定逻辑
  - [Layer 3 — 根因 — RC-08 (fail-open)]: `restart_state.py:107` 的原代码为 `except Exception: return`，将**整个 journal 解析逻辑**包裹在单层 try/except 中。任何非 JSON 解析异常（如 datetime.fromisoformat 失败、MAGIC_TO_STRATEGY 导入失败、ExitRecord 构造异常）均导致函数静默返回，`_reentry_states` 保持空字典。下游 `check_and_record_entry()` 发现 `last_exit = None` → 返回 `"first_entry"` → **所有重入检查被绕过**。这是 Fail-Open 反模式的标准案例：恢复失败时系统不应放行，而应进入保守状态（Fail-Closed）。
    - 证据: `restart_state.py:107` `except Exception: return` (FIX-138 修复前) + `reentry_guard.py:435-442` first_entry 分支

- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 05:21:14 open + 05:39-05:45 rejected close ×9
  - Source 2 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — 05:21:13 system_online + 03:39:46 cooldown block
  - Source 3 (State): `data_btc/state/execution_state.json.bak` — cooldown deadline=03:49:44, exit_reason=brain_flip
  - Source 4 (Source Code): `core/runtime/restart_state.py:107` `except Exception: return` → `reentry_guard.py:435` `if self.last_exit is None: return True, "first_entry", 1.0`
- **是否被推翻**: 否（补充 Layer 3 根因，Layer 1-2 结论不变）
- **关联 ReB Pattern**: ReB-20260606-002 (`bootstrap_silent_fail_to_open`)
- **关联 FIX**: FIX-20260606-138

### CCT-20260606-003
- **Docket ID**: DQAF-20260606-005
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: Alert 系统报告 `rolling_win_rate=2.56%`、`daily_pnl=-$674.75`、`win_rate_collapse` 紧急告警。实际逐仓位去重后真实胜率 41.5%，真实盈亏 +$60.97。告警数据与实盘严重偏离，触发误杀级风控告警。
    - 证据: Alert audit `win_rate_collapse` + Journal 逐笔去重统计 41 笔唯一仓位
  - [Layer 2 — 中间异常 — 消费者聚合无过滤]: `_execute_alert_dispatch` 的 PnL 聚合逻辑（`live_cycle.py:770-800`）对所有 `action=="close"` 的 journal 条目无差别求和，不区分 `ack_status`（accepted/rejected/closed），不按 `position_ticket` 去重。同一仓位的 28 次 REJECTED 重试被计为 28 笔独立亏损。
    - 证据: `live_cycle.py:781` `if _e.get("action") != "close": continue` — 无 ack_status 过滤
  - [Layer 3 — 根因 — RC-10 (ontology-violation) × 消费端幂等性缺失]: Journal 作为 append-only event log 正确记录了每次尝试（包括重试），但消费者（告警聚合器）将 event log 错误地解释为 trade ledger。**Event log ≠ Trade ledger** — 前者记录所有尝试，后者只记录最终结果。这是本体论层面的范畴错误：把"发生了什么"和"结果是什么"混为一谈。
    - 证据: Journal 157 条 close 条目 (event log) vs 41 个唯一仓位 (trade ledger)
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 157 close entries, 108 retry pollution
  - Source 2 (Source Code): `core/runtime/live_cycle.py:770-800` — alert aggregation logic
  - Source 3 (Cross-check): 逐仓位去重脚本 — 41 unique positions, WR=41.5%, PnL=+$60.97
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-003 (`metric_pollution_via_rejected_retries`)
- **关联 FIX**: FIX-20260606-138-Phase0 / FIX-20260606-138-Phase2 / FIX-20260606-138-Phase3

### CCT-20260606-004
- **Docket ID**: DQAF-20260606-006
- **日期**: 2026-06-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 钉钉平仓通知永远显示 "盈亏: N/A"，且同一仓位收到多条重复通知轰炸（28 次/仓位）
  - [Layer 2 — 根因]: RC-06 (contract-violation) — `DispatchResult` 数据契约不包含 `pnl` 字段。`_net_out_close_dispatch_fn` 内部计算了 `_net_pnl` 但未通过返回值向上游传递 → `execution_queue.flush()` 无法在 `DispatchResult` 中携带 PnL → `notify_trade()` 参数链断裂 → pnl 永远为 None
- **证据引用**:
  - Source 1: `execution_queue.py:41-50` — DispatchResult 无 pnl/volume 字段
  - Source 2: `live_cycle.py:4640` (修复前) — notify_trade 调用缺失 pnl= 参数
  - Source 3: `live_alert_hub.py:317-328` — pnl_str 回退到 "N/A"
- **是否被推翻**: 否
- **关联 ReB Pattern**: `missing_pnl_in_trade_notification`
- **关联 FIX**: FIX-20260606-138-Phase3
- **Follow-up**: Phase 3 初版在 `execution_queue.py` 中引用 `_close_result` 时未初始化（变量仅在 close 分支存在），导致开仓路径 `UnboundLocalError`。已通过分支前初始化 + None 检查修复（RC-05 boundary-error）。

### CCT-20260606-005
- **Docket ID**: DQAF-20260606-004
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: 07:00 平仓后 6+ 小时零开仓。每周期产出 SHORT 信号 (conf=0.82)，GM 记录 C1-C3 全部 `should_trade=False`
    - 证据: Golden Master (Source 1) 3 周期全部 blocked + Journal (Source 2) 零 open
  - [Layer 2 — 中间异常 — 双闸门交替拦截]: C1 被 p_win=0.44 < 0.45 拦截；C2-C3 p_win 偶尔通过后被 bleed_stop_price_not_confirming 补位拦截。p_win=0.44 来自 rolling WR（含 9 次假 brain_flip 污染），在 breakeven=0.45 下方 0.01。Fail-Closed 兜底触发线 0.40 太低，留下 0.40-0.45 死锁带
    - 证据: strategy_line.py:1556-1562 Fail-Closed 逻辑 + reentry_guard.py:297-300 bleed_stop 价格确认
  - [Layer 3 — 根因 — RC-05 (boundary-error)]: FIX-137 修复了假 brain_flip，但 9 次 bug 导致的真实亏损已将 rolling WR 压低至 0.44。Fail-Closed 的触发线 0.40 是针对"系统完全盲"场景设计的，未覆盖"系统有数据但受污染"的中间态。死锁机制：p_win 略低于地板 → 不能交易 → 无新数据 → p_win 不更新 → 永久冰封
    - 证据: 去重统计 73 笔唯一仓位，真实 WR=47.7%，SHORT WR=47.5%，均高于 floor=0.45。但 rolling WR（近期窗口）因假 brain_flip 集中亏损被压低至 0.44
- **解决方案评估（三选一）**:
  - 方案一（贝叶斯收缩）: ✅ 长期稳定，但单独无法根治边界死锁
  - 方案二（卡尔曼滤波）: ❌ 问题不在噪音过滤——p_win=0.44 是真实信号
  - 方案三（UCB 弹性地板）: ✅ 精准命中死锁机制——置信度 × 不确定性溢价填平死锁带
- **证据引用**:
  - Source 1 (Golden Master): `data_btc/golden_master.jsonl` — C1-C3 全部 blocked
  - Source 2 (Journal): `data_btc/live_trade_journal.jsonl` — 去重统计 47.7% WR
  - Source 3 (Source Code): `strategy_line.py:1556-1562` Fail-Closed + `reentry_guard.py:297-300` bleed_stop
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-005 (`p_win_statistical_freeze_dead_zone`)
- **关联 FIX**: FIX-20260606-139

### CCT-20260607-007
- **Docket ID**: DQAF-20260607-007
- **日期**: 2026-06-07
- **置信度**: confirmed (双源确认 — journal PnL + golden master trend_direction)
- **因果链**:
  - [Layer 1 — 症状]: BTC 5/31-6/6 43笔交易中40笔为sell。架构师关注趋势衰竭和V型反转风险。
    - 证据: Source 1 (Journal) — 43笔BTC trade, 40 short, WR 44.2%, 盈亏比 2.50, PnL +$102.12
    - 证据: Source 2 (Golden Master) — 50周期全部 trend_direction="short", macro_regime="risk_off"
  - [Layer 2 — 中间异常 — 信号已计算但未消费]: Kalman velocity + Hurst 由 TrendDetector 每周期 O(1) 计算，RegimeGate.classify() 返回 dict 已包含 m5_hurst 和 h1_ema_slope，但从未接入仓位缩放或出口决策。trend/swing 策略在趋势成熟时依然等额开仓，无自适应的仓位调节机制。
    - 证据: strategy_line.py:1748 volume *= _ct_vol_mult — 仅 counter-trend 罚则，无趋势成熟折扣
    - 证据: position_manager.py:700-754 — 出口仅 consensus flip + brain flip + confidence decay，无 Kalman 一阶导信号
  - [Layer 3 — 根因 — RC-12 (missing-feature)]: 信号源→消费端的接线缺失。TrendDetector 和 RegimeGate 体系已完备，但 strategy_line 和 position_manager 的 evaluate 入口从未消费 Hurst/Kalman velocity 信号。纯架构债——不需要新信号，只需要接线。
    - 证据: strategy_line.py:510 evaluate() 签名缺少 hurst/kalman_velocity_bps 参数
    - 证据: live_cycle.py:3862-3865 仅提取 trend_direction/trend_strength/h4_trend_strength/macro_regime
- **解决方案**: 三步纯增量接线:
  - Step 1 (双因子入口折扣): `trend_maturity_discount(hurst, trend_strength, strategy_family)`:
    - 因子 A — **Hurst 持续性衰减**: H=0.60→1.00x, H=0.55→0.85x, H=0.50→0.55x, H≤0.45→0.40x (floor)。度量趋势结构是否仍在（分形市场假说）。
    - 因子 B — **Kalman 速度确信度衰减**: `trend_strength` = h1_trend_strength，来自 `KalmanTrendFilter.strength` = `sigmoid(|v|/σ)`。当 trend_strength < 0.5 时，比例折扣 `strength/0.5`。度量 Kalman 对当前趋势速度的确信程度——速度相对于不确定性的 SNR 下降时自动收缩仓位。
    - 双因子乘性叠加，floor=0.40。仅 trend_following/swing 策略族生效，statarb/mean_reversion 豁免（已有独立 sizing）。
    - **已知 Phase 2 缺口**: 当前实现使用 `trend_strength`（速度×信噪比复合分），而非纯速度比率 `|v|/EMA(|v|)`。后者能更早检测到"速度自身的历史性衰减"（加速度丧失），是更干净的领先指标。当前方案保守——仅在信噪比恶化时折扣，不会因短期速度波动误触发。EMA velocity ratio 作为 Phase 2 升级路径，需要先积累 30-50 周期的 velocity EMA 样本。
  - Step 2 (Kalman 速度翻转快速出口): `evaluate_brain_exit()` 第0层检查 — long仓位且 v < -3bps 或 short仓位且 v > +3bps → 立即退出。阈值过滤 M5 噪声。充当 PID 出口控制器的微分(D)项——在价格触及 trail stop **之前**根据动量方向提前撤退。
  - Step 3 (数据接线): live_cycle.py 从 regime_gate_result 提取 m5_hurst + h1_ema_slope → 经 _evaluate_strategy_lines → strategy.evaluate()。h1_ema_slope(h1 速度) 存入 LiveCycleState._last_kalman_velocity_bps 供下一周期的 exit management 使用（落后一个周期，对趋势级别变化可接受）。
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 完整 PnL 统计
  - Source 2 (Golden Master): `data_btc/golden_master.jsonl` — 50 周期 trend_direction + confidence
  - Source 3 (Source Code): 6个文件的完整追踪
- **是否被推翻**: 否 — AR假设(长方向偏见)被 journal 中 3笔 long 的开仓记录推翻
- **关联 ReB Pattern**: ReB-20260607-007 (`signal_wiring_unconsumed_computed_output`)
- **关联 FIX**: FIX-20260607-143

---

### CCT-20260607-006
- **Docket ID**: DQAF-20260607-006
- **日期**: 2026-06-07
- **置信度**: confirmed (3 源确认)
- **因果链**:
  - [Layer 1 — 症状]: Ticket=3807506009 开仓后在 80 分钟内被拒绝 75 次平仓请求，journal 中产生 76 条 close 记录。同时 position_snapshots 显示 bars 3-16（13根K线/65分钟）的所有字段值完全相同（unrealized_pnl_r=-1.29, trailing_sl_distance=1269.17, current_atr=385.58），数据管道完全冻结。
    - 证据: `data_btc/live_trade_journal.jsonl` — ticket 3807506009 的 76 条 close 记录（ack=rejected × 75, ack=closed × 1）
    - 证据: `data_btc/position_snapshots.jsonl` — ticket 3807506009 的 18 条快照，bar 3-16 所有字段值完全一致
  - [Layer 2 — 中间异常]: `_mid_and_prices()` 持续从 MT5 获取价格数据，但 MT5 返回的是**相同的过期 tick**（`tick.time` 不推进）。该函数仅检查价格有效性（NaN/Inf/零值/越界/点差），无 staleness 检测。`live_cycle.py` 主循环用过期价格计算特征→开仓→管理→平仓，形成完整的"瞎子指挥"链条。同时 ExitWatchdog 在每个管理周期被重新触发（跨周期雪崩），每个 batch 5 次重试全部被 MT5 拒绝（deviation 超限——价格已偏离订单价格 $500+）。
    - 证据: `core/runtime/market_ingress.py:77-120` — `_mid_and_prices()` 返回 `(mid, bid, ask)` 无时间戳
    - 证据: `core/runtime/live_cycle.py:1542-1574` — bleed_stop 每周期触发 `_dispatch_managed_close` 但不检查之前是否已派发
    - 证据: `core/execution/exit_watchdog.py:43-49` — MAX_RETRIES=5, MAX_TOTAL_DURATION=30s，但外部管理循环不断重启新 batch
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 反模式) + RC-09 (数据新鲜度契约缺失)**。MT5 Bridge 在断连/数据停滞时返回旧 tick 而非抛出异常，上层无 staleness 检测机制，系统将过期数据当作实时数据处理。同时 exit dispatch 路径缺少 pending_close 状态锁，导致 watchdog batch 被管理循环反复重新触发，形成 75 次拒绝的雪崩。
    - 证据: `core/runtime/market_ingress.py` — tick.time 字段存在但从未被提取和传播
    - 证据: `core/execution/position_manager.py` — 修复前无 `_pending_close` 锁机制
- **修复** (FIX-20260613-052: resolved placeholder):
  - (a) `_mid_and_prices()` 返回值扩展为 `(mid, bid, ask, tick_time)`
  - (b) `live_cycle.py` 主循环头部增加 staleness 检测：`data_age > 120s` → 跳过本周期；连续 3 次触发 `_circuit_breaker_tripped`
  - (c) `_dispatch_managed_close()` 增加价格年龄守卫：`tick_age > 60s` → 拒绝派发
  - (d) `ActivePositionManager` 增加 `_pending_close` 锁：同一 ticket 在 3 周期内不允许重复派发平仓
  - (e) `trail_activation_atr` 从 1.0 降为 0.3（BTC 配置）
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — ticket 3807506009 完整生命周期
  - Source 2 (Snapshots): `data_btc/position_snapshots.jsonl` — 13 根 bar 的数据冻结证据
  - Source 3 (Source Code): `market_ingress.py` + `live_cycle.py` + `position_manager.py` + `exit_watchdog.py` — 4 文件完整追踪
  - Source 4 (Audit Script): `scripts/analyze_live_journal.py` — Trail SL 3.465x ATR + 83% 仓位 SL 从未收紧
- **是否被推翻**: 否 — AR 假设 (Trail 乘数计算 bug) 被代码审计推翻：乘数正确，问题在于激活水印 + staleness 导致的"Trail 从未启动"
- **关联 ReB Pattern**: ReB-20260607-008 (`stale_data_fail_open_blind_trading`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260607-007
- **Docket ID**: DQAF-20260607-007
- **日期**: 2026-06-07
- **置信度**: confirmed (双源确认)
- **因果链**:
  - [Layer 1 — 症状]: 钉钉告警 `策略性能下降` 中显示 `策略盈亏(USD): -2105.05` 和 `策略胜率: 0.1429`，用户反馈数值不准确。实际 `当日盈亏(USD): 2.96` 与 `策略盈亏(USD): -2105.05` 差距 700 倍，引起困惑。
    - 证据: 钉钉消息截图 + `alert_audit.jsonl` — strategy_degradation 告警
  - [Layer 2 — 中间异常]: 两个独立问题叠加：(a) **标签错位**: `alert_channels.py:160` 将 `strategy_pnl` 映射为 `策略盈亏(USD)`，但 `brain_pnl_ledger.py:53` 中 `cumulative_pnl` 的注释明确写的是 `total P&L per unit`（每单位 R-multiple），不是 USD；(b) **缝合怪指标**: `live_cycle.py:886-888` 对 PnL 和 WinRate 独立取 `min()`，导致 `_worst_pnl` 来自 BTC_Swing_V4（-2105R），`_worst_wr` 来自 BTC_Swing_LGB_V1（0.1429）。告警描述的 "策略" 在物理世界中不存在——是两个不同大脑的碎片拼接。
    - 证据: `live_cycle.py:886-888` — `_worst_pnl = min(...)` 和 `_worst_wr = min(...)` 是独立循环
    - 证据: `brain_pnl_ledger.py:53` — `cumulative_pnl: float = 0.0  # total P&L per unit`
    - 证据: `alert_channels.py:160` — `"strategy_pnl": "策略盈亏(USD)"`
  - [Layer 3 — 根因]: **RC-08 (语义契约断裂)** — 数据生产者（BrainPnLStore）的 `cumulative_pnl` 明确标注为 per-unit R-multiple，但消费者（告警标签）将其错误解释为 USD。同时 "最差策略" 的构建使用了两个独立 `min()` 而非选择单一最差大脑，产生了一个无物理对应物的虚假指标。
    - 证据: `live_cycle.py:878-890` 修复前代码 vs 修复后代码
- **修复** (FIX-20260613-052: resolved placeholder):
  - (a) `live_cycle.py:886-888`: 独立 `min()` → `min(_all_m.values(), key=lambda m: m.cumulative_pnl)` 选择单一最差大脑，PnL 和 WR 同源
  - (b) `alert_channels.py:160-161`: `策略盈亏(USD)` → `最差大脑累计PnL(R)`, `策略胜率` → `最差大脑胜率`, 新增 `最差大脑ID`
  - (c) 新增 `_ctx["worst_brain_id"]` 使告警可溯源到具体大脑
- **证据引用**:
  - Source 1 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — strategy_degradation 告警上下文
  - Source 2 (Governance State): `data_btc/governance_state.json` — BTC_Swing_V4 pnl_r=-2171.86 vs 告警值 -2105.05
  - Source 3 (Source Code): `live_cycle.py:878-890` + `brain_pnl_ledger.py:53` + `alert_channels.py:160`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-009 (`frankenstein_metric_independent_min`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260607-008
- **Docket ID**: DQAF-20260607-008
- **日期**: 2026-06-07
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: Phase A 焊死了价格 staleness 检测，但特征存储、Bridge 心跳、周期停顿三个组件仍处于 Fail-Open 状态——检测存在但仅发告警/记录，不阻断交易。
  - [Layer 2 — 中间异常]: 三个防线的"检测→告警"链路完整，但"告警→熔断"链路缺失。特征冻结时系统继续用过期特征推理；Bridge 断连时继续用旧价格评估。
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 残余)** — 告警 ≠ 熔断 的模式在三个子系统中重复出现。
- **修复** (FIX-20260613-052: resolved placeholder Phase B):
  - (a) B1: `feature_stale_warning` → `_consecutive_stale_features`，连续 3 次 → 熔断
  - (b) B2: `_bridge_silence > 300s` → 立即熔断（无需等 3 周期）
  - (c) B3: `cycle_duration > 180s` → `_consecutive_degraded_cycles++`，连续 3 次 → 熔断
  - (d) Config 新增 `max_bridge_silence_seconds=300.0` + `cycle_stall_threshold_seconds=180.0`
- **证据引用**:
  - Source 1: `live_cycle.py:3817-3829` (修复前 feature_stale 仅 print)
  - Source 2: `live_cycle.py:777-799` (bridge_last_ack 仅用于告警上下文)
  - Source 3: `live_cycle.py:2519` (_last_cycle_start_time 已采集但未用于 stall 检测)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-008 (`stale_data_fail_open_blind_trading`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260608-003: 断路器碎片化 trip 路径死亡螺旋 (DQAF-20260608-003)

- **发现日期**: 2026-06-08
- **严重等级**: Sev 2 — 交易阻断
- **因果链**:
  - [Layer 1 — 症状] (confirmed): 断路器反复触发，系统频繁重启 (May 31: 110次)，trade_decisions=0
    - Source 1: `data_btc/logs/alert_audit.jsonl` — 123 daily_loss + 57 strategy_degradation
    - Source 2: `data_btc/state/execution_state.json` — consecutive_degraded=0 but circuit_breaker_tripped=true (矛盾)
    - Source 3: `data_btc/golden_master.jsonl` — 仅3个cycle, 全部 trade_decisions=0
  - [Layer 2 — 中间异常] (confirmed): Auto-reset (live_cycle.py L2815) 仅重置 `_consecutive_degraded_cycles=0`，未重置 `_consecutive_stale_cycles` 和 `_consecutive_stale_features`
    - Source 4: `live_cycle.py` L2817 vs L3313 — reset 只清 degraded 不清 stale
    - 后果: breaker 由 data_staleness 触发后，auto-reset 后 stale counter >= 3 仍存活，同一 cycle 内 L3296 重新 trip
  - [Layer 3 — 根因] (confirmed): 断路器架构碎片化 — 6条 trip 路径使用 3种独立计数器，auto-reset 未覆盖全部
    - Source 5: `live_cycle.py` 5条 trip 路径仅 bridge+stall+wakeup 共用 `_consecutive_degraded_cycles`
    - Source 6: `execution_state.py` save 未持久化 stale counters → 重启后 breaker=True 但计数器丢失 → "幽灵 breaker"
    - Source 7: FIX_REGISTRY — 6+次独立断路器修复均未根除
- **修复** (FIX-20260608-009):
  - (a) 新增 `_circuit_breaker_trip_reason` 字段 — 所有 5 条 trip 路径记录触发原因
  - (b) Auto-reset 统一清除全部 3 种计数器 (degraded + stale_cycles + stale_features)
  - (c) `save/restore_execution_state` 补齐全部计数器 + trip_reason 持久化
- **是否被推翻**: 否 — AR 反证确认：多次修复均为单路径打补丁
- **关联 ReB Pattern**: ReB-20260608-003 (`FRAGMENTED_BREAKER_TRIP_PATHS_WITH_STALE_COUNTER_LEAK`)
- **关联 FIX**: FIX-20260608-009

---

## CCT-20260609-001: BTC Hesitation Permanent Deadlock

- **Docket ID**: DQAF-20260609-001
- **Severity**: Sev 2
- **Date**: 2026-06-09
- **Causal Chain**:
  - **Layer 1 — 症状**: BTC btc_swing 自 2026-06-08 01:02 UTC 起零开仓。148 次连续信号评价（confidence 0.746-0.750, p_win 0.45-0.48, regime=full trending, 3/4 brains 支持 LONG）全部被 `reentry_blocked` 拦截，reason=`hesitation_confidence_not_improved`。
  - **Layer 2 — 中间异常**: 最后一笔 hesitation 退出（ticket=3808448708）通过 bootstrap 重放时 `exit_reason` 被跨记录借用到最新 close（ticket=3810297338），形成 `exit_reason="exit_watchdog:hesitation_15c_no_breakeven"` + `exit_confidence=0.7668` 的组合。`check_reentry_quality()` 的 hesitation 路径计算阈值 `max(0.7668+0.15, 0.70)=0.9168` — 此值超过 BTC 树模型 P99 输出 (~0.685) 和绝对最大值 (~0.77)，**数学上不可达**。
  - **Layer 3 — 根因 (RC-05 + RC-12)**: `reentry_guard.py` 的 `hesitation` 类别是唯一同时缺少两项保护的退出类别：(a) `_MAX_THRESHOLD=0.82` 天花板 — FIX-117 已施加于 brain_flip/sl_hit/ou_revert/unknown_close 但遗漏了 hesitation；(b) TTL 硬解锁 — FIX-127 已施加于 brain_flip+meta_exit，FIX-011 已施加于 sl_hit，但均遗漏了 hesitation。唯一的逃生通道是 24h stale exit override。
- **是否被推翻**: 否 — AR 反证确认：BTC 信号质量正常（confidence>0.74, p_win>0.45），hesitation 后 confidence 从 0.5 提升到 0.75（+50%），实为合理重入时机。死锁非市场质量导致，纯为代码边界条件缺陷。
- **关联 ReB Pattern**: ReB-20260609-001 (`HESITATION_PERMANENT_DEADLOCK`)
- **关联 FIX**: FIX-20260609-001

---

### CCT-20260609-001-B: Breakeven Floor Trail Deadlock (DQAF-20260609-001 sub-finding)

- **Docket ID**: DQAF-20260609-001
- **发现日期**: 2026-06-09
- **严重等级**: Sev 2 — 出场质量退化，保本后利润保护失效
- **因果链**:
  - **Layer 1 — 症状** (confirmed): BTC trade 3809501680 bar 16-23 共 8 根 bar，`trail_sl_candidate: null`，SL 锁死 62924。只有 TP 单向收紧。入场后价格继续涨了 +$306，仓位只能通过 TP 被命中出场，而非通过 trailing SL 逐步锁利。
    - Source 1: `management_phase_diag` 日志 — bar 16-23 全部 `trail_sl_candidate: null, trail_fired: false`
    - Source 2: `live_trade_journal.jsonl` — SL 从 bar 15 dispatch 后始终 62923.98
  - **Layer 2 — 中间异常** (confirmed): `trail_stop_engine.py:158` — `max(candidate, entry_price)` + `candidate <= current_sl + min_step` 形成双重锁定。`highest_high` 停滞在 63313，但 Chandelier 需要 `trail_mult * ATR` 的利润缓冲才能突破保本地板。trail_mult=2.5 + ATR≈185 → 需要 ~460 pts 利润。最高只到 389 pts。candidate 被地板抬到 62951, 但 62951 ≤ 62924 + 0.15 → return None。
    - Source 3: `trail_stop_engine.py` L161-173 — 完整的循环死锁代码路径
  - **Layer 3 — 根因 (RC-05)** (confirmed): trail_mult 是**静态常量** — 从入场到出场永远不变（regime-given 2.5）。没有随着利润积累而收紧的机制。保本前的大 multiplier 是为了防止水下仓位被过早止损——这是正确的。但保本后仓位已经安全，multiplier 应该变小以允许 trail 逐步锁利。静态 multiplier 无法区分"水下求生"和"水上锁利"两个阶段。
    - Source 4: `trail_stop_engine.py` L158 (旧) — `effective_mult = max(tp.min_trail_mult, pos.trail_multiplier)` — trail_multiplier 只被 regime gate 调整，永不考虑利润
- **修复** (FIX-20260609-003): 新增 `_compute_decayed_mult()` — trail_mult 随 R-max 从 base 平滑衰减到 min_trail_mult(R: 0.5→2.0)。`TrailPolicy` 新增 `decay_start_r, decay_full_r, decay_enabled`。
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-001-B (`BREAKEVEN_FLOOR_TRAIL_DEADLOCK`)
- **关联 FIX**: FIX-20260609-003

---

### CCT-20260609-001b
- **Docket ID**: DQAF-20260609-001
- **日期**: 2026-06-09
- **置信度**: confirmed (双源交叉验证 — 同事审计 + Agent 审计)
- **因果链**:
  - [Layer 1 — 症状 A]: `execution_state.json` 显示 `total_trades_today: 0`, `consecutive_losses: 0`，但 alert_audit 记录至少有 4 笔已关仓交易、3 笔亏损。Daily Loss Limit (-$30) 未触发。
  - [Layer 2 — 中间异常 A]: `live_cycle.py:4044` `_build_strategy_lines()` 每个 cycle 创建全新的 `StrategyBudget` 对象（计数器=0）。`live_cycle.py:4433` `restore_execution_state()` 仅在 `loop_iteration == 1` 时恢复。Cycle 2+ budget 恒为零 → 所有累计风控闸门（daily_loss_limit, max_consecutive_losses, intraday_dd）永久失效。
  - [Layer 3 — 根因 A]: RC-03 (state-leak) — `_build_strategy_lines()` 每 cycle 重建策略对象是 FIX-20260530-070 (Strangler Fig #5) 的架构残余。原设计中策略对象在循环外创建一次，提取后移入循环内但未配套恢复逻辑。
  - [Layer 1 — 症状 B]: alert_audit 显示 `hesitation_confidence_not_improved_0.746_need_0.820` 连续 150 cycles (6/8-6/9)。BTC btc_swing 重入被永久封锁。
  - [Layer 2 — 中间异常 B]: FIX-001 部署了 `_MAX_THRESHOLD=0.82` 天花板 + 2h TTL，但 `reentry_guard.py:298` 的 `exit_confidence + 0.15` 边际加法在 floor 0.70 约束下仍产生 0.82 阈值。BTC 树模型 (LightGBM/XGBoost) P99 输出 ≈ 0.685-0.75，无法达到 0.82。
  - [Layer 3 — 根因 B]: RC-05 (boundary-error) — threshold calibration 未根据目标模型的输出分布校准。+0.15 边际对 BTC tree-based 模型过大（对比 brain_flip +0.05, BTC P99≈0.685）。
- **证据引用**:
  - Source 1 (A): `data_btc/state/execution_state.json` — `total_trades_today: 0, consecutive_losses: 0` (2026-06-09 09:59 UTC)
  - Source 2 (A): `data_btc/logs/alert_audit.jsonl` — 6/9 trade_notification close events: PnL=-1.74, -1.36, -13.93, -14.01
  - Source 3 (A): `core/runtime/live_cycle.py:4044-4054` + `core/runtime/live_cycle.py:4433` — 每 cycle 重建 + 仅 cycle 1 恢复
  - Source 1 (B): `data_btc/logs/alert_audit.jsonl` — reentry_persistent_block: 150 cycles (6/8 23:29-6/9 00:44)
  - Source 2 (B): `core/execution/reentry_guard.py:298` — `min(max(exit_confidence + 0.15, 0.70), _MAX_THRESHOLD)`
  - Source 3 (B): BTC brain performance data (governance_state.json) — 4 brains all candidate, P99 confidence 0.685-0.75
- **修复** (FIX-20260609-010):
  - Sub-fix A: `live_cycle.py` 新增 per-cycle budget 恢复块 — `load_execution_state()` → `budget.load_state()` 在 `_build_strategy_lines()` 之后、pending records 之前执行
  - Sub-fix B: `reentry_guard.py:298` — margin 0.15→0.08, floor 0.70→0.65. 排序: brain_flip+0.05 < hesitation+0.08 < sl_hit+0.10
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-001-B (`Cap-Output Mismatch Deadlock`) + `Budget Reconstruction Amnesia`
- **关联 FIX**: FIX-20260609-001, FIX-20260609-010

---

### CCT-20260609-011
- **Docket ID**: DQAF-20260609-011
- **日期**: 2026-06-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC 4 个大脑全部 `candidate` 状态，0 个 `live`。系统以 0.02-0.1 lot 正常开单，今日 PnL ≈ -$30，胜率 ~31% (4关3亏)。候选大脑 profit_factor=0.72, sharpe=-30, 从未证明盈利能力。
  - [Layer 2 — 中间异常 A]: `live_startup.py:178-193` 治理过滤仅移除 `retired`/`frozen`，`candidate` 获全票权通过。更严重的是逻辑倒挂：`probation`（曾 live 后退化）被罚 vote_weight×0.5，而 `candidate`（从未证明）无任何限制。整个开单链路（strategy_evaluator, strategy_line, signal_pipeline）无 governance status check。
  - [Layer 2 — 中间异常 B]: 大脑绩效极差（profit_factor < 1.0, sharpe < -29），但治理服务（daily_ops）无法将任何大脑晋升为 `live`。系统陷入"全 candidate 死循环"——没有 live 大脑 → 开单亏损 → 绩效恶化 → 更不可能晋升 → 永远 candidate。
  - [Layer 3 — 根因]: RC-07 (missing-validation) × RC-09 (config-drift) — (A) 大脑治理状态从未作为开单前置条件，governance_state.json 在整个 live 交易链路中是"只读不用的死数据"；(B) candidate 的 vote_weight 设计意图应是 ≤ probation，但代码实现相反。
- **证据引用**:
  - Source 1: `data_btc/governance_state.json` — 4 brains all `candidate`, 0 `live`
  - Source 2: `data_btc/logs/alert_audit.jsonl` — 6/9 trade_notification: 4 closes, 3 losses, PnL ≈ -$30
  - Source 3: `core/runtime/live_startup.py:178-193` — candidate falls through to "kept" without penalty
  - Source 4: `core/runtime/strategy_evaluator.py` — zero governance status checks in entire evaluation chain
- **修复** (FIX-20260609-011):
  - (1) `live_startup.py`: candidate 加 vote_weight×0.5 惩罚
  - (2) `live_cycle.py`: 每 cycle 读取 governance_state.json → 传入 strategy_evaluator
  - (3) `strategy_evaluator.py`: Cut 4 — 无 live 大脑时 confidence<0.50→blocked, volume→0.01
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-011 (`GOVERNANCE_VACUUM_CADET_BRAINS`)
- **关联 FIX**: FIX-20260609-011

---

### CCT-20260609-012
- **Docket ID**: DQAF-20260609-012
- **日期**: 2026-06-09
- **置信度**: confirmed (60次数据集构建 + Walk-Forward CV)
- **因果链**:
  - [Layer 1 — 症状]: BTC_Swing_V5 test_PF=1.81 → live_PF=0.73 (训练/实盘鸿沟)。V5 训练准确率 33.96% vs 随机基线 33.3%——模型对方向几乎无预测能力。V6-V8 训练指标全部缺失（盲盒大脑）。
  - [Layer 2 — 中间异常 A]: 归一化器为 XAU 复制品（`_note`: "BTC-specific normalization not yet calibrated. DO NOT set normalize=true"），但 normalize=false 正确禁用了归一化。真正的问题是 V5 仅训练了 19 天数据（5,407 样本），且训练标签不含摩擦。
  - [Layer 2 — 中间异常 B]: 跨 4 个时间框架 × 15 组 SL/TP 网格搜索——所有 R:R ≥ 1.0 的组合 EV 为负。BTC 价格行为规律：在任何 N 小时窗口内，价格移动 X ATR 的概率 >> 移动 2X ATR 的概率 → 宽 TP 打不到、紧 SL 先被扫。
  - [Layer 3 — 根因]: RC-12 (missing-feature) × RC-05 (boundary-error) — (A) 旧大脑使用不匹配的训练数据（XAU 特征集 / 过短训练期 / 无摩擦标签）；(B) BTC 市场结构不支持传统高盈亏比 Alpha，需要"宽止损 + 高胜率"的生存策略。
- **证据引用**:
  - Source 1: `configs/brains_btc/v9_institutional_01.normalization.json` — XAU copy, normalize=false
  - Source 2: `configs/brains_btc/BTC_Swing_V5.json` — test_accuracy=33.96%, test_PF=1.81
  - Source 3: 60 次数据集构建结果（M5/M15/M30/H1 × 15 SL/TP combos）— 全部高 R:R 组合 EV 为负
  - Source 4: M15 SL=3.0/TP=2.0 — WR=92.1%, EV=+0.456R (Walk-Forward CV 验证)
- **修复** (FIX-20260609-012):
  - B1: 特征管道审计 → 归一化正确禁用，特征维度不匹配已识别
  - B2: 构建 963 行训练管线（时间衰减权重 + Walk-Forward Purged CV + 真实摩擦）
  - B3: V9 H1 (SL=3.0/TP=2.0, WR=90.0%, EV=+0.38R) + V10 M15 (SL=3.0/TP=2.0, WR=92.2%, EV=+0.46R) shadow 注册
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-012 (`BTC_SURVIVAL_ALPHA`)
- **关联 FIX**: FIX-20260609-012

---

### CCT-20260610-001
- **Docket ID**: DQAF-20260610-001
- **日期**: 2026-06-10
- **置信度**: confirmed (双源交叉验证: journal + snapshots)
- **因果链**:
  - [Layer 1 — 症状]: 移动止损修改后(AFTER) 16笔平仓 0%胜率 -$29.79 → 表面似trail修改导致退化
  - [Layer 2 — 中间异常 A — 数据证伪]: 亏损全部来自修改前开仓的3笔旧仓位(#3838975389/#3840851050/#3843860976, trail完全卡死 ∆=0), 修改后2笔有trail的仓位(#3853350396 ∆=+768pts, #3854799088 ∆=+74pts)均保本出场. 11/16笔为close_accepted/breakeven且PnL=None(MIA清理).
  - [Layer 2 — 中间异常 B — 微生命周期]: 修改后13笔新开仓平均持仓21分钟(4根M5 bar), 全部long方向. 逆势摸底(V9/V10 brain信号) + 宏观SHORT趋势 + trail_activation_atr 0.3-0.5激进防守 → 反弹短暂触及trail激活→衰竭被扫→保本微亏快速出场
  - [Layer 2 — 中间异常 C — 遥测盲区]: 'trail' exit label 从未在188笔闭仓中出现. 69% AFTER平仓(11/16)无PnL记录. trail行为变化只能间接通过modify_sltp和snapshot推测
  - [Layer 3 — 根因]: (A) 保本地板死锁(static trail_mult) → FIX-003衰减曲线已解除; (B) 逆势交易中激进防守的必然微生命周期→非bug,防御机制正常工作; (C) MIA管道PnL缺失→状态机同步泄漏, close_accepted/breakeven标签不记录PnL
- **证据引用**:
  - Source 1: `scripts/analyze_trail_impact.py` stdout — 21 BEFORE vs 2 AFTER SL迁移对比
  - Source 2: `data_btc/live_trade_journal.jsonl` — 385条记录, 'trail'标签count=0, close_accepted/breakeven PnL=None
  - Source 3: `data_btc/position_snapshots.jsonl` — 426条快照, SL迁移中位数BEFORE=0, AFTER=+420.9
- **是否被推翻**: 否 (AR验证通过 — 0%胜率被证伪为遥测污染而非trail退化)
- **关联 ReB Pattern**: ReB-20260610-001 (`TRAIL_TELEMETRY_BLINDSPOT`), ReB-20260610-002 (`MICRO_LIFESPAN_COUNTER_TREND`)
- **关联 FIX**: — (诊断报告, 无代码修改; IC Mandate转入MIA管道修复)

---

### CCT-20260610-002
- **Docket ID**: DQAF-20260610-002
- **日期**: 2026-06-10
- **置信度**: confirmed (code audit × git history bisect × config validation × 31 pattern tests)
- **因果链**:
  - [Layer 1 — 症状 A]: V9_H1_Survival/V10_M15_Survival training SL=3.0/TP=2.0 与 btc_swing 策略线 SL=2.0/TP=2.5 不一致
  - [Layer 2 — 中间层 A]: 非 bug——FIX-20260609-012 网格搜索确认 BTC 不支持 R:R≥1.0，特意训练生存模式(SL>TP, 90%+ WR)并注册为 shadow。但缺少 label_contract 声明其不同契约
  - [Layer 3 — 根因 A]: 训练管线未自动生成非对齐大脑的 label_contract。V6/V7/V8 有 `aligned_with: live_btc.yaml`，但 V9/V10 与任何现有策略线都不对齐——需要 `aligned_with: null` + `requires_dedicated_strategy_line: true`
  - [Layer 1 — 症状 B]: BTC_Swing_V5(retired)残留在 XAU live.yaml enabled=true
  - [Layer 2 — 中间层 B]: FIX-001 退役 V5 仅更新 BTC 配置和脑 JSON，遗漏 XAU 配置——无跨配置扫描
  - [Layer 3 — 根因 B]: 无跨配置文件一致性检查。退役操作是"点修复"模式，依赖人工同步
  - [Layer 1 — 症状 C]: 10+ 种出场原因被归为 "unknown" → 标签污染
  - [Layer 2 — 中间层 C]: `_classify_exit_reason()` 手工维护，新出场逻辑未同步更新分类规则
  - [Layer 3 — 根因 C]: 缺少"新出场原因必须注册"的强制机制
- **证据引用**:
  - Source 1: `configs/brains_btc/BTC_Swing_V9_H1_Survival.json:20-23` — training SL=3.0/TP=2.0
  - Source 2: `configs/live_btc.yaml:58-59` — strategy line SL=2.0/TP=2.5
  - Source 3: `git log bb5b386 -p -- configs/live.yaml` — V5 added to XAU Jun 6
  - Source 4: `git show 1f59e29` — V5 retired in BTC only, missed XAU config
  - Source 5: `core/execution/reentry_guard.py:21-50` — only 12 patterns before fix
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260610-003 (`CONFIG_SYMMETRY_DRIFT`)
- **关联 FIX**: FIX-20260610-008

### CCT-20260612-004
- **Docket ID**: DQAF-20260612-004
- **日期**: 2026-06-12
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `resolve_p_win_from_brains()` 三条 fallback 路径全部静默返回 0.40 (`pwin_chain.py:53/62/74`)
  - [Layer 2 — 中间异常]: 调用方 `strategy_line.py:1209-1215` confidence override 掩盖了 fallback，journal 中 `p_win=0.40` 占比 0%——降级不可观测
  - [Layer 3 — 根因]: RC-06 (contract-violation): 函数接口只返回 float 不返回质量标记。FIX-20260526-031 引入 fail-closed 0.40 时只改了值未加可观测性
- **证据引用**:
  - Source 1: `core/execution/pwin_chain.py:53` — `pnl_store is None → return 0.40` 无日志
  - Source 2: `core/execution/pwin_chain.py:63` — `except Exception: pass  # noqa: BLE001` 吞一切
  - Source 3: `data_btc/live_trade_journal.jsonl` — 98 opens, p_win=0.40 count=0
  - Source 4: `data/live_trade_journal.jsonl` — 816 opens, p_win=0.40 count=0
- **是否被推翻**: 否 — AR 反向假设（不需要改，下游有安全网）被推翻：BLE001 吞一切异常是真实风险
- **关联 ReB Pattern**: ReB-20260612-001 (`SILENT_FALLBACK_ZERO_OBSERVABILITY`)
- **关联 FIX**: FIX-20260612-001

### CCT-20260612-001
- **Docket ID**: DQAF-20260612-001
- **日期**: 2026-06-12
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: data_health_overall=CRITICAL. trade_journal FAIL (PnL null 17.6%), journal_completeness FAIL (close_price 38%, dupes 89, trail 6.9%), conformal_calibrator FAIL (0 computations). 位置脆弱性 5× `.values()` 站点.
  - [Layer 2 — 中间异常]: (a) 幽灵平仓洪水: ticket 3807506009 76次平仓/80分钟, 75 rejected. (b) PnL回填断裂: bridge worker 用 mid-price 估算 PnL, receipt 不更新实际成交价. (c) MIA PnL 捕获: 单次 history_deals_get 无重试 → 23% 失败率 (10/43). (d) Trail 标签盲点: reconciliation close_reason=4 一律 sl_hit_first, 不管 trail_advances. (e) calibrator cold_started 永不 False.
  - [Layer 3 — 根因]: RC-06 (contract-violation): close-in-flight 去重缺失 + dict.values() 顺序依赖 + PnL 写入早于 fill 确认 + label taxonomy 缺失 trail 维度 + cold_started 无过渡逻辑
- **证据引用**:
  - Source 1: `data_btc/state/data_health_state.json` — overall=CRITICAL, 3 fail + 8 warn
  - Source 2: `data_btc/live_trade_journal.jsonl` — 589 lines, 36/246 PnL null, 89 dupes, 0 trail label
  - Source 3: `core/execution/position_manager.py:348` — PENDING_CLOSE_MAX_CYCLES=3 太短
  - Source 4: `scripts/mt5_bridge_worker.py:665` — pnl=msg_payload.get("pnl") 估算值, 永不更新
  - Source 5: `core/runtime/reconciliation.py:132-133` — close_reason=4 → sl_hit_first 无条件
  - Source 6: `core/execution/conformal_calibrator.py:306` — cold_started 写入后永不改为 False
- **是否被推翻**: 否 — AR 反向假设 (CRITICAL 是误报) 被推翻
- **关联 ReB Pattern**: ReB-20260612-002 (`PHANTOM_CLOSE_FLOOD`), ReB-20260612-003 (`TRAIL_LABEL_BLINDSPOT`), ReB-20260612-004 (`PNL_BACKFILL_GAP`), ReB-20260612-005 (`CALIBRATOR_COLD_STALLED`), ReB-20260612-006 (`POSITIONAL_FRAGILITY`)
- **关联 FIX**: FIX-20260612-002, FIX-003, FIX-004, FIX-005

## DQAF-20260612-002: no_live_brains 全交易阻塞

- **Label**: TRIPLE_BOOKKEEPING_RESIDUAL
- **Docket**: DQAF-20260612-002 (Sev 1)
- **Causal Chain (3 Layers)**:
  - **Layer 1 (症状)**: Golden Master 所有 12 周期标记 [degraded: no_live_brains], should_trade=True 但 decisions=0, 交易降级至 0.01 vol
  - **Layer 2 (中间异常)**: strategy_evaluator.py Cut 4 计算出 _live_count=0. strategy.brains 含 7 个 brain, 无一在 governance_state 中 status=live
  - **Layer 3 (根因)**: FIX-20260610-001 退役 BTC_Swing_V5 留下三处残留——(a) registry status=retired→strategy_builder 过滤, (b) vote_weight=0.0→投票权归零, (c) live_btc.yaml enabled=false→load_brain_entries 级别禁用. Governance 将 V5 升为 live 但上述三处均未同步.
- **Evidence**:
  - Source 1: live_btc.yaml:18 — BTC_Swing_V5 enabled: false
  - Source 2: BTC_Swing_V5.json — status=retired, vote_weight=0.0
  - Source 3: intent log — disabled_brains_filtered: before=8 after=5 (V5 被过滤)
  - Source 4: governance_state.json — BTC_Swing_V5.status=live
  - Source 5: _dqaf_probe_cut4.json — voted_ids=['V11_H1','V11_M15'] (V5 缺席)
- **Fix**: 三步同步 + Cut 4 SSOT 重构 → FIX-20260612-006
- **关联 ReB**: ReB-20260612-007 (TRIPLE_BOOKKEEPING_RESIDUAL), ReB-20260612-008 (GOVERNANCE_BRAIN_SOURCE_MISMATCH)
- **关联 FIX**: FIX-20260612-006

---

### CCT-20260621-033 (UPDATED 2026-06-21 — P0 Investigation Complete)
- **Docket ID**: DQAF-20260621-033
- **日期**: 2026-06-21
- **严重等级**: Sev 2 — 66%仓位在系统控制外平仓
- **置信度**: **confirmed** — H1 (double-journaling) FALSIFIED by temporal analysis. H2 (broker/external close) confirmed at 100%.
- **因果链 (已确认)**:
  - [Layer 1 — 症状]: 99/150 (66%) 仓位关闭不由系统 managed_close 触发。`close_accepted`(51笔) 和 `mt5_deal_reason_3`(99笔) 形成零交集（时序 asof merge ±5s: 0 matched pairs in `scripts/analyze_dqaf033_temporal_coupling.py`）。
  - [Layer 2 — 中间异常]: MT5 DEAL_REASON_SIGNAL (3) = Python API 自动化交易归类。99 笔: 91% LONG (逆势), 93% 0.01 lot, 中位持仓 40min, 24h 均匀分布。全部有 SL/TP 设置, 66/99 有 modify_sltp trail。V4 受冲击最重 (66笔), 但 MIA 交易 PnL (+26.68R) > 非 MIA (+6.50R)。
  - [Layer 3 — 根因]: **RC-08 (observability gap) + RC-04 (taxonomy drift)** — (A) bridge worker 不捕获 deal_reason, PCA 使用裸格式串, 两路径命名不一致；(B) 两条路径覆盖互斥仓位集合, 66% 出场失去可控性溯源；(C) 均未使用 `position_identifier` 作为对账主键。
- **P0 热补丁 (FIX-20260621-035)**: `mt5_bridge_worker.py` detail 新增 `deal_reason` + `position_close_adapter.py` `_DEAL_REASON_MAP.get()`
- **P1 已完成 (FIX-20260621-036)**: ✅ `PositionClosed` 合约新增 `position_identifier` 字段, PCA 从 `deal.position_id` 捕获, bridge detail + journal record 两路径注入
- **P2 待执行**: 从 MT5 终端导出原始账单核对 99 笔 MIA 的 Comment 字段

---

### CCT-20260621-034 (UPDATED 2026-06-21 — FIX-037 + FIX-038 Deployed)
- **Docket ID**: DQAF-20260621-034
- **日期**: 2026-06-21
- **严重等级**: Sev 2 — 出场质量退化，Trailing SL 功能大面积失效
- **置信度**: confirmed (Iron Law #11 脚本数据 + 代码审计 + V3 恢复路径硬编码证实三层根因 + 实盘部署后审计追加两刀)
- **因果链**:
  - [Layer 1 — 症状]: 48.7% 仓位零快照, trail 从未激活。Δ PnL = +282R (ACTIVE vs INACTIVE)
  - [Layer 2 — 中间异常 A]: V3 恢复 `current_sl=0.0` → snapshot 守卫 `_current_sl <= 0 → SKIP` → 相互死锁
  - [Layer 2 — 中间异常 B (FIX-038 追加发现)]: V3 恢复不设 strategy_name → 仓位脱离策略归属 → trail_policy 降级。entry_price 漂移 (64445.31↔64456.0) → 风险原点移动 → 保本/移动止损基准错乱
  - [Layer 3 — 根因]: RC-08 (V3 restore+snapshot guard mutual deadlock) + RC-06 (V3 序列化缺失策略归属字段) + RC-02 (可变 @dataclass entry_price 无保护)
- **Fix Summary**:
  - **FIX-037** (三刀热补丁): sync_position_from_mt5() + force_init_snapshot + fallback_unmanaged — 阻断死锁
  - **FIX-038** (两刀架构修复): V3 strategy 序列化补全 + entry_price 不可变锁 — 消除数据模型缺口
  - **FIX-039** (L3 架构收敛): 移除冗余 per-cycle sync → CRITICAL 告警替代 — recovery 失败可见
- **关联 ReB**: STATE_INITIALIZATION_DEADLOCK, SERIALIZATION_ATTRIBUTION_GAP, MUTABLE_RISK_ORIGIN, DEAD_SAFETY_NET_MASKING_RECOVERY_FAILURE
- **关联 FIX**: FIX-20260621-037, FIX-20260621-038, FIX-20260621-039
- **状态**: **CLOSED** — 架构债清偿, 全链路收敛于 recovery-once + alert-on-failure
  - H2: `register_position()` 是否同步注册 snapshot listener？→ 检查 `position_manager.py` listener attachment
  - H3: 0-snapshot 仓位是否全部为微生命周期(< 5 bar)？→ 交叉验证 snapshot count vs bars_held
- **是否被推翻**: 部分 — H1/H2 (竞态) 证伪: snapshot 在 management phase 内部执行, 与 registration 存在 happens-before。真正根因是 **STATE_INITIALIZATION_DEADLOCK**: (A) V3 恢复 `current_sl=0.0` 硬编码 → snapshot 守卫拒绝写入, (B) 31 仓位注册流水线断裂 (空白 strategy)
- **IC 裁决**: APPROVED WITH HOTFIX MANDATE — 三刀斩断 (FIX-20260621-037)
- **关联 ReB Pattern**: `STATE_INITIALIZATION_DEADLOCK` — 状态机冷启动默认值 (0.0) 与下游激活门槛 (>0) 形成逻辑互斥
- **关联 FIX**: FIX-20260621-037 (deployed)
- **部署后验证**: 下次系统重启后, 所有 V3 恢复仓位应在首个管理周期从 MT5 同步真实 SL, snapshot 不再抛弃 SL 未初始化仓位

---

### CCT-20260621-046

- **Docket ID**: DQAF-20260621-046
- **日期**: 2026-06-21
- **置信度**: confirmed (双源: probe script stdout + code audit × 16 brain configs)

**Layer 1 — 症状 (信号真空)**:
  - XAU live_shadow_ensemble 连续 45 天产出空决策文件 — 41/41 brains 返回 `neutral`/`ABSTAIN`, 0 条方向信号
  - 证据: `scripts/probe_xau_signal_generation.py` stdout — decision file history: 45 files, 0 nonempty; per-brain inference: 16/21 fallback (dim_mismatch), 5/21 real (weak), 41/41 neutral
  - 置信度: confirmed

**Layer 2 — 中间异常 (双根因)**:
  - **2a. BrainSignal API fracture**: `signal.prediction` dict 被替换为 `signal.direction: Direction` Literal + `signal.confidence: float` + `signal.raw_score: float` frozen dataclass。live_shadow_ensemble `_run_single_brain()` line ~106 仍使用旧接口 `signal.prediction.get("direction_bias", "neutral")` → 所有脑返回 neutral
  - **2b. Feature dimension mismatch**: 16 个 swing/barrier/trend brains (35-dim swing_enhanced_35 schema) 收到 40-dim institutional v9 特征 → 完全不同特征空间 → `dim_mismatch` fallback → 5 个 institution brains 信号极弱 (confidence < 0.52)
  - 证据: `scripts/live_shadow_ensemble.py:_run_single_brain()` — dict access on frozen dataclass; `core/features/schemas/swing_enhanced_schema.py` vs `v9_institutional_schema.py` — 35 vs 40 dim, different feature definitions
  - 置信度: confirmed

**Layer 3 — 根因 (L2 逻辑缺陷: 特征流水线无 schema 路由)**:
  - 特征生产层 (feature store/computers) 与特征消费层 (brain inference) 之间缺少 **schema routing contract**。brain config 中虽已有 `feature_schema_id` 字段, 但未在 feature resolution 路径中消费——特征路由器未实现 → 所有 brain 默认收到 v9 40-dim vector
  - 反模式: (1) BrainSignal 接口无向后兼容层——consumer 在不知情的情况下被破坏, (2) 特征维度无运行时校验——35-dim model 静默接收 40-dim input → model.predict() 内部 pandas/numpy 列对齐可能产生无警告截断或错误广播
  - 证据: `core/features/schemas/registry.py` — schema registry exists but not consumed; 16 brain configs — `feature_schema_id` field present but routing code missing
  - 置信度: confirmed

**证据引用**:
  - Source 1: `scripts/probe_xau_signal_generation.py` stdout — 完整诊断输出 (Iron Law #11 compliant)
  - Source 2: `scripts/live_shadow_ensemble.py` line 106 — `signal.prediction.get("direction_bias", "neutral")` 旧接口
  - Source 3: 16 brain configs `configs/brains/*.json` — `feature_schema_id: "swing_enhanced_35"`
  - Source 4 (cross-symbol): BTC ensemble 未受影响 — BTC brain 全部使用 institution schema
- **是否被推翻**: 否 — AR 反向假设 (单点配置错误) 被推翻: 16/21 brains dim_mismatch 证明是系统性 schema 路由缺失
- **关联 ReB Pattern**: `FEATURE_SCHEMA_ROUTING_AND_BRAIN_API_CONTRACT`
- **关联 FIX**: FIX-20260622-003 (XAU dual-track), FIX-20260622-001 (Plan B State Governance Protocol)
- **状态**: **CLOSED** — Dual-track feature pipeline deployed: 35-dim swing resolver (DailyFeatureComputer 24 daily + 9 micro + 2 TF) + feature router (feature_schema_id) + BrainSignal API fix (direction/confidence/raw_score). 0/21→11/21 non-neutral. Plan B 同步交付防止复发

## DQAF-20260623-066: p_win Cold-Start Triple-Break

- **Label**: COLD_EXPLORE_TRAP
- **Docket**: DQAF-20260623-066 (Sev 1)
- **Causal Chain (4 Layers)**:
  - **Layer 1 (症状)**: 30 笔交易亏损 -34.84R, 胜率 ~10% (XAU 0/6, BTC 3/24)。系统盈利能力崩溃。
  - **Layer 2 (直接原因)**: 所有获批交易使用 p_win=0.50 (cold_explore_neutral)。Kelly sizing, RR gate, volume 全部基于假数据 → 好策略和坏策略获得相同仓位规模。
  - **Layer 3 (中间异常)**: DQAF-065 MetaFilter 切除后 swing 策略永远返回 (None, None) → 触发 `_is_cold_explore=True` → p_win 硬编码为 0.50。BrainPnLStore 重启后为空 → `resolve_p_win_from_brains()` 返回 0.40 → 低于 min_p_win → 所有通过 rolling WR 路径的交易被拒。
  - **Layer 4 (根因 — L3 架构缺陷)**: p_win 计算链路三连环断裂:
    1. DQAF-065 → swing 策略唯一可行通道是 cold_explore
    2. BrainPnLStore 纯内存, 重启后为空 → 无真实统计 → fail-closed 0.40
    3. Governance `performance_metrics` 存在但未接入 p_win 解析链
- **Evidence**:
  - Source 1: `live_trade_journal.jsonl` — XAU 6 笔全部 p_win=0.50, BTC 24 笔全部 p_win=0.50
  - Source 2: `golden_master.jsonl` — XAU 1107 决策中 21 approved (1.9%), BTC 414 中 104 approved (25%)
  - Source 3: `strategy_line.py:922-930` — cold_explore 触发条件: _meta_p_win is None AND _meta_reject is None
  - Source 4: `meta_filter_routing.py:74-89` — DQAF-065: swing 策略不在 statarb 条件中 → passthrough (None, None)
  - Source 5: `pwin_chain.py:81-106` — DQAF-059 governance gate: sample_count<10 排除 → 0 valid rates → 0.40 fallback
  - Source 6: `brain_pnl_ledger.py:548-553` — BrainPnLStore.__init__() 纯内存, 无 data_dir 参数
- **是否被推翻**: 否 — AR 反向假设 (行情不利) 被推翻: BTC 在窗口中仅下跌 2%, 但 16/24 笔保本退出 (PnL=0.00) 表明是系统决策问题非行情问题
- **关联 ReB Pattern**: `COLD_EXPLORE_TRAP`
- **关联 FIX**: FIX-20260623-066 (P0-1 governance fallback, P0-2 cold_explore→governance, P0-3 ≥2 LIVE brains gate)
- **状态**: **CLOSED** — 三修复部署: `resolve_p_win_from_brains()` governance cold-start fallback + `resolve_p_win()` cold_explore governance 替代盲 0.50 + cold explore ≥2 LIVE brain 准入门禁

### CCT-20260623-070
- **Docket ID**: DQAF-20260623-070
- **日期**: 2026-06-23
- **置信度**: confirmed (code audit × grep × production log evidence)
- **因果链**:
  - [Layer 1 — 症状]: 每周期 `session_guard_error`: `'LiveCycleState' object has no attribute '_feature_buffers_warm'`。重启后 feature freshness check 从未真正执行 — 冷特征直接进入交易决策。
  - [Layer 2 — 中间异常]: `session_guards.py:148` 访问 `state._feature_buffers_warm`, 但 `LiveCycleState` dataclass (live_cycle.py:214-300) 从未定义此字段。AttributeError 被外层 `except Exception` (line 167) 捕获 → fail-open → 周期继续。
  - [Layer 3 — 根因]: L1 — Strangler Fig 重构时 `_feature_buffers_warm` 字段未被提取到 dataclass。L2 — `run_session_guards()` 的外层异常处理过宽 (`except Exception`) — 状态完整性错误与瞬时 MT5 超时被同等对待 (fail-open)。
- **证据引用**:
  - Source 1: `core/runtime/live_cycle.py:214-300` — LiveCycleState 缺少 `_feature_buffers_warm`
  - Source 2: `core/runtime/session_guards.py:148` — 直接属性访问 `state._feature_buffers_warm`
  - Source 3: `tests/runtime/test_session_guards.py:46,61,78,111,139,158,174,189` — 测试代码 mock 了此字段, 证实设计意图但从未在生产代码中实现
  - Source 4: `data_btc/logs/intent_*.log` — 每周期 `session_guard_error`
- **是否被推翻**: 否 — AR 假设 (字段在其他地方动态设置) 被全库 grep 推翻: 仅在测试中有设置, 生产代码零初始化
- **关联 ReB Pattern**: `MISSING_DATACLASS_FIELD`
- **关联 FIX**: FIX-20260623-070 (补齐字段 + MT5 bootstrap 后置 True + getattr 安全访问 + 异常处理分层)

### CCT-20260623-071
- **Docket ID**: DQAF-20260623-071
- **日期**: 2026-06-23
- **置信度**: confirmed (production log evidence × code analysis)
- **因果链**:
  - [Layer 1 — 症状]: 定期出现 "FeatureService stale cache for BTCUSDc: age=300.1s" → Tier 2 实时计算不必要触发 → MT5 IPC 负载增加。
  - [Layer 2 — 中间异常]: 特征持久化间隔 (~60s × 5 = 300s) 恰好等于新鲜度 SLA (300s)。缓存恰好在边界翻转 — 第 5 周期时 age≈300s, 第 6 周期时 age≈360s。每次翻转触发 live compute。
  - [Layer 3 — 根因]: L2 — 写入间隔与读取 SLA 相同 (两个独立参数设为同一值 300s), 无抖动余量保证写入持续领先检查线。
- **证据引用**:
  - Source 1: `core/features/feature_service.py:139` — `max_age_seconds=300.0` (修复前)
  - Source 2: `data_btc/logs/intent_*.log` — "age=300.1s (limit=300s), falling through to live compute"
- **是否被推翻**: 否 — AR 假设 (特征持久化失败) 被代码审查推翻: persist_micro_features 正常运行, 只是间隔恰好 300s
- **关联 ReB Pattern**: `CACHE_SLA_BOUNDARY`
- **关联 FIX**: FIX-20260623-071 (SLA 300→310s 负向抖动余量)

### CCT-20260623-072
- **Docket ID**: DQAF-20260623-072
- **日期**: 2026-06-23
- **置信度**: confirmed (code audit × grep × production log evidence)
- **因果链**:
  - [Layer 1 — 症状]: DQAF-059 "ZERO LIVE brains found" 警告每周期触发 + DQAF-066 governance cold-start fallback 日志从未出现 + p_win 始终退化为 `brain_confidence`。
  - [Layer 2 — 中间异常]: `strategy_line.py:538` — `governance_state.items()` 遍历顶层键 (`"brain_states"`, `"schema_version"`, `"performance_metrics"`), 而非 `governance_state["brain_states"].items()`。`_live_brain_ids` 恒为空集 `set()` (非 None)。在 `resolve_p_win_from_brains()` 中, `live_brain_ids is not None` → True, 但 `brain_id not in live_brain_ids` → True (所有 brain_id 都不在空集内) → 所有 brain 被治理门过滤 → governance fallback 也因相同检查而失败。
  - [Layer 3 — 根因]: L1 — `governance_state.items()` 应该在 `governance_state["brain_states"].items()` 上迭代。L2 — 缺少集成测试: 空 `_live_brain_ids` 从未被任何测试捕获。
- **证据引用**:
  - Source 1: `core/execution/strategy_line.py:536-540` (修复前) — `for bid, b_info in governance_state.items()`
  - Source 2: `core/execution/pwin_chain.py:99-101` — `if live_brain_ids is not None and brain_id not in live_brain_ids: continue`
  - Source 3: `data_btc/logs/intent_*.log` — `FALLBACK_PATH_3c: All 1 brain(s) filtered out by governance gate (none are LIVE)`
  - Source 4: governance_state.json 结构 — `{"brain_states": {...}, "performance_metrics": {...}}` — 顶层键不含 `status` 字段
- **是否被推翻**: 否 — BTC_Swing_V12_H1_Survival 是 LIVE (WR=51.5%, 56 trades) 但从未出现在 `_live_brain_ids` 中
- **关联 ReB Pattern**: `WRONG_DICT_LEVEL_GOVERNANCE`
- **关联 FIX**: FIX-20260623-072 (`governance_state.get("brain_states",{}).items()` 单行修复)
- **状态**: **CLOSED** — DQAF-059 治理门过滤 + DQAF-066 治理冷启动回退 + DQAF-066 cold_explore 门禁全部自愈

---

### CCT-20260625-125
- **Docket ID**: DQAF-20260625-125
- **日期**: 2026-06-25
- **置信度**: confirmed (code audit × production log evidence × file path verification)
- **因果链**:
  - [Layer 1 — 症状]: XAU `data/reports/leaderboard.json` generated_at=2026-06-22T05:44 (66h/3956min stale), BTC leaderboard 819min stale. `daily_ops_complete` 事件从未发出。
  - [Layer 2 — 中间异常]: `live_launcher` log L13632: intent loop crashed with exit code 1 (restart #12) during daily_ops execution — pipeline interrupted after feedback step, before retraining_check. Watchdog 未能自动恢复。
  - [Layer 3 — 根因]: FIX-20260622-001 (Plan B StateWriter 迁移) 遗漏 `watchdog_daily_ops.py`。三处 bug 叠加: (a) 路径 `base_dir/"daily_ops_state.json"` 与实际 `base_dir/"state"/"daily_ops_state.json"` 不匹配, (b) 字段名 `last_run_utc`/`updated_utc` 与实际 `last_daily_ops_utc` 不匹配, (c) `age_h is None` 分支未实现 auto_run。Watchdog 完全盲化 — 即使 `--auto-run` 也不执行。
- **证据引用**:
  - Source 1: `data/reports/leaderboard.json` — generated_at=2026-06-22T05:44:49 (文件 mtime 验证)
  - Source 2: `data/logs/live_launcher_20260624T114818Z.log:13632` — `intent exited with code 1`
  - Source 3: `scripts/watchdog_daily_ops.py:61` (修复前) — `state_path = base_dir / "daily_ops_state.json"`
  - Source 4: `core/state/catalog.py:321` — `path_template="state/daily_ops_state.json"` (SSOT)
- **是否被推翻**: 否 — 所有三处 bug 均已代码审计确认
- **关联 ReB Pattern**: `ORPHAN_WATCHDOG_MIGRATION_SWEEP_INCOMPLETE`
- **关联 FIX**: FIX-20260625-125 (路径修正 + 字段名修正 + never-run auto_run)
- **状态**: **CLOSED** — FIX-20260625-125 deployed. Leaderboard 手动恢复: XAU 69 brains 1192 decisions

---

### CCT-20260630-202
- **Docket ID**: DQAF-20260630-202
- **日期**: 2026-06-30
- **置信度**: confirmed (code audit × ShadowTracker evidence × governance_state.json verification)
- **因果链**:
  - [Layer 1 — 症状]: H4_V3 (456 directional signals, 100% SHORT, avg_conf=0.551) stuck at `candidate` status despite meeting all Rule 85 auto-promotion criteria. No promotion event in governance transition_log.
  - [Layer 2 — 中间异常]: (Contract 1) `v9_onnx_brain_adapter.py::get_signal()` used global hardcoded `activation_threshold=0.1` — H4_V3 raw_scores [-0.07, -0.12] fell entirely within dead-zone → 0% directional signals before 6/29 config fix. (Contract 2) `_promote_shadow_brains()` in `governance_scheduler.py:332` hard-coded `if m.long_count < 5 or m.short_count < 5: continue` — 0L/456S rejected. The same Rule 85 logic in `governance_rule_engine.py:338-349` had already been fixed with macro exemption (FIX-20260701-203), but the duplicate in `governance_scheduler.py` was not updated.
  - [Layer 3 — 根因]: **L3 Architectural — Rule 85 duplicated across two governance paths without synchronization mechanism.** The BTC path (`scheduler_service.py` → `GovernanceRuleEngine.evaluate()`) and XAU path (`daily_ops_scheduler.py` → `run_governance_cycle()` → `_promote_shadow_brains()`) independently implement the same rule. The architecture has no unified rule evaluation entry point — each path must be maintained separately. Additionally, `daily_ops_scheduler.py:200` failed to pass `base_dir=config.base_dir`, causing XAU ShadowTracker to default to `data_btc/brain_votes/` (empty for XAU).
- **证据引用**:
  - Source 1: `data/brain_votes/2026-06-28.jsonl` through `2026-07-01.jsonl` — 456 H4_V3 directional signals, 0 long, 456 short
  - Source 2: `scripts/training/governance_scheduler.py:327-334` (pre-fix) — hard-coded diversity check without macro exemption
  - Source 3: `core/governance/governance_rule_engine.py:338-349` — macro exemption already present (FIX-20260701-203) but only on BTC path
  - Source 4: `data/governance_state.json` — H4_V3 status=candidate, no promotion transition
  - Source 5: `core/runtime/daily_ops_scheduler.py:200-201` (pre-fix) — missing `base_dir=config.base_dir` pass-through
- **是否被推翻**: 否
- **关联 ReB Pattern**: `TOXIC_DIVERSITY_GATE`, `DUPLICATE_RULE_UNSYNC`
- **关联 FIX**: FIX-20260630-202, FIX-20260701-203, FIX-20260701-204

---

### CCT-20260706-003
- **Docket ID**: DQAF-20260706-003
- **日期**: 2026-07-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 重启后 3 笔 XAU 实盘交易由 vote_weight=0.0 的影子大脑决策 — H4 SHORT (05:08Z), M15 LONG (05:15Z), M30 LONG (06:14Z). 证据: `data/golden_master.jsonl` cycle=1 H4 SHORT `[degraded: no_live_brains]`, cycle=3 M15 LONG `[degraded: non_live_dominance]`, cycle=15 M30 LONG `[degraded: no_live_brains]` + `data/live_trade_journal.jsonl` 对应的 3 笔 open 事件
  - [Layer 2 — 中间异常]: `strategy_evaluator.py:605-611`: `_live_count` 计算仅使用 `status in ("live","probation")` 过滤，未考虑 `vote_weight`。governance_state 中 8 个 Tracer 大脑注册为 status=candidate + vote_weight=0.0 — 重启后 live 大脑尚未产出信号时，仅 candidate/shadow 大脑投票 → `_live_count=0` → 触发降级路径 (`confidence ≥ 0.50 + volume ≤ 0.01`)
  - [Layer 3 — 根因]: L3 架构缺陷: Cut 4/Cut 4-bis 降级门缺少 vote_weight 门禁。FIX-20260625-139 在信号管线层面修复了 vote_weight 传透 (BrainSignal → `_compute_weighted`)，但 `strategy_evaluator.py` 的并行治理门从未被更新。这是 strategy_evaluator.py 中「跨文件重复门逻辑」的第 4 个实例 — 前三次: FIX-20260629-174 (governance 访问路径), FIX-20260703-061 (status 维度), FIX-20260625-139 (信号管线 vote_weight — 仅修了 strategy_line.py 漏了 strategy_evaluator.py)
- **证据引用**:
  - Source 1: `core/runtime/strategy_evaluator.py:605-611` (pre-fix) — `_live_count` 仅按 status 过滤
  - Source 2: `core/runtime/strategy_evaluator.py:612-651` (pre-fix) — `_live_count==0` 降级路径无 vote_weight 检查
  - Source 3: `data/governance_state.json` — 8 个 Tracer 大脑: status=candidate, vote_weight=0.0
  - Source 4: `configs/brains/XAU_Swing_*_A.json` — IC Mandate: "Shadow, vote_weight=0.0, 7-day observation"
  - Source 5: `core/runtime/strategy_line.py` — FIX-20260625-139 已修复 signal 管线 vote_weight (证明 parallel gate 遗漏)
- **是否被推翻**: 否
- **关联 ReB Pattern**: `CROSS_FILE_DUPLICATE_GATE_LOGIC`
- **关联 FIX**: FIX-20260706-003
- **状态**: **CLOSED** — FIX-20260701-204 deployed. H4_V3 macro exemption active on both governance paths. Restart required for promotion evaluation.

---

### CCT-20260708-001
- **Docket ID**: DQAF-20260708-001
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: analyze_live_journal 报 157 BTC 孤儿平仓 (-$170.04); JournalGate 隔离区 184 条全 `close_without_open` (153 in July, ~17/day); label_builder 静默丢弃换号交易 (双轨标签损失)。证据: `scripts/analyze_live_journal.py:88/116`, `core/ledger/services/journal_gate.py:92`, `scripts/_forensic_orphan_closes.py` stdout
  - [Layer 2 — 中间异常]: MT5 在 partial-close/netting 换号 → 平仓携带新 `position_ticket`, 开仓保留原始 ticket; 所有 join 站点用可变 ticket 配对 open<->close。证据: `core/runtime/live_cycle.py:3749-3786` (换号只更新内存), `core/runtime/position_close_adapter.py:366/429` (close 从 deal.position_id 取 identifier)
  - [Layer 3 — 根因]: RC-02 type-confusion — 无单一以不可变 `position_identifier` 为键的生命周期权威; 可变 ticket 被当作稳定 join 键。~30 次历史修复全在下游打补丁; TECH_DEBT-003 命名 remedy 但记错 SSOT key。
- **证据引用**:
  - Source 1: Journal — `data_btc/live_trade_journal.jsonl` (34/35 identifier-matched-to-open, 0 identifier==ticket)
  - Source 2: State — `data_btc/journal_orphan_quarantine.jsonl` (184 close_without_open, 153 July)
  - Source 3 (cross-symbol): `data/live_trade_journal.jsonl` — XAU 53 orphan + 8009 no-ticket (异质分支: 键缺失而非键变, 3975/Jun→26/Jul 已自愈, 独立跟踪)
- **是否被推翻**: 否 (AR 证伪了 "pre-June-7 legacy" 与 "open leg lost" 两个反假设)
- **关联 ReB Pattern**: `MUTABLE_TICKET_JOIN_ON_IMMUTABLE_POSITION`
- **关联 FIX**: FIX-20260708-001
- **状态**: **CLOSED** — FIX-20260708-001 committed f139ab87. BTC 孤儿 157→119, $126 回收; journal_gate 覆盖 0%→86%。

---

### CCT-20260708-002
- **Docket ID**: DQAF-20260707-003
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: btc_swing_h1 (V12_H1_15) golden_master 近乎 100% 输出 LONG (记忆 diagnostics_20260628_btc_all_long_bias); 模型对多空无判别力 (Wasserstein=0.0084)。
  - [Layer 2 — 中间异常]: 补 7 个 H1 时间尺度方向特征 (H1_Ret_1/2/4, H1_Realized_Vol, H1_Ret_Accel, H1_MeanRev, H1_M5_Div) 做 48-dim 重训后, 判别力不升反降 (Wasserstein 0.0084→0.0019)。证据: `data_btc/models/btc_swing_h1_binary_48/training_summary.json` cv mean_val_wr xgb=0.5081 / lgbm=0.4895。
  - [Layer 3 — 根因]: L3 结构性 — H1 尺度方向不可从现有 M5/D1/H4 特征空间线性分离; 加特征无法拯救不可分信号 → 正确响应是退役该策略线, 而非继续调参 (反例 BTC 三连打地鼠)。
- **证据引用**:
  - Source 1: 训练 — `data_btc/models/btc_swing_h1_binary_48/training_summary.json` (val_wr ≈ 50%, 与随机不可区分)
  - Source 2: 配置 — `configs/live_btc.yaml:152/313` (V12_H1_15 + btc_swing_h1 retired, Wasserstein 记录)
  - Source 3 (机制复用): `core/brains/adapters/base_adapter.py:217-228` (quantile_gaussian 已 live 于 8 XAU brain)
- **是否被推翻**: 否 (AR 证伪了 "48-dim 模型其实可用只是没部署" — val_wr 50.8%±0.7% 与随机不可区分)
- **关联 ReB Pattern**: `FEATURE_ENGINEERING_CANNOT_RESCUE_UNSEPARABLE_SIGNAL`
- **关联 FIX**: FIX-20260708-002
- **状态**: **CLOSED (退役决策)** — FIX-20260708-002. BTC live 收敛至 V4 + B-path binary(probation); V4 confidence 采用 quantile_gaussian 校准 (T22 监控); 48-dim serving 休眠留待 Path C horizon=4。

---

### CCT-20260709-001
- **Docket ID**: DQAF-20260709-001
- **日期**: 2026-07-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `scripts/analyze_live_journal.py --data-dir data` (XAU) 在 Section 3 崩 `TypeError: unsupported format string passed to NoneType.__format__` (line 559 `{lbl:<55s}`), 完整 7 段审计不可得。证据: `scripts/analyze_live_journal.py:559`; 直接调用 `analyze_journal(Path("data"))` 产出 `pnl_by_label` 含 None 键 `{count:114, pnl_usd:0.68, wins:34, losses:20}`。
  - [Layer 2 — 根因]: RC-01 missing-null-check — line 135 `final_close.get("label", "?")` 对 present-but-null 失效: `dict.get(k, default)` 仅在 key **缺失**时替换 default; key 存在值为 None 时返回 None。114 条 XAU close 合法携带 `label: null` (no-ticket 孤儿分支, 早于 FIX-20260626-144 write-side 加固) → None 贯穿 realized → 成 `pnl_by_label` 字典键 → :559 `:s` 格式化崩溃。同构潜伏点 :133 side / :136 ack。上游 write-side null-label 已由 FIX-20260626-144 封堵, 但 114 条为不可变历史遗留 → 审计脚本须对合法 null 鲁棒。
- **证据引用**:
  - Source 1: 代码 — `scripts/analyze_live_journal.py:135` (`.get("label","?")`), :559 (`{lbl:<55s}`)
  - Source 2: 数据 — `data/live_trade_journal.jsonl` 114 条 label:null; `analyze_journal()` 直调产出 None 键
  - Source 3 (cross-symbol): `data_btc/live_trade_journal.jsonl` — BTC 0 null-label 不崩 (影响面隔离)
- **是否被推翻**: 否 (AR 证伪了 "None 是 pnl_usd 不是 label" — 该项 pnl_usd=0.68 有效 float, KEY 才是 None; 并证伪 "在 :559 打印处加 guard" — FIX-20260613-066 已在 Section 4 打印处 guard 却复发 → 须摄入边界根治)
- **关联 ReB Pattern**: `GET_DEFAULT_NULL_TRAP`
- **关联 FIX**: FIX-20260709-001
- **状态**: **CLOSED** — FIX-20260709-001 committed d9c147e8. `_coalesce()` 摄入边界单点规整 side/label/ack; XAU 恢复完整审计 + `(unlabeled)` 桶 114/+$0.68; 6 回归测试 (tests/scripts/test_analyze_live_journal_null_label.py)。

---

### CCT-20260709-002
- **Docket ID**: DQAF-20260709-002
- **日期**: 2026-07-09
- **置信度**: confirmed (出场/进场) + refuted-then-reclassified (持仓)
- **因果链** (三相独立根因, Iron Law #12 禁捆绑):
  - **[出场相]**
    - [Layer 1 — 症状]: XAU LONG 4098917446 (m30_swing) 开在 broker 却失管不平, 与 SHORT 4098792728 (h4_swing) 构成对冲。证据: LONG 快照停于 21:00:27; LONG 0 真实平仓 / 19 拒绝。
    - [Layer 2 — 中间异常]: 21:00 休市重启风暴 (launcher log 13 次重启 20:55–22:40); LONG 脱离 known_open_tickets → Guard 1 `position_manager_stale_cleared` ×11 (21:06–22:05) ↔ orphan 采纳 (仅 loop_iteration==1) → 乒乓; `active_position.json` 持久化 LONG 缺席 (orphan_position_adopted: active_position_tickets=[SHORT], mt5_tickets=[both])。
    - [Layer 3 — 根因]: L3 — Guard 1 (management_phase.py:947) 以 known_open_tickets 缺席推断"已平仓", **从不查 broker** (positions_get 才是 SSOT); 违反"broker 开着的仓必在管理集"不变量。
  - **[进场相]**
    - [Layer 1 — 症状]: 16:24 m30_swing LONG 对既有 (15:59) h4_swing SHORT 成形对冲。
    - [Layer 2/3 — 根因]: L3 — CrossStrategyCoordinator (block 默认) 自 P4-2 从未注入 live; strategy_evaluator.py:1071 守卫 `is not None` 恒 False → 反向持仓守卫死代码。
  - **[持仓相 — AR 推翻]**
    - [Layer 1 — 表观症状]: snapshot `unrealized_pnl_r` 达 -6.5R, SHORT SL 全程冻结 4162.674, 疑"亏损腿裸奔无保护"。
    - [Layer 2 — AR 证伪]: SL distance 127.76 = 2.0×63.88; h4_swing 配置 SL=2.0×ATR (H4 ATR≈63.9, FIX-20260706-027 per-TF ATR)。snapshot `entry_atr`=6.41 是 M5/入场 ATR, 仅供 R 度量。"-6.5R"=-6.5×M5_ATR≈-0.65×H4_ATR = 仅到 H4 止损的 ~26%。**正常 H4 swing, 非交易缺陷**。
    - [Layer 3 — 重分类]: 表观"亏损腿失护"根因**被推翻**; 真实次生问题为 R 度量 ATR 错配 (M5 vs H4) 与 bars_held 重启冻结 (可观测性/连续性, 非交易), 经 IC 裁决登记 Deferred, 不做投机交易改动 (机构级 mandate #1)。
- **证据引用**:
  - Source 1: 取证 — `scripts/forensic_xau_hedge_20260709.py` stdout (Iron Law #11); `data/position_snapshots.jsonl`; `data/live_trade_journal.jsonl` open 事件 sl=4162.674
  - Source 2: 日志 — `data/logs/live_launcher_20260708T154609Z.log` (stale_cleared ×11, orphan_position_adopted, 13 restarts, data_health current_positions=2)
  - Source 3: 代码 — `core/runtime/management_phase.py:947`, `core/runtime/strategy_evaluator.py:158/1071`, `configs/live.yaml` h4_swing SL=2.0×ATR
- **是否被推翻**: 出场/进场 否 (AR 证伪"经重启过滤/周期对账脱管"—SHORT 对照存活+LONG 0 exit deal; 证伪"PNG 已覆盖"—PNG 同周期, 对冲跨周期)。持仓 **是** (AR 证伪"亏损腿失护"—SL 按 H4 ATR 正确定尺, R 单位错配假象)。
- **关联 ReB Pattern**: `BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK` (出场), `DORMANT_SAFETY_GUARD_NEVER_WIRED` (进场), `R_UNIT_MISMATCH_CROSS_TIMEFRAME` (持仓)
- **关联 FIX**: FIX-20260709-002 (出场), FIX-20260709-003 (进场)
- **状态**: **CLOSED** — 出场 017d726d + 进场 10e22cb2 committed+pushed; 持仓相搁置 (Deferred: R 度量 ATR 错配 + bars_held 重启冻结)。

---

## CCT Entry — DQAF-20260709-005

- **Docket ID**: DQAF-20260709-005
- **Severity**: Sev 4 (IC revised DOWN from initial Sev 1 escalation via Adversarial Review)
- **Date**: 2026-07-09
- **Layer 1 — Symptom (Extinction)**:
  `exit_watchdog.py:164` `_check_time_decay` `return unrealized_r < 0`.
  Because `position.unrealized_pnl_r` was NEVER set on ActivePosition dataclass
  (grep-confirmed: no `.unrealized_pnl_r =` / `setattr` / kwarg / SimpleNamespace),
  `getattr(position, "unrealized_pnl_r", 0)` always returns 0 → the check always
  returns False → `_check_time_decay` and `_check_price_decay` never fire.
- **Layer 2 — Intermediate (Viability)**:
  `evaluate_position` (the ONLY caller of `_check_time_decay` / `_check_price_decay`)
  has ZERO external callers repo-wide (grep-confirmed: only `def` at line 128).
  Zero `time_decay_` / `price_decay_` exits in any journal (data/ + data_btc/).
  → Not a "silently failed safety net" but a superseded, never-wired code path.
- **Layer 3 — Root Cause (Architectural)**:
  FIX-20260613-086 added `evaluate_position` as a model-independent structural
  evaluator.  Sometime later the time-decay exit role was absorbed by
  `PositionManager.should_exit_hesitation` (per-strategy `exit_hesitation_cycles`
  × `timeframe_scaling` → per-TF correct), producing the real
  `hesitation_Nc_no_breakeven` exits wired at `management_phase.py:1775`.
  The old evaluator was superseded but NEVER deleted, and its docstring
  (`exit_watchdog.py:137`) still claims "Live Cycle calls this once per open
  position per cycle" — a latent re-wiring trap.
- **AR Adversarial Review**:
  Reverse hypothesis "the attribute IS injected elsewhere" tested and REFUTED
  (grep all *.py). The initial IC Sev 1 "silent safety-net failure requiring
  immediate Hotfix" premise was overturned: the net was never deployed, not
  silently failing; fixing the attribute would resurrect an INFERIOR
  M5-hardcoded (60c) path that was deliberately superseded.
- **ECoL Evidence**:
  - Source 1: `exit_watchdog.py:161` getattr(position, "unrealized_pnl_r", 0) — never set
  - Source 2: grep `evaluate_position` repo-wide — 0 external callers
  - Source 3: journal grep `time_decay_\d+c` / `price_decay_\d+b` — 0 occurrences
  - Source 4: `position_manager.py:1826` `should_exit_hesitation` — TF-scaled wired equivalent
- **是否被推翻**: 是 — 原 Sev 1 前提被 AR 证伪; 降 Sev 4, 撤 Hotfix, 执行死代码删除
- **关联 ReB Pattern**: `SUPERSEDED_ORPHAN_CODE_WITH_STALE_DOCSTRING` (子签名: `PHANTOM_ATTR_IN_DEAD_BRANCH`)
- **关联 FIX**: FIX-20260709-005 (446ba31f)

---

## CCT-20260715-011: Counter-Trend Gate cold_explore Exemption → Systematic Counter-Trend Loss

- **Layer 1 — Symptom (Observable)**:
  BTC SHORT trades systematically lose in confirmed H4 bull trend market.
  Jul 14 cycle 9: trend_direction=long, trend_strength=0.6, yet btc_swing_h4
  opens SHORT at confidence=0.7449 (volume=0.01).  $ grep counter_trend in GM
  returns ZERO matches since 2026-06-09 — the gate has been silent for 5 weeks.
  Total SHORT loss: -$51.54 (102 trades, 39.2% WR).

- **Layer 2 — Intermediate (Mechanism)**:
  Two independent bypasses converge:
  (a) `strategy_line.py:1225`: `not _is_cold_explore` condition excludes all
      probation strategies (MetaFilter vacuum → `_is_cold_explore=True`).
      The counter-trend gate was designed to apply universally but the
      cold_explore exemption was added without architectural review.
  (b) `trend_volume_guard.py:268`: `thresholds.get(strategy_name)` uses exact
      match only.  `btc_swing_h4` does not match `btc_swing` → falls through
      to default (h4_block=0.70), which is above the current trend_strength=0.6.
  These two failures compound: even if (a) is fixed, (b) would let multi-TF
      strategies through the lenient default.  Even if (b) is fixed, (a) would
      skip the gate entirely for cold_explore strategies.

- **Layer 3 — Root Cause (Architectural Design Flaw)**:
  The design assumption that "cold exploration should be unconstrained" conflates
  TWO orthogonal dimensions: (1) model uncertainty (p_win unknown) and (2)
  structural market constraints (H4 trend gravity).  Trend alignment is NOT a
  statistical confidence problem — it is a physical market law.  A cold model
  exploring counter-trend is not "gathering data" — it is donating capital to
  the trend.  The exemption was an architectural error: cold_explore should
  reduce volume (uncertainty penalty), not bypass structural constraints.

- **AR Adversarial Review**:
  Hypothesis "trend_strength 0.6 is a false positive" → REFUTED: GM confirms
  trend_direction=long consistently across cycles 150-155.  Price action
  confirms bull trend (62k→64.5k).  The Kalman velocity signal is reliable.
  Hypothesis "cold_explore exemption is intentional for data gathering" →
  REFUTED: gathering counter-trend data during strong trend produces
  systematically negative-EV samples.  Trend-aligned exploration gathers
  equally valid data without structural penalty.

- **ECoL Evidence**:
  - E1: GM `grep counter_trend` → 124 matches, all before 2026-06-09
  - E2: Jul 14 cycle 9 GM: H4 SHORT, trend=long/0.6, should_trade=true
  - E3: `strategy_line.py:1225`: `not _is_cold_explore` condition
  - E4: `trend_volume_guard.py:268`: exact-match only for strategy_name
  - E5: Jul 15 restart cycle 4: H4 p_win=0.400 blocked by floor (Catch-22)

- **AR 是否被推翻**: 否 — AR 证伪两个反向假设, 根因确认

- **关联 ReB Pattern**: `COLD_EXPLORE_GATE_EXEMPTION` (子签名: `EXPLORATION_OVERRIDES_STRUCTURAL_CONSTRAINT`)

- **关联 FIX**: FIX-20260715-011 (a1886cfa)

- **状态**: **CLOSED** — 3 代码修复 + M15 退役 + BLE001 fail-open guard committed+pushed
- **状态**: **CLOSED** — 3 方法删除 + BLE001 noqa 标化 committed+pushed; 构造参数保留 vestigial (hot-path omega 约束, 下次 live_intent_loop.py 变更清理); 零僵尸测试

---

## CCT-20260722-002

- **Docket**: DQAF-20260722-002
- **Layer 1 (症状)**: 8/9 give-back positions (MFE≥1R, PnL≤0) labeled "loss" with zero exit signal provenance. p_win rolling_wr WR=41.2% vs brain_confidence WR=72.4%.
- **Layer 2 (传导)**: `position_close_adapter.py` label chain — watchdog→SL→TP→PnL fallthrough. For managed closes (bleed_stop, hesitation, time-based), the MT5 deal comment carries the exit reason set by dispatch_managed_close(), but the adapter ignores it and labels by PnL sign. `pwin_chain.py` resolve_p_win() rolling_wr step → resolve_p_win_from_brains() returns median win_rate regardless of aggregate sample count.
- **Layer 3 (根因)**: (P0) Adapter label assignment lacks comment-based signal preservation — PnL is not a causal signal. (P2) No sample-size degradation gate on rolling_wr source — Kelly chain receives noisy small-N estimates.
- **Fix**: FIX-20260722-002
- **Status**: **CLOSED**

## CCT-20260722-003

- **Docket**: DQAF-20260722-003
- **Layer 1 (症状)**: Ticket 4207155654 (h4_swing SHORT): +6.03R in entry_atr → SL never trailed from 4110.62 → gave back all profit → closed -$19.70. SL modification NEVER occurred throughout position lifecycle.
- **Layer 2 (传导)**: trail_dispatch.compute_and_dispatch_trail() → pm.compute_trail_stop() → TrailStopEngine.compute_trail_stop() → activation watermark check uses _resolve_geometry_atr() which returns bracket_atr (~57 for H4). At peak MFE, price move ≈30.85 cents → unrealized_r = 30.85/57 = 0.54 < trail_activation_atr=1.0 → return None (trail not activated). The ratchet floor (_ratchet_lock_r) correctly uses entry_atr and would have locked +2.0R, but it's gated behind the activation watermark.
- **Layer 3 (根因)**: Cross-TF ATR mismatch — FIX-20260709-004 (per-TF bracket_atr) correctly moved geometry distances to bracket_atr but also changed activation measurement. The activation threshold trail_activation_atr=1.0 was calibrated for entry_atr scale; using bracket_atr makes it 10× harder to reach for H1/H4.
- **Fix**: FIX-20260722-003
- **Status**: **CLOSED**

## CCT-20260730-011

- **Docket**: DQAF-20260730-011
- **Layer 1 (症状)**: BTC July 2026 journal PnL系统性偏离MT5真相。MT5经纪商报表: 426笔, PnL=+$26.86, WR=49.1%, PF=1.07。系统journal: PnL=-$140.89（偏差+$167.75）。53个票号级别PnL不匹配。`scripts/_diagnose_pnl_mismatch.py` stdout。
- **Layer 2 (传导)**: (C1) Bridge `order_send()`后立即`mt5.history_deals_get(position=ticket)`查询deal → MT5 deal清算为异步，`deal.profit`未填充 → Bridge回退到`msg_payload["pnl"]`(引擎中价估算`(mid-entry)*volume`)。`mt5_bridge_worker.py:820,1132-1136`。(C2) Bridge写入journal时无provenance标签(FIX-20260716-005前)，796/1226条(65%)`_pnl_status`缺失 → 无法区分verified vs estimated。`scripts/_diagnose_pnl_provenance.py` stdout。(C3) Engine `managed_close.py:75`注释"reconciliation corrects it later" — 但`known_open_tickets`在reconciliation运行前被清除(management_phase MIA/stale-clear路径直接`pop`)→ Reconciliation仅写9条(0.7%)vs Bridge 1217条(99.3%)。(C4) Journal dedup允许`_source=mt5_reconciliation` supersede但Reconciliation饥荒→ Bridge估算值永不被修正。
- **Layer 3 (根因)**: L3架构缺陷 — Journal PnL字段无Single Source of Truth。双写者(Bridge + Reconciliation)竞争写入同一字段，Bridge在deal.profit异步清算窗口中静默回退到中价估算并写入无provenance条目，Reconciliation修正路径因`known_open_tickets`提前清除而饥饿 → 99.3%的journal PnL值来自中价估算而非MT5权威数据。
- **Fix**: FIX-20260730-011 — Settlement Queue Isolation (委员会覆写): Bridge写`pnl=null`+`_pnl_status="pending_mt5_settlement"`; SettlementQueue三态隔离(`known_open_tickets→pending_settlement_tickets→settled`); Reconciliation消费`pending_settlement_tickets`通过`resolve_exit_deal()`轮询验证deal.profit; 四级超时上报(T1 5min→T2 1hr→T3 24hr→T4 terminal); 队列持久化+僵尸单告警; Journal dedup扩展权威来源白名单+null PnL自动被非null supersede。
- **Status**: **CLOSED**

### CCT-20260801-006
- **Docket ID**: DQAF-20260801-006
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 双塔每周期弹出 brain_alert `feature_dimension_mismatch expected: 37, got: 40` (live_launcher_20260801T032917Z.log, BTC_Expected_R_V4_SHORT + _LONG); btc_expected_r_m15 在 golden_master 9 条记录全 neutral, voter_count=0, confidence=0.0
  - [Layer 2 — 中间异常]: feature_assembler.py:92-97 路由条件含 swing_enhanced/daily_swing/btc_macro/btc_h1 但不含 btc_expected_r → 落入 :108-115 fallback 返回原始 40-dim V9 向量 → lightgbm_brain_adapter.py:153 维度守卫 (num_feature=37) 打回零向量 → 双塔零投票
  - [Layer 3 — 根因]: L2 逻辑缺陷 — btc_expected_r_37 仅注册于 SCHEMA_DIMENSIONS (FIX-20260731-004) + FeatureService._IMPLEMENTED_SCHEMAS (FIX-20260801-001), 漏同步 6 处分发点: feature_router SCHEMA_CONTRACTS 缺注册 + build_lake Source 7 对子集 schema 按位置 zip 41-dim (29/37 偏移) + live_cycle:4841/management_phase:478 路由条件 + management_phase:494/swing_strategy:103 btc_augment gating + swing_strategy _needs_daily
- **证据引用**:
  - Source 1: [日志] data_btc/logs/live_launcher_20260801T032917Z.log — brain_alert feature_dimension_mismatch expected=37 got=40 双塔每周期
  - Source 2: [代码路径] core/features/feature_assembler.py:92-97 路由条件 → :108-115 V9 40-dim fallback → core/brains/adapters/lightgbm_brain_adapter.py:153 维度守卫
  - Source 3: [复现脚本] scripts/_verify_expected_r_routing.py — build_lake 子集 zip 29/37 misaligned (XAUUSDc_return 取到 slot 8 而非 slot 12); 修复后 0/41
- **是否被推翻**: 否
- **关联 ReB Pattern**: SCHEMA_ROUTING_MISSING_NEW_SCHEMA

### CCT-20260801-008
- **Docket ID**: DQAF-20260801-008
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 重启后 live_launcher_20260801T060541Z.log 8 类告警: Freshness Contract 3 VIOLATION / exit_config_validation_warning ev_trajectory_enabled ×5 策略线 / SSOT drift M15+V4_LGB / startup_integrity missing artifact_hash(3) + artifact_hash mismatch(H1_V2,V12) / conformal_ou_gate disabled / 7 python 进程
  - [Layer 2 — 中间异常]: (a) strategy_config_validator.py:14 `_EXPECTED_EXIT_KEYS` 缺 ev_trajectory_enabled 但 management_phase.py:1853 实际读取 → 5 条 BTC 策略线 ev_trajectory_enabled:false 全误报; live_intent_loop.py:442 从 live_cycle.py:797 旧重复副本导入 (Strangler Fig #22 提取后 caller 未迁移); (b) catalog.py:508 validate_freshness_contract 将 telemetry 产物 (EXECUTION_STATE 30min/MT5_BRIDGE_HEALTH 15min/ALERT_COOLING 2h) TTL 与 daily_ops batch max_age 6h 比较 → 3 误报; (c) brain config artifact_hash 为空/过时/截断; (d) M15 config=retired 但 governance 残留 probation
  - [Layer 3 — 根因]: L2 分类学错误 + L1 副本残留 — (a) validator 白名单滞后运行时消费 key 且 strangler fig 提取后 caller 未迁移 (同逻辑双文件, Iterability 违规); (b) 实时遥测产物 TTL 强行套用批处理产物 freshness contract; (c) artifact_hash 无完整 64 位校验准入 (幽灵更新风险); (d) 治理状态未随 config 退役同步
- **证据引用**:
  - Source 1: [日志] data_btc/logs/live_launcher_20260801T060541Z.log L21/25/27/29/31/47-49/75 — 8 类告警证据包
  - Source 2: [代码路径] core/runtime/strategy_config_validator.py:14 (白名单) vs core/runtime/management_phase.py:1853 (运行时读取); core/state/catalog.py:508-552 (contract) + L411/433/452 (telemetry TTL)
  - Source 3: [配置] configs/brains_btc/*.json artifact_hash 空/过时/16字符截断 vs 磁盘模型 sha256 (H1_V2 9f7e9d6c, V12 a4f9eb8cde8a9915... 前缀截断)
- **是否被推翻**: 否
- **关联 ReB Pattern**: WHITELIST_LAGGING_RUNTIME_KEYS, STRANGLER_FIG_CALLER_NOT_MIGRATED, TELEMETRY_TTL_VS_BATCH_CONTRACT

### CCT-20260801-010
- **Docket ID**: DQAF-20260801-010
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC_Swing_V4 自 07-09 起 live↔probation 振荡; governance transition_log 交替出现 `throttle live→probation` 与 `pnl:stable`/config-floor 拉回; 振荡期 V4 间歇性 vote_weight 惩罚 (0.5)。Brain_performance window-100 干跑实测: V4 41W/59L PF=0.695 (见 data_btc/brain_performance.json)。
  - [Layer 2 — 中间异常]: 三个独立治理政策对同一大脑给出矛盾结论 — (1) BrainPromotionEvaluator throttle (PF=0.695 < throttle_pf=0.80, **政策正确**); (2) daily_ops governance_scheduler.py pnl:stable (all-time PnL 健康) 拉回 live; (3) Iron Law #14 config floor (V4 config status=live) 启动 reconcile 拉回 live。双轨数据源是帮凶非真凶: live_intent_loop apply_promotion_decisions (BrainPnLStore last-20, FIX-20260611-001) + daily_ops 直写 (governance_scheduler.py:664 绕过 rule engine)。
  - [Layer 3 — 根因]: L3 架构缺陷 — **政策冲突无豁免机制**: 风控 throttle 与战略观察 (IC 8/3 终审) 无仲裁层。修复: SSOT 统一 (单一写入器) + Observation Hold (观察期豁免, 机器降级在人类战略观察窗口内显式让位)。
- **证据引用**:
  - Source 1: [状态] data_btc/governance_state.json BTC_Swing_V4 transition_log (throttle 与 pnl:stable 交替)
  - Source 2: [性能] data_btc/brain_performance.json BTC_Swing_V4 window-100 (41W/59L PF=0.695) — SSOT 干跑输出 `profit_factor(0.69) < 0.80`
  - Source 3: [代码] core/brains/services/brain_promotion.py:283-284 (throttle_pf=0.80) + scripts/training/governance_scheduler.py:664 (第三轨直写) + configs/brains_btc/BTC_Swing_V4.json (config floor=live)
- **是否被推翻**: 否
- **关联 ReB Pattern**: POLICY_CONFLICT_THROTTLE_VS_CONFIG_FLOOR, DUAL_TRACK_WRITER_OSCILLATION

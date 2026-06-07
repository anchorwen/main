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

### CCT-20260607-005
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

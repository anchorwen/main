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
- **修复** (FIX-20260607-XXX):
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
- **关联 FIX**: FIX-20260607-XXX

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
- **修复** (FIX-20260607-XXX):
  - (a) `live_cycle.py:886-888`: 独立 `min()` → `min(_all_m.values(), key=lambda m: m.cumulative_pnl)` 选择单一最差大脑，PnL 和 WR 同源
  - (b) `alert_channels.py:160-161`: `策略盈亏(USD)` → `最差大脑累计PnL(R)`, `策略胜率` → `最差大脑胜率`, 新增 `最差大脑ID`
  - (c) 新增 `_ctx["worst_brain_id"]` 使告警可溯源到具体大脑
- **证据引用**:
  - Source 1 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — strategy_degradation 告警上下文
  - Source 2 (Governance State): `data_btc/governance_state.json` — BTC_Swing_V4 pnl_r=-2171.86 vs 告警值 -2105.05
  - Source 3 (Source Code): `live_cycle.py:878-890` + `brain_pnl_ledger.py:53` + `alert_channels.py:160`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-009 (`frankenstein_metric_independent_min`)
- **关联 FIX**: FIX-20260607-XXX

---

### CCT-20260607-008
- **Docket ID**: DQAF-20260607-008
- **日期**: 2026-06-07
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: Phase A 焊死了价格 staleness 检测，但特征存储、Bridge 心跳、周期停顿三个组件仍处于 Fail-Open 状态——检测存在但仅发告警/记录，不阻断交易。
  - [Layer 2 — 中间异常]: 三个防线的"检测→告警"链路完整，但"告警→熔断"链路缺失。特征冻结时系统继续用过期特征推理；Bridge 断连时继续用旧价格评估。
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 残余)** — 告警 ≠ 熔断 的模式在三个子系统中重复出现。
- **修复** (FIX-20260607-XXX Phase B):
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
- **关联 FIX**: FIX-20260607-XXX

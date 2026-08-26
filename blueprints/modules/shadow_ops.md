# Shadow Ops — Phase 4 暗影接线蓝图 (The Wiring Blueprint)

> **状态**: ✅ IC 已批准 (2026-08-24) — 部署纪律三令锁定: 时间窗 18:01 北京时间 | 最小手术切口 (仅 live_cycle 单点调用 + live_order_sender 物理拦截) | 实证锁 (遥测快照 + 零穿透证明)
> **原稿**: ⚡ DRAFT v0.1 — 2026-08-24 → 投委会正式批准
> **战役**: Phase 4 (The Shadow Ops 暗影行动) — 实盘接线阶段, 风控 DEFCON 1
> **范围**: Micro Scaler v2 (入场排序模型) + MetaExit v3 (出场模型) → 实盘引擎实时消费 → 输出端**死焊**暗影遥测
> **铁律**: 绝对不允许向 mt5_bridge_worker 发送哪怕 0.01 手的真实订单 (IC 最高行动令)
> **实施窗口**: 北京时间 2026-08-24 18:01 (IC 指定), 前置条件 = IC 批准本蓝图

---

## Purpose

Phase 4 Shadow Ops 暗影接线 (The Wiring Blueprint) — 将 **Micro Scaler v2** (入场排序模型) + **MetaExit v3** (出场模型) 接入实盘执行引擎作为**纯观察者** (Shadow Mode): 实时消费真实 Tick/Bar 特征 → 每 cycle 计算实时 pred + Quantile Trigger → 输出端**死焊**暗影遥测 ledger。风控 **DEFCON 1**: 绝对零真实订单穿透至 `mt5_bridge_worker` (Air-Gap 三要素 + 三层防御)。

## Key Files

| File | Role |
|------|------|
| `core/runtime/shadow_ops/runtime.py` | `ShadowOpsRuntime` — 每 cycle 编排器 (scorer + trigger + telemetry), fail-open |
| `core/runtime/shadow_ops/micro_scaler_scorer.py` | `MicroScalerScorer` — LightGBM v2 加载 + 40-dim 断言 + isotonic clip 复刻 (`np.interp`) + D10 触发 → `ShadowOpsSignal` |
| `core/runtime/shadow_ops/trigger_contract.py` | `TriggerContract` — Quantile Trigger 契约 SSOT (启动加载 + 60s TTL 热刷新 + fail-closed VIOLATION) |
| `core/runtime/shadow_ops/telemetry.py` | `ShadowTelemetryLedger` — append-only JSONL 写入 (fail-open, BLE001) |
| `core/runtime/shadow_ops/dispatch_filter.py` | Layer-2 熔断: `shadow_ops_dispatch_filter(payload)` — stdlib-only, 函数级 import |
| `core/runtime/live_cycle.py` | Phase 4 特征计算后**单点注入** `ShadowOpsRuntime.run()` (最小手术切口) |
| `core/execution/live_order_sender.py` | `dispatch_live_order()` 入口 Layer-2 物理拦截 (venue=shadow_ops → filtered, 永不触 MT5) |
| `configs/live.yaml` | `shadow_ops:` 顶层段 (enabled + micro_scaler_v2 + meta_exit_v3) |
| `scripts/_shadow_ops_watchdog.py` | Layer-3 每日巡检 (liveness + zero-real-order + mandate 完整性) |
| `scripts/_audit_shadow_ops_liveness_probe.py` | 实证锁探针 (真实特征 → 真实预测 → 遥测 ledger) |
| `data/shadow_ops/*.jsonl` | 遥测 ledger (predictions / shadow_orders / dispatch_blocks), gitignored |

## Data Flow

```
FeatureService (V9_40, real ticks/bars) → ShadowOpsRuntime.run()  [live_cycle Phase 4 复用同一向量]
  → MicroScalerScorer.predict() → raw_pred_pct → isotonic clip (np.interp) → cal_pred_pct
  → |raw_pred| ≥ D10 (0.01867%, 动态读 trigger json; FIX-20260824-005 触发源 cal→raw) → triggered
  → ShadowTelemetryLedger (micro_scaler_predictions.jsonl + micro_scaler_shadow_orders.jsonl)
  → venue=shadow_ops / action=OBSERVE (双字段标记 — 任何下游见之必旁路遥测)
  → dispatch chain: shadow_ops_dispatch_filter() 物理拦截 → dispatch_blocks.jsonl → 永不 MT5
```

## Inbound Dependencies

| Dependency | Source |
|------|------|
| V9_40 特征向量 (40-dim canonical) | FeatureService (live_cycle Phase 4 复用同一份, 零额外 MT5 调用) |
| Micro Scaler v2 模型 | `data/training/micro_scaler_v2/micro_scaler_v2_reg.txt` (LightGBM Booster) |
| Trigger 规格 | `micro_scaler_v2_trigger.json` — Quantile Trigger, \|raw pred\|≥D10=0.01867% (FIX-20260824-005), mandate `FIXED_THRESHOLD_FORBIDDEN` |
| 校准曲线 | `micro_scaler_v2_reg_report.json` (isotonic calibration_curve) |
| 配置 | `configs/live.yaml` `shadow_ops:` 段 |
| 契约异常 | `core.contracts.exceptions.DataIntegrityError` (fail-closed) |

## Outbound Dependents

| Consumer | Interface |
|------|------|
| `data/shadow_ops/micro_scaler_predictions.jsonl` | 全量预测流 (每 cycle), 供 OOS ρ 实测 + 分布漂移监控 |
| `data/shadow_ops/micro_scaler_shadow_orders.jsonl` | D10 触发的 shadow order intent (永不派发, 供未来晋级评审) |
| `data/shadow_ops/dispatch_blocks.jsonl` | Layer-2 拦截审计 |
| `scripts/_shadow_ops_watchdog.py` | Layer-3 每日巡检读取 (只读 ledger) |
| ⛔ 零派发下游 | 绝不 export 至 exec_queue / dispatch_live_order / mt5_bridge_worker (DEFCON 1) |

## Known Issues

- 无已知运行缺陷 (G1-G4 回归锁 + 实证锁 2026-08-24 全绿)。
- 已知约束: `verify.py --full` pytest 全量 300s 上限 (仓库性, 非本模块缺陷)。
- 构造性约束 (设计): 评分器 import 黑名单静态断言 (zmq/mt5_bridge_worker/live_order_sender/communication_dispatcher/execution_queue/live_execution_contract/dispatch_context), 任何触线启动即拒。
- 相关债: TECH_DEBT-023 (Schema Mutagenesis) 由锚定 current-gen V9_40 规避; v2 训练链 SSOT = `build_micro_cost_model` 数据装配。

## Fix History

| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260826-003 | 2026-08-26 | cursor-agent | 5d84d2f5 | **FIX-20260826-003 (IC 开火令批准, DQAF-20260826-003): 熔断器双计根治 — Consumer-side 幂等去重**. `evaluate_drawdown` PASS 2 引入 `ticket_seen` set — 同一 position_ticket 双写重复 close 行只计首条有效 pnl 一次, 防重复累加. 病态: XAU journal twin-write (19 ticket × 2 相同行) 双计 → XAU -62.20 (真 -31.10) → 全局假 -76.58 → 假 breached → 派发链下次触发自动重写 flag → 放行失效. 真值: XAU -31.10 + BTC -14.38 = **-$45.48**, 余 **$4.52**, 未击穿. 回归锁 G10-G11 (twin-write dedup + distinct ticket 不误伤) + `scripts/_audit_breaker_dedup_verify_20260826.py` EXIT=0. | consumer 无幂等: 聚合未对 position_ticket 去重 |
| FIX-20260826-001 | 2026-08-26 | cursor-agent | 89461edb | **FIX-20260826-001 (IC 2026-08-26 Sev-1 热修复, DQAF-20260826-001): 熔断器双重失效根治 — Magic Nullification + 全局共享生死状**. (1) `evaluate_drawdown` 改 position_ticket 反查 open 记录继承真实 magic (MT5 Deal IN/OUT close 顶层 magic=0/None 陷阱), 无匹配 open 回退 close.magic — 单遍流式. (2) 全局生死状: `LIVE_FIRE_BASE_DIRS=("data","data_btc")` 单点扩展点 + `aggregate_live_fire_drawdown` + `live_fire_pool_for` (生产全局池 / 隔离测试单树池). (3) `live_cycle` 派发前置先查 flag 再 aggregate; 全局 PnL<=-$50 → 双树双写 `write_breaker_flag` → fail-closed 拦截. 投委会定性: 生死状全局共享 (XAU -$31.10 与 BTC -$15.96 并入同一 $50, 真实余量 $2.94). 回归: 全局聚合 -$47.06 (62 单) 未击穿 + MT5 magic=0 陷阱用例 PASS. | L3 — 熔断 flag 无生产写入链路 + close.magic 字段错配读 0 (双重失效) |
| FIX-20260824-005 | 2026-08-24 | cursor-agent | 9d2d2fd0 | **FIX-20260824-005 (IC 裁决, 点火前校准): 敢死队执行结构 + 触发语义双修正**. (1) 对称 1×ATR 括号 (SL=TP=1×ATR, RR=1.0): 原 TP=pred 1.0×(~\$1.41) vs SL=2×ATR(~\$13.19) → RR=0.107, 盈亏平衡 WR 90.3% >> OOS 59.46% → EV=-\$4.51/单 (被证伪). dispatcher `tp_pred_mult`→`tp_atr_mult`, config/runtime 键同步默认 1.0. (2) 触发源 cal→raw \|pred\|: Isotonic 平坦区实测触发率 75.6% vs 设计 9.89% → 触发判定改 \|raw\|>=D10, 阈值重导 raw p90=0.01867% (emit 脚本, 池内 10.40%). 规范 mode 固化 `quantile_top_decile_abs_raw_pred` (validator/scorer/watchdog/测试/蓝图同步; 旧 cal 系 trigger.json 构造即 VIOLATION fail-closed). `build_trigger_spec` 强制 raw_p90, train 不再自落 trigger (emit 唯一生产者). G8 对称括号 + G9 raw 触发不变量回归锁. | IC 裁决 — 结构 RR=0.107 负期望 + 校准平坦区触发率 8 倍膨胀 (被脚本证据证伪) |
| FIX-20260824-004 | 2026-08-24 | cursor-agent | db541864 | **Phase 4.1 Live Fire 敢死队 (投委会方向 B 裁决 2026-08-24 — 真实闭环破局)**: Micro Scaler v2 从纯观察者晋升**单点真实执行** — D10 触发 + Trigger 契约 OK 时, `live_cycle` 模块级函数 `_dispatch_live_fire_micro_scaler` 直接 `dispatch_live_order` (旁路 strategy_line / GodsEye health veto / MetaExit veto / Shadow Veto, 仅敢死队 magic 专属). 物理拆除保护伞, **保留止血带**: 熔断器 `core/runtime/shadow_ops/live_fire_breaker.py` (生死状 max_drawdown_usd=$50, journal 事件溯源 magic=90601 聚合, flag OPEN 即 fail-closed 永久停火) + 单笔 SL/TP (实时 ATR ×2.0 / 预测幅度, 双下限取 max) + 同向冷却 1500s + 有持仓不叠加 + dispatch 链 protection flag / MAX_ALLOWED_LOT_SIZE (下游). 新 magic 90601 `micro_scaler_v2_live_fire` (strategy_magic, 不注册 strategy_line). `configs/live.yaml` shadow_ops.micro_scaler_v2.live_fire 段 (默认 enabled:false — 部署安全, 投委会点火才翻). runtime 暴露 `live_fire_enabled`/`live_fire_config` 只读投影. 每 cycle fail-open (派发故障留痕不打断实盘). tests G5-G8 回归锁 13 (熔断器/开关语义/止血带/真实派发). **§8 红线 3 单点豁免声明**: 仅 `micro_scaler_v2_live_fire` (magic 90601) 例外, 其余 shadow 信号仍零派发. | IC 裁决 — 收盘几乎不能触发, 影子收集不解决根本问题, 方向 B 真实闭环是唯一生路 |
| FIX-20260824-003 | 2026-08-24 | cursor-agent | c304ac3a | **Phase 4 Shadow Ops 暗影接线 (DEFCON 1, IC 批准 blueprint shadow_ops.md)**: 新 `core/runtime/shadow_ops/` 六模块 (Quantile Trigger 契约 D10=0.06007% 动态读 + LightGBM v2 评分 isotonic clip + 遥测死焊 data/shadow_ops/*.jsonl + Layer-2 派发链熔断) + `live_cycle` Phase 4 单点注入 `ShadowOpsRuntime` (每 cycle 复用同一 V9_40 真实特征向量, 零额外 MT5 调用, fail-open) + `live_order_sender` 入口 Layer-2 物理拦截 + `configs/live.yaml` shadow_ops 段 + `scripts/_shadow_ops_watchdog.py` Layer-3 每日巡检 + 实证锁探针. 实证: 真实 mt5_live V9_40 特征 0.005307 raw → 0.003197 cal → 遥测 ledger; 零穿透 25789 行实盘 journal 扫描 PASS; mandate 0.06007 OK; 构造性隔离 PASS. | RC-06 — contract-violation: 暗影策略无派发链物理熔断 |

## Phase 4.1 Live Fire 敢死队 (投委会方向 B 裁决 2026-08-24)

**背景**: 系统 30+ 天几乎零真实开单 → 无真实标签 → 无法验证/改进 → 更不信任 → 门禁更严的死循环。
投委会裁决: **全面启动方向 B (真实闭环)** — 选 XAU Micro Scaler v2 为敢死队, 物理拆除全局保护伞,
签生死状 (max drawdown = 5000 美分 = $50, 3 个月不拔插头)。

**执行语义** (与 DEFCON 1 的共存):
- D10 触发 + Trigger 契约 OK → 敢死队真实派发 (不再是 shadow order)。这是 §8 红线 3 的**唯一 IC 豁免**。
- 旁路: strategy_line / GodsEye 0.55 health veto / MetaExit intervention / Shadow Veto — 物理拆除。
- 保留止血带 (绝不可拆, 见下表): 熔断器 / SL+TP / 同向冷却 / 有持仓不叠加 / 下游 protection flag。

| 止血带 | 实现 | 语义 |
|---|---|---|
| 🩸 生死状熔断器 | `live_fire_breaker.py` `evaluate_drawdown()` — journal 事件溯源 (magic=90601 + action=close + pnl 非空) 累计已实现 PnL ≤ −$50 → `live_fire_breaker.flag` (幂等, 保留首熔断时间) | flag 存在 = **fail-closed 永久停火**, 人工裁决删 flag |
| 🩸 单笔 SL/TP | `sl_dist=max(ATR×2.0, 0.05%×price)`, `tp_dist=max(pred×1.0, 0.03%×price)`, 实时 ATR/price | 单笔敞口物理封顶, 无实时价格/ATR 不开单 |
| 🩸 同向冷却 | `state._live_fire_last_open[side]` + `cooldown_seconds=1500` | 防同向连击 |
| 🩸 有持仓不叠加 | `broker.count_positions()>0 → skip` (查询失败保守跳过) | 防叠加 |
| 🩸 下游防线 | dispatch 链 protection flag + `MAX_ALLOWED_LOT_SIZE` blast limit + `SENTINEL_UNATTRIBUTED_MAGIC` (90601≠90401) | defense-in-depth |

**事件审计**: 每个决策点 (skip_breaker_open / skip_cooldown / skip_position_open / skip_no_price /
dispatch_error / dispatched) 结构化 JSON → stdout + `data/shadow_ops/live_fire_events.jsonl`
(Repairability: watchdog 可直接审计敢死队行为)。

**Layer 约束**:
- 敢死队派发逻辑在 `live_cycle.py` **模块级函数** (非 shadow_ops 包内) — Layer-1 import denylist 静态断言仍成立。
- 熔断器 `live_fire_breaker.py` 在 shadow_ops 包内但**仅 stdlib import** (与 denylist 兼容, 无派发能力)。
- runtime 只暴露 `live_fire_enabled`/`live_fire_config` 只读开关 — 配置解析, 不派发。

**点火流程**: `configs/live.yaml` → `shadow_ops.micro_scaler_v2.live_fire.enabled: true` → 重启 → 敢死队开火。
默认 **false** — 部署全程零真实派发风险。

## Cross-Module Contracts

- **Quantile Trigger 契约**: `trigger_mode == "quantile_top_decile_abs_raw_pred"` (FIX-20260824-005: 触发源 cal→raw |pred|) + `mandate` 含 `FIXED_THRESHOLD_FORBIDDEN`; VIOLATION → 拒出 shadow order (fail-closed), 预测遥测保留。**绝无 fixed threshold fallback** (IC 绝对红线)。旧 cal 系 mode/阈值 trigger.json 构造即 VIOLATION (阈值语义与 raw 判定不兼容)。
- **Air-Gap 三要素契约**: 生命周期计算点 (live_cycle Phase 4) / Intent Shadow 标记 (venue=shadow_ops + action=OBSERVE) / Dispatcher 物理拦截 (Layer-2 fuse at `dispatch_live_order` 入口)。
- **三层防御契约** (互为独立): Layer 1 构造性隔离 (import denylist 静态断言) / Layer 2 派发链熔断 / Layer 3 ShadowOpsWatchdog 每日审计。
- **DEFCON 1 红线**: 任何将 shadow 信号引向派发链的路径 = Sev 1 安全漏洞。Micro Scaler v2 不注册 brain/strategy line/contract group — 无 live 转换路径。
- **Live Fire 敢死队契约 (FIX-20260824-004, IC 豁免)**: `live_cycle` 每 cycle 经 `live_fire_enabled` + D10 信号决定派发; 熔断器 flag 存在 = fail-closed 永久停火; 单笔 SL/TP 永远在场; 同向冷却 + 有持仓不叠加; 派发失败留痕不打断 cycle。默认 disabled (config live_fire.enabled=false)。
- **MetaExit v3 防回退断言**: `management_phase.py` 必须保留 `meta_exit_shadow_telemetry` + `close NOT dispatched` 块, 误删 → 引擎启动即拒 (fail-closed)。

## Verification

- 单测: `pytest tests/test_shadow_ops.py` (G1-G4, 8 tests PASS) — 触发边界 / 构造性隔离 / 派发链熔断 / 端到端零派发。
- 静态: `python scripts/verify.py --quick/--full` (mypy/ruff/blueprint/imports/FIX_REGISTRY)。
- 实证锁: `python scripts/_audit_shadow_ops_liveness_probe.py` — 真实 mt5_live 特征 → 真实预测 → 遥测 ledger + watchdog 全绿 EXIT=0。
- 每日巡检: `python scripts/_shadow_ops_watchdog.py` (liveness STREAMING / zero-real-order / mandate 完整性)。

---

## 0. 战役定调

投委会终局裁决 (2026-08-24) 已豁免 Micro Scaler v2 的 [0.9,1.1] 校准斜率门禁并晋升 SHADOW
(FIX-20260824-002)。Phase 4 将其与 MetaExit v3 接入实盘执行引擎——**不是让它们交易, 而是让它们
在真实时间流里消费真实的 Tick/Bar, 计算实时预测与 Trigger, 输出端焊死暗影遥测**。

**方向 B 裁决 (FIX-20260824-004)**: 同日投委会加码 — 影子收集不解决"收盘几乎不能触发"的死循环,
全面启动**真实闭环**。Micro Scaler v2 晋升敢死队 (`micro_scaler_v2_live_fire`, magic 90601),
经 live_cycle 旁路分支真实派发, 以生死状熔断器 ($50) + SL/TP 兜底, 用真金白银换真实标签。
详见 §Phase 4.1。

风控 DEFCON 1 的语义: 任何一处把 shadow 信号引向派发链的路径, 都视为 Sev 1 安全漏洞。
蓝图以**双重独立气隙 + 可证伪的零真实订单断言**保证绝对隔离。

---

## 1. 现状资产盘点 (侦察证据)

| 资产 | 状态 | 证据 |
|---|---|---|
| Micro Scaler v2 模型 | `data/training/micro_scaler_v2/micro_scaler_v2_reg.txt` (LightGBM Booster) | `train_micro_scaler_v2.py:511` |
| Micro Scaler v2 Trigger 规格 | `micro_scaler_v2_trigger.json` — Quantile Trigger, \|raw pred\|≥D10=0.01867% (FIX-20260824-005) | `emit_micro_scaler_v2_raw_trigger.py` (raw_p90 重导, 唯一生产者) + 磁盘 |
| Micro Scaler v2 特征契约 | `V9_INSTITUTIONAL_40_FEATURES` (40 维 canonical, 严格全键断言) | `train_micro_scaler_v2.py:170-192` |
| Micro Scaler v2 运行时消费方 | **零** — 全库仅 emit 脚本与训练脚本引用 | §5.3 grep |
| MetaExit v3 模型 | `data/models/meta_exit_model_v3_xau.txt` / `data_btc/..._v3_btc.txt` (19-dim) | `path_defaults.py:38-40` |
| MetaExit v3 运行时 | **已接线 + 已 SHADOW** — telemetry only, 绝不 dispatch close | `management_phase.py:2110-2134` |
| 原生 shadow 抑制 | `strategy_line.py:1779-1781` (should_trade=False/volume=0.0/venue=shadow) + `strategy_evaluator.py:1273` (continue 不入队) | §2 |
| 物理派发链 | `exec_queue.flush` → `dispatch_live_open_order` → `CommunicationDispatcher.dispatch` → ZMQ PUSH → `mt5_bridge_worker` PULL → `_send_to_mt5` | §3.1 |
| 进程拓扑 | `live_launcher.py` → `mt5_bridge_worker.py`(唯一 MT5 边界) + `live_intent_loop.py`(LiveCycle 引擎) | `live_launcher.py:872/960` |
| 暗影遥测既有资产 | golden_master / brain_votes/{date} / v6_shadow_exits / meta_exit_snapshots 四路 JSONL (无单一 "shadow_telemetry" 管道名) | §2.4 |

---

## 2. Air-Gap 三要素 (投委会强制要求明确标注)

### 2.1 生命周期计算点 (Where is the model computed?)

**Micro Scaler v2 — 入场路径, 实时每 cycle 计算**:
- 接入点: `live_cycle.py` Phase 4 特征计算之后 (`L3216 feature computation, entry_context build`)。
- 评分器 `ShadowOpsRuntime.run()` 每 cycle 复用**同一份** `FeatureService` 产出的 V9_40 向量
  (零额外 MT5 调用, 零新 I/O 到派发链)。
- 推理: LightGBM Booster → `pred` (3-bar 前向收益 %)。启动时加载模型 + 维度断言
  (canonical 40, 名称集断言, DataIntegrityError 语义 — 严禁 dict.get 抹平)。
- 与策略评估链**零交互**: 评分的输出不进入 `StrategyLine.evaluate()` / `strategy_evaluator` /
  `exec_queue`。纯旁路观察者。

**MetaExit v3 — 出场路径, 已实时计算**:
- 已有: `management_phase.py:2078-2150` per-position 构建 `ExitFeatureSnapshot` (19-dim)
  → `position_manager.evaluate_meta_exit()` → `meta_exit_engine.evaluate()`。
- per-asset 模型加载已由 FIX-20260821-008 修正 (杜绝跨品种串台)。

### 2.2 Intent Shadow 标记 (How is the signal marked shadow?)

**Micro Scaler v2 — 产生 `ShadowOpsSignal`, 不是 StrategyDecision**:
```
ShadowOpsSignal(
  model_id="micro_scaler_v2",
  pred_pct: float,          # 模型原始输出 (%)
  abs_pred_pct: float,      # |pred|
  threshold_abs_pred_pct: float,  # 动态读自 trigger json
  triggered: bool,          # |pred| >= D10 阈值
  direction: "long"|"short"|"neutral",  # sign(pred); 未触发→neutral
  venue="shadow_ops",       # 标记: 永不进入 live 派发链
  action="OBSERVE",         # 标记: 永不 OPEN/REVERSE
  feature_ts_utc, cycle_count, model_version, ...
)
```
- 标记语义: `venue="shadow_ops"` + `action="OBSERVE"` 双字段 — 任何下游若见该标记必须旁路到遥测。

**MetaExit v3 — 已有标记**:
- `management_phase.py:2125-2134` → `_emit("meta_exit_shadow_telemetry", ..., action="BLOCKED — telemetry only, close NOT dispatched")`。
- 该标记已存在, 蓝图对其加**防回退断言** (见 §6.2)。

### 2.3 Dispatcher 物理拦截与旁路 (How intercepted & bypassed?)

**三层防御, 互为独立**:

```
 Layer 1 — 构造性隔离 (Micro Scaler v2 评分器)
 ─────────────────────────────────────────────
 ShadowOps 评分器仅允许 import: 特征 schema / model loader / telemetry writer / trigger spec。
 静态禁令 (import 黑名单): zmq, mt5_bridge_worker, live_order_sender,
 CommunicationDispatcher, execution_queue, live_execution_contract, dispatch_context。
 评分器没有任何函数能构造/发送订单。即使代码 bug, 最坏后果 = 写错一行遥测。
 ─────────────────────────────────────────────
 Layer 2 — 派发链熔断 (Dispatcher 物理拦截, 投委会点名要素)
 ─────────────────────────────────────────────
 在 canonical choke point 插入 shadow 过滤器:
   live_order_sender.py:166 dispatch_live_order() 入口第一道闸
   → shadow_ops_dispatch_filter(payload):
       若 payload 携带 venue="shadow_ops" / action="OBSERVE" / strategy 前缀 "shadow_ops_"
       → 物理拦截, 旁路写入 shadow 遥测 ledger, 返回 DISPATCH_FAILURE_REASON_SHADOW_OPS (永不触达 MT5)
       否则 → 放行 (零开销, 正常路径零影响)
 该过滤器是防御纵深: 现网 shadow 决策 (should_trade=False) 本就不会到达此点; 它保证
 即使未来某处错误地把 shadow 信号入队, 派发链也物理拒收。
 ─────────────────────────────────────────────
 Layer 3 — ShadowOpsWatchdog 可证伪断言 (每日巡检)
 ─────────────────────────────────────────────
 scripts/_shadow_ops_watchdog.py (Iron Law #11 审计脚本):
   (a) liveness: shadow ledger 在模型应触发时段有信号流, 无静默断流;
   (b) 零真实订单证明: live_trade_journal.jsonl + golden_master 中 ZERO 条带
       shadow_ops 策略归属的真实持仓/订单;
   (c) trigger json mandate 完整性: trigger_mode=="quantile_top_decile_abs_raw_pred"
       (FIX-20260824-005) 且 mandate 含 "FIXED_THRESHOLD_FORBIDDEN", 被篡改 → 告警 + 评分器 fail-closed。
 违规 → DingTalk 告警 (复用 monitor_dashboard 告警通道)。
 ─────────────────────────────────────────────
```

---

## 3. Micro Scaler v2 Quantile Trigger 契约 (投委会强制要求明确动态读取执行)

### 3.1 规格 SSOT

**Trigger 规格唯一来源 = `micro_scaler_v2_trigger.json`** (随训练报告落档, IC 部署令免重训)。
当前值 (emit 脚本 stdout 证据, Iron Law #11):

```
trigger_mode          : quantile_top_decile_abs_raw_pred   (FIX-20260824-005)
threshold_abs_pred_pct: 0.01867   (训练池 raw |pred| p90 = D10, emit 脚本重导)
trigger_rate_pct_oos  : 9.89     (D10 人口不变, Isotonic 单调; 池内 raw 触发率 ~10%)
direction_semantics   : sign(cal): LONG if cal>0 else SHORT; 触发基于 raw |pred|
mandate               : FIXED_THRESHOLD_FORBIDDEN — Quantile Trigger ONLY
```

### 3.2 动态读取 (The dynamic read)

1. **启动加载**: 引擎启动时读 trigger json, 加载 threshold / trigger_mode / mandate。
2. **TTL 刷新**: 每 `trigger_refresh_ttl_seconds` (设计默认 60s) 重读文件 mtime;
   未来重训重发射的 trigger (更高/更低阈值) **无需重启即生效** — 这是"动态"的落点。
3. **fail-closed 校验**: 若重读发现 `trigger_mode != "quantile_top_decile_abs_raw_pred"`
   (FIX-20260824-005) 或 `mandate` 字段不含 "FIXED_THRESHOLD_FORBIDDEN" → 评分器**拒绝产生 shadow order**
   (保留预测遥测), 输出 `shadow_ops_trigger_contract_violation` 事件 + 告警。
   **绝不允许 fallback 到任何固定阈值** — 这是 IC 绝对红线。

### 3.3 每 cycle 触发逻辑 (纯函数, 可单测)

```
raw       = model.predict(X_v9_40)                    # % 原始 3-bar 前向收益
cal       = isotonic_interp(raw)                      # 校准 (仅方向/量纲, 不判触发)
triggered = abs(raw) >= threshold_abs_pred_pct        # D10 判定 (raw 基底, FIX-20260824-005)
direction = "long"  if triggered and cal > 0
          = "short" if triggered and cal < 0
          = "neutral" otherwise                        # 未触发 → 无 shadow order
decile_est = 10 if triggered else (1..9 by |raw| 桶)   # 仅诊断
```

### 3.4 触发率预算监控

OOS 触发率 9.89% 为基线。实盘触发率漂移 (>3σ 或连续 N 周期 0 触发) → 告警
(潜在特征分布漂移 / 模型退化 / 遥测断流三类原因分流诊断)。

---

## 4. 遥测管道设计 (The Shadow Telemetry Pipe)

新管道名: **`shadow_ops`** (统一 IC 语义; 既有 golden_master/brain_votes 等仍独立运行, 不合并)。

```
data/shadow_ops/
├── micro_scaler_predictions.jsonl     # 全量预测流 (每 cycle, 供 OOS ρ 实测 + 分布漂移)
└── micro_scaler_shadow_orders.jsonl   # 仅 D10 触发的 shadow order intent (供未来回测/晋级评审)
```

预测流 schema (append-only, JSONL, UTC 时间戳):
```json
{
  "event": "micro_scaler_prediction",
  "time_utc": "...", "symbol": "XAUUSDc",
  "model_id": "micro_scaler_v2", "model_version": "v2_20260824",
  "pred_pct": 0.0088, "abs_pred_pct": 0.0088,
  "trigger_threshold_pct": 0.01867, "trigger_mode": "quantile_top_decile_abs_raw_pred",
  "triggered": false, "direction": "neutral",
  "feature_schema": "v9_institutional_40", "feature_ts_utc": "...",
  "cycle_count": 12345, "venue": "shadow_ops", "action": "OBSERVE"
}
```

Shadow order schema (triggered=true 时追加):
```json
{
  "event": "micro_scaler_shadow_order",
  "time_utc": "...", "symbol": "XAUUSDc",
  "model_id": "micro_scaler_v2", "model_version": "v2_20260824",
  "pred_pct": 0.07537, "abs_pred_pct": 0.07537,
  "trigger_threshold_pct": 0.01867, "triggered": true,
  "direction": "long", "decile_estimate": 10,
  "feature_schema": "v9_institutional_40", "feature_ts_utc": "...",
  "cycle_count": 12345, "venue": "shadow_ops", "action": "OBSERVE"
}
```

MetaExit v3: **复用既有** `meta_exit_snapshots.jsonl` (position_manager.py:1712) +
`v6_shadow_exits.jsonl` — 不新建重复管道 (Iterability 铁律: 同逻辑不分散)。

---

## 5. 接线切口清单 (待 IC 批准后于 18:01 实施)

| # | 文件 | 改动类型 | 说明 |
|---|---|---|---|
| 1 | `core/runtime/shadow_ops/__init__.py` | 新建 | 模块声明 + import 禁令文档 |
| 2 | `core/runtime/shadow_ops/trigger_contract.py` | 新建 | Quantile Trigger 契约读取 (启动 + TTL + fail-closed 校验), 纯函数 |
| 3 | `core/runtime/shadow_ops/micro_scaler_scorer.py` | 新建 | 模型加载 + 维度断言 + 推理 + 触发判定 → ShadowOpsSignal |
| 4 | `core/runtime/shadow_ops/telemetry.py` | 新建 | `ShadowTelemetryLedger` append-only JSONL 写入 (fail-open, BLE001) |
| 5 | `core/runtime/shadow_ops/runtime.py` | 新建 | `ShadowOpsRuntime.run()` — 编排 2/3/4, 每 cycle 一次 |
| 6 | `core/runtime/shadow_ops/dispatch_filter.py` | 新建 | Layer-2 熔断: `shadow_ops_dispatch_filter(payload)` |
| 7 | `core/runtime/live_cycle.py` | 最小改 | Phase 4 后单点调用 `ShadowOpsRuntime.run()` (fail_open_guard 包裹) |
| 8 | `core/execution/live_order_sender.py` | 最小改 | `dispatch_live_order()` 入口插 Layer-2 过滤器 (单点) |
| 9 | `configs/live_xau.yaml` | 配置 | 新增 `shadow_ops:` 顶层段 (见 §6.1) |
| 10 | `scripts/_shadow_ops_watchdog.py` | 新建 | 审计脚本 (Iron Law #11, 只读 ledger) |
| 11 | `tests/` | 新建 | 见 §7 验收闸门 |

> 禁区执行: #7/#8 是唯一触及实盘核心的两处, 改动面=每处单点调用/单点闸, 零行为变化
> (正常路径零开销、零 I/O、零新线程)。全部改动在 IC 批准后才执行。

---

## 6. 配置与防回退断言

### 6.1 live_xau.yaml 新增段 (Scene C 配置, 随实施)

```yaml
shadow_ops:
  enabled: true
  micro_scaler_v2:
    model_path: data/training/micro_scaler_v2/micro_scaler_v2_reg.txt
    trigger_path: data/training/micro_scaler_v2/micro_scaler_v2_trigger.json
    feature_schema: v9_institutional_40
    telemetry_dir: data/shadow_ops
    trigger_refresh_ttl_seconds: 60
    import_denylist_enforced: true      # 静态检查: 评分器不得 import 派发能力模块
  meta_exit_v3:
    enforce_shadow_only: true           # 防回退断言: 启动时校验 management_phase shadow 块仍存在
```

**关键**: Micro Scaler v2 **不**注册 brain registry entry, **不**声明 strategy line,
**不**进 contract_groups — 彻底隔离出 live-capable 机制。任何"转 live"必须经 IC 新裁决。

### 6.2 MetaExit v3 防回退断言

启动时断言 (fail-closed): `management_phase.py` 存在 `meta_exit_shadow_telemetry` + 
`action="BLOCKED — telemetry only, close NOT dispatched"` 路径, 且无任何 dispatch close
调用可达该分支。若未来代码误删 shadow 块 → 引擎启动即拒绝 (DEFCON 1 语义)。

---

## 7. 验收闸门 (Acceptance Gates)

| 闸门 | 方法 |
|---|---|
| **G1 触发逻辑** | 单测: pred=0.06006 (未触发) / 0.06007 (边界触发) / -0.06008 (SHORT 触发); mandate 篡改→fail-closed |
| **G2 构造性隔离** | 静态断言: 评分器模块 import 集合 ∩ 派发能力模块 = ∅ (analyze_deps 可复用) |
| **G3 派发链熔断** | 集成测试: mock 一条 `venue="shadow_ops"` payload 注入 `dispatch_live_order` → 断言拦截 + 旁路写 telemetry + 零 dispatch 调用 |
| **G4 端到端** | 模拟 cycle 喂 V9_40 向量 → ShadowOpsRuntime → ledger 有行; 断言 `dispatch_live_open_order` mock 从未被调用 |
| **G5 零回归** | `verify.py --full` (mypy/ruff/blueprint/imports/artifact/FIX_REGISTRY) + `pytest` 全量 |
| **G6 部署后巡检** | `_shadow_ops_watchdog.py` 每日: liveness + zero-real-order 证明 + mandate 完整性 |

---

## 8. 明确禁区 (DEFCON 1 红线)

1. ⛔ 评分器 import 黑名单 (zmq / mt5_bridge_worker / live_order_sender / CommunicationDispatcher /
   execution_queue / live_execution_contract / dispatch_context) — 静态强制。**豁免**: 派发逻辑在
   live_cycle 模块级函数 (包外), 包内仅新增 stdlib-only 熔断器 `live_fire_breaker.py`。
2. ⛔ Micro Scaler v2 绝无 fixed threshold 触发路径 — `mandate` 校验 fail-closed。
3. ⛔ 不注册 brain / strategy line / contract group — 无晋升机制, 无 live 转换路径。
   **FIX-20260824-004 单点豁免 (IC 裁决 2026-08-24 方向 B)**: 仅 `micro_scaler_v2_live_fire`
   (magic 90601) 经 live_cycle 敢死队分支真实派发 — 物理拆除全局保护伞 (旁路 strategy_line/
   GodsEye/MetaExit/veto), **保留生死状熔断器 + SL/TP + 同向冷却 + 有持仓不叠加**。
   其余 shadow 信号仍零派发 (DEFCON 1 语义不变)。
4. ⛔ MetaExit v3 绝无 dispatch close — 防回退断言。
5. ⛔ shadow_ops 遥测绝不写入 live_trade_journal / golden_master (独立 ledger, 防污染实盘分析)。
   **豁免**: 敢死队真实派发单 (magic 90601) 经正规 journal 通道入账 — 熔断器事件溯源聚合依赖此账。

---

## 9. 四维闸门评估 (设计稿)

| 维度 | 评估 |
|---|---|
| Stability | → 纯增量旁路评分器 + 派发链单点闸; 不开新线程/新进程/新 IPC; 正常路径零开销 |
| Repairability | ↑ 每 cycle 结构化事件 (pred/trigger/venue), 未来 OOS ρ 实测直接读 ledger 定位 |
| Decoupling | → 新模块 self-contained; live_cycle 单点调用 + live_order_sender 单点闸; 无新跨层 import 链 |
| Iterability | ↑ Trigger 契约单一来源 (trigger json + trigger_contract.py), 未来 v3 重训免改引擎 |

---

## 10. 实施前置条件

1. **IC 批准本蓝图** (本文件定稿)。
2. 批准后于**北京时间 2026-08-24 18:01** 执行接线 (调度窗口)。
3. 实施后 `verify.py --full` + G1-G5 闸门全绿 → 部署 → G6 每日巡检。

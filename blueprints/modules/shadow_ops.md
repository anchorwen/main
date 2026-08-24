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
  → |cal_pred| ≥ D10 (0.06007%, 动态读 trigger json) → triggered
  → ShadowTelemetryLedger (micro_scaler_predictions.jsonl + micro_scaler_shadow_orders.jsonl)
  → venue=shadow_ops / action=OBSERVE (双字段标记 — 任何下游见之必旁路遥测)
  → dispatch chain: shadow_ops_dispatch_filter() 物理拦截 → dispatch_blocks.jsonl → 永不 MT5
```

## Inbound Dependencies

| Dependency | Source |
|------|------|
| V9_40 特征向量 (40-dim canonical) | FeatureService (live_cycle Phase 4 复用同一份, 零额外 MT5 调用) |
| Micro Scaler v2 模型 | `data/training/micro_scaler_v2/micro_scaler_v2_reg.txt` (LightGBM Booster) |
| Trigger 规格 | `micro_scaler_v2_trigger.json` — Quantile Trigger, \|pred\|≥D10=0.06007%, mandate `FIXED_THRESHOLD_FORBIDDEN` |
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
| FIX-20260824-003 | 2026-08-24 | cursor-agent | — | **Phase 4 Shadow Ops 暗影接线 (DEFCON 1, IC 批准 blueprint shadow_ops.md)**: 新 `core/runtime/shadow_ops/` 六模块 (Quantile Trigger 契约 D10=0.06007% 动态读 + LightGBM v2 评分 isotonic clip + 遥测死焊 data/shadow_ops/*.jsonl + Layer-2 派发链熔断) + `live_cycle` Phase 4 单点注入 `ShadowOpsRuntime` (每 cycle 复用同一 V9_40 真实特征向量, 零额外 MT5 调用, fail-open) + `live_order_sender` 入口 Layer-2 物理拦截 + `configs/live.yaml` shadow_ops 段 + `scripts/_shadow_ops_watchdog.py` Layer-3 每日巡检 + 实证锁探针. 实证: 真实 mt5_live V9_40 特征 0.005307 raw → 0.003197 cal → 遥测 ledger; 零穿透 25789 行实盘 journal 扫描 PASS; mandate 0.06007 OK; 构造性隔离 PASS. | RC-06 — contract-violation: 暗影策略无派发链物理熔断 |

## Cross-Module Contracts

- **Quantile Trigger 契约**: `trigger_mode == "quantile_top_decile_abs_pred"` + `mandate` 含 `FIXED_THRESHOLD_FORBIDDEN`; VIOLATION → 拒出 shadow order (fail-closed), 预测遥测保留。**绝无 fixed threshold fallback** (IC 绝对红线)。
- **Air-Gap 三要素契约**: 生命周期计算点 (live_cycle Phase 4) / Intent Shadow 标记 (venue=shadow_ops + action=OBSERVE) / Dispatcher 物理拦截 (Layer-2 fuse at `dispatch_live_order` 入口)。
- **三层防御契约** (互为独立): Layer 1 构造性隔离 (import denylist 静态断言) / Layer 2 派发链熔断 / Layer 3 ShadowOpsWatchdog 每日审计。
- **DEFCON 1 红线**: 任何将 shadow 信号引向派发链的路径 = Sev 1 安全漏洞。Micro Scaler v2 不注册 brain/strategy line/contract group — 无 live 转换路径。
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

风控 DEFCON 1 的语义: 任何一处把 shadow 信号引向派发链的路径, 都视为 Sev 1 安全漏洞。
蓝图以**双重独立气隙 + 可证伪的零真实订单断言**保证绝对隔离。

---

## 1. 现状资产盘点 (侦察证据)

| 资产 | 状态 | 证据 |
|---|---|---|
| Micro Scaler v2 模型 | `data/training/micro_scaler_v2/micro_scaler_v2_reg.txt` (LightGBM Booster) | `train_micro_scaler_v2.py:511` |
| Micro Scaler v2 Trigger 规格 | `micro_scaler_v2_trigger.json` — Quantile Trigger, \|pred\|≥D10=0.06007% | `build_trigger_spec` (train L353-385) + 磁盘 |
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
   (c) trigger json mandate 完整性: trigger_mode=="quantile_top_decile_abs_pred"
       且 mandate 含 "FIXED_THRESHOLD_FORBIDDEN", 被篡改 → 告警 + 评分器 fail-closed。
 违规 → DingTalk 告警 (复用 monitor_dashboard 告警通道)。
 ─────────────────────────────────────────────
```

---

## 3. Micro Scaler v2 Quantile Trigger 契约 (投委会强制要求明确动态读取执行)

### 3.1 规格 SSOT

**Trigger 规格唯一来源 = `micro_scaler_v2_trigger.json`** (随训练报告落档, IC 部署令免重训)。
当前值 (emit 脚本 stdout 证据, Iron Law #11):

```
trigger_mode          : quantile_top_decile_abs_pred
threshold_abs_pred_pct: 0.06007   (OOS 历史样本 |pred| p90 = D10)
trigger_rate_pct_oos  : 9.89
direction_semantics   : sign(pred): LONG if pred>0 else SHORT (幅度排序器)
mandate               : FIXED_THRESHOLD_FORBIDDEN — Quantile Trigger ONLY
```

### 3.2 动态读取 (The dynamic read)

1. **启动加载**: 引擎启动时读 trigger json, 加载 threshold / trigger_mode / mandate。
2. **TTL 刷新**: 每 `trigger_refresh_ttl_seconds` (设计默认 60s) 重读文件 mtime;
   未来重训重发射的 trigger (更高/更低阈值) **无需重启即生效** — 这是"动态"的落点。
3. **fail-closed 校验**: 若重读发现 `trigger_mode != "quantile_top_decile_abs_pred"`
   或 `mandate` 字段不含 "FIXED_THRESHOLD_FORBIDDEN" → 评分器**拒绝产生 shadow order**
   (保留预测遥测), 输出 `shadow_ops_trigger_contract_violation` 事件 + 告警。
   **绝不允许 fallback 到任何固定阈值** — 这是 IC 绝对红线。

### 3.3 每 cycle 触发逻辑 (纯函数, 可单测)

```
pred      = model.predict(X_v9_40)                    # %
triggered = abs(pred) >= threshold_abs_pred_pct       # D10 判定
direction = "long"  if triggered and pred > 0
          = "short" if triggered and pred < 0
          = "neutral" otherwise                        # 未触发 → 无 shadow order
decile_est = 10 if triggered else (1..9 by |pred| 桶)  # 仅诊断
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
  "trigger_threshold_pct": 0.06007, "trigger_mode": "quantile_top_decile_abs_pred",
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
  "trigger_threshold_pct": 0.06007, "triggered": true,
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
   execution_queue / live_execution_contract / dispatch_context) — 静态强制。
2. ⛔ Micro Scaler v2 绝无 fixed threshold 触发路径 — `mandate` 校验 fail-closed。
3. ⛔ 不注册 brain / strategy line / contract group — 无晋升机制, 无 live 转换路径。
4. ⛔ MetaExit v3 绝无 dispatch close — 防回退断言。
5. ⛔ shadow_ops 遥测绝不写入 live_trade_journal / golden_master (独立 ledger, 防污染实盘分析)。

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

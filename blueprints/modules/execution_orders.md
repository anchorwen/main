# Execution / Orders

## Purpose
Order lifecycle management (creation → ack → fill → close), position tracking with multi-layer exits, dynamic SL/TP computation, capital allocation across strategy groups, and broker gateway abstraction.

## Key Files
| File | Role |
|------|------|
| `core/execution/order_state_machine.py` | `OrderStateMachine` — canonical state transitions |
| `core/execution/execution_manager.py` | `ExecutionManager` — order lifecycle, venue event processing |
| `core/execution/position_manager.py` | `ActivePositionManager` — 3-layer exits (Confidence Spring Chandelier, consensus flip, EV Trajectory sqrt time-exit) |
| `core/execution/meta_exit_engine.py` | `MetaExitEngine` — multi-factor exit urgency scorer |
| `core/execution/dynamic_sl_tp.py` | `compute_dynamic_sl_tp()`, `compute_sl_tp_levels()` |
| `core/execution/capital_allocator.py` | `resolve_conflicts()`, `compute_volume()`, `GroupCorrelationTracker` |
| `core/execution/live_order_sender.py` | `dispatch_live_order()` — broker-agnostic dispatch |
| `core/execution/execution_queue.py` | `ExecutionQueue` — staggered multi-strategy dispatch |
| `core/execution/quality_analyzer.py` | `SlippageTracker`, `ExecutionQualityAnalyzer` |
| `core/execution/broker_adapter.py` | `BrokerAdapter` interface |
| `core/execution/mt5_broker_adapter.py` | MT5 broker adapter implementation |
| `core/execution/fill_simulator.py` | `FillSimulator` — deterministic fill simulation |
| `core/execution/market_impact.py` | `estimate_market_impact()` — Almgren-Chriss model |
| `core/execution/kelly_sizer.py` | `compute_kelly_mult()` — fractional Kelly edge sizing with EV veto |
| `core/execution/correlation_sizer.py` | `apply_sqrt_n_discount()` — √N correlation decay for multi-strategy consensus |

## Data Flow
```
DecisionIntent → ExecutionQueue → dispatch_live_order() → BrokerAdapter
                                      ↓
                              OrderStateMachine (state transitions)
                                      ↓
                              ActivePositionManager (exit monitoring)
                                      ↓
                              ExecutionQualityAnalyzer (VWAP benchmarking)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | CommunicationEnvelope, ExecutionEvent | Message passing, event recording |
| contracts/enums | CommunicationMessageType, CommunicationPriority | Message typing |
| contracts/ids | new_execution_event_id | Event ID generation |
| deployment | EnvironmentConfig, ServiceContainer | Config and DI |
| observability | metric_names | Execution metrics |
| protocol | live_execution_contract, schema_versions | Message building |
| metrics | portfolio_optimizer | Capital allocation optimization |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | ExecutionQueue, ActivePositionManager | Live order execution |
| runtime/execution_pipeline | ExecutionQualityAnalyzer | Post-trade quality |
| deployment/lifecycle | ExecutionManager | Service wiring |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260523-004 | 2026-05-23 | cursor-agent | — | statarb_m15 counter-trend gate: _counter_trend_action() thresholds dict had no statarb_m15 entry, falling to generic default (block at H1≥0.40). Added dedicated entry mirroring statarb_dynamic's permissive thresholds (block at H1≥0.55, penalise at H1≥0.30, H4 block at 0.35). Mean-reversion IS counter-trend — blocking at H1≥0.40 would silence the M15 OU brain. | RC-09 (config-drift: new strategy not registered in threshold map) |
| FIX-20260522-001 | 2026-05-22 | cursor-agent | — | Net-out close confirmation blind spot: execution_queue.py else-branch treated empty intent_id as unconditional success, opening new positions against still-open opposing positions when ExitWatchdog failed. Now honours dispatched flag from _net_out_close_dispatch_fn. | RC-06 |
| FIX-20260523-001 | 2026-05-23 | cursor-agent | — | **P0: P(win) feedback loop** — p_win and kelly_mult added to trade journal. dispatch_live_open_order() gains p_win/kelly_mult params → execution_payload → mt5_bridge_worker.py extracts to journal record. Also entry_context (previously passed but never extracted). Enables precision-curve calibration: compare predicted P(win) against actual trade outcomes to find optimal Meta Filter threshold empirically. | RC-12 (missing-feature: feedback loop) |
| FIX-20260523-002 | 2026-05-23 | cursor-agent | — | **P1: OU z_entry harmonized at Optuna-validated 1.3**. Artifact (arb_params_v7.json) already had z_entry=1.3 (Optuna TPE, 300 trials), but strategy_line.py:680 hardcoded _z_entry=2.0 for statarb inflection gate — forming an effective bottleneck of max(1.3, 2.0)=2.0. Also fixed position_manager.py defaults (1.5→1.3). OU brain now uses consistent 1.3σ threshold across brain adapter, inflection gate, and position manager. | RC-09 (config-drift: artifact value diverged from code overrides) |
| FIX-20260522-004 | 2026-05-22 | cursor-agent | — | Journal confidence pipeline: dispatch_live_open_order() lacked confidence parameter, execution_queue flush() never passed decision.confidence, mt5_bridge_worker.py never extracted confidence/brain_votes from execution_payload. Full E2E wired. | RC-06 |
| FIX-20260522-020 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: `QueuedDecision` and `DispatchResult` in execution_queue.py converted to frozen dataclasses (`frozen=True`). `QueuedDecision.decision` typed as `StrategyDecision` from trading_contracts. dispatch_status semantic rule rename `protocol_validated`→`transport_delivered` propagated to tests, semantic rules, and disk baselines. v9_shadow SmokeTest assertions + rebuild-formal-baselines synced. | RC-06 |
| FIX-20260521-007 | 2026-05-21 | cursor-agent | — | Meta Pipeline injection test: test_meta_pipeline.py validates full Track 3 chain (Huber→LGB+MLP+Platt+Conformal) producing P(win) distribution 0.37-0.68 | RC-06 |
| FIX-20260521-003 | 2026-05-21 | cursor-agent | — | statarb_dynamic _counter_trend_action() 阈值启用：从全0.99(禁用)改为block 0.55/penalise 0.30 + H4 block 0.35/penalise 0.20。均值回归本质反向交易但强趋势中OU空头被碾碎(W/L 0.92 vs 多头1.23)。H4优先—higher-TF趋势更强不可逆。更新docstring反映新策略。 | RC-09 |
| FIX-20260519-018 | 2026-05-19 | cursor-agent | — | P1 regression: `_update_single_position()` M5 OHLC branch missing `r_now` assignment → `UnboundLocalError` at return statement when M5 bar available → management phase silently aborted → trail SL / breakeven / trail TP NEVER executed. SL frozen, breakeven_triggered never set. Added `r_now = self._compute_r_multiple(mid, ticket=ticket)` in M5 branch. Also added `management_phase_diag` JSON event before dispatch for future diagnostics. | RC-06 (contract-violation, regression) |
| FIX-20260519-017 | 2026-05-19 | cursor-agent | — | Four-Pillar Architecture (Pillars 1-3): (P1) M5 bar OHLC-calibrated extreme tracking — `_update_single_position()` uses bar high/low with bid/ask spread calibration instead of instantaneous bid/ask, with graceful degradation if M5 bar unavailable. Also updates `highest_r` from extreme-based R. (P2) Profit Pardon — `should_exit_hesitation()` grants 2× hesitation_cycles grace period when `highest_r >= 0.30` (position had meaningful profit but breakeven was missed due to sampling blind spot). (P3) `prev_r` added to `save_state()` serialization. | RC-06 (sampling-blind-spot, contract-violation) |
| FIX-20260519-015 | 2026-05-19 | cursor-agent | — | Gamma-parameterised EV trajectory envelope: replaces hardcoded sqrt curve with strategy-archetype power-law family (γ=0.5 breakout concave, γ=1.0 trend linear, γ=2.0 statarb convex). Removes hardcoded grace-period cliff (t_ratio<10%). Fixes override_min_r unused-parameter bug — YAML min_r_for_hold now defines envelope endpoint. ActivePosition gains strategy_name field for gamma dispatch. | RC-05, RC-06 |
| FIX-20260519-012 | 2026-05-19 | cursor-agent | — | Absolute SL Distance Floor + RR Guard: compute_dynamic_sl_tp()新增min_sl_distance(绝对价格距离保底,防止ATR塌陷时SL<点差无净呼吸空间)和min_rr_ratio(TP随SL保底同步拉伸维持最低盈亏比)。StrategyLineConfig透传+live_cycle.py全部11策略从YAML sl块接线。live.yaml为barrier_12bar/micro_3bar/statarb_dynamic设置min_sl_distance:8.0+min_rr_ratio:1.5 | RC-05 |
| FIX-20260519-011 | 2026-05-19 | cursor-agent | — | 周期感知分层出场架构 (Waves A-D): (A) timeframe自动缩放—YAML配置人类可读(hesitation_cycles=3在H1策略=3×H1 K线)，代码自动换算M5 bar数，config live.yaml所有策略+timeframe字段; (B) √t ATR法则—compute_dynamic_sl_tp()按√(timeframe_mult)缩放ATR，H1止损14→48.5 pips; (C) Meta Exit维度隔离—大周期仓位(≥H1)仅使用同级别共识，M5涟漪不惊扰H4货轮; (D) 方向坍塌模型回退—m30/h1/h4 swing禁用(enabled:false)，宏观偏见过拟合，重训前只保留m15_swing(有双向识别能力) | RC-05, RC-06 |
| FIX-20260519-010 | 2026-05-19 | cursor-agent | — | dispatch_live_open_order新增brain_votes参数→execution_payload→journal透传; execution_queue.flush新增brain_votes从decision透传; StrategyDecision新增brain_votes字段。三轨归因Track 3所需的brain_votes完整数据管道。 | RC-06 |
| FIX-20260519-008 | 2026-05-19 | cursor-agent | — | Global Directional Cooldown: PortfolioRiskController增加net_out_cooldown_seconds(默认600s)+last_net_out_timestamp/last_net_out_direction追踪。net_out强制平仓后记录被平仓方向,cooldown期间拦截该方向所有新开单(任意策略),阻断net_out→新开仓→反向net_out的死亡连锁。Cooldown检查在策略重复检查之后、总敞口检查之前执行。 | RC-12 |
| FIX-20260519-007 | 2026-05-19 | cursor-agent | — | Trail SL物理学增强: (1) 棘轮规则—compute_trail_stop()集成self.min_step硬门槛,long candidate≤current_sl+min_step不更新/short candidate≥current_sl-min_step不更新,杜绝跟踪拖尾抖动; (2) Confidence Spring调节减半—conf_adj ±0.6→±0.3, ±0.5→±0.25, 减少Layer-2情绪化放大; (3) min_step默认值0.005→0.15(15pip) | RC-05 |
| FIX-20260519-006 | 2026-05-19 | cursor-agent | — | 机构级参数校准Wave 1+3: (P1) barrier_12bar hesitation_cycles 2→4; (P3) breakeven_threshold_atr 1.0→1.5; min_sl_step 0.005→0.15 (15pip绝对防抖) | RC-05 |
| FIX-20260519-005 | 2026-05-19 | cursor-agent | — | PnL盲区根治: ExitWatchdog._build_close_payload()漏掉pnl字段→execute_exit()和_build_close_payload()添加pnl参数; ExecutionQueue net_out close payload支持pnl透传 | RC-06 |
| FIX-20260518-044 | 2026-05-18 | cursor-agent | — | Commit catch-up: execution_queue (close_dispatch_fn callback, direction field, net_out_ticket_update), exit_watchdog (L2 forced liquidation fallback), live_order_sender (_validate_ack_sl_tp canary, dispatched field), mt5_broker_adapter (close_position L2 method), mt5_bridge_worker (spin-wait trap fixes #1/#2). All previously documented under FIX-20260517-019/021/022 but never committed due to pre-commit blueprint check deadlock. | process-violation |
| FIX-20260517-021 | 2026-05-17 | cursor-agent | — | Phase 2: Bridge worker spin-wait confirmed SL/TP readback (陷阱一修正) + _validate_ack_sl_tp() canary upgrade warn→ERROR with ack receipt polling. | missing-error-handling, contract-violation |
| FIX-20260517-022 | 2026-05-17 | cursor-agent | — | Phase 3: 4 exit gaps wired to Watchdog + partial close new_ticket capture via POSITION_IDENTIFIER (陷阱二修正) + net-out upper-layer interception (陷阱三修正). ExecutionQueue DispatchResult extended with direction field. | missing-error-handling, contract-violation |
| FIX-20260517-019 | 2026-05-17 | cursor-agent | — | ExitWatchdog institutional refactor: (1) dispatch_live_order() returns "dispatched" key fixing contract mismatch; (2) L2 forced liquidation via MT5BrokerAdapter.close_position() on timeout/retry-exhaustion. | missing-error-handling, contract-violation |
| FIX-20260517-020 | 2026-05-17 | cursor-agent | — | Ack receipt SL/TP validation hook: _validate_ack_sl_tp() checks transport_metadata for SL/TP post-dispatch, warns if missing. Non-blocking — full validation deferred to Phase 2. | contract-violation |
| FIX-20260517-016 | 2026-05-17 | cursor-agent | — | brain_status_map pass-through: strategy_line.evaluate() now derives status_map from self.brains (pure in-memory: {brain_id: status}) and passes it to record_brain_votes(). Previously brain_status_map defaulted to None, causing all brain_votes.jsonl entries to show "unknown". Hot-path safe — no disk I/O. | contract-violation |
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260515-013 | 2026-05-15 | cursor-agent | — | Three-knife OU exit refactor: (1) Smart Entry inflection gate z_entry 1.5→2.0 + volume climax, (2) Drift Lock spatial re-entry lock after mean-drift exit, (3) Alpha Handoff OU→trailing-stop on profit+trend | missing-feature |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 4 E2: Enriched confidence rejection reason in strategy_line.evaluate() — format: `low_confidence_{value:.4f}_lt_{threshold}` so multi_strategy_eval logs show exact rejection gap. Previously reason was generic `low_confidence`. | RC-06 |
| FIX-20260518-038 | 2026-05-18 | cursor-agent | — | Single merge dispatch: trail SL + breakeven + trail TP merged into one modify_sltp per cycle (was 2-3 back-to-back → MT5 retcode 10006 rejections). Ticket param added to 12 position_manager methods for multi-position correctness. State path default unified (data/state/ → state/) across load/save/shutdown. | contract-violation |
| FIX-20260518-037 | 2026-05-18 | cursor-agent | — | Multi-position refactor: ActivePositionManager converted from single-position singleton to multi-position dict (ticket→ActivePosition). register_position() no longer blocks when a position already exists. Recovery iterates ALL MT5 positions. _execute_management_phase loops all positions. Backward-compat `_position` property returns primary. Save/load supports v2 multi-position format. | boundary-error |
| FIX-20260518-036 | 2026-05-18 | cursor-agent | — | Phase A+B: Confidence Spring (Layer-2 confidence_ema modulates Chandelier trail multiplier, ±0.6 range) + EV Trajectory Envelope (sqrt-law Alpha decay exit with grace period first 10% horizon + 0.5R tolerance floor) replacing linear time-decay phases | boundary-error |
| FIX-20260518-032 | 2026-05-18 | cursor-agent | — | Tier 2 Kelly/Edge sizing: `StrategyDecision` extended with `p_win`/`kelly_mult` fields. `evaluate()` computes fractional Kelly multiplier from MetaFilter P(TP|signal) or PnLStore rolling win rate. EV veto: kf≤0 → hard reject (should_trade=False, reason=negative_kelly_ev). `_compute_volume()` keeps Tier 1 vol-targeted sizing only; Kelly applied at call site. | missing-feature |
| FIX-20260514-003 | 2026-05-14 | cursor-agent | a4a1005 | Fixed raw_proposals UnboundLocalError: elif indentation error caused multi-strategy evaluation to be unreachable | type-confusion |
| FIX-20260513-001 | 2026-05-14 | cursor-agent | a4a1005 | PnL recording moved before approval gate: each proposal gets isolated PnL record to prevent missing ledger entries | state-leak |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: 6 execution pipeline files. Previously registered as FIX-20260518-044, FIX-20260517-019/021/022, FIX-20260518-037/038. | process-violation |
| FIX-20260519-003 | 2026-05-19 | cursor-agent | — | New file: correlation_sizer.py — Tier 3 sqrt(N) correlation discount for same-direction strategy volumes. Previously registered as FIX-20260518-033. | missing-feature |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OrderStateMachine.transition(current, event)` → `OrderState` | ExecutionManager | Stable |
| `dispatch_live_order(envelope)` → `bool` | ExecutionQueue | Stable |
| `ActivePositionManager.evaluate_exits(market_data)` → `list[ExitEvaluation]` | live_cycle | Stable |
| `compute_dynamic_sl_tp(atr, regime)` → `DynamicSLTP` | strategy_line | Stable |

## Evolution Roadmap (开单/止损/止盈 机构化路线图)

> **状态**: Phase 1/2/3 已全部交付 (FIX-20260517-018 ~ 022)。仅剩 Phase 4 动态 SL/TP 校准。

| Phase | 范围 | 关键改动 | 依赖 |
|-------|------|----------|------|
| **Phase 2** | Ack receipt 完整化 | bridge worker 补全 ack receipt SL/TP 字段 → `_validate_ack_sl_tp()` 从 warn 升级为阻断 (偏差 > 0.5 pip 拒绝) | bridge worker 改动 (C++/Python) |
| **Phase 3** | ExitWatchdog 实盘集成 | 确认所有出场路径 (bleed_stop, Z-reversion, consensus_flip, time_decay) 都经过 Watchdog.execute_exit()；Watchdog 健康监控接入 daily_ops | live_cycle.py 出场段审查 |
| **Phase 4** | 动态 SL/TP 校准 | 每策略基于已实现波动率自适应调整 SL/TP 乘数；策略级 ref_atr 自动更新；MetaExitEngine 与 Watchdog 集成 | Phase 3 完成 + 至少 100 笔实盘出场记录 |
| **Phase C** | 微结构部分止盈 (DEFERRED) | VPIN/订单簿深度驱动的部分止盈决策；流动性不足预警；OIM 代理指标先行验证 | Phase A+B ≥30 笔完整出场 + VPIN/订单簿数据源就绪 |

### Phase 2 详细说明 ✅ 已交付 (FIX-20260517-021)
- **已完成**: bridge worker `_mt5_market_open()` order_send 成功后自旋等待（5次×100ms）MT5 Positions Pool 同步，读回 `confirmed_sl`/`confirmed_tp` 写入 ack receipt
- **已完成**: `_validate_ack_sl_tp()` 灰度升级：实际轮询 ack receipt（5s 超时），偏差 > 0.5 pip 记录 ERROR 日志。灰度期不阻断，收集 50+ 笔数据后开启阻断
- **受益**: 消除静默 SL/TP 设置错误风险（历史上发生过 SL 设在入场价上方导致立即止损）

### Phase 3 详细说明 ✅ 已交付 (FIX-20260517-022)
- **已完成**: 审查并修复 4 条出场旁路 — partial TP（陷阱二：ticket 更迭）、force_close_dd v3、legacy dd（死代码标记）、net-out（陷阱三：上层拦截回调）
- **已完成**: bridge worker 部分平仓后通过 POSITION_IDENTIFIER 锚定新 ticket，自旋等待后写入 receipt detail
- **已完成**: ExecutionQueue.flush() 新增可选 `close_dispatch_fn` 回调参数，live_cycle 上层注入 Watchdog 包装
- **受益**: 所有出场都有 5 次重试 + L2 强平保护，不再有静默失败的出场

### Phase 4 详细说明
- **问题**: 当前 SL/TP 乘数 (`base_sl_atr_mult`, `base_tp_atr_mult`) 是静态配置，不随波动率环境变化
- **操作**: 引入 `AdaptiveSLTP` 类，根据滚动窗口已实现波动率与 ref_atr 的比值动态调整乘数；MetaExitEngine 输出接入 Watchdog 作为出场信号源之一
- **受益**: 高波动期自适应放宽止损避免震荡出局，低波动期收紧止盈锁定利润

### Phase C 详细说明 ⏸️ DEFERRED (条件未成熟)

> ⚠️ **提醒机制**: 此 Phase 写入蓝图时设置了自动提醒。触发条件满足时 (`memory/phase_c_microstructure_reminder.md`) 会提示推进。触发条件见下方"推进闸门"。

- **问题**: 当前出场逻辑 (Confidence Spring + EV Trajectory Envelope + MetaExitEngine) 无法感知订单簿微观结构。在流动性枯竭时，部分止盈应该提前触发以降低滑点损耗；在深度充足时，应该让利润奔跑。
- **目标**: 引入基于 VPIN (Volume-synchronized Probability of Informed Trading) 和订单簿深度的部分止盈决策模块 (`MicrostructurePartialTP`)。
- **操作**:
  1. 计算 VPIN 指标 (tick-volume bucketed by time, 50-bucket rolling window) 作为逆向选择概率代理
  2. 从 Bridge 获取买一/卖一挂单量 (bid_volume, ask_volume)，计算加权深度
  3. VPIN > 0.8 (高位) + 深度 < 2x 平均深度 → 流动性枯竭预警 → 提前部分止盈 (partial_close_ratio=0.5)
  4. 若无 VPIN 数据: 使用 OIM (Order Imbalance Metric) 作为价格驱动代理 — `(bid_vol - ask_vol) / (bid_vol + ask_vol)`
- **受益**: 流动性枯竭时降低持仓风险暴露；深度充足时减少过早止盈的概率

**推进闸门 (所有条件必须满足)**:
1. ✅ Phase A (Confidence Spring) + Phase B (EV Trajectory Envelope) 实盘验证 ≥30 笔完整出场记录
2. ✅ MetaExit 模型文件就绪 (至少一个 live 状态模型)
3. ❌ VPIN 数据源可用 — 需要 tick-volume 按时间分桶数据 (当前无)
4. ❌ 订单簿深度数据可用 — Bridge 已支持 `market_depth` 事件但无持久化存储

**当前替代方案**:
- OIM (Order Imbalance Metric) 可从价差和成交量代理计算，不依赖真实订单簿
- 可作为 Phase C 第一步，VPIN/深度数据就绪后再升级

**建议复评日期**: 2026-06-15 (约1个月后，预计 ≥30 笔出场 + VPIN 数据源评估)

## Verification
```bash
python -m pytest tests/ -k "execution or order or fill" -q
```

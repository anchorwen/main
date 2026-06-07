# Execution / Orders

## Purpose
Order lifecycle management (creation → ack → fill → close), position tracking with multi-layer exits, dynamic SL/TP computation, capital allocation across strategy groups, and broker gateway abstraction.

## Key Files
| File | Role |
|------|------|
| `core/execution/order_state_machine.py` | `OrderStateMachine` — canonical state transitions |
| `core/execution/execution_manager.py` | `ExecutionManager` — order lifecycle, venue event processing |
| `core/execution/position_manager.py` | `ActivePositionManager` — 3-layer exits (Confidence Spring Chandelier, consensus flip, EV Trajectory sqrt time-exit) |
| `core/execution/trail_stop_engine.py` | `TrailStopEngine` + `TrailPolicy` — physically isolated Risk Exit subsystem (Chandelier trailing stop) |
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

- **KI-004: p_win 静默回退陷阱** (2026-05-26) — `resolve_p_win_from_brains()` 有三个静默回退路径全部返回 0.40 (FIX-20260526-031 从 0.50 降为 Fail-Closed)。三个 failure mode: (1) pnl_store is None, (2) 所有 brain sample_count < 10 冷启动守卫, (3) brain_id 不匹配 PnL store key。当前已加诊断日志区分三个路径。若 future p_win 再次静态出现, 搜索 `resolve_p_win` 诊断日志排查。min_p_win=0.45 (statarb) / 0.50 (barrier_12bar) → 0.40 低于两者 → 确保盲区信号被拒。
- **2026-05-29**: m15_swing/m30_swing counter_trend default-trap — 与FIX-20260523-004 (statarb_m15)相同模式：无`_counter_trend_action()`专属阈值→默认block=0.40→mild_trend中全部短线空头被硬阻断。Fixed by FIX-20260529-039 (Phase 2: strategy_line.py code change).

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260607-144 | 2026-06-07 | cursor-agent | — | **Entry/Exit timeframe alignment + btc_augment wiring**: H4 trend protection umbrella blocks M5 noise exits. adaptive bleed_stop (3→5 bars) + brain_flip shield. btc_augment threaded through evaluate()→_run_inference()→assemble_features_by_schema() for all strategy subclasses. Pending close lock prevents cross-cycle retry avalanche. | RC-06 |
| FIX-20260606-138-Phase3 | 2026-06-06 | cursor-agent | — | **DingTalk PnL + DispatchResult contract**: Added pnl/volume/price to DispatchResult. `_net_out_close_dispatch_fn` returns PnL. `notify_trade` receives actual PnL. Per-cycle dedup prevents notification storms. **Follow-up**: `_close_result` initialized to None before net_out branch to fix UnboundLocalError on open path. DQAF-006. | RC-06 → RC-05 |
| FIX-20260606-139 | 2026-06-06 | cursor-agent | — | **UCB elastic floor for p_win statistical freeze**: strategy_line.py Fail-Closed dead zone (0.40 < p_win < min_p_win) filled with confidence-derived elastic floor. `p_win = max(raw, floor - 0.05 + conf×0.10)`. Kelly auto-sizes micro-lot exploration. DQAF-004. | RC-05 |
| FIX-20260605-128 | 2026-06-05 | cursor-agent | d9d9f49 | **MT5BrokerAdapter.get_account_equity() added**: Broker equity fetch was failing (AttributeError) because adapter lacked the method. Added delegation to worker.account_info(). Eliminates fallback WARNING in live logs. | RC-06 |
| FIX-20260605-124 | 2026-06-05 | cursor-agent | — | **entry_spread journal pipeline fix**: strategy_line.py:1766 entry_context dict lacked entry_spread. bid/ask spread correctly computed but only fed pnl_ledger, never journal. XAU 0/777 + BTC 0/36 opens had entry_spread=0 permanently. Single-line fix. | RC-06 |
| FIX-20260605-123 | 2026-06-05 | cursor-agent | 6110bc6 | **Core test长城**: 16 TrailStopEngine tests (activation watermark, vol adjustment -0.5/+0.5, regime-based mult, breakeven, per-position TrailPolicy). 13 execution_state tests (save/load roundtrip, stale rejection, corrupt JSON, circuit breaker restore, SL streak preservation). | RC-12 |
| FIX-20260605-121 | 2026-06-05 | cursor-agent | 5892b3f | **Trail stop tests updated for FIX-064/071**: Trail activation watermark test (no trail before 1.0x ATR profit), vol adjustment tests updated for inverted logic (high vol -0.5 tightens, low vol +0.5 widens). Old +0.8/-0.3 delta model replaced. | RC-06 |
| FIX-20260605-120 | 2026-06-05 | cursor-agent | — | **Asset-specific reentry thresholds**: Reentry guard SL/bleed cooldowns and penalties now read from YAML per asset. XAU: 180s/0.10, BTC: 300s/0.15. Hardcoded BTC values no longer leak to XAU. | RC-09 |
| FIX-20260604-089 | 2026-06-04 | cursor-agent | — | **Brain vote recording swallow fix**: strategy_line.py `record_brain_votes()` exception no longer silently dropped — now `logger.warning`. | RC-07 |
| — (arch audit) | 2026-06-05 | cursor-agent | — | **PositionManager architecture roadmap**: 1,720-line class diagnosed — high essential cohesion, 10 exit types documented with evaluation hierarchy. Roadmap comment injected at class header. `_compute_weighted_fallback` confirmed as defensive safety net (not dead code). | RC-06 |
| FIX-20260603-064 | 2026-06-03 | cursor-agent | — | **Trail activation watermark**: trail_activation_atr=1.0, trail stays at initial SL until 1.0x ATR profit. | RC-05 |
| FIX-20260601-039 | 2026-06-01 | cursor-agent | — | **Feature Assembly Factory**: Created `core/features/feature_assembler.py` — central schema-driven factory. `BarrierStrategy._run_inference()` now uses factory (was hardcoded V9 40-dim → `Barrier_V9_12B_V1` got dimension mismatch forever). `SwingStrategy` refactored to use factory. TF close buffer + OU/Hurst computation extracted to `StrategyLine` base class. `_reorder_for_brain` restored passthrough when lengths differ (factory already assembled correct dim). | RC-06 |
| FIX-20260601-031 | 2026-06-01 | cursor-agent | — | **MT5Worker hardcoded symbol_select removed**: `MT5Worker.__init__` now accepts `symbol` param (default `"XAUUSDc"`). `_mt5_initialize` uses `self._default_symbol`. `live_intent_loop.py` passes `symbol=args.symbol`. | RC-09 |
| FIX-20260531-022 | 2026-05-31 | cursor-agent | — | **Swing strategy hardcoded schema check**: `swing_strategy.py:93` had independent if/elif checking only `swing_enhanced_35`. BTC schemas (29/21 dim) fell to `else` → `adapter.inference(daily_feature_vector)` → raw 24-dim vector → 8 rounds of adapter-level patches couldn't fix it because the wrong dimension was baked in upstream. Fixed: replaced with data-driven `assemble_swing_features()` matching live_cycle.py FIX-021. This was the 3rd hardcoded schema assembly point (after live_cycle.py ×2) — all 3 now unified. | RC-06 |
| FIX-20260531-008 | 2026-05-31 | cursor-agent | — | StrategyLineConfig: added symbol+contract_size + Defense 2 assertion | RC-09 |
| FIX-20260531-007 | 2026-05-31 | cursor-agent | — | MT5 bridge reconnect: `_reconnect_mt5` now accepts `symbol` parameter instead of hardcoding `symbol_select("XAUUSDc")`. Added `--default-symbol` CLI arg; live_launcher.py passes `cfg["symbol"]` from config. | RC-05 |
| FIX-20260530-068 | 2026-05-30 | cursor-agent | — | entry_features injection point: moved to strategy_line.py entry_context dict | RC-06 |
| FIX-20260530-065 | 2026-05-30 | cursor-agent | — | Phase 1: 40-dim V9 feature vector → journal entry_context on every open order. 3 data contract guardrails (schema_version, tuple immutability, NaN safety). Injected at both dispatch call sites. | RC-06 |
| FIX-20260530-059 | 2026-05-30 | cursor-agent | — | P2 entry_spread: strategy_line.py record_signal() now passes real ask-bid spread. All 4 call sites now consistent. | RC-06 |
| FIX-20260529-051 | 2026-05-30 | cursor-agent | — | Last Mile Protocol Phase 2: exit_watchdog.py LOG → log_and_continue(), strategy_line.py DEGRADE → FTC(DEGRADE), mt5_bridge_worker.py MT5 IPC → FTC(CRASH), market_ingress.py 4 MT5 IPC → FTC(CRASH). | RC-07 |
| FIX-20260529-049 | 2026-05-29 | cursor-agent | — | Architect Defense 2: jitter added to MT5 reconnect backoff sleep (random.uniform(0,1.0)s) in mt5_worker.py + mt5_bridge_worker.py — prevents synchronized retry bursts that trigger broker-side DDoS rate limiting. | RC-04 |
| FIX-20260529-048 | 2026-05-29 | cursor-agent | — | PR#3 Phase 2: strategy_line.py DynamicBrainWeighter DEGRADE (weight fallback→1.0 logged) + exit_watchdog.py position_verification LOG (MT5 verify failure logged before retry loop). | RC-07 |
| FIX-20260529-047 | 2026-05-29 | cursor-agent | — | Day 5+ Strangler Fig: extracted `_run_scheduled_daily_ops` (~155 lines) from live_cycle.py → core/runtime/daily_ops_scheduler.py. Thin 3-line delegation wrapper left in live_cycle.py. Orphaned _save_daily_ops_state removed. | RC-08 |
| FIX-20260529-046 | 2026-05-29 | cursor-agent | — | PR#4 SSOT State Slimming: position_manager.py save_state() v3 — 4 intent fields (cycles_held/breakeven_triggered/partial_tp_done/brain_consensus_hash) replacing ~27-field v2. MT5 authoritative for physical state. load_state() supports v1/v2/v3 backward-compat. live_intent_loop.py v3 recovery backfills physical fields from MT5 positions_get. | RC-06 |
| FIX-20260529-045 | 2026-05-29 | cursor-agent | — | PR#3 Layered Crash: (1) strategy_line.py — PnL record_signal silent pass→logger.debug with traceback. (2) exit_watchdog.py — 2× L2 forced liquidation failures: silent pass→logger.critical + alert.append. Both sites now capture exception type + full traceback. | RC-06, RC-07 |
| FIX-20260529-044 | 2026-05-29 | cursor-agent | — | PR#2 Reconnection & Zombie Defense: (1) mt5_bridge_worker.py — MT5 heartbeat every 30s + exponential backoff reconnect (1s→2s→4s→8s→16s→30s) + 5 failures→exit(1) + auto symbol_select. (2) mt5_worker.py — reconnect() backoff with retry counter+reset. (3) mt5_worker.py — command queue bounded (maxsize=1000, put_nowait + RuntimeError on Full). (4) mt5_worker.py — CB→AlertHub cross-propagation via send_critical("mt5_circuit_open"). _mt5_initialize() auto-selects XAUUSDc. | RC-04, RC-06 |
| FIX-20260529-043 | 2026-05-29 | cursor-agent | — | PR#1 MetaFilter fail-closed: filter() exception handler changed from passed=True/p_win=0.5 (fail-open, dangerous for risk gate) to passed=False/p_win=0.0 (fail-closed, trade blocked). Crash logged via logging.critical() with full traceback. | RC-07 |
| FIX-20260529-042 | 2026-05-29 | cursor-agent | — | Phase C 三刀手术 (Fix 1+2): (1) Hard multi-TF trend filter — H4+H1方向一致时对swing家族硬挡逆势信号 ("strategy_line.py" 4b gate). (2) 摩擦调整动态盈亏平衡点 — p_win gate改为 sl_dist/(tp_dist+sl_dist)+0.02 安全边际，替代静态 min_p_win. | RC-06 |
| FIX-20260529-039 | 2026-05-29 | cursor-agent | — | Swing zero-trade unfreeze (Phase 2): `_counter_trend_action()`添加m15_swing/m30_swing阈值(block=0.55/penalise=0.25)。Phase 1(config): live.yaml confidence_threshold 0.45→0.35, min_rr_ratio 1.0→0.85。 | RC-05, RC-09 |
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260529-038 | 2026-05-29 | cursor-agent | — | Max_Spread_Gate: StrategyLineConfig新增max_spread_points字段；evaluate() Gate 1b插入点差熔断门（current_spread>threshold→should_trade=False）；live.yaml m15_swing(msp=60)/m30_swing(msp=70)。替代被架构师否决的H12/H22硬编码时段方案。 | RC-06 |
| FIX-20260529-031 | 2026-05-29 | cursor-agent | — | FillSimulator slippage wiring: `apps/engine/cli.py` `PaperExecutionGateway(slippage_points=10)` — 10pt×0.01tick=0.10价格单位 ~0.5bps. | RC-06 |
| FIX-20260529-030 | 2026-05-29 | cursor-agent | — | SL/TP spread cost alignment: `live_cycle.py` 12个StrategyLineConfig全部添加`spread_points=_cfg()`←live.yaml. `compute_sl_tp_levels()`已有spread_points参数. Net_TP=Gross_TP-spread. | RC-06 |
| FIX-20260528-022 | 2026-05-28 | cursor-agent | — | swing_enhanced_35 live inference: `SwingStrategy._run_inference()` now assembles 35-dim vector for `swing_enhanced_35` brains — concatenates 24 daily + 9 micro + 2 TF-specific (OU_Theta, Hurst) computed from rolling close buffer. Legacy swing brains still receive 24-dim daily vector unchanged. Added rolling `_tf_close_buffer: deque[float]` (maxlen=25) for per-bar OU/Hurst computation. | RC-06 |
| FIX-20260527-010 | 2026-05-27 | cursor-agent | — | MT5Worker resilience hardening (Phase 1B+1C): (1B) Per-command execution tracking — `_command_in_flight`, `_last_command_start`, `_stuck_since` timestamps; `is_stuck()` method for hung-MT5 detection; fast-fail in `_submit()` when worker is stuck (avoids queuing into a blocked thread). (1C) CircuitBreaker wired into MT5Worker — 3 consecutive failures open circuit for 60s; `_submit()` checks `allow_request()` before queuing; `_run()` records success/failure per business command. Circuit breaker from `core/protocol/services/resilience.py` was previously defined but unused. | RC-04, RC-06 |
| FIX-20260527-006 | 2026-05-27 | cursor-agent | — | COLD phase deadlock: ConformalOU gate `passed=False` + `force_min_volume=True` triggered early return at line 673, making downstream COLD exploration bypass unreachable. Similarly MetaFilter statarb rejection returned at line 814 before cold explore check. Fix A: ConformalOU condition `not passed AND NOT force_min_volume`. Fix B: MetaFilter statarb checks `_last_ou_result.get("force_min_volume")` before rejecting. 22-cycle zero-trade deadlock resolved. | RC-05 |
| FIX-20260527-008 | 2026-05-27 | cursor-agent | — | OFI (Order Flow Imbalance) toxicity gate: computes real OFI from MT5 tick volume+flags with 100-bar rolling z-score. Hard blocks counter-trend statarb signals when OFI_Z > 2.0 (short direction) or OFI_Z < -2.0 (long direction). Gate sits BEFORE ConformalOU in priority — OFI is a standalone risk signal, not an ML feature, zero train-serve skew risk. | RC-12 |
| FIX-20260527-005 | 2026-05-27 | cursor-agent | — | Cold exploration trailing bypass: `StrategyDecision.cold_explore` field → `ActivePosition.cold_explore` → Layer 1 Chandelier skip. Forced exploration trades now run to hard SL or hard TP, collecting uncensored labels for ConformalOU online calibration. `trail_atr_mult_low` 1.2→1.8 for statarb_dynamic — mean-reversion in low vol needs WIDER trail (sticky noise decapitation). | RC-09, RC-12 |
| FIX-20260527-003 | 2026-05-27 | cursor-agent | — | Remove hardcoded brain ID references (3 files, 5 sites): `strategy_line.py` regression check now uses `training_contract` field exclusively instead of `_brain_id == "Meta_Stage1_Huber_V1"`. `bootstrap_v9.py` 3× fallback brain IDs replaced with direct key access. `online_feedback_hook.py` fallback removed. If config lacks required `brain_id` field, KeyError surfaces immediately instead of silently operating with wrong brain name. | RC-09 |
| FIX-20260526-041 | 2026-05-26 | cursor-agent | — | Entry precision deep fix (3-stage): (1A) COLD deadlock break via Forced Exploration Budget — `_is_cold_explore` flag bypasses min_p_win gate during COLD phase, p_win=0.50 neutral Kelly sizing, risk bounded by 0.01 lot cap. (1B) MetaFilter EXPERIMENTAL routing for statarb — z_score*12.5 as s1_prediction proxy through 48-dim LGB+Platt, with auto-kill-switch criteria (corr<0.05 for 2×50-trade eval periods). (1C) OU confidence→p_win monotonic fallback: p_win=0.40+conf*0.20 bounded in [0.40, 0.60]. (3A) p_win_source tracking in kelly_sizing JSON (meta_filter/rolling_wr/brain_confidence/cold_explore_neutral). Three-tier degradation chain: MetaFilter→PnLStore→confidence fallback. | RC-05, RC-06 |
| FIX-20260526-043 | 2026-05-26 | cursor-agent | — | ConformalOUGate diagnostics: added `ou_confidence` field from brain proposal to both `_extract_ou_diagnostics()` return dict and filter() features output — enables correlation analysis between OU physics scoring and brain confidence. | RC-12 |
| FIX-20260526-035 | 2026-05-26 | cursor-agent | — | Phase 8 (P1): Direction-aware p_win calibration — `_adjust_p_win_for_regime()` now receives `trade_direction` parameter. With-trend pullbacks ("千金难买牛回头") bypass trend penalty entirely (`return p_win`). Counter-trend signals retain existing harsh penalty (65% floor). Prevents double-penalization of with-trend OU signals that passed the direction-aware ADX gate. Architect directive: "不要去惩罚顺势交易。不要用代码去扼杀最肥美的 Alpha。" | RC-06 |
| FIX-20260526-034 | 2026-05-26 | cursor-agent | — | Phase 8 (P0): MetaLabel feature skew HARD BUG — `live_intent_loop.py:1106-1119` brains dict construction stripped `features` and `normalization_config_path` fields from registry entry, causing `_build_meta_feature_vector()` to fall back to V9 schema order. 40/43 feature positions scrambled → LightGBM positional indexing received random noise → MetaFilter gate (barrier_12bar) predictions were garbage. Two-line pass-through fix restores training-order feature assembly. | RC-06 |
| FIX-20260526-033 | 2026-05-26 | cursor-agent | — | Phase 8: Direction-aware ADX gate — replace symmetric ADX>25 block (FIX-20260526-030) with counter-trend gating using Kalman fusion trend detection. With-trend MR (pullback in uptrend, bounce in downtrend) is now allowed; only counter-trend signals are blocked. Uses `primary_trend` (H4>H1>M5) direction + `h1_trend_direction` from existing RegimeGate infrastructure. Explains LONG +44.2 vs SHORT -100.8 PnL asymmetry. | RC-06 |
| FIX-20260526-032 | 2026-05-26 | cursor-agent | — | Phase 7 (P0): resolve_p_win_from_brains() now passes window=100 to pnl_store.get_metrics() — fixes all-time aggregation bias where 1268-trade history (WR=49.05%) masked recent R100=51.0% improvement. One-line API fix: rolling window replaces stale multi-week data with current-regime win rate. | RC-05 |
| FIX-20260526-031 | 2026-05-26 | cursor-agent | — | Phase 6: (Fix 3) z_depth hard veto in ConformalOUGate — z_depth_q<0.25→score=0.0, cuts masking effect where theta/vel rescue absent deviation; (Fix 2) resolve_p_win_from_brains() fallback 0.50→0.40 Fail-Closed + diagnostic logging per failure mode; (Fix 1) P1 _adjust_p_win_for_regime() thresholds: ADX 20→15, |z| 1.5→0.8, z_amplification baseline 1.0→0.5 | RC-05, RC-12 |
| FIX-20260526-030 | 2026-05-26 | cursor-agent | — | May 25-26 post-mortem 5-priority surgery: (P0) ADX trend isolation gate; (P1) Dynamic p_win adjustment via `_adjust_p_win_for_regime()`; (P4) Dynamic SL/TP wiring verified complete; (P5) barrier_12bar_meta RR: min_sl_distance 8→3 + min_rr_ratio 0.5→0.4 + **hardcoded 1.2→self.config.min_rr_ratio** at L1075 | RC-06, RC-05 |
| FIX-20260525-024 | 2026-05-25 | cursor-agent | — | ExitWatchdog MIA retry storm: `execute_exit()` retry loop had no pre-flight position-open check. If position was closed in MT5 between cycles (MIA), the watchdog exhausted all retries against a non-existent position → false CRITICAL alerts. Fix: added `get_position_open(position_ticket)` check before retry loop — returns `already_closed` success if position is gone. | RC-05 (mia-no-preflight) |
| FIX-20260525-022 | 2026-05-25 | cursor-agent | — | Budget guard calibration for low-WR (30%) strategies: max_consecutive_losses relaxed 4→7 (statarb_dynamic) and 3→7 (statarb_m15). daily_loss_limit_pct relaxed -1.5%→-3.0% and -1.0%→-2.0%. StrategyBudget already supports these params — config-only change. | RC-05 |
| FIX-20260525-021 | 2026-05-25 | cursor-agent | — | Dynamic hesitation tied to OU half-life: entry_half_life field added to StrategyDecision + ActivePosition dataclasses. Captured from proposals[].diagnostics["half_life"] in evaluate(). should_exit_hesitation() uses `max(12, int(entry_half_life * 0.75))` for statarb strategies — MR exit patience now scales with physics instead of static 30-min timeout. | RC-05, RC-06 |
| FIX-20260526-028 | 2026-05-26 | cursor-agent | — | P4+P1 May 25 trade analysis fixes: (a) Binary_Cls_V1 train-serve feature order mismatch — model trained H1-first (H1→M15→M30→M5, 10 metrics/TF inline) but inference fed V9 canonical order (M5→H1, 8 core + OU + Hurst blocks). LightGBM positional indexing → 38/40 positions wrong → 785 votes 100% LONG frozen conf 0.865. Fix: `_reorder_for_brain()` in barrier_strategy.py maps V9-ordered vector → brain training order by feature name before `adapter.inference()`. Brain config `features` list updated to training order (from model meta.json). (b) counter_trend gate complete bypass for statarb family — `"statarb" not in name` added to exclusion condition alongside barrier_12bar. Mean-reversion is inherently counter-trend; blocking statarb SHORT during BULL is a category error. 5 new reordering tests. | RC-06 |
| FIX-20260525-020 | 2026-05-25 | cursor-agent | — | Bleed stop abolished for OU/mean-reversion: should_exit_bleed() still available for trend-following strategies. live_cycle.py skips bleed_stop block entirely when strategy name contains "statarb". Category error fix: trend exit heuristic applied to mean-reversion positions. | RC-06 |
| FIX-20260525-018 | 2026-05-25 | cursor-agent | — | M15 parliament deadlock diagnostics: strategy_line.py parliament gate_diag now includes brain_diag list (z_score, half_life, buffer_len, theta per brain). Relies on params_brain_adapter.py fix to include half_life/buffer_len in BrainSignal.diagnostics. | RC-06 |
| FIX-20260525-016 | 2026-05-25 | cursor-agent | — | Per-strategy min_p_win calibration: wired YAML→StrategyLineConfig for statarb_dynamic + statarb_m15. Lowered min_p_win 0.50→0.45 (OU empirical WR 49.7%, RR 2:1→breakeven 33.3%, p_win from BrainPnLStore has ±3-5% sampling noise). Unblocks 12+ signals/day at p_win 0.489-0.491. | RC-05, RC-12 |
| FIX-20260525-015 | 2026-05-25 | cursor-agent | — | Layer 3 COLD phase volume safety: strategy_line captures ConformalOUGate result via self._last_ou_result; after volume computation + lot_step rounding, force_min_volume overrides to 0.01 regardless of Kelly/position sizer output. Bounds exploration risk during calibrator cold-start. | RC-06, RC-12 |
| FIX-20260525-014 | 2026-05-25 | cursor-agent | — | Gate audit observability: StrategyDecision.gate_diag field + ConformalOU/parliament/counter-trend gate diagnostics. gate_diag captures per-gate scoring breakdown (z_depth_q, hl_q, theta_q, adx_q, vel_q) when ConformalOU blocks; confidence/threshold for parliament; trend_direction/strength for counter-trend. | RC-12 |
| FIX-20260525-012 | 2026-05-25 | cursor-agent | — | Phase 4 Dynamic SL/TP Calibration: asymmetric volatility regime response per strategy family. StrategyFamily enum (mean_reversion vs trend_following). Mean reversion: SL widens ×√vol_ratio, TP tightens ×vol_ratio^-0.25. Trend following: both widen synchronously ×√vol_ratio. Hard clipping: MIN_SL_ATR=0.8, MAX_SL_ATR=4.0, MIN_TP_ATR=1.0, MAX_TP_ATR=6.0. Dynamic ref_atr from regime_info["atr_mean"] (RegimeDetector EWMA). _STRATEGY_FAMILY_MAP auto-inference with YAML override priority. 4 active strategies wired in live.yaml (barrier_12bar→trend, barrier_12bar_meta→trend, statarb_dynamic→mr, statarb_m15→mr). 26 tests (14 new) pass. | RC-12 |
| FIX-20260525-010 | 2026-05-25 | cursor-agent | — | Phase A+B+C: three-subsystem physical isolation — (A1) hard p_win gate before Kelly sizing (min_p_win=0.50 statarb/OU); (A2) brain_scale floor 0.6→1.0 + conf_adj removed from trail_k (death spiral severed); (A3) min_trail_mult=1.2 floor in compute_trail_stop; (A4) statarb breakeven 0.8→0.5; (B1) TrailPolicy frozen dataclass — single source of truth for Risk Exit params, physically isolated from Model Exit; (B1b) _adjust_trail_for_regime / compute_trail_stop / should_breakeven all read from pos.trail_policy when available; (B1c) live_cycle.py wires TrailPolicy from live.yaml exit.* block; (C1) TrailStopEngine extracted to trail_stop_engine.py — independent class, physically isolated from Model Exit (evaluate_brain_exit). ActivePositionManager delegates all trail ops via thin wrappers. TrailPolicy frozen dataclass moved to trail_stop_engine.py as single canonical definition. | RC-05, RC-12 |
| FIX-20260525-009 | 2026-05-25 | cursor-agent | — | MT5 single-threaded worker (T1-C1/C2/C3): created MT5Worker (dedicated thread + queue + Future API), refactored MT5BrokerAdapter (delegate to worker, zero daemon threads), live_order_sender (remove per-call init/shutdown). 2670 tests pass. | RC-04, RC-06 |
| FIX-20260524-042 | 2026-05-24 | cursor-agent | — | T1-H1: Symbol Quarantine in execution_queue — upgraded bare except:pass to structured logging. T1-H4: PositionManager update_prices per-ticket result collection (was overwrite). T1-H5: execution_manager filled_quantity > 0 guard + negative detection. | RC-06, RC-07 |
| FIX-20260524-043 | 2026-05-24 | cursor-agent | — | T2-C2: execution_queue price guard exception now rejects (was silently passing with "let the order through"). T2-H3: strategy_line OU/Meta gate exceptions block trades instead of non-blocking pass. T2-H8: removed hardcoded skip_price_guard=True from dispatch_fn call — dispatch_live_order now always validates. | RC-06 |
| FIX-20260524-007 | 2026-05-24 | cursor-agent | — | Track 3d Conformal OU Gate: created ConformalOUGate (OU physics scoring: Z-Depth, Z-Velocity, Half-life, Theta, ADX) replacing 47-dim LightGBM MetaFilterGate for statarb_dynamic + statarb_m15. Strategy-aware OU parameter loading from brain artifacts (V6 M5: z_entry=3.9, V7 M15: z_entry=1.2). Shares ConformalCalibrator with MetaFilterGate. Wired into strategy_line.evaluate() with MetaFilterGate fallback. | RC-06, RC-12 |
| FIX-20260523-006 | 2026-05-23 | cursor-agent | — | Day 1 graveyard cleanup: (1) statarb_m15 added to MetaFilterGate (Track 3 47-dim LGB) gating in strategy_line.py — previously only statarb_dynamic was covered; (2) live.yaml 5 swing strategy lines disabled (daily/m15/m30/h1/h4) — all brains removed, no active voters; (3) regime_map swing entries cleaned from all 5 regimes | RC-09, RC-06 |
| FIX-20260523-004 | 2026-05-23 | cursor-agent | — | statarb_m15 counter-trend gate: _counter_trend_action() thresholds dict had no statarb_m15 entry, falling to generic default (block at H1≥0.40). Added dedicated entry mirroring statarb_dynamic's permissive thresholds (block at H1≥0.55, penalise at H1≥0.30, H4 block at 0.35). Mean-reversion IS counter-trend — blocking at H1≥0.40 would silence the M15 OU brain. | RC-09 (config-drift: new strategy not registered in threshold map) |
| FIX-20260522-001 | 2026-05-22 | cursor-agent | — | Net-out close confirmation blind spot: execution_queue.py else-branch treated empty intent_id as unconditional success, opening new positions against still-open opposing positions when ExitWatchdog failed. Now honours dispatched flag from _net_out_close_dispatch_fn. | RC-06 |
| FIX-20260523-001 | 2026-05-23 | cursor-agent | — | **P0: P(win) feedback loop** — p_win and kelly_mult added to trade journal. dispatch_live_open_order() gains p_win/kelly_mult params → execution_payload → mt5_bridge_worker.py extracts to journal record. Also entry_context (previously passed but never extracted). Enables precision-curve calibration: compare predicted P(win) against actual trade outcomes to find optimal Meta Filter threshold empirically. | RC-12 (missing-feature: feedback loop) |
| FIX-20260523-002 | 2026-05-23 | cursor-agent | — | **P1: OU z_entry harmonized at Optuna-validated 1.3**. Artifact (arb_params_v7.json) already had z_entry=1.3 (Optuna TPE, 300 trials), but strategy_line.py:680 hardcoded _z_entry=2.0 for statarb inflection gate — forming an effective bottleneck of max(1.3, 2.0)=2.0. Also fixed position_manager.py defaults (1.5→1.3). OU brain now uses consistent 1.3σ threshold across brain adapter, inflection gate, and position manager. | RC-09 (config-drift: artifact value diverged from code overrides) |
| FIX-20260522-004 | 2026-05-22 | cursor-agent | — | Journal confidence pipeline: dispatch_live_open_order() lacked confidence parameter, execution_queue flush() never passed decision.confidence, mt5_bridge_worker.py never extracted confidence/brain_votes from execution_payload. Full E2E wired. | RC-06 |
| FIX-20260522-020 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: `QueuedDecision` and `DispatchResult` in execution_queue.py converted to frozen dataclasses (`frozen=True`). `QueuedDecision.decision` typed as `StrategyDecision` from trading_contracts. dispatch_status semantic rule rename `protocol_validated`→`transport_delivered` propagated to tests, semantic rules, and disk baselines. v9_shadow SmokeTest assertions + rebuild-formal-baselines synced. | RC-06 |
| FIX-20260524-046 | 2026-05-24 | cursor-agent | — | DEFERRED: MT5 thread model architecture debt (T1-C1/C2/C3) — per-call daemon threads, repeated init/shutdown, non-thread-safe methods. Requires dedicated MT5 worker thread + session-level init. Short-term mitigations already in place. | RC-04, RC-06 |
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
| FIX-20260524-042 | 2026-05-24 | cursor-agent | — | T1-H1: Symbol Quarantine — net_out_close_not_confirmed now quarantines symbol via PortfolioRiskController, blocking ALL new entries until MT5 independently confirms zero positions. ExecutionQueue net-out exception handling upgraded from bare `except:pass` to logged errors. T1-H4: PositionManager.update_prices() now returns per-ticket dict instead of overwriting result with last position only. T1-H5: ExecutionManager.process_venue_event() validates filled_quantity>0; new_total≤0 guards prevent negative filled_quantity on corrupt upstream data. | RC-06 (contract-violation, silent-error-swallowing) |
| FIX-20260528-019 | 2026-05-28 | cursor-agent | — | MetaExitEngine-Watchdog urgency integration: ExitWatchdog now accepts `exit_urgency` + `factor_breakdown` params — high-urgency exits (>=0.9) use 200pt slippage from attempt 1 with 0.5s fixed backoff. `position_manager.evaluate_meta_exit()` return type `tuple[bool,str]` → `ExitEvaluation\|None`. All 12+ non-meta-exit call sites receive default urgency=0.5. | RC-06 |
| FIX-20260529-030 | 2026-05-29 | cursor-agent | — | SL/TP spread cost mechanism: added `spread_points`/`tick_size` kwargs to `compute_sl_tp_levels()`. When enabled, TP is tightened by spread cost (exit fills at bid/ask, not mid) and SL is widened (stop fills suffer adverse slippage). Aligns live order placement with training-label barrier adjustments in `label_contract.py`. Default `spread_points=0.0` preserves backward compat; enable after price basis audit confirms no double-counting. | RC-06 |
| FIX-20260529-031 | 2026-05-29 | cursor-agent | — | FillSimulator zero-slippage fix: added `FillSimulationConfig.from_slippage_points()` classmethod converting MT5 points (10 pt × 0.01 tick = 0.10 price on XAUUSD) to bps (≈0.5 bps). `PaperExecutionGateway` now accepts optional `slippage_points`/`approximate_price` params. CLI wired `slippage_points=10`. Previously training configs used `slippage_points: 10` but FillSimulator always defaulted to `slippage_bps=0.0` — paper trading systematically understated execution costs. | RC-09 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OrderStateMachine.transition(current, event)` → `OrderState` | ExecutionManager | Stable |
| `dispatch_live_order(envelope)` → `bool` | ExecutionQueue | Stable |
| `ActivePositionManager.evaluate_exits(market_data)` → `list[ExitEvaluation]` | live_cycle | Stable |
| `compute_dynamic_sl_tp(atr, regime)` → `DynamicSLTP` | strategy_line | Stable |

## Evolution Roadmap (开单/止损/止盈 机构化路线图)

> **状态**: Phase 1/2/3/4 已全部交付 (FIX-20260517-018 ~ 022, FIX-20260525-012)。MetaExitEngine-Watchdog 紧急度集成已交付 (FIX-20260528-019)。

| Phase | 范围 | 关键改动 | 依赖 |
|-------|------|----------|------|
| **Phase 2** | Ack receipt 完整化 | bridge worker 补全 ack receipt SL/TP 字段 → `_validate_ack_sl_tp()` 从 warn 升级为阻断 (偏差 > 0.5 pip 拒绝) | bridge worker 改动 (C++/Python) |
| **Phase 3** | ExitWatchdog 实盘集成 | 确认所有出场路径 (bleed_stop, Z-reversion, consensus_flip, time_decay) 都经过 Watchdog.execute_exit()；Watchdog 健康监控接入 daily_ops | live_cycle.py 出场段审查 |
| **Phase 4** ✅ 已交付 (FIX-20260525-012) | 动态 SL/TP 校准 | StrategyFamily 枚举（mean_reversion/trend_following），非对称波动率响应（√vol_ratio 缩放 + 硬裁剪边界），动态 ref_atr（RegimeDetector EWMA），自动推断策略族映射 | FIX-20260525-012 |
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

### Phase 4 详细说明 ✅ 已交付 (FIX-20260525-012)
- **已完成**: `StrategyFamily` 枚举（mean_reversion/trend_following）区分策略族非对称波动率响应。均值回归：SL 放宽 ×√vol_ratio 以存活噪声，TP 收紧 ×vol_ratio^-0.25（反向收益在湍流中收缩）。趋势跟随：SL 和 TP 同步放宽 ×√vol_ratio。硬裁剪边界：MIN_SL_ATR=0.8, MAX_SL_ATR=4.0, MIN_TP_ATR=1.0, MAX_TP_ATR=6.0
- **已完成**: 动态 ref_atr 从 `regime_info["atr_mean"]` 读取（RegimeDetector EWMA），取代静态 `config.ref_atr`
- **已完成**: `_STRATEGY_FAMILY_MAP` 自动推断映射表（statarb→mean_reversion，其余→trend_following），YAML 显式配置优先级高于自动推断
- **已完成**: 4 个活跃策略已在 live.yaml 接线（barrier_12bar/barrier_12bar_meta→trend_following, statarb_dynamic/statarb_m15→mean_reversion）
- **已完成**: 26 个测试全部通过（14 新增：7 个 RegimeFactors + 7 个 Phase4DynamicSLTP）
- **受益**: 高波动期自适应放宽止损避免震荡出局，低波动期收紧止盈锁定利润。每笔交易的风险贡献在波动率区间内近似恒定（Grinold & Kahn 原则）

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

| FIX-20260607-143 | 2026-06-07 | cursor-agent | — | **Trend Maturity Discount + Kalman Velocity Flip Exit**: (1) `trend_maturity_discount()` in strategy_line.py — Hurst persistence loss + Kalman velocity decay → position size scaling for trend/swing strategies. (2) Kalman velocity sign flip as fast-path exit in evaluate_brain_exit() — exits BEFORE price hits SL when |v|>3bps reverses. Pure wiring — signals already computed, now consumed. DQAF-20260607-007. | RC-12 |

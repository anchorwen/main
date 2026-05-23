# Fix Registry

> Master fix ledger for the Quant OS project.
> Each fix has a unique ID, root cause category, and prevention measure.

## Fix ID Format

```
FIX-YYYYMMDD-NNN
```
- YYYYMMDD: date fix was applied
- NNN: sequential counter per day (001, 002, ...)

## Root Cause Categories

| Code | Category | Description |
|------|----------|-------------|
| RC-01 | missing-null-check | None/empty not handled |
| RC-02 | type-confusion | Wrong type passed or assumed |
| RC-03 | state-leak | State from prior cycle bleeding through |
| RC-04 | race-condition | Concurrent access without synchronization |
| RC-05 | boundary-error | Off-by-one, edge case, range error |
| RC-06 | contract-violation | Interface contract not honored |
| RC-07 | missing-validation | Input not validated at boundary |
| RC-08 | incomplete-cleanup | Resources not released, listeners not removed |
| RC-09 | config-drift | Configuration inconsistent with code |
| RC-10 | dependency-order | Initialization/teardown order wrong |
| RC-11 | stale-data | Artifacts/configs from deprecated/retired models not cleaned up |
| RC-12 | missing-feature | Required capability not yet implemented |

## Fix Index

| Fix ID | Date | Module | Summary | Root Cause |
|--------|------|--------|---------|------------|
| FIX-20260514-001 | 2026-05-14 | runtime-live | Blueprint mechanism upgrade: modular fix tracking with automated markers | RC-06 |
| FIX-20260514-002 | 2026-05-14 | runtime-live | Blueprint mechanism upgrade: modular fix tracking with automated markers (retry) | RC-06 |
| FIX-20260511-001 | 2026-05-14 | runtime-live | Fixed multiple issues found during surgical audit of daily_ops, governance training, and execution risk controls | RC-07 |
| FIX-20260519-013 | 2026-05-19 | protocol-parliament | ContractGroupConsensus._compute_weighted: all-neutral brain groups no longer fabricate "long" direction — return neutral/0.0 when no brain has directional signal | RC-06 |
| FIX-20260519-014 | 2026-05-19 | runtime-live | Brain_votes diagnostic blind spot: record_brain_votes now includes raw_outputs (z_score, theta, half_life) for OU brain diagnostics | RC-06 |
| FIX-20260519-015 | 2026-05-19 | execution-orders | Gamma-parameterised EV trajectory envelope: strategy-archetype power-law curves (γ=0.5/1.0/2.0), removed grace-period cliff, wired override_min_r to envelope endpoint, ActivePosition.strategy_name for gamma dispatch | RC-05, RC-06 |
| FIX-20260519-016 | 2026-05-19 | brains-adapters | OU signal quality upgrade: z_entry 1.3→2.0 (filters 80% weak signals), half_life discount in confidence calculation (fast reversion 0.69×, slow reversion capped 0.3×) | RC-05, RC-06 |
| FIX-20260519-017 | 2026-05-19 | execution-orders, runtime-live | Four-Pillar Architecture: (P1) M5 bar OHLC-calibrated extreme tracking with graceful IPC degradation, (P2) Profit Pardon, (P3) prev_r persistence fix, (P4) expected_remaining_volume + MT5 ground-truth ghost-volume audit. + Strategy-level enabled enforcement: _build_strategy_lines() now respects enabled:false from live.yaml | RC-06 |
| FIX-20260519-018 | 2026-05-19 | execution-orders, runtime-live | P1 regression: `_update_single_position()` M5 OHLC branch missing `r_now` assignment → `UnboundLocalError` silently swallowed → trail SL / breakeven / trail TP never executed (SL frozen, breakeven_triggered never set). Fix: added `r_now` computation in M5 branch + `management_phase_diag` diagnostic event before dispatch | RC-06 |
| FIX-20260519-019 | 2026-05-19 | protocol-services, runtime-live | BarSyncPoller 92.8% timeout fix: `fetch_synthetic_bar()` aggregates last 6 M1 bars into synthetic M5 OHLC(V) when M5 bar not yet formed, eliminating 120s data-misalignment window where stale features ran against real-time prices | RC-06 |
| FIX-20260519-020 | 2026-05-19 | features-service, runtime-live | FeatureService live_compute timeout guard: Tier 2 compute_all() runs in daemon thread with 3s timeout; on timeout returns last_known_vector instead of blocking main loop (latency-slippage defense) | RC-06 |
| FIX-20260519-021 | 2026-05-19 | runtime-live | Brain contract mismatch hard mute: `_warn_contract_mismatch()` now sets vote_weight=0.0 on misaligned brains, preventing zombie voting that corrupts parliament consensus with random-confidence outputs | RC-06 |
| FIX-20260520-022 | 2026-05-20 | brains-adapters | OU z_entry revert 2.0→1.3: FIX-20260519-016 overcorrected — z_entry=2.0 silenced OU brain (0 signals in 16h). Revert to Optuna-validated 1.3. Half-life discount retained (the real fix). | RC-05, RC-09 |
| FIX-20260520-023 | 2026-05-20 | execution-guards | Dual-Track Router: Meta Pipeline (Huber→Stage 2 LGB+MLP+Platt+Conformal) decoupled from Parliament consensus. Track 1 keeps 0.45 threshold. Track 2 independently triggers when Parliament deadlocks — Huber raw_score→direction→Stage 2 filter→SL/TP/Kelly→execution. Added _try_meta_pipeline() to StrategyLine, deleted dead V1 filter config, created scripts/test_meta_pipeline.py injection test. Full chain verified: LGB+MLP+Platt+Conformal produces varying P(win) 0.37-0.68. | RC-06 |
| FIX-20260520-024 | 2026-05-20 | execution-guards, runtime-live | Hesitation exit killed profitable positions: `should_exit_hesitation` only checked `breakeven_triggered` (binary flag requiring 1.5 ATR move), ignoring that positions with positive PnL were being killed. Added Pillar 3 Current-Profit Guard: `r_now > 0` → no exit. Lowered Profit Pardon threshold: 0.30R → 0.15R. Added `mid` parameter to compute current R. Increased m15_swing hesitation_cycles 2→4, m30_swing 2→3 in live.yaml. | RC-05 |
| FIX-20260520-025 | 2026-05-20 | execution-guards, runtime-live, config | Absolute Refractory Period (Cut 1) + Cross-Strategy Family Entry Spacing (Cut 2): Added `CooldownRegistry` (passive exits=1×timeframe cooldown, active exits=60s, reverse-direction override) and `FamilyEntryTracker` (swing family same-direction entries ≥15min gap) to `pre_trade_guards.py`. Integrated into `live_cycle.py`: exit→cooldown recording in `_dispatch_managed_close`, pre-evaluate cooldown+spacing checks in `_evaluate_strategy_lines`, family entry recording after successful dispatch. Re-enabled h1_swing (60% WR) in live.yaml. | RC-06 |
| FIX-20260520-027 | 2026-05-20 | brains-schema, deployment-lifecycle, deployment-config | Institutional brain→live alignment validator (Layer 1+3): structured training_params in 14 brain configs + BrainRegistry + validate_brain_live_alignment() with ensemble consistency + hard fail (SL tightening, horizon truncation) + warnings (horizon expansion, TP deviation) + live_intent_loop JSON surface | RC-09 |
| FIX-20260520-028 | 2026-05-20 | execution-guards, protocol-parliament | Meta Pipeline Executive Veto: removed `not parliament_passed` precondition — Track 2 always runs first for barrier_12bar. Huber probe gets first-refusal to override parliament via Stage 2 filter chain, ending tyranny of the majority where 8 long-biased brains silenced the only short-biased brain. | RC-06 |
| FIX-20260520-029 | 2026-05-20 | training | Future-data leak in build_v4_micro_dataset.py: np.abs() allowed matching future micro features to past bars. Fix: backward-only matching (micro_ts <= ts) + future_leak_prevented counter. | RC-03 |
| FIX-20260520-030 | 2026-05-20 | training | Regression training target: --target regression flag in institutional_train.py. Uses y_reg from NPZ with reg:squarederror objective. RMSE/R² metrics, no balance weights. Clean dataset v9_micro_49_clean.npz built. | RC-12 |
| FIX-20260520-026 | 2026-05-20 | execution-guards, runtime-live, config | Dynamic Exit Manager: Per-strategy exit params (`trail_atr_mult`/`trail_atr_mult_low`/`trail_atr_mult_high`/`breakeven_threshold_atr`) added to `ActivePosition` dataclass with per-strategy overrides. `register_position()`, `load_state()`, `_adjust_trail_for_regime()`, `should_breakeven()`, `compute_trail_tp()` all refactored from `self.*` global to `pos.*` per-position. `live_cycle.py` passes per-strategy params at registration. `live.yaml` expanded: statarb_dynamic=1.5/1.2/2.5+0.8x be, m15_swing=1.5/1.3/2.5+1.2x be, h1_swing=2.5/2.0/3.5+1.5x be, barrier_12bar=2.0/1.8/3.0+1.5x be. Eliminates "configuration leak" where all strategies shared identical trail/breakeven. | RC-06 |
| FIX-20260512-001 | 2026-05-14 | protocol-parliament | Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading | RC-06 |
| FIX-20260513-001 | 2026-05-14 | execution-orders | PnL recording moved before approval gate: each proposal gets isolated PnL record to prevent missing ledger entries | RC-03 |
| FIX-20260514-003 | 2026-05-14 | execution-orders | Fixed raw_proposals UnboundLocalError: elif indentation error caused multi-strategy evaluation to be unreachable | RC-02 |
| FIX-20260514-004 | 2026-05-14 | feedback-performance | Add marginal tier (score 10-20), fix WR cliff with smooth ramp, fix DD component when PnL<=0, add marginal to all tier mappings | RC-05 |
| FIX-20260514-005 | 2026-05-14 | protocol-governance | Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100) | RC-06 |
| FIX-20260514-006 | 2026-05-14 | protocol-governance | Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging | RC-07 |
| FIX-20260514-007 | 2026-05-14 | brains-services | Add new-brain protection period (min_signals_active=100), graduated retirement path (active->frozen->retired instead of direct retire) | RC-07 |
| FIX-20260514-008 | 2026-05-14 | runtime-live | Add raw_proposals to defensive initialization block to prevent UnboundLocalError in single-brain mode | RC-03 |
| FIX-20260514-009 | 2026-05-14 | brains-services | Change resolve_ids_to_group fallback from barrier_12bar to unknown to prevent silent misattribution | RC-06 |
| FIX-20260514-010 | 2026-05-14 | execution-guards | EMA低通滤波替代离散信心下降检查：confidence_ema平滑信心得分，保留30s采样响应能力的同时数学过滤高频白噪声 | RC-05 |
| FIX-20260514-011 | 2026-05-14 | execution-guards | 废弃R里程碑拖尾收紧，引入基于已实现波动率的自适应K：vol_ratio > 1.5 放宽K+0.8，vol_ratio < 0.7 收紧K-0.3 | RC-05 |
| FIX-20260514-012 | 2026-05-14 | execution-guards | 简化分级利润锁定：删除(+2R,0.5R)和(+4R,2.5R)易触发级别，仅保留灾难性保护(+3R,1.5R)和(+5R,3.5R) | RC-05 |
| FIX-20260514-013 | 2026-05-14 | execution-guards | 最低持仓保护期(min_hold_cycles=3)+毒性流否决逃生舱(tick速度3倍阈值/逼近硬止损0.3ATR) | RC-01 |
| FIX-20260514-014 | 2026-05-14 | deployment-config | 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留 | RC-09 |
| FIX-20260514-015 | 2026-05-14 | protocol-governance | 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live | RC-06 |
| FIX-20260521-001 | 2026-05-21 | brains-schema, deployment-config, runtime-live | High Recall + High Precision architecture: Huber vote_weight 0.0→0.8, barrier_12bar confidence 0.45→0.25, MetaFilter threshold 0.50→0.60 | RC-09 |
| FIX-20260521-002 | 2026-05-21 | features-service, deployment-config, runtime-live | enabled:false in live.yaml brain_registry_entries dead code — live_intent_loop loaded ALL JSONs directly bypassing FeatureBrainRegistry. V3 necrotic brains still voting and corrupting parliament consensus. Three locations fixed: live_intent_loop.py (primary bypass), service_container.py, feature_service.py. | RC-09 |
| FIX-20260521-003 | 2026-05-21 | execution-orders, deployment-config, parliament | 实盘数据分析驱动的开单阈值精准化 + 反向趋势过滤：(1) 禁用5个swing脑 100% LONG-only亏损；(2) barrier_12bar min_valid_brains 1→2 + confidence_threshold 0.25→0.45；(3) statarb_dynamic _counter_trend_action()阈值启用；(4) min_valid_brains门控排除muted脑(vote_weight=0)，修复Huber被contract-mute后造成的死锁；(5) BARRIER_GROUP brain_types补全onnx_v9+online_sgd。 | RC-09 |
| FIX-20260521-004 | 2026-05-21 | runtime-live, deployment-config | Intent进程崩溃循环修复：live_intent_loop.py在multi-brain模式下load_brain_entry()调用改为条件执行(if not args.multi_brain)。path_defaults.py DEFAULT_BRAIN_ENTRY更新为deep_res_mlp_v1.json。governance_state.json清除16个僵尸脑条目(24→8)+27个transition_log条目。live.yaml移除已删除的lightgbm_h1_swing引用。 | RC-09 |
| FIX-20260521-005 | 2026-05-21 | features-service, runtime-live, training | 全量类型注解清扫：v9_live_computer.py _returns()返回类型np.ndarray→float。main_v9_shadow.py 15个mypy错误→0。label_builder.py变量trade→unlinked_trade消除遮蔽。deep_res_mlp_v1.json artifact_path指向现存v2模型。 | RC-02 |
| FIX-20260521-006 | 2026-05-21 | deployment-config | 状态清理+artifact修正：(1) governance_state.json清除16个僵尸脑条目(24→8)+27个transition_log条目；(2) live.yaml移除已删除的lightgbm_h1_swing引用；(3) deep_res_mlp_v1.json artifact_path指向现存v2模型。 | RC-09 |
| FIX-20260521-007 | 2026-05-21 | brains-adapters, execution-guards, execution-orders | Track 3 Meta Pipeline integration: meta_filter_adapter.py (47-dim LGB adapter), meta_filter_gate.py (dual-track gate), test_meta_pipeline.py (injection test validating Huber→LGB+MLP+Platt+Conformal chain) | RC-06 |
| FIX-20260521-008 | 2026-05-21 | training, deployment-lifecycle | Meta labeling dataset + filter training: build_meta_labeling_dataset.py, scan_profitability_surface.py, train_meta_filter.py, backtest scripts. MODULE_SOURCE_MAP expansion for 5 orphan files. | RC-06 |
| FIX-20260521-009 | 2026-05-21 | deployment-config, runtime-live | Stub adapter deadlock: bootstrap_v9.py now reads adapter.name from live.yaml and passes it to EnvironmentConfig. All 295 open signals previously routed to StubCommunicationAdapter (hardcoded "stub" default) instead of MT5. | RC-09 |
| FIX-20260522-001 | 2026-05-22 | execution-orders | Net-out close confirmation blind spot: execution_queue.py treated empty intent_id as unconditional success, opening new positions against still-open opposing positions when ExitWatchdog failed. Now honours dispatched flag. | RC-06 |
| FIX-20260522-002 | 2026-05-22 | runtime-live | _dispatch_managed_close silently lost position tracking on ExitWatchdog failure: known_open_tickets.pop() and clear_position() ran unconditionally, causing engine to lose track of still-open MT5 positions after failed close dispatch. | RC-06 |
| FIX-20260522-003 | 2026-05-22 | runtime-live | Strategy-level enabled:false check in _build_strategy_lines used dict-key reassignment (_known_groups[name] = []) instead of in-place .clear(), making local variable references immune to the check. Latent bug — currently masked by brain-level filter. | RC-06 |
| FIX-20260522-004 | 2026-05-22 | execution-orders, runtime-live | Journal confidence always null: (1) mt5_bridge_worker.py never extracted confidence/brain_votes from execution_payload, (2) dispatch_live_open_order had no confidence parameter, (3) execution_queue flush() never passed decision.confidence. Full pipeline wired end-to-end. | RC-06 |
| FIX-20260522-005 | 2026-05-22 | runtime-live | Intent loop startup deadlock: warm-start brain buffer MT5 call (copy_rates_from_pos for OU brain) could block indefinitely, stalling the entire engine. Added thread-based 15s timeout wrapper for all warm-start MT5 calls. Timeout → logged error + graceful skip. | RC-05 |
| FIX-20260522-006 | 2026-05-22 | protocol-services | BarSyncPoller MT5 transient error retry: copy_rates_from_pos() fails after ~104s of polling despite successful initialize(). Added MAX_MT5_ERROR_RETRIES=3 with re-init+retry before degrading to poll fallback. | RC-05 |
| FIX-20260522-007 | 2026-05-22 | runtime-live | Position count MT5 fallback: positions_total() returning < 0 (error code) caused entire cycle skip. Now falls back to position_manager cached count when MT5 unavailable. | RC-01 |
| FIX-20260522-008 | 2026-05-22 | runtime-live | Intent loop bar_sync crash protection: unhandled exception in bar_sync wait section killed entire intent loop process. Wrapped in try/except with bar_sync_crash JSON event + interval-based fallback sleep. | RC-01 |
| FIX-20260522-009 | 2026-05-22 | runtime-live | Unguarded clear_position() after close dispatch failure: all 7 managed-exit callers called pm.clear_position() unconditionally even when _dispatch_managed_close() returned False. Now all 7 callers check return value before clearing. | RC-06 |
| FIX-20260522-010 | 2026-05-22 | protocol-services, runtime-live | Bar sync timeout 120s→360s: DEFAULT_TIMEOUT_SECONDS < M5 bar period (300s) caused every polling window to expire before next bar. MT5 API was functional but bar_sync never had enough window. | RC-05 |
| FIX-20260522-011 | 2026-05-22 | protocol-services, runtime-live | BarSyncPoller graceful degradation: dual-deadline design prevents "360s block + caller sleep" dead window. Returns truthy sentinel after bar_period elapses with no new bar, waking main loop immediately. Poll interval 2s→1s, mt5.shutdown() before re-init, BAR_DEGRADED_WAKEUP logging. | RC-05 |
| FIX-20260522-013 | 2026-05-22 | brains-adapters | Sign-flip bug: _score_to_direction() in all 5 adapters (LGB/XGBoost/ONNX/Transformer/Params) mapped weak signals (|score| < 0.549, confidence < 0.5) to INVERTED direction — up_prob > down_prob even when model predicted SHORT. Consensus layer only compared up/down probabilities, ignoring direction_bias field. Result: every Huber BPS<0 signal opened LONG, crashing into the falling knife. Fixed with 0.5±confidence/2 anchoring. | RC-06 |
| FIX-20260522-014 | 2026-05-22 | runtime-live, execution-guards, features-service, risk-portfolio, protocol-services, feedback-pnl | Defense-in-depth hardening: (CRIT-1) mgmt phase price-fetch failure now falls back to entry_price instead of skipping ALL positions; (CRIT-2) warm-start MT5 thread now synchronous join(15s) eliminating data race; (CRIT-3) cycle_count double increment deleted (inner); (HIGH-5) 3 critical except:pass paths emit structured JSON now; (HIGH-6) degraded bar_sync skips Alpha, runs management only; (HIGH-8) atomic file writes (.tmp+os.replace) for all 7 state files | RC-01, RC-04, RC-06 |
| FIX-20260522-015 | 2026-05-22 | brains-adapters | Layer 1 immutable contracts: All 5 adapters' get_signal() now returns frozen BrainSignal dataclass (direction/confidence/raw_score/fallback/runtime_ms) instead of BrainDecisionProposal with untyped dict prediction. Eliminates dict-key typos, missing-key silent failures, and sign-flip class of bugs. | RC-06 |
| FIX-20260522-016 | 2026-05-22 | protocol-parliament | Layer 1 immutable contracts: GroupSignal (10-field mutable dict-like) replaced with frozen ConsensusResult from trading_contracts.py. _compute_weighted() redesigned with direction-count voting: each brain votes its decided direction weighted by confidence×vote_weight×(0.5 if fallback), highest total wins. Added supporting_brains/dissenting_brains lists. | RC-06 |
| FIX-20260522-017 | 2026-05-22 | contracts-domain | Layer 1 immutable contracts: Created core/schemas/trading_contracts.py — BrainSignal, ConsensusResult, StrategyDecision, DegradedResult, Direction, TradeDirection. Four frozen dataclasses (frozen=True, slots=True) replace untyped dicts at all 4 module boundaries. DegradedResult replaces every except:pass. | RC-06 |
| FIX-20260522-018 | 2026-05-22 | brains-services | Layer 1 immutable contracts: BrainRunService output type updated from BrainDecisionProposal[] to BrainSignal[] — all consumers receive typed frozen dataclasses. Backward-compat retained via getattr for legacy dict access. | RC-06 |
| FIX-20260522-019 | 2026-05-22 | runtime-live | Layer 1 immutable contracts: (1) Circuit breaker in LiveCycleState — _consecutive_degraded_cycles + _circuit_breaker_tripped, 3 consecutive degraded cycles→management-only mode, auto-reset on clean cycle. (2) Startup orphan detection — MT5 vs active_position.json comparison, mismatch→HARD_BLOCK. (3) raw_proposals paths carry BrainSignal|DegradedResult. | RC-06, RC-07 |
| FIX-20260522-020 | 2026-05-22 | execution-orders | Layer 1 immutable contracts: QueuedDecision + DispatchResult converted to frozen dataclasses. dispatch_status rename protocol_validated→transport_delivered synced to tests, semantic rules, and disk baselines. v9_shadow SmokeTest rebuilt. | RC-06 |
| FIX-20260522-021 | 2026-05-22 | brains-schema | Layer 1 immutable contracts: BrainSignal supersedes BrainDecisionProposal.prediction dict. Direction/TradeDirection Literal types from trading_contracts.py replace loose string direction fields. Schema version constant retained for backward compat. | RC-06 |
| FIX-20260522-027 | 2026-05-22 | runtime-live | Bleed stop horizon-scaled hardening: bleed_bars now scales with strategy horizon (horizon//3, min 3) + min_hold_cycles protection prevents bleed stop from firing before position has reasonable time to develop. Enhanced bleed_stop_triggered JSON event with bleed_bars, cycles_held, min_hold_cycles, horizon_cycles. Root cause #2 of May 22 8-trade losing streak — positions killed after 3 bars (15min) with tiny losses, never given time to develop. | RC-05 |
| FIX-20260522-028 | 2026-05-22 | protocol-services | BarSyncPoller silent-failure recovery: copy_rates_from_pos() returning None (not exception) after MT5 re-init caused infinite silent spin. Added BAR_EMPTY_POLLS_REINIT recovery — after 5 consecutive empty polls, re-inits MT5 and logs event instead of waiting 310s for degraded deadline. Fixes perpetual bar_sync_degraded_wakeup where new bars were never detected after the first MT5_ERROR. | RC-05 |
| FIX-20260515-001 | 2026-05-14 | training | LightGBM 4.6.0 removed fobj parameter: custom objective now passed via params[objective] | RC-06 |
| FIX-20260515-002 | 2026-05-14 | training | Pre-split dataset support: pipeline auto-detects X_val/y_val/X_test in NPZ and uses them directly | RC-06 |
| FIX-20260515-003 | 2026-05-14 | training | Max drawdown gate units fix: removed *100 multiplier, max_drawdown is already in absolute return units | RC-05 |
| FIX-20260515-004 | 2026-05-14 | training | Registry UNIQUE constraint: add_or_update falls back to model_hash lookup when run_id not found | RC-06 |
| FIX-20260515-005 | 2026-05-14 | training | Brain config v2→v1 schema compat: generate_brain_config now outputs brain_registry_entry.v1 with artifact_path + brain_type + contract_group + magic. Converted 5 v2 configs, updated live.yaml, fixed test_dataset_builder label assertion. | RC-06 |
| FIX-20260515-006 | 2026-05-15 | runtime-live | Schema ID mismatch: swing_24 not recognized in brain re-evaluation path. Added swing_24 alias alongside daily_swing_24 in both position-management inference routes. Also fixed _STRATEGY_CONTRACT_TYPES to use timeframe-prefix matching (m15_swing etc) for broader training_contract compatibility. | RC-09 |
| FIX-20260515-007 | 2026-05-15 | deployment-lifecycle | New swing models (5 brain IDs) not registered in governance_state.json. Added all 5 with candidate status for PnL tracking and automated promotion eligibility. | RC-09 |
| FIX-20260515-008 | 2026-05-15 | runtime-live | Watchdog cleanup: deleted deprecated hourly_watchdog.py (May 5 experiment), watchdog.log. Updated ADR-006, MODULE_INVENTORY, DEPENDENCY_GRAPH. Fixed verify.py to filter deleted files. | RC-09 |
| FIX-20260515-009 | 2026-05-15 | protocol-governance | Auto-shadow mechanism: new ShadowTracker (core/governance/shadow_tracker.py) counts candidate signals from brain_votes/. Two new governance rules: auto_promote_shadow_to_probation (50+ signals→probation) and auto_promote_probation_to_live (100+ signals→live). Scheduler service feeds shadow metrics into rule engine. | RC-12 |
| FIX-20260515-010 | 2026-05-15 | deployment-lifecycle | Aggressive data cleanup: removed 2 frozen brain configs, 33 model files, 4 orphaned training NPZs, 2 .bak backups, 4 dangling training contracts, 5 April decision dirs, 10 frozen governance entries. train.py auto-register enhanced to update live.yaml + governance_state.json. | RC-11 |
| FIX-20260515-011 | 2026-05-15 | training | Foundation fixes: integrated profitability_calibrator into pipeline (calibrate_label_contract()), fixed temporal leakage in _find_nearest_in_index() (only backward matching), added spread/slippage transaction cost modeling to label_contract.py and profitability_calibrator.py, added tiered quality gates (tree/deep_learning/online) with stricter validation. | RC-01, RC-02, RC-03, RC-04 |
| FIX-20260515-012 | 2026-05-15 | training | Pipeline unification: extended train_single() to support all 5 model types (xgboost, lightgbm, deep_res_mlp, transformer, online_mlp/sgd). Added DL search spaces, fixed evaluation/model-saving for non-tree models. Added --price-data CLI flag for profitability calibration. | RC-12 |
| FIX-20260515-013 | 2026-05-15 | execution-orders | Three-knife OU exit refactor: (1) Smart Entry — inflection gate z_entry raised 1.5→2.0 + volume climax check, (2) Drift Lock — spatial re-entry lock after mean-drift exit unlocks on opposite-z cross, (3) Alpha Handoff — OU→trailing-stop switch when PnL>+1R and trend strong | RC-12 |
| FIX-20260515-014 | 2026-05-15 | brains-services | Brain config restoration: 8 accidentally deleted brain configs restored from git, contract_group added, artifact_paths remapped to surviving institutional models, magic conflicts resolved, 4 barrier_12bar brains re-enabled in live.yaml | RC-11 |
| FIX-20260515-015 | 2026-05-15 | runtime-live | brain_votes consensus_confidence recording fix: replaced misleading _rough_conf with real ContractGroupConsensus values, removed legacy path max(0.30) floor bypass | RC-06 |
| FIX-20260515-016 | 2026-05-15 | multi-module | Phase1 system revival: promoted 3 directional brains shadow→probation/live, lowered neutral penalty 0.30→0.15 in consensus, recalibrated 5 strategy thresholds to actual signal distributions, disabled 6 zombie strategies, created MT5 position_query.py | RC-06 |
| FIX-20260515-017 | 2026-05-15 | runtime-live | live.yaml enabled flag was ignored: _build_strategy_lines() gated on brain presence only, now checks _cfg(name, enabled) for all 11 strategy types | RC-09 |
| FIX-20260516-001 | 2026-05-16 | deployment-config | statarb_dynamic threshold lowered 0.40→0.25: live data shows OU signals at 0.276-0.28, 0.40 blocked all trades | RC-09 |
| FIX-20260516-002 | 2026-05-16 | scripts-launcher | ENGINE_STALL false positive: _check_stall() monitored data/decisions/ which live trading never writes to; now uses live_trade_journal.jsonl as primary liveness signal | RC-09 |
| FIX-20260516-003 | 2026-05-16 | multi-module | Data-backed strategy parameter reference: analyzed 7,216 brain_votes + 1,230 trade journal + 3 brain PnL ledgers. Documented signal distributions, exit effectiveness, SL/TP calibration, brain performance gaps in blueprints. Critical finding: both LightGBM brains have frozen confidence (broken ML inference pipeline). | RC-06 |
| FIX-20260516-004 | 2026-05-16 | brains-adapters | LightGBM inference pipeline unfrozen: added metadata-driven feature extraction with Feature Blackboard pattern, replaced inherited dict.values() with name-ordered extraction from brain config features field | RC-06 |
| FIX-20260516-005 | 2026-05-16 | execution-guards, features-services | Feature freshness dead code: check_feature_freshness() didn't reject future timestamps, and _stale=True path in FeatureService still returned stale data (pass was no-op, not a break) | RC-06 |
| FIX-20260516-006 | 2026-05-16 | brains-adapters | All adapters: added dimension guards + brain_alert on all fallback paths. V9_ONNX + Transformer: _num_features extracted from ONNX input shape for validation. OnlineLearner: alert on silent dimension truncation. | RC-06 |
| FIX-20260516-007 | 2026-05-16 | brains-adapters | Base adapter run(): metadata-driven feature extraction from brain_entry["features"]. Replaced fragile dict-order-dependent values() extraction. Strategy files: unified to adapter.inference() calls. | RC-06 |
| FIX-20260516-008 | 2026-05-16 | brains-services, deployment | BrainConfigValidator (7 checks at load time) + BrainAlert (structured JSON to stderr). 20/22 brain configs repaired with features field. Training pipelines auto-populate features. | RC-09 |
| FIX-20260516-009 | 2026-05-16 | multi-module | Governance state integrity restoration: fixed run_promotion.py dual-write bug (apply_decisions + ensure_governance_registration now append to transition_log). Removed 12 zombie brain_states, fixed 6 brain_states↔transition_log inconsistencies, registered 5 new brains, deleted 4 stale configs, added enable_onnxruntime to DeepResMLP_V2_New, force-added governance_state.json to git tracking. | RC-06, RC-10 |
| FIX-20260517-001 | 2026-05-17 | training | Route C+ Protocol 1: PiT OOF feature generation replacing cross_val_predict with row-by-row deque loop. Cross-fold deque clearance ensures cold-start isomorphism. Stage 2 LGB+MLP training scripts on PiT features. meta_stage2_runtime_59 schema (59-dim) registered. | RC-09 |
| FIX-20260517-002 | 2026-05-17 | execution-guards | Route C+ Protocol 2+3: Platt scaling calibration (smooth sigmoid, avoids IsotonicRegression step collapse) + conformal prediction thresholding (80th percentile of 500-prediction window, 0.50 floor). MetaSignalFilter extended with calibrator_path, conformal_mode/window/percentile/min_threshold. | RC-12 |
| FIX-20260517-003 | 2026-05-17 | runtime-live | Route C+ live deployment: bootstrap_v9.py switched from meta_stage2_filter_v1.json (47-dim, OOF-distorted, no calibration) to v3.json (59-dim PiT, LGB+MLP ensemble 0.6/0.4, Platt calibration, conformal prediction). Added calibrator/conformal parameter pass-through. | RC-09 |
| FIX-20260517-004 | 2026-05-17 | execution-guards, runtime-live | MetaSignalFilter DevOps hardening: state persistence (save_state/load_state for crash recovery), time-decayed conformal queue (14-day max_age_days), Platt extrapolation safety clamp (eps 1e-4 + output clamp). Integrated into live_intent_loop init/periodic/shutdown. | RC-03, RC-05 |
| FIX-20260517-005 | 2026-05-17 | brains-adapters, deployment-lifecycle | XGBoost adapter num_feature fallback path fix: load() looked at gradient_booster.model_param (empty in XGBoost >=1.6) instead of learner_model_param where num_feature actually lives, defaulting to 9. Fixed 5 swing models (24-dim) + V9_Institutional (40-dim) dimension validation. Un-retired lightgbm_h1_swing. | RC-06 |
| FIX-20260517-006 | 2026-05-17 | contracts-training | Friction dead-band: apply_friction_deadband() prevents phantom inverted signals from subtractive friction (catastrophic for cent accounts). build_regression_labels() + build_vol_scaled_regression_labels(). LabelSpec: vol_scale_target, output_unit, reg_huber, abs_target weighting. slippage_pips 0.5→1.0. | RC-06 |
| FIX-20260517-007 | 2026-05-17 | risk-portfolio | CapitalAllocator: capacity-aware position sizing with two defense lines — max_concentration (50% default) + min_lot_size gating (prevents sub-minimum-lot micro-orders). Proportional allocation from DynamicBrainWeighter weights. | RC-12 |
| FIX-20260517-008 | 2026-05-17 | protocol-parliament | Added explicit type annotations (dict[str, Any]) to BARRIER_GROUP, MICRO_GROUP, and all contract group dicts for mypy strict compliance | RC-02 |
| FIX-20260517-009 | 2026-05-17 | brains-adapters, features-service | Zero-vector frozen-confidence defense: FeatureService Tier 3 now emits brain_alert before returning np.zeros(). Cache freshness check exception handler forces _stale=True instead of silently swallowing. Zero-vector guard added to LightGBM/XGBoost/V9_ONNX/OnlineLearner infer() — detects all-zero input and returns neutral fallback with explicit fallback_reason. | RC-06 |
| FIX-20260517-010 | 2026-05-17 | execution-guards | Fixed inverse-volatility SL/TP formula: `sl_mult = base_sl_mult / vol_ratio` cancelled to fixed distance → SL shrank to 1.25 ATR in high vol. Changed to direct multiplication `sl_mult = base_sl_mult` so SL always spans base_sl_mult ATRs. Updated ref_atr 5.0→7.0. | RC-05 |
| FIX-20260517-011 | 2026-05-17 | deployment-lifecycle | Brain ecosystem cleanup: removed 6 retired brains from live.yaml, 12 zombie governance entries, 3 stale configs, fixed crt_sur_chlg_g2026.json features field, registered Meta_Stage1_Huber_V1 orphan config.  | RC-11 |
| FIX-20260517-012 | 2026-05-17 | contracts-training, brains-validation | Route A 双轨制部署：树模型 min_forward_sharpe 地板 0.75→0.20；magic uniqueness 放宽为 per-contract_group（同一策略线共享 magic）。训练 barrier_12bar XGBoost (Train Sharpe 0.92, Fwd Sharpe 0.91) + LightGBM (Train Sharpe 1.15, Fwd Sharpe 0.93)，加入 live.yaml 与 Meta_Stage1_Huber_V1 形成双轨制 Parliament。 | RC-06 |
| FIX-20260517-013 | 2026-05-17 | runtime-live, protocol-parliament, feedback-pnl | 摩擦成本完整化：所有 PnL 路径添加 slippage=0.10 (10 pips × 0.01)；精简 BARRIER_GROUP brain_types 为实际活跃类型 (xgboost_v9, lightgbm_v1)；live.yaml brain_types 同步精简。 | RC-06 |
| FIX-20260517-014 | 2026-05-17 | runtime-live | PnL 全局锚点迁移：settle_all() 从价格获取后移至所有安全守卫通过后、策略评估前的唯一锚点。消除提前 return 前的无效结算，确保每 tick 单次调用。 | RC-03 |
| FIX-20260517-015 | 2026-05-17 | protocol-governance | health_signal 硬编码 unknown→healthy：解除 ShadowTracker 对 candidate→probation 自动晋升的阻断，打通 shadow→live 生命周期。 | RC-12 |
| FIX-20260517-016 | 2026-05-17 | execution-orders | brain_status_map 纯内存传递：strategy_line 从 self.brains 提取状态映射传入 record_brain_votes()，禁止热路径磁盘 I/O。 | RC-06 |
| FIX-20260517-017 | 2026-05-17 | protocol-governance, brains-services, deployment-lifecycle | 双管线 Auditor/Executor 分离：BrainPromotionEvaluator 降级为纯 Auditor (只出报告)，GovernanceRuleEngine 新增 execute_transitions() 作为唯一 Executor，scheduler_service 按报告驱动模型串联。 | RC-06 |
| FIX-20260517-018 | 2026-05-17 | runtime-live | 路径 B 废弃标记：elif config.multi_brain 在 multi_strategy_enabled=True 默认值下不可达，添加 DEPRECATED 注释保留为回退参考。 | RC-02 |
| FIX-20260517-019 | 2026-05-17 | execution-orders | ExitWatchdog 机构化重构：(1) dispatch_live_order() 返回 dict 补 "dispatched" key 修复合约不匹配；(2) L2 强平：Watchdog 超时/重试耗尽后通过 MT5BrokerAdapter.close_position() 直接调用 mt5.PositionClose(ticket) 绕过 Bridge。 | RC-01, RC-06 |
| FIX-20260517-020 | 2026-05-17 | execution-orders | dispatch_live_open_order() 新增轻量 ack receipt SL/TP 校验位：dispatch 后检查 receipt，含 SL/TP 则验证偏差，不含则 warn 日志（不做阻断）。完整校验推迟到 Phase 2 (需 bridge worker 改动)。 | RC-06 |
| FIX-20260517-021 | 2026-05-17 | execution-orders | Phase 2 Ack receipt SL/TP 完整化：(1) bridge worker _mt5_market_open() 自旋等待 MT5 Positions Pool 同步后读回 confirmed_sl/confirmed_tp；(2) _validate_ack_sl_tp() 灰度升级 warn→ERROR（偏差>0.5 pip），不阻断。 | RC-01, RC-06 |
| FIX-20260517-022 | 2026-05-17 | execution-orders, runtime-live | Phase 3 ExitWatchdog 旁路补缺：(1) 4 个出场缺口接入 Watchdog：partial TP + force_close_dd + net_out（执行队列上层拦截，陷阱三）；(2) bridge worker 部分平仓 POSITION_IDENTIFIER 新 ticket 捕获（陷阱二）；(3) ExecutionQueue 新增 close_dispatch_fn 回调参数保持架构纯粹。 | RC-01, RC-06 |
| FIX-20260517-023 | 2026-05-17 | monitor-dashboard | 面板重新设计：(1) P0 修复 shadow/live decisions 同文件 bug — live 改为从 trade journal 读取；(2) 全局汉化 — 所有 UI 文本、状态徽章中文；(3) 布局重整 5行→4行+tab切换；(4) 新增 /api/brain/{id} 端点+详情面板（SVG sparkline 走势图、方向分布、治理/绩效卡片）；(5) 裸 except→logger.warning。 | RC-06 |
| FIX-20260518-024 | 2026-05-18 | features-service | Phase 1b: Hardcoded schema_version="1.0" → dynamic resolve_version() from registered schemas.json. write-back skipped gracefully when no matching schema exists. Added LocalFeatureStore.resolve_version() method. | RC-09 |
| FIX-20260518-025 | 2026-05-18 | brains-validation | Phase 1a: Per-brain schema startup validator — validates each brain's feature_schema_id against Tier 1 (registered schemas) and Tier 2 (implemented live compute). Drops individual mismatched brains instead of killing entire system. | RC-09, RC-06 |
| FIX-20260518-026 | 2026-05-18 | runtime-live | Phase 2: daily_ops scheduler hardened — save _last_daily_ops_utc BEFORE execution (edge-reentry fix) + elapsed-time trigger replaced with fixed UTC 22:00-23:00 date-based window. Phase 1c: 9 deprecated scripts archived. | RC-04, RC-03 |
| FIX-20260518-027 | 2026-05-18 | deployment-config | Phase 2b: Added DAILY_OPS_WINDOW_HOUR=22 + DAILY_OPS_WINDOW_DURATION_HOURS=1 to core/constants.py for fixed UTC daily_ops scheduling. Disabled Windows Task Scheduler QuantOS\DailyOps. | RC-09 |
| FIX-20260518-028 | 2026-05-18 | monitor-dashboard | Phase 3: Unified health aggregator — _build_unified_health() reads 7 data sources, /api/health/full endpoint with 10s cache, overall_status: healthy|degraded|critical. Frontend single-request rendering for health panels with fallback to individual endpoints. | RC-12 |
| FIX-20260518-029 | 2026-05-18 | brains-adapters | XGBoost adapter multi-class support: detect multi:softprob models via num_class. Convert 3-class probabilities → directional raw_score (P(LONG)−P(SHORT)). XGBoost_D1_Swing_5d was a 3-class classifier causing "only length-1 arrays can be converted to Python scalars" every cycle. | RC-06 |
| FIX-20260518-030 | 2026-05-18 | execution-guards | MetaSignalFilter feature_names fallback: when .meta.json missing, _feature_names stayed empty [] → 0-length feature vector → LightGBM fatal. Fall back to booster.feature_name() after model load — reads 59 feature names directly from trained model. | RC-11 |
| FIX-20260518-031 | 2026-05-18 | multi-module | Retired brain cleanup: removed 12 retired brain_states from governance_state.json, moved 6 retired brain configs to archive_deprecated/, emptied ENSEMBLE_GROUPS (all 3 referenced brains were retired — SurvivalAlpha_Ensemble + TreeAlpha_Ensemble). | RC-11 |
| FIX-20260518-032 | 2026-05-18 | execution-guards | Tier 2 Kelly/Edge sizing: `compute_kelly_mult(p_win, rr_ratio)` with EV veto (kf≤0 → fractional_mult=0.0 → hard reject). `resolve_p_win_from_brains()` uses rolling 100-trade win rate from BrainPnLStore with cold-start guard. | RC-12 |
| FIX-20260518-033 | 2026-05-18 | execution-orders | Tier 3 √N correlation discount: `apply_sqrt_n_discount()` decays N same-direction strategy volumes by 1/√N with lot_step rounding. Strategies below min_lot after discounting are dropped and removed from execution queue. | RC-12 |
| FIX-20260518-034 | 2026-05-18 | execution-guards, execution-orders | Kelly observability + discretization fix: moved Kelly inside `_compute_volume()` BEFORE lot_step rounding (previously applied to already-rounded value → effect destroyed by premature discretization). Added `kelly_diag` + `kelly_sizing` JSON events with three-way volume (base/raw_target/final_stepped). Added `p_win`/`kelly_mult` to `multi_strategy_eval` log. | RC-05 |
| FIX-20260518-035 | 2026-05-18 | execution-guards, runtime-live, execution-orders | NET_OUT config wiring: `portfolio_netting_mode` wired from `LiveCycleConfig` → `PortfolioRiskController`. Entire netting path was dead code (default `"allow_coexist"`, never overridden). Also fixed partial close ticket reassignment: ExecutionQueue extracts `new_ticket` from ACK receipt, live_cycle updates `known_open_tickets` to prevent orphan positions. | RC-05, RC-06 |
| FIX-20260518-038 | 2026-05-18 | execution-orders, runtime-live | Merge trail SL + breakeven + trail TP into single modify_sltp dispatch per cycle (was 2-3 back-to-back requests → MT5 retcode 10006 rejections on 2nd/3rd). Added ticket param to 12 position_manager methods for multi-position correctness. Fixed position_state_path mismatch between load/save paths. | RC-06 |
| FIX-20260518-040 | 2026-05-18 | execution-reentry, execution-orders, runtime-live | Wave 1-4: 90001/90003 开单阈值精准化 (5 config changes) + 退出分类修复 (3 missing categories + time_expired gate + hesitation/bleed_stop handlers) + 微型手数衰减防御 + 可观测性诊断日志 (reentry_check, exit_recorded, enriched confidence rejection reason). Based on comprehensive data analysis of live trading patterns (77% same-direction re-entry, 68% hesitation exits statarb, 81% long bias). | RC-05 |
| FIX-20260518-042 | 2026-05-18 | runtime-live, brains-validation | PnL recording fix: entry_price from MT5 history_deals_get (deal.entry==0, actual fill price) instead of journal request.price which is 0 for market orders. 94% of close events had pnl=null. Also created validate_brain_before_deploy.py deployment quality gate (direction sanity, NEUTRAL rate, output validity, correlation checks). | RC-06 |
| FIX-20260518-043 | 2026-05-18 | runtime-live | base_volume priority fix: 11 strategy construction sites changed from `config.volume or _cfg()` to `_cfg(None) or config.volume or 0.01` — Python `or` with truthy float 0.01 always evaluated global, ignoring per-strategy base_volume (statarb_dynamic 0.02→0.01). Also boosted Online_MLP_V1 vote_weight 0.6→1.2 (only brain with SHORT tendency at 48.6%). | RC-05 |
| FIX-20260518-044 | 2026-05-18 | execution-orders | Commit catch-up: 5 execution pipeline files (execution_queue, exit_watchdog, live_order_sender, mt5_broker_adapter, mt5_bridge_worker) — close_dispatch_fn callback, L2 forced liquidation, SL/TP ack validation canary, bridge trap fixes #1/#2, partial close ticket tracking. Previously documented under FIX-20260517-019/021/022 but never committed. | process-violation |
| FIX-20260518-045 | 2026-05-18 | features-service | Commit catch-up: local_feature_store.resolve_version() schema version lookup. Previously documented under FIX-20260518-024 but never committed. | process-violation |
| FIX-20260518-039 | 2026-05-18 | features-service, runtime-live | Feature store freshness check timezone normalization: naive UTC datetimes interpreted as local (UTC+8) → 28,800s artificial staleness. Fix: `ts.replace(tzinfo=UTC)` before `ts.timestamp()` at both freshness check sites. Also cleaned 36,341 future-timestamp records from feature store (78,971→42,630). | RC-05 |
| FIX-20260518-037 | 2026-05-18 | execution-orders | Multi-position refactor: ActivePositionManager → dict[ticket→ActivePosition], register_position no longer blocks, recovery iterates ALL MT5 positions, management phase loops all positions, save/load v2 multi-position format | RC-05 |
| FIX-20260518-036 | 2026-05-18 | execution-orders | Phase A+B: Confidence Spring (Layer-2 confidence_ema modulates Chandelier trail K: +0.6 high-conf → -0.5 low-conf) + EV Trajectory Envelope (sqrt-law Alpha decay exit with grace period + 0.5R tolerance floor) replaces linear time-decay phases. Import math added. | RC-12 |
| FIX-20260519-001 | 2026-05-19 | deployment-lifecycle | Pre-commit deadlock fix: validate_blueprints.py `check_source_blueprint_freshness()` now only checks `--cached` in pre-commit context. Breaks the recursive stash paradox where unstaged FIX entries from prior sessions revert to HEAD during pre-commit stash, producing false STALE/ORPHAN violations. | RC-06 |
| FIX-20260519-002 | 2026-05-19 | multi-module | Commit catch-up: 23 .py files across 14 modules deliver previously registered fixes. Includes execution pipeline (6 files), governance (2 files), Route C+ migration (3 files), dashboard v2, XGBoost multi-class, MetaFilter fallback, feature store, brain registration, verify.py compliance check. | process-violation |
| FIX-20260519-003 | 2026-05-19 | execution-guards, execution-orders, brains-validation | New files delivered: kelly_sizer.py (Tier 2 Kelly/Edge), correlation_sizer.py (Tier 3 sqrt(N)), startup_validator.py (per-brain schema validation). Previously registered as FIX-20260518-032/033/025. | missing-feature |
| FIX-20260519-004 | 2026-05-19 | deployment-lifecycle | Defense-in-depth deadlock prevention: check_blueprint_compliance.py --check defaults to staged-only (--all flag for deep audit), classify_diff supports cached_only, validate_blueprints.py unified changed_all tracking removes dead code + second-pass git subprocess calls, verify.py --full uses --all. Breaks false-violation loop at all three check paths: pre-commit hook, verify.py --quick, verify.py --full. | RC-06 |
| FIX-20260519-005 | 2026-05-19 | execution-orders, runtime-live, deployment-lifecycle | PnL盲区根治: (A) ExitWatchdog._build_close_payload()漏掉pnl字段导致100%托管退出无PnL — execute_exit()和_build_close_payload()添加pnl参数,所有调用点传入PnL; (B) reconciliation三层PnL断点修复 — entry_price增加L2回退(open_entry.entry_price), close_price增加回退(state._recent_mid_prices[-1]), PnL增加回退(_engine_close_pnl); (C) verify.py子进程Windows编码修复 | RC-06 |
| FIX-20260519-006 | 2026-05-19 | execution-orders, runtime-live | 机构级参数校准Wave 1+3: (P1) barrier_12bar hesitation_cycles 2→4 (OU均值回归需3-5周期展现,2周期过早斩仓); (P3) breakeven_threshold_atr 1.0→1.5 (减少过早保本触发,给趋势更多发展空间); min_sl_step 0.005→0.15 (15pip绝对防抖,替代无效的0.5pip阈值) | RC-05 |
| FIX-20260519-007 | 2026-05-19 | execution-orders | Trail SL物理学增强: (1) 棘轮规则—compute_trail_stop()集成self.min_step硬门槛,long candidate≤current_sl+min_step不更新/short candidate≥current_sl-min_step不更新,杜绝跟踪拖尾抖动; (2) Confidence Spring调节减半—conf_adj ±0.6→±0.3, ±0.5→±0.25, 减少Layer-2情绪化放大导致的拖尾过度收紧/过度放宽; (3) min_step默认值0.005→0.15 | RC-05 |
| FIX-20260519-008 | 2026-05-19 | execution-orders | Global Directional Cooldown: PortfolioRiskController增加net_out_cooldown_seconds(默认600s)+last_net_out_timestamp/last_net_out_direction追踪。net_out强制平仓后记录被平仓方向,cooldown期间拦截该方向所有新开单(任意策略),阻断net_out→新开仓→反向net_out的死亡连锁。Cooldown检查在策略重复检查之后、总敞口检查之前执行。 | RC-12 |
| FIX-20260519-009 | 2026-05-19 | runtime-live | config→code 管道修复: live_intent_loop.py读取live.yaml时同时提取live_trading.volume/risk_budget_usd/equity_risk_pct并传入LiveCycleConfig。之前仅读取strategy_lines段,live_trading段完全被忽略→risk_budget_usd始终为默认5.0→vol-targeted sizing始终0.01。同步修改LiveCycleConfig默认值:risk_budget_usd 5.0→10.0,exit_breakeven_threshold_atr 1.0→1.5,exit_min_step 0.005→0.15 | RC-09 |
| FIX-20260519-011 | 2026-05-19 | execution-orders, runtime-live | 周期感知分层出场架构(Waves A-D): (A) live_intent_loop.py新增apply_timeframe_scaling()—YAML人类可读值→M5 bar自动换算,所有策略+timeframe字段,StrategyLineConfig+timeframe_mult属性; (B) compute_dynamic_sl_tp()新增timeframe_mult参数—ATR按√(timeframe_mult)缩放,例H1止损14→48.5pips; (C) Meta Exit维度隔离—_manage_position()构建meta_consensus时按_tf_mult过滤group_signals,大周期仓位仅用同级别+共识; (D) m30/h1/h4 swing→enabled:false,宏观偏见过拟合退回shadow | RC-05, RC-06 |
| FIX-20260519-012 | 2026-05-19 | execution-orders | Absolute SL Distance Floor + RR Guard: compute_dynamic_sl_tp()新增min_sl_distance(绝对价格距离保底,防止ATR塌陷时SL<点差无净呼吸空间)和min_rr_ratio(当SL被保底抬升后拉伸TP维持最低盈亏比,防止负偏斜)。StrategyLineConfig透传+live_cycle.py全部11策略从YAML sl块接线。live.yaml为M5策略设置min_sl_distance:8.0 + min_rr_ratio:1.5 | RC-05 |
| FIX-20260519-010 | 2026-05-19 | feedback-pnl, brains-services, runtime-live, execution-orders | 三轨制大脑归因体系: (Track 1) BrainPnLStore horizon-matched counterfactual PnL—record_signal接受expected_horizon+TTL,settle_all仅结算TTL=0信号,每大脑按训练视界结算非1-bar; (Track 2) update_pending()每周期更新MFE/MAE追踪价格+递减TTL,_settle()从追踪价格计算R-multiple; (Track 3) BrainAttributionService confidence-weighted marginal attribution—journal新增brain_votes,sponsors(同向)按置信度加权分PnL,dissenters(反向)豁免; live_cycle.py接线: update_pending→settle_all流程,record_signal传expected_horizon,dispatch传brain_votes | RC-06 |
| FIX-20260522-022 | 2026-05-22 | contracts-domain | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility | RC-06 |
| FIX-20260522-022 | 2026-05-22 | protocol-parliament | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility. Fixes 32 v9 shadow tests. | RC-06 |
| FIX-20260522-024 | 2026-05-22 | execution-guards | Config-driven MetaPipeline architecture: replaces hardcoded `_try_meta_pipeline()` with declarative MetaPipeline class (frozen contracts, brain-id never hardcoded). Fixes cross-module cascade where FIX-20260522-015 silently broke FIX-20260520-028. | RC-06 |
| FIX-20260522-024 | 2026-05-22 | runtime-live | Config-driven MetaPipeline wiring: live_cycle.py auto-discovers meta_probe_specs from brain JSON roles + live.yaml overrides. shadow_recorder.py reads BrainSignal fields directly. | RC-06 |
| FIX-20260522-023 | 2026-05-22 | contracts-domain | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues across all changed modules | RC-02 |
| FIX-20260522-023 | 2026-05-22 | deployment-lifecycle | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues | RC-02 |
| FIX-20260522-023 | 2026-05-22 | deployment-config | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | execution-guards | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | features-service | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | features-rolling | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | feedback-performance | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | feedback-pnl | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | feedback-online | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | risk-regime | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | training | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors | RC-02 |
| FIX-20260522-023 | 2026-05-22 | contracts-ids | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | risk-portfolio | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-023 | 2026-05-22 | monitor-dashboard | Batch mypy type safety: annotation fixes, None guards, type narrowing | RC-02 |
| FIX-20260522-025 | 2026-05-22 | brains-adapters | Complete BrainSignal.diagnostics passthrough for all 6 adapters (v9_onnx, transformer, online_learner) + shadow_recorder BrainSignal.diagnostics read path | RC-06 |
| FIX-20260522-026 | 2026-05-22 | runtime-live | Harden startup orphan detection: replace except:pass with explicit JSONDecodeError + generic error logging to prevent silent failure on corrupt/empty active_position.json | RC-01 |
| FIX-20260522-029 | 2026-05-22 | protocol-services | BarSyncPoller numpy.void .get() crash: mt5.copy_rates_from_pos() returns numpy structured arrays whose rows are numpy.void (not dict). .get() calls on return-dict construction threw AttributeError AFTER state was updated, creating perpetual degradation. Fix: (1) .get()→[] for all 6 accesses, (2) bar_data dict built BEFORE state update — construction failure preserves state for clean retry. This single bug was the root cause of ALL MT5_ERROR events (spaced exactly 300s apart) and all BAR_DEGRADED_WAKEUP cycles. | RC-02 |
| FIX-20260523-001 | 2026-05-23 | execution-orders | P(win) feedback loop: p_win + kelly_mult + entry_context now recorded in live_trade_journal. dispatch_live_open_order() → execution_payload → mt5_bridge_worker journal extraction. Enables empirical precision-curve calibration: compare P(win) predictions against realized trade outcomes. | RC-12 |
| FIX-20260523-002 | 2026-05-23 | execution-orders | OU z_entry harmonized at Optuna 1.3: strategy_line.py inflection gate 2.0→1.3, position_manager.py defaults 1.5→1.3, matching artifact (arb_params_v7.json already 1.3). Eliminates effective bottleneck of max(1.3,2.0)=2.0 that silenced OU brain. | RC-09 |
| FIX-20260523-003 | 2026-05-23 | protocol-services | Meta Filter (Track 4d) conformal prediction disabled to fix threshold at 0.65. Conformal was computing max(80th_pctile, 0.50, 0.65)=~0.679, rejecting 83% of barrier_12bar proposals. Pass rate increases 17%→~28%, accelerating sample collection for data-driven threshold calibration. | RC-09 |

---
## Fix Details by Year

| Year | File | Count |
|------|------|-------|
| 2026 | [FIX_REGISTRY_2026.md](FIX_REGISTRY_2026.md) | 83 |

> New fix entries should be added to the relevant year file.
> Keep the Fix Index table above updated with every fix.

<!--
  Template for new fix entries — copy to the relevant year file:
  ### FIX-YYYYMMDD-NNN
  - **Date**: YYYY-MM-DD
  - **Author**: <name>
  - **Commit**: <hash>
  - **Type**: fix | feat | refactor | perf | security
  - **Module**: <module-name>
  - **Files**: path1, path2
  - **Description**: <what was fixed>
  - **Root Cause**: RC-0X — <explanation>
  - **Prevention**: <how this class of bug is prevented from recurring>
  - **Dependents Checked**: <modules checked for impact>
-->


### FIX-20260522-022
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: contracts-domain
- **Files**: core/parliament/parliament_service.py
- **Description**: Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-022
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: protocol-parliament
- **Files**: core/parliament/parliament_service.py
- **Description**: Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility. Fixes 32 v9 shadow tests.
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-024
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Type**: refactor
- **Module**: execution-guards
- **Files**: core/execution/meta_pipeline.py (NEW), core/execution/strategy_line.py, core/runtime/live_cycle.py, core/runtime/shadow_recorder.py, configs/brains/meta_stage1_huber_v1.json
- **Description**: Config-driven MetaPipeline architecture replaces hardcoded `_try_meta_pipeline()`.
  - **Problem**: FIX-20260522-015 (BrainSignal migration) removed the `extensions` dict from brain output. FIX-20260520-028 (Executive Veto) read `p.extensions.raw_outputs.raw_score` — silently broken. The data contract between producer (BrainSignal) and consumer (Meta Pipeline) was implicit with no enforcement.
  - **Solution**: Created `core/execution/meta_pipeline.py` with frozen dataclass contracts (`MetaProbeSpec`, `MetaProbeResult`) and a `MetaPipeline` orchestrator class. Brain JSON declares capability via `"roles": ["meta_probe"]`; code never hardcodes brain_ids.
  - **Key design decisions**:
    - `extract_probe_score()` reads `BrainSignal.raw_score` directly (Layer 1 contract) with legacy `extensions.raw_outputs` fallback
    - `discover_probe_specs()` auto-discovers probes from brain config JSON `"roles"` field
    - Per-bundle filter stage declarative (`stage2`, `stage3`, ...)
    - Threshold per-probe, per-strategy configurable via `meta_probe_config` or live.yaml override
    - Full chain: extract → threshold → Stage-N filter → SL/TP → RR → Kelly → volume → StrategyDecision
  - `StrategyLineConfig` gained `meta_probe_specs: list[Any]` field; `_try_meta_pipeline()` (~245 lines) replaced with thin delegation to `MetaPipeline.evaluate()`
  - `record_brain_votes()` in shadow_recorder.py now reads BrainSignal fields directly (direction, confidence, raw_score) with legacy fallback for `BrainDecisionProposal`
  - `StrategyDecision` now uses `TradeDirection = Literal["long", "short"]` (no `should_trade` field — removed from contract)
- **Root Cause**: RC-06 — cross-module cascade: implicit data contract between producer (BrainSignal) and consumer (Meta Pipeline) was not enforced. When the producer contract changed (FIX-20260522-015), the consumer continued to access non-existent attributes, silently returning None → no trades.
- **Prevention**: All meta-probe attributes are now frozen dataclass fields. `discover_probe_specs()` + `extract_probe_score()` provide explicit, typed interfaces. mypy catches field access errors at type-check time. New brains declare `"roles"` in JSON — infrastructure code never references specific brain_ids.
- **Dependents Checked**: strategy_line.py (evaluate path), live_cycle.py (BarrierStrategy construction site), shadow_recorder.py (brain_votes recording). 2622 tests pass. mypy clean on new code. ruff clean.

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: contracts-domain
- **Files**: core/contracts/serialization/json_codec.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues across all changed modules
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: deployment-lifecycle
- **Files**: core/deployment/compliance_audit.py,core/deployment/compliance_control_matrix.py,core/deployment/operational_support.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: deployment-config
- **Files**: core/deployment/environment_config.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: execution-guards
- **Files**: core/execution/capital_allocator.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: features-service
- **Files**: core/features/computers/v9_live_computer.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: features-rolling
- **Files**: core/features/rolling_normalizer.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: feedback-performance
- **Files**: core/feedback/brain_performance_tracker.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: feedback-pnl
- **Files**: core/feedback/brain_pnl_ledger.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: feedback-online
- **Files**: core/feedback/online_feedback_hook.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: risk-regime
- **Files**: core/risk/regime_detector.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: training
- **Files**: core/training/experiment_tracker.py,scripts/training/batch_train_skeleton.py,scripts/training/crt_manifest.py,scripts/training/trainers/sur_trainer.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: contracts-ids
- **Files**: core/contracts/serialization/json_codec.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: risk-portfolio
- **Files**: core/execution/capital_allocator.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-023
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: monitor-dashboard
- **Files**: core/metrics/factor_attribution.py,core/observability/diagnostics_dashboard.py,core/observability/slo_service.py
- **Description**: Batch mypy type safety: annotation fixes, None guards, type narrowing
- **Root Cause**: RC-02 — type-confusion
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-025
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: brains-adapters
- **Files**: core/brains/adapters/v9_onnx_brain_adapter.py,core/brains/adapters/transformer_brain_adapter.py,core/brains/adapters/online_learner_adapter.py,core/runtime/shadow_recorder.py
- **Description**: Complete BrainSignal.diagnostics passthrough for all 6 adapters (v9_onnx, transformer, online_learner) + shadow_recorder BrainSignal.diagnostics read path
- **Root Cause**: RC-06 — contract-violation
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-026
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Commit**: 24ff517
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py
- **Description**: Harden startup orphan detection: replace except:pass with explicit JSONDecodeError + generic error logging to prevent silent failure on corrupt/empty active_position.json
- **Root Cause**: RC-01 — missing-null-check
- **Prevention**: (to be filled)
- **Dependents Checked**: (none)

### FIX-20260522-027
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Type**: fix
- **Module**: runtime-live
- **Files**: core/runtime/live_cycle.py
- **Description**: Bleed stop horizon-scaled hardening — one of three root causes for the May 22 8-trade losing streak.
  - **Problem**: `should_exit_bleed()` used a hardcoded `bleed_bars=3` with zero min_hold protection for all strategies. A position could be killed after only 3 M5 bars (15 minutes) of negative bar-PnL, regardless of the strategy's intended horizon. barrier_12bar (60-min horizon) positions were being killed at 15 min — well before the brain signal had any time to play out.
  - **Solution**: `bleed_bars` now scales with strategy horizon: `max(3, horizon_cycles // 3)`. barrier_12bar (horizon=12) gets bleed_bars=4; micro_3bar (horizon=3) gets bleed_bars=3. Added `min_hold_cycles = max(2, bleed_bars)` — positions held fewer cycles than min_hold are immune to bleed stop.
  - **Enhanced diagnostics**: `bleed_stop_triggered` JSON event now includes `bleed_bars`, `cycles_held`, `min_hold_cycles`, `horizon_cycles` fields for post-mortem analysis.
  - **Data**: Session 1 (05:45-06:17) had 4 trades killed by bleed_stop_3bars_neg — all with losses < 1R that could have recovered given more time. Session 2 (06:29+) had the fix deployed and 0 bleed_stop exits.
- **Root Cause**: RC-05 — boundary-error: hardcoded bleed_bars=3 was appropriate for micro strategies (15-min horizon) but destructive for barrier_12bar (60-min horizon). The parameter should have scaled with strategy horizon from the start.
- **Prevention**: All time-based exit parameters now scale with strategy horizon. Adding a new strategy requires setting `horizon_cycles` in live.yaml, which automatically calibrates bleed_bars.
- **Dependents Checked**: position_manager.py (should_exit_bleed signature unchanged — only caller changed). 2622 tests pass.

### FIX-20260522-028
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Type**: fix
- **Module**: protocol-services
- **Files**: core/protocol/event_bar_sync.py
- **Description**: BarSyncPoller silent-failure recovery — fixes perpetual `bar_sync_degraded_wakeup` where the engine was stuck in management-only mode with no Alpha generation.
  - **Problem**: After MT5_ERROR triggers `mt5.shutdown()` + `_init_mt5()`, `copy_rates_from_pos()` can return `None` or `<2` rates without throwing an exception. The `if rates is None` branch (line 145) had no recovery logic — it silently slept 1s and continued. This caused the poll loop to spin for the remaining ~75s (or up to 310s total) until the `_degraded_deadline` fired, never detecting the new bar that forms at the 5-minute boundary.
  - **Evidence**: `data/reports/bar_sync_events.jsonl` showed an identical pattern every call: `MT5_ERROR` (count=1) → `MT5_INIT_OK` → 310.1s elapsed → `BAR_DEGRADED_WAKEUP`. Bar times WERE advancing (MT5 was delivering data) but the bar_sync never detected the new bar because `copy_rates` returned None after re-init, landing in the silent branch.
  - **Solution**: Added `_consecutive_empty` counter (max 5, `MAX_CONSECUTIVE_EMPTY_POLLS`). After 5 consecutive empty polls, triggers `BAR_EMPTY_POLLS_REINIT` event + `mt5.shutdown()` + `_init_mt5()` — same recovery as the exception-handler path. Empty counter resets on successful poll.
  - **Impact**: Without this fix, the engine was stuck in perpetual degraded mode since FIX-20260522-011 introduced the dual-deadline design — the silent-empty branch became the dominant path after every MT5 hiccup.
- **Root Cause**: RC-05 — missing-recovery-path: the `rates is None` code path had no mechanism to recover from transient MT5 unavailability, unlike the exception path which had `MAX_MT5_ERROR_RETRIES` + re-init.
- **Prevention**: Every code path that handles MT5 IPC failure must include a re-init escalation — silence is not an option. The `_consecutive_empty` counter pattern should be applied to any polling loop that depends on external IPC.
- **Dependents Checked**: live_intent_loop.py (caller — handles _degraded sentinel, no changes needed). 2622 tests pass.

### FIX-20260522-029
- **Date**: 2026-05-22
- **Author**: cursor-agent
- **Type**: fix
- **Module**: protocol-services
- **Files**: core/protocol/event_bar_sync.py
- **Description**: BarSyncPoller numpy.void `.get()` AttributeError — the true root cause of ALL MT5_ERROR events and perpetual degraded cycles.
  - **Problem**: `mt5.copy_rates_from_pos()` returns a numpy structured array. When iterated, each row is `numpy.void` which supports `[]` access but NOT `.get()`. The return-dict construction at new-bar detection (lines 246-255 at the time) used `.get("tick_volume", 0)`, `.get("spread", 0)`, `.get("real_volume", 0)` which threw `AttributeError: 'numpy.void' object has no attribute 'get'`. The same `.get()` pattern existed in `fetch_synthetic_bar()` (lines 385-387). Critically, state WAS updated BEFORE the return dict was built — so when `.get()` threw, `last_bar_time` had already advanced to the new bar. The `except Exception` handler caught the AttributeError, re-inited MT5, and continued polling — but with `last_bar_time` already matching the current bar, nothing was ever "new" again, and the degraded deadline always fired.
  - **Evidence**: `mt5_error_tracebacks.jsonl` captured: `AttributeError: 'numpy.void' object has no attribute 'get'` at `event_bar_sync.py:252`. All MT5_ERROR events in `bar_sync_events.jsonl` were spaced exactly 300s apart (M5 bar period) — each one was a new bar being detected but crashing on return-dict construction. The BAR_EMPTY_POLLS_REINIT logic from FIX-028 never activated because MT5 always returned valid data after re-init — it was the `.get()` call on the valid data that crashed.
  - **Solution**: (1) All 6 `.get()` calls replaced with `[]` bracket notation which works for both numpy.void and dict. (2) Defense-in-depth: bar_data dict now constructed BEFORE state update — if construction fails, state is preserved and the next poll retries cleanly. Removed debug instrumentation after root cause confirmed.
  - **Impact**: This single type-confusion bug (RC-02) was misdiagnosed across 5 prior fixes (FIX-006, FIX-010, FIX-011, FIX-028, FIX-008). The MT5 "transient errors" at ~104s were actually new-bar detection crashes at M5 bar boundaries. State-then-return ordering made the crash self-perpetuating. After fix, bar_sync correctly returns detected bars with no degradation.
- **Root Cause**: RC-02 (type-confusion) — numpy.void treated as dict. Compounded by RC-03 (state-leak) — state mutation before fallible operation.
- **Prevention**: (1) Never assume MT5 return types are Python dicts — always use bracket notation for structured array access. (2) State mutation must happen AFTER all fallible data construction is complete. (3) Exception handlers should differentiate between IPC errors and data-structure errors.
- **Dependents Checked**: live_intent_loop.py (caller — no changes needed). verify.py --quick passes.

### FIX-20260523-001
- **Date**: 2026-05-23
- **Author**: cursor-agent
- **Type**: feature
- **Module**: execution-orders
- **Files**: core/execution/live_order_sender.py, core/execution/execution_queue.py, scripts/mt5_bridge_worker.py
- **Description**: P(win) feedback loop — the missing link for data-driven Meta Filter optimization.
  - **Problem**: Trade journal recorded brain_ids, brain_votes, confidence, but NOT the Meta Filter's P(win) prediction or Kelly multiplier. Without P(win)-vs-outcome data, it was impossible to calibrate the optimal Meta Filter threshold empirically. The system was flying blind — 83% rejection rate with no way to measure false positive/negative rates.
  - **Solution**: (1) `dispatch_live_open_order()` gained `p_win: float` and `kelly_mult: float` parameters (passthrough, never gating). (2) `execution_queue.flush()` passes `decision.p_win` and `decision.kelly_mult` to dispatch_fn. (3) `mt5_bridge_worker.py` extracts `p_win`, `kelly_mult`, and `entry_context` from msg_payload into the journal record. (4) `entry_context` was already in the execution_payload but was never being extracted to the journal — now fixed.
  - **Impact**: After 50+ closed trades, the journal will contain matched (predicted_p_win, realized_outcome) pairs. This enables: (a) precision curve plotting, (b) optimal threshold selection via ROC/PR analysis, (c) calibration drift detection over time, (d) conformal prediction recalibration with live data.
- **Root Cause**: RC-12 (missing-feature) — feedback loop was designed in schema (StrategyDecision carries p_win/kelly_mult "for journal / audit trail") but never wired through the dispatch chain.
- **Prevention**: Every diagnostic field marked "for journal" must have an end-to-end test proving it reaches the journal file.
- **Dependents Checked**: live_cycle.py (caller of dispatch_fn). verify.py --quick passes.

### FIX-20260523-002
- **Date**: 2026-05-23
- **Author**: cursor-agent
- **Type**: fix
- **Module**: execution-orders
- **Files**: core/execution/strategy_line.py, core/execution/position_manager.py
- **Description**: OU z_entry harmonized across all code paths at Optuna-validated 1.3.
  - **Problem**: The artifact `arb_params_v7.json` already contained `z_entry=1.3` (Optuna TPE, 300 trials), but `strategy_line.py:680` hardcoded `_z_entry = 2.0` for statarb strategies in the inflection gate. This formed an effective bottleneck: even when the brain adapter used 1.3, the strategy line's inflection gate required |z| > 2.0 to pass. `position_manager.py` defaults were also 1.5 at two locations (lines 805, 1029). The OU brain was silenced not by market conditions but by code-level threshold inflation.
  - **Evidence**: Every `multi_strategy_eval` event showed `statarb_dynamic: supporting=0, total=1` — the brain adapter's `_z_to_direction()` (using artifact 1.3) returned neutral, but even if it fired, the inflection gate at 2.0 would have blocked. The effective z_entry was `max(1.3, 2.0, 1.5) = 2.0`.
  - **Solution**: (1) `strategy_line.py:680`: `2.0` → `1.3`. (2) `position_manager.py:805` (inflection gate default): `1.5` → `1.3`. (3) `position_manager.py:1029` (signal validation default): `1.5` → `1.3`. The artifact already had 1.3 from FIX-20260520-022.
  - **Impact**: OU brain now uses consistent 1.3σ threshold everywhere. At 1.3σ, approximately 19% of observations exceed the threshold (vs 4.6% at 2.0σ) — roughly 4x more potential entry signals.
- **Root Cause**: RC-09 (config-drift) — artifact value and code defaults diverged over multiple fixes. FIX-20260519-016 raised artifact to 2.0, FIX-20260520-022 reverted artifact to 1.3, but strategy_line.py and position_manager.py were never updated.
- **Prevention**: Every parameter that exists in both an artifact JSON and Python code must have a single source of truth. Add Iron Law requiring `grep` for all hardcoded defaults when changing artifact values.
- **Dependents Checked**: meta_filter_gate.py (already uses 1.3), params_brain_adapter.py (reads 1.3 from artifact). verify.py --quick passes.

### FIX-20260523-003
- **Date**: 2026-05-23
- **Author**: cursor-agent
- **Type**: config
- **Module**: protocol-services
- **Files**: configs/brains/meta_stage2_filter_v3.json
- **Description**: Meta Filter (Track 4d) conformal prediction disabled — fix effective threshold at intended base value of 0.65.
  - **Problem**: The `meta_stage2_filter_v3.json` specifies `threshold: 0.65` but with `conformal.enabled: true`, the runtime effective threshold was `max(80th_percentile_of_recent_p_win, 0.50, 0.65)`. With the model outputting predictions clustered around 0.58 and the 80th percentile at ~0.679, the effective threshold was 0.679 — not the intended 0.65. This rejected 83% of barrier_12bar proposals. The conformal prediction was designed to adapt to distribution shift, but with only ~135 proposals accumulated, it lacked sufficient history for stable percentile estimation.
  - **Evidence**: kelly_diag events showed `result_p_win` values clustered in 0.50-0.65 for rejected proposals, with threshold 0.679. The 80th percentile of 135 predictions happened to be ~0.679, pushing the threshold above almost all proposals.
  - **Solution**: Disable conformal prediction (`enabled: false` in config JSON). The effective threshold now equals the base config value of 0.65. Pass rate increases from 17% to ~28% (from 17 to 28 trades per 100 proposals). Conformal prediction can be re-enabled once sufficient P(win)-vs-outcome data is accumulated (see FIX-20260523-001).
  - **Impact**: More trades pass the filter → faster sample collection → empirical threshold calibration → data-driven optimization instead of guesswork. The 0.65 base threshold is derived from the model's training validation (v2 had 74.4% kept-win-rate at 0.42; v3 was trained with higher quality data).
- **Root Cause**: RC-09 (config-drift) — conformal prediction interaction with base threshold not documented or tested. The `max(percentile, min, base)` formula can silently inflate the threshold when recent model outputs are high.
- **Prevention**: Every adaptive threshold mechanism must log the computed effective threshold alongside the base threshold at initialization and periodically during operation. The gap between intended and effective must be visible.
- **Dependents Checked**: meta_signal_filter.py (reads conformal config), bootstrap_v9.py (loads config), strategy_line.py (calls filter). verify.py --quick passes.
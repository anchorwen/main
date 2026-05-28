# Runtime / Live

## Purpose
The central live trading cycle orchestration. Wires together market data ingress, signal pipeline, strategy evaluation, execution pipeline, risk gating, order dispatch, shadow recording, and daily operations.

## Key Files
| File | Role |
|------|------|
| `core/runtime/live_cycle.py` | Main `LiveCycle` class — the trading loop (ingress → signal → execute → dispatch) |
| `core/runtime/signal_pipeline.py` | `_ensemble_proposals()` — merges brain proposals into ensemble votes |
| `core/runtime/market_ingress.py` | ATR retrieval, mid price, position count, regime gate bootstrap |
| `core/runtime/order_dispatch.py` | SL/TP computation, risk evaluation, feature snapshot, SL streak tracking |
| `core/runtime/execution_pipeline.py` | `RuntimeExecutionPipeline` — strategy→gate→route→quality chain |
| `core/runtime/signal_health.py` | `FeatureGate` — data freshness, ATR anomaly, drift, spread checks |
| `core/runtime/shadow_recorder.py` | `record_brain_votes()` — per-brain vote recording to ledger |
| `core/runtime/execution_gates.py` | `RuntimeRiskGate`, `RuntimeGovernanceGate`, `RuntimeExecutionApprovalChain` |
| `core/runtime/alpha_risk_budget_gate.py` | `AlphaRiskBudgetGate` — alpha budget enforcement |
| `core/runtime/alpha_budget_contracts.py` | Budget contract validators |
| `core/runtime/alpha_budget_usage_store.py` | `AlphaBudgetUsageStore` |
| `core/runtime/alpha_budget_usage_reporter.py` | `AlphaBudgetUsageReporter` |
| `core/runtime/cycle_replay.py` | `RuntimeCycleReplay` — cycle replay and reconciliation |
| `core/runtime/evidence_reader.py` | `RuntimeEvidenceReader` |
| `core/runtime/evidence_writer.py` | `RuntimeEvidenceWriter` |
| `core/runtime/summary_service.py` | `RuntimeSummaryService` |
| `core/runtime/execution_gateway_router.py` | `ExecutionGatewayRouter` |
| `core/runtime/integration_contracts.py` | `RuntimePipelineResult`, `OrderSizingPolicy` |
| `core/runtime/signal_order_builder.py` | `SignalOrderRequestBuilder` |
| `scripts/daily_ops.py` | Daily ops pipeline — feedback, retraining, governance, alpha lifecycle orchestration |

## Data Flow
```
┌──────────────────────────────────────────────────────────┐
│                    LiveCycle.run_cycle()                  │
│                                                          │
│  1. market_ingress → ControlSnapshot                     │
│  2. FeatureService → feature vectors                     │
│  3. BrainRunService → BrainSignal[]                       │
│  4. signal_pipeline → ensemble votes                     │
│  5. StrategyLine.evaluate() × 4 strategies               │
│  6. PortfolioRiskController → cross-strategy risk        │
│  7. ExecutionQueue → staggered dispatch                  │
│  8. shadow_recorder → decision ledger                    │
│  9. signal_health → drift/anomaly checks                 │
│ 10. daily_ops (on schedule)                              │
└──────────────────────────────────────────────────────────┘
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| alpha | Budget contracts | Alpha risk budget |
| brains | DynamicBrainWeighter | Vote weight computation |
| contracts | ids, enums, domain types | Core types |
| deployment | ServiceContainer, LifecycleManager | DI and lifecycle |
| execution | BarrierStrategy, MicroStrategy, StatArbStrategy, SwingStrategy, ExecutionQueue, PortfolioRiskController, StrategyBudget, RegimeGate | Strategy execution |
| features | FeatureService | Feature resolution |
| feedback | FeedbackLoop, OnlineFeedbackHook | Post-trade feedback |
| governance | GovernanceService | Brain lifecycle |
| ledger | Ledger services | Event persistence |
| market | Position tracking | Position context |
| observability | Metrics, alerts | Monitoring |
| parliament | Contract groups | Brain grouping |
| state | SystemModeStore, OverrideStore | Runtime state |
| strategies | Strategy contracts | Plugin system |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| apps/engine/ | LiveCycle, various types | CLI entry points |
| deployment/lifecycle | (via ServiceContainer) | Lifecycle management |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260528-016 | 2026-05-28 | cursor-agent | — | `_build_meta_feature_vector()` was still building 43-dim vectors (40 V9 + 3 OU) after retrained Meta_Stage1_MetaLabel_Binary_V1 model was trained on 40-dim V9-only features. Fix: removed OU feature concatenation (ou_z_score, ou_half_life, ou_theta) from feature vector assembly — OU params still computed for diagnostic logging but no longer appended to vector. Length checks updated 43→40. Removed early return on OU params failure (now diagnostic-only, doesn't block vector build). | RC-06 |
| FIX-20260528-013 | 2026-05-28 | cursor-agent | — | meta_signal_filter.py: redirected 3 `print()` calls (conformal_warning, calibrator_load_error, meta_filter_unavailable) to `sys.stderr` — JSON events were polluting stdout and breaking CLI `--json` output when calibrator load failed. Also fixed Meta_Stage2_Filter_V3 calibrator_path (.meta.json manifest → empty string) to prevent spurious calibrator load errors. | RC-06 |
| FIX-20260528-011 | 2026-05-28 | cursor-agent | — | Reentry guard TTL: live_cycle.py now passes `entry_half_life` from StrategyDecision to `ReentryState.check_and_record_entry()`, enabling TTL hard unlock in reentry_guard.py. Added `entry_half_life` + `ttl_seconds` to reentry_check diagnostic log for live monitoring. | RC-06 |
| FIX-20260528-014 | 2026-05-28 | cursor-agent | — | Config SSOT hygiene: renamed 5 brain config files to match brain_id, updated live.yaml + bootstrap_v9.py path references. Resolved magic 90001 collision: Meta_Stage1_Huber_V1 (frozen) magic 90001→90011. Eliminates 5 SSOT_VIOLATION warnings + 1 magic collision at startup. | RC-09 |
| FIX-20260527-010 | 2026-05-27 | cursor-agent | — | Phase 1A: RegimeGate fail-open→fail-closed. Replaced `regime_gate = None` on ANY exception (silently disabling ALL trend guards for the cycle) with stale counter logic: ≤12 cycles → use last valid regime_gate, >12 cycles → `RegimeGate.default_fail_closed()` (all strategies "shadow"). Added `_regime_gate_stale_counter` to `LiveCycleState`, reset on successful classify(). Architect guardrail: fail-closed blocks new entries only — Exit Manager continues to manage existing positions. | RC-06 |
| FIX-20260527-009 | 2026-05-27 | cursor-agent | — | Defensive try/except on `micro_feature_computer.compute_all()` call + traceback capture in cycle_error handler. `compute_all()` was unprotected — any exception (including the OFI tick index OverflowError from FIX-20260527-009 in microstructure_computer.py) propagated to the outer exception handler and logged as generic `cycle_error` without traceback. Fix: wrapped `compute_all()`/`build_model_input()` in try/except with zeros fallback; enhanced cycle_error log with `traceback` + `error_type` fields. | RC-06 (missing-guard, lost-diagnostic) |
| FIX-20260527-005 | 2026-05-27 | cursor-agent | — | Cold exploration trailing bypass in management phase: `if not getattr(pos, "cold_explore", False)` guards Layer 1 Chandelier trail. Cold explore trades run to hard SL/TP collecting uncensored ConformalOU labels. `cold_explore` threaded from `StrategyDecision` → `register_position()` → `ActivePosition`. | RC-12 |
| FIX-20260527-003 | 2026-05-27 | cursor-agent | — | Remove hardcoded brain ID fallbacks: `bootstrap_v9.py` 3× direct `brain_entry["brain_id"]` access replaces stale defaults (Meta_Stage1_Huber_V1, Online_MLP_V1, DeepResMLP_V1_Institutional). `meta_pipeline_wired` log now reads brain_id from config. | RC-09 |
| FIX-20260527-002 | 2026-05-27 | cursor-agent | — | Brain performance data contamination root fix: `daily_ops.py` now passes `BrainPnLStore` to `run_governance_cycle()` so governance uses clean per-brain P&L data (PnL-first path) instead of falling back to tracker composite_scores contaminated by `_find_brains_by_time()`. | RC-11 |
| FIX-20260527-001 | 2026-05-27 | cursor-agent | — | Governance auto-freeze recovery: daily governance froze Meta_Stage1_Binary_Cls_V1 and retired OU_Params_V6_Sniper on 2026-05-26 22:02 UTC — both based on contaminated brain_performance records (all 6 brains share identical 84 records). Restored both to probation with correct vote_weight. Without this fix, barrier_12bar and statarb_dynamic had zero active voters — entry precision fixes (FIX-20260526-041) impossible to exercise. | RC-11, RC-09 |
| FIX-20260526-042 | 2026-05-26 | cursor-agent | — | barrier_12bar shadow→probation activation: Full Pipeline Rebuild complete — Meta_Stage2_Filter_V3 (48-dim LGB+Platt, Forward Sharpe 1.30) wired through Track 4d gate chain. live.yaml mode→probation, base_volume→0.01, max_volume→0.01. Budget unchanged: daily_loss -3%, max 5 consecutive losses. Probation validates train-serve skew elimination with real capital at micro size. | RC-09, RC-06 |
| FIX-20260525-023 | 2026-05-25 | cursor-agent | — | M15 strategies SL/TP instant stop-out: `_evaluate_strategy_lines()` set `_effective_mid` to `MTFPriceService.latest_m15_close` (close of just-completed M15 bar, ~4571.8) instead of current spot `mid_price` (~4574.5). SL computed from stale reference → 2.7-point price gap → effective SL cut from 5.22 ATR to 2.56 ATR (1.07× raw M5 ATR) → MT5 native SL fired within 4 minutes. Fix: remove M15 close override; `_effective_mid` always uses spot mid_price. M15 boundary gating (cycle-level skip at non-00/15/30/45 minutes) is sufficient to prevent future function leakage. | RC-05 (reference-frame-mismatch) |
| FIX-20260525-024 | 2026-05-25 | cursor-agent | — | MIA close journal gap + stale state + reentry block: (a) `_execute_management_phase()` detected position MIA (closed in MT5) but removed it from known_open_tickets without writing close journal entry — reconciliation never saw it → permanent journal hole. Fix: `_build_mia_close_entry()` + `_enrich_mia_from_deals()` construct close entry at detection point, stored in `state._pending_mia_closes`, consumed by caller which writes to journal (with FileLock), records exit for reentry guard, and saves position state immediately. (b) `reentry_guard.py` classifier had no pattern for mia_close/unknown_close/manual_close → fell into "unknown" category with NO timeout → permanent same-direction reentry block. Fix: added "unknown_close" category with 900s timeout + confidence check; converted catch-all "unknown" from permanent to 900s timeout. (c) `exit_watchdog.py` retried against non-existent positions (MIA) → false CRITICAL alerts. Fix: pre-flight position-open check skips retry loop if position already closed. | RC-05 (missing-close-journal), RC-06 (stale-state), RC-07 (no-timeout-permanent-block) |
| FIX-20260525-026 | 2026-05-25 | cursor-agent | — | MetaLabel 43-dim train-serve feature order skew: `_build_meta_feature_vector()` assembled features in V9 schema order (M5→H1, OU_Theta/Hurst trailing) instead of brain config training order (H1→M5, OU_Theta/Hurst inline per-TF). All 43 features present but in completely scrambled positions — LightGBM position-based indexing → model received random noise → shadow mode validation garbage. Fix: Step 4 now reads authoritative `features` list from brain config (or model metadata fallback) and assembles in exact training order. 6 new unit tests in `tests/unit/test_meta_feature_vector.py` validate positional accuracy. | RC-06 (contract-violation — feature vector assembly order didn't match training contract) |
| FIX-20260525-025 | 2026-05-25 | cursor-agent | — | Shutdown SIGINT shield: `live_intent_loop.py` shutdown `finally` block saved 6 state files (positions, rolling_norm, regime_detector, tracker, pnl_ledger, meta_signal_filter) without signal protection. A SIGINT/Ctrl+C during save would corrupt state files mid-write. Fix: wrapped all save operations in `signal.signal(signal.SIGINT, signal.SIG_IGN)` shield with `try/finally` restore. Combined with existing atomic write pattern (tmp→replace), this is defense in depth against shutdown corruption. | RC-04 (race-condition — signal during save corrupts state) |
| FIX-20260525-022 | 2026-05-25 | cursor-agent | — | Budget guard calibrated to 30% WR reality: statarb_dynamic max_consecutive_losses 4→7 + daily_loss_limit_pct -1.5%→-3.0%; statarb_m15 max_consecutive_losses 3→7 + daily_loss_limit_pct -1.0%→-2.0%. P(4 consecutive losses) = 24% at 30% WR — fires almost daily on normal variance. P(7) = 8.2% — rare enough to warrant review without false-triggering. | RC-05 |
| FIX-20260525-020 | 2026-05-25 | cursor-agent | — | Bleed stop abolished for OU/mean-reversion: statarb strategies now skip the 3-bar consecutive-negative bleed_stop check. OU enters at trend extremes — price continuing in the same direction is normal rubber-band stretching, not thesis failure. Bleed stop is a trend-following heuristic; applying it to mean-reversion is a category error. | RC-06 |
| FIX-20260525-019 | 2026-05-25 | cursor-agent | — | M15 OU warm-start starvation: M15 OU brain (window=280 M15 bars) was bootstrapped from 300 M5 bars via resampling (every 3rd bar → ~100 M15 bars, 缺口 180). Changed statarb_m15 to fetch 350 M15 bars directly (MT5_TIMEFRAME_M15=15). Requires restart to take effect — buffer fills immediately with 350 bars ≥ 280 window, eliminating cold-start z_score=0 deadlock. | RC-05 |
| FIX-20260525-017 | 2026-05-25 | cursor-agent | — | Startup reconciliation gap: first-cycle `known_open_tickets` filter (line 3777) now reconciles positions closed during downtime BEFORE filtering. Gone tickets are reconciled via `_reconcile_closed_positions()` and close journal entries written with SL/TP/external reason + PnL. Prevents permanent journal gaps (e.g. order 3609962737: 2 open-related events, 0 close). | RC-05, RC-06 |
| FIX-20260525-016 | 2026-05-25 | cursor-agent | — | Per-strategy min_p_win wiring: `_build_strategy_lines()` now reads `min_p_win` from YAML via `_cfg()` for statarb_dynamic + statarb_m15. Default 0.50 preserved. YAML sets 0.45 for OU strategies (empirical WR 49.7%, RR 2:1→breakeven 33.3%). | RC-05, RC-12 |
| FIX-20260525-014 | 2026-05-25 | cursor-agent | — | Gate audit observability: (1) `gate_audit_recorder.py` — thin JSONL recorder writing per-cycle gate block diagnostics to `data/gate_audit/{date}.jsonl`; (2) `StrategyDecision.gate_diag` dict field for per-gate diagnostics; (3) 3 gate instrumentation points in strategy_line.py (ConformalOU — composite_score/threshold/z_score/half_life/theta/adx; parliament — confidence/threshold/direction; counter_trend — signal/trend direction/strength); (4) `record_gate_block()` call in live_cycle.py when `should_trade=False`. | RC-07, RC-12 |
| FIX-20260525-012 | 2026-05-25 | cursor-agent | — | Phase 4 Dynamic SL/TP Calibration: asymmetric volatility regime response per strategy family. _STRATEGY_FAMILY_MAP in _build_strategy_lines() auto-infers family (statarb→mean_reversion, else→trend_following) with YAML override priority. All 12 StrategyLineConfig blocks pass strategy_family from config or map. Dynamic ref_atr from regime_info wired in strategy_line.evaluate(). | RC-12 |
| FIX-20260525-011 | 2026-05-25 | cursor-agent | — | BarSyncPoller timeout decoupled from M5 timeframe: `--bar-sync-timeout` value now subject to dynamic floor `max(360, bar_seconds×1.5)` in BarSyncPoller.__init__. CLI help text updated. | RC-05 |
| FIX-20260525-010 | 2026-05-25 | cursor-agent | — | Phase B+C: TrailPolicy wired from live.yaml exit.* → register_position(). Phase C: TrailPolicy import source corrected to canonical `core.execution.trail_stop_engine` (was re-export via position_manager). TrailStopEngine is now the physically isolated Risk Exit file with zero knowledge of strategy/brain/confidence/consensus. | RC-12 |
| FIX-20260525-009 | 2026-05-25 | cursor-agent | — | MT5 worker refactoring: live_cycle.py (~30 MT5 call sites → worker), market_ingress.py (worker param, hardcoded constants), order_dispatch.py (eliminate daemon threads from _build_risk_context), live_intent_loop.py (worker lifecycle — start/stop/singleton). | RC-04, RC-06 |
| FIX-20260524-034 | 2026-05-24 | cursor-agent | — | Meta-labeler production deployment: barrier_12bar_meta strategy line (BarrierStrategy, magic=90014). OU feature augmentation via _build_meta_feature_vector (40 V9 raw + 3 OU with z_score clipped [1.3, 2.5]). _evaluate_strategy_lines accepts meta_feature_vector. LiveCycleState._last_ou_params for diagnostics. Brain config: status→probation, vote_weight→0.8. live.yaml: mode→probation, volume→0.01. | feature |
| FIX-20260524-036 | 2026-05-24 | cursor-agent | — | Brain SL/TP audit: barrier_12bar SL/TP corrected 2.0/3.5→3.0/1.5 to match retrained calibration. Strategy Parameter Reference updated with current brain roster + barrier_12bar_meta section. Binary_Cls_V1 status comment clarified. | RC-09 |
| FIX-20260524-033 | 2026-05-24 | cursor-agent | — | Batch mypy type safety: bootstrap_v9.py (6→0 — assert container services), live_micro_rollout_gate.py (2→0 — assert dispatcher/health_check), live_read_only_preflight.py (7→0 — assert services + dict[str, Any] annot), communication_ops_cli.py (2→0 — result annotation + None guard). MODULE_SOURCE_MAP: add communication_ops_cli.py to runtime_live. | type-confusion |
| FIX-20260522-027 | 2026-05-22 | cursor-agent | — | Bleed stop horizon-scaled hardening: `bleed_bars` now scales with strategy horizon (`horizon//3`, min 3) + `min_hold_cycles` protection prevents bleed stop from firing before position has reasonable time to develop. Enhanced `bleed_stop_triggered` JSON event with bleed_bars, cycles_held, min_hold_cycles, horizon_cycles. Root cause #2 of May 22 8-trade losing streak. | RC-05 (boundary-error) |
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add v9_shadow_sse.py, _diag_cycle_stall.py, live_daily_recap.py. Mypy fixes: v9_shadow_sse (3→0 — Generator annotations, data_lines: list[str]), _diag_cycle_stall (2→0 — result/exc_info list[int|None]/list[Exception|None]), live_daily_recap (1→0 — sys.stdout.reconfigure union-attr). Baseline 30→19. | RC-02 |
| FIX-20260524-007 | 2026-05-24 | cursor-agent | — | Track 3d Conformal OU Gate wiring: ConformalOUGate initialized alongside MetaFilterGate with shared ConformalCalibrator. New LiveCycleState._conformal_ou_gate attribute. Gate passed to all evaluate_all_strategies() call sites (live + shadow). OU configs auto-discovered from configs/brains/. | RC-06, RC-12 |
| FIX-20260524-001 | 2026-05-24 | cursor-agent | — | live_intent_loop.py: verify_startup_integrity() now uses auto_repair=True — auto-registers disk brains missing from governance as candidate. Eliminates manual governance_state.json registration for new brains. | RC-09 |
| FIX-20260524-002 | 2026-05-24 | cursor-agent | — | Layer 1 trailing stop premature exit fix: trailing stop now respects `min_hold_cycles` (previously ran from cycle 1 with no protection), breakeven_threshold_atr lowered 1.5→1.0 for barrier_12bar. Root cause of Meta_Stage1_Huber_V1 -369.65R loss: trailing stop tightened hard SL from cycle 1 causing exits at 0.5-1.0R instead of designed 2.0R SL. | RC-05 (boundary-error — protection gap in Layer 1) |
| FIX-20260523-008 | 2026-05-24 | cursor-agent | — | Track 3d ConformalCalibrator full pipeline: live_cycle.py lazy-inits ConformalCalibrator alongside MetaFilterGate, cold-starts from journal, passes as calibrator. daily_ops.py creates calibrator, passes to both hooks, returns conformal diagnostics. End-to-end: journal → daily_ops → calibrator.update → state file → live_cycle → MetaFilterGate.filter(adaptive). | calibrator lifecycle for Track 3d |
| FIX-20260523-007 | 2026-05-23 | cursor-agent | — | Mini-batch online learning integration: `_step_online_feedback()` now creates ExperienceReplayBuffer, passes to both live+paper hooks. Buffer collects 20 closed trades → R-weight expansion → Fisher-Yates shuffle → sequential partial_fit. Only calls adapter.save_weights() if buffer flushed. Returns buffer_size, buffer_ready, running_r_mean, class_dist diagnostics. | RC-06, RC-12 |
| FIX-20260524-046 | 2026-05-24 | cursor-agent | — | DEFERRED: MT5 thread model architecture debt (T1-C1/C2/C3) — per-call daemon threads, repeated init/shutdown, non-thread-safe methods. Requires dedicated MT5 worker thread + session-level init. Short-term mitigations already in place. | RC-04, RC-06 |
| FIX-20260522-026 | 2026-05-22 | cursor-agent | 24ff517 | Harden startup orphan detection: replace except:pass with explicit JSONDecodeError + generic error logging to prevent silent failure on corrupt/empty active_position.json | missing-null-check |
| FIX-20260522-024 | 2026-05-22 | cursor-agent | — | Config-driven MetaPipeline: live_cycle.py now auto-discovers meta_probe specs from brain JSON `"roles": ["meta_probe"]` + live.yaml overrides. shadow_recorder.py `record_brain_votes` reads BrainSignal fields directly (direction, confidence, raw_score) with legacy fallback. | RC-06 (cross-module cascade) |
| FIX-20260521-001 | 2026-05-21 | cursor-agent | — | High Recall + High Precision: MetaFilterGate threshold 0.50→0.60. Loose upstream (Huber confidence 0.25) generates more candidates; tight downstream MetaFilter (0.60) filters noise. Validation: blind WR 54.1% → filtered 64.6%, PnL +15R → +29R. | RC-09 |
| FIX-20260521-002 | 2026-05-21 | cursor-agent | — | live_intent_loop.py _load_brain_entries_from_dir() bypassed enabled:false — loaded ALL JSONs from configs/brains/ directly. Added _source_path tracking + live.yaml filtering to exclude disabled brains before schema validation. | RC-09 |
| FIX-20260520-022 | 2026-05-20 | cursor-agent | — | OU z_entry revert 2.0→1.3: 16h 0 signals traced to FIX-20260519-016 overcorrection. arb_params_v7.json optimal_params.z_entry restored to Optuna-validated 1.3 (all top-10 trials converged on 1.3). Half-life discount retained — the real quality filter. | RC-05 (boundary-error — 2.0 toowide) + RC-09 (config-drift — artifact diverged from Optuna) |
| FIX-20260519-021 | 2026-05-19 | cursor-agent | — | Brain contract mismatch hard mute: `_warn_contract_mismatch()` upgraded from soft warning to hard enforcement — sets `brain_info["vote_weight"] = 0.0` on any brain whose `training_contract` doesn't match `strategy_requires`. Prevents "zombie voting" where XGBoost/LightGBM brains with misaligned feature schemas produce random-confidence outputs that corrupt parliament consensus. Adds `brain_hard_muted_contract` event with previous_weight and action_required fields. | RC-06 (contract-violation, zombie-decision) |
| FIX-20260519-018 | 2026-05-19 | cursor-agent | — | management phase silently killed by `UnboundLocalError` from position_manager: `_update_single_position()` M5 OHLC branch missing `r_now` assignment → crash caught by `except Exception: pass` at line 3835 → trail/breakeven/trail-TP never executed. Added `management_phase_diag` JSON event (all key decision variables: trail candidate, breakeven trigger, reasons, final SL/TP) printed every cycle before dispatch. | RC-06 (contract-violation, silent-error-swallowing) |
| FIX-20260519-017 | 2026-05-19 | cursor-agent | — | Four-Pillar Architecture (P1-P4) + Strategy-level enabled enforcement: (P1) `_execute_management_phase()` fetches M5 bar OHLC for calibrated extreme tracking with graceful IPC degradation. (P4) Ghost-volume audit with `mt5.positions_get()` ground truth + `expected_remaining_volume` synced on partial_tp/net_out. Also: `_build_strategy_lines()` now enforces `enabled: false` from live.yaml at the strategy level — clears brain lists before construction so disabled strategies (e.g. m30_swing) cannot open positions regardless of shadow brain votes. | RC-06 (sampling-blind-spot, ghost-volume, config-drift) |
| FIX-20260519-014 | 2026-05-19 | cursor-agent | — | brain_votes诊断盲区修复: record_brain_votes()新增raw_outputs字段(z_score/theta/half_life/mu等)透传。之前OU脑冻结无法从brain_votes确认是趋势市场(theta≤0)还是buffer问题—现在每个周期可见完整OU参数。 | RC-06 |
| FIX-20260519-012 | 2026-05-19 | cursor-agent | — | SL/TP距离保底管道接线: live_cycle.py全部11策略StrategyLineConfig构造点新增min_sl_distance+min_rr_ratio从YAML sl块读取透传 | RC-05 |
| FIX-20260519-011 | 2026-05-19 | cursor-agent | — | 周期感知分层出场架构(Waves A-D): (A) live_intent_loop.py新增apply_timeframe_scaling()—YAML人类可读值→M5 bar自动换算; live.yaml所有策略+timeframe字段; StrategyLineConfig+timeframe/timeframe_mult; (C) _manage_position() Meta Exit构建meta_consensus时按_tf_mult过滤group_signals—同级别及以上才参与投票 | RC-05, RC-06 |
| FIX-20260519-010 | 2026-05-19 | cursor-agent | — | 三轨制归因接线: settle_all前新增update_pending(mid_price)调用(Track 2 MFE/MAE追踪+TTL递减); record_signal从BrainRegistry获取training_horizon→expected_horizon参数(Track 1); dispatch时构建brain_votes从raw_proposals→dispatch_live_open_order→journal(Track 3); known_open_tickets新增brain_votes存储 | RC-06 |
| FIX-20260519-009 | 2026-05-19 | cursor-agent | — | config→code管道修复: live_intent_loop.py读取live.yaml时同步提取live_trading.volume/risk_budget_usd/equity_risk_pct→LiveCycleConfig。之前仅strategy_lines被读取,live_trading段完全忽略→risk_budget_usd恒为5.0→vol-targeted sizing恒0.01。同步更新LiveCycleConfig默认值risk_budget_usd→10.0,exit_breakeven_threshold_atr→1.5 | RC-09 |
| FIX-20260519-006 | 2026-05-19 | cursor-agent | — | 机构级参数校准: (P1) barrier_12bar hesitation_cycles 2→4; (P3) breakeven_threshold_atr 1.0→1.5; min_sl_step 0.005→0.15(15pip防抖); LiveCycleConfig.exit_min_step默认0.005→0.15 | RC-05 |
| FIX-20260519-005 | 2026-05-19 | cursor-agent | — | PnL盲区根治: (A) _dispatch_managed_close()通过watchdog退出时PnL被丢弃(100%丢失率),4个调用点传入pnl; (B) reconciliation三层PnL回退: entry_price→open_entry.entry_price, close_price→state._recent_mid_prices, pnl→engine_close_pnl | RC-06 |
| FIX-20260518-042 | 2026-05-18 | cursor-agent | — | PnL recording fix: entry_price now resolved from MT5 history_deals_get (deal.entry==0, actual fill price) instead of journal request.price which is 0 for market orders. 94% of close events had pnl=null because entry_price was always None. Also tightened the None guard to require >0 values. | contract-violation |
| FIX-20260518-043 | 2026-05-18 | cursor-agent | — | base_volume priority fix: changed 11 strategy construction sites from `config.volume or _cfg(name, base_volume, 0.01)` to `_cfg(name, base_volume, None) or config.volume or 0.01`. Python `or` with truthy float 0.01 always evaluated global, ignoring strategy-specific base_volume (e.g. statarb_dynamic 0.02 was overridden to 0.01). | contract-violation |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 3 B4: Volume decay with micro-lot defense integrated into live_cycle — consecutive same-direction entries get scaled volume via apply_reentry_volume_scale(), hard blocked when penalty ineffective. Wave 4 E1: reentry_check JSON diagnostic event after check_and_record_entry (fields: strategy, direction, confidence, allowed, reason, elapsed_since_exit, last_exit_category, consecutive_same_dir). Wave 4 E3: exit_recorded JSON event after record_exit() in _dispatch_managed_close (raw_reason + classified_category). | RC-06 |
| FIX-20260521-004 | 2026-05-21 | cursor-agent | — | Intent进程崩溃循环修复：live_intent_loop.py在multi-brain模式下load_brain_entry()调用改为条件执行(if not args.multi_brain)，避免因单一brain entry默认路径指向已删除配置而exit(2)。 | RC-09 |
| FIX-20260522-002 | 2026-05-22 | cursor-agent | — | _dispatch_managed_close silently lost position tracking on ExitWatchdog failure: known_open_tickets.pop() + clear_position() ran unconditionally. Added _close_dispatched guard — tracking removal now gated on confirmed close success. Affects all managed exit paths. | RC-06 |
| FIX-20260522-003 | 2026-05-22 | cursor-agent | — | Strategy-level enabled:false check in _build_strategy_lines used dict-key reassignment (_known_groups[name] = []) instead of in-place .clear(). Local variable references immune to reassignment — latent bug currently masked by brain-level filter. | RC-06 |
| FIX-20260522-004 | 2026-05-22 | cursor-agent | — | Journal confidence E2E: live_cycle.py direct dispatch path now passes confidence=confidence to dispatch_live_open_order(). Works with execution-orders side pipeline fix. | RC-06 |
| FIX-20260522-005 | 2026-05-22 | cursor-agent | — | Intent loop startup deadlock: warm-start brain buffer MT5 API call (copy_rates_from_pos for OU brain) could block indefinitely, stalling the entire engine. Added thread-based 15s timeout wrapper (_call_mt5_with_timeout) for all warm-start MT5 calls. Timeout → logged error + graceful skip. | RC-05 (blocking-call) |
| FIX-20260522-007 | 2026-05-22 | cursor-agent | — | Position count MT5 fallback: positions_total() < 0 (MT5 error code) caused complete cycle skip. Now falls back to position_manager cached count via has_position()/get_all_positions(). position_count_fallback JSON event emitted every 5 cycles for monitoring. | RC-01 (missing-null-check) |
| FIX-20260522-008 | 2026-05-22 | cursor-agent | — | Intent loop bar_sync crash protection: bar_sync wait section in live_intent_loop.py wrapped in try/except Exception. Unhandled exceptions killed entire intent loop process; now logs bar_sync_crash JSON event + falls back to interval-based sleep. | RC-01 (missing-exception-handler) |
| FIX-20260522-009 | 2026-05-22 | cursor-agent | — | Unguarded clear_position() after close dispatch failure: all 7 managed-exit callers (grace_period_emergency, bleed_stop, OU exit, brain_flip exit, meta exit, hesitation exit, time-based exit) now check _dispatched return value before pm.clear_position(). Prevents permanent position tracking loss when close dispatch fails. | RC-06 (contract-violation) |
| FIX-20260522-010 | 2026-05-22 | cursor-agent | — | Bar sync timeout 120s→360s: DEFAULT_TIMEOUT_SECONDS < M5 bar period (300s) caused every polling window to expire before next bar. Now 360s (300s + 60s buffer) across event_bar_sync.py, live.yaml, live_intent_loop.py, live_launcher.py. | RC-05 (boundary-error) |
| FIX-20260522-011 | 2026-05-22 | cursor-agent | — | BarSyncPoller graceful degradation: caller (live_intent_loop.py) now logs bar_sync_degraded_wakeup when receiving _degraded sentinel from wait_for_new_bar(). Degraded deadline prevents 390s main-loop dead window. | RC-05 (architectural) |
| FIX-20260521-005 | 2026-05-21 | cursor-agent | — | live_intent_loop.py brain_entry加载守卫+label_builder.py变量遮蔽修复。multi-brain模式跳过单一brain entry加载；unlinked循环变量重命名trade→unlinked_trade消除遮蔽。 | RC-02 |
| FIX-20260521-009 | 2026-05-21 | cursor-agent | — | Stub adapter deadlock: bootstrap_v9.py now reads adapter.name from live.yaml and passes it to EnvironmentConfig.development(adapter_name=...). Fixes all 295 open signals routing to StubCommunicationAdapter instead of MT5. | RC-09 |
| FIX-20260522-014 | 2026-05-22 | cursor-agent | — | Defense-in-depth hardening: (CRIT-1) mgmt phase price-fetch failure now falls back to entry_price + structured warning, continues managing; (CRIT-2) warm-start MT5 thread synchronized with join(15s) eliminating data race; (CRIT-3) cycle_count double increment deleted (kept outer only); (HIGH-5) 3 critical except:pass paths (meta_exit_engine, config_hot_reload, regime_gate) emit JSON events; (HIGH-6) degraded bar_sync wakeup skips Alpha, runs management only; (HIGH-8) atomic file writes (.tmp+os.replace) for all 7 state files. Files: live_cycle.py, live_intent_loop.py + 7 state persistence files. | RC-01, RC-04, RC-06 |
| FIX-20260522-019 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: (1) Circuit breaker in LiveCycleState — `_consecutive_degraded_cycles` counter tracks DegradedResult occurrences; after 3 consecutive degraded cycles, `_circuit_breaker_tripped` suspends new entries (management-only mode). Auto-resets on first clean cycle. (2) Startup orphan detection — compares MT5 positions against active_position.json tickets on first cycle; mismatched orphans emit `orphan_position_mismatch` HARD_BLOCK event and refuse to start. (3) All raw_proposals paths now carry `BrainSignal | DegradedResult` instead of bare dicts. | RC-06, RC-07 |
| FIX-20260523-004 | 2026-05-23 | cursor-agent | — | M15 infrastructure assault: MTFPriceService in core/market/ for decoupled M15 OHLC reconstruction from M5 tick history. Bar-boundary gating (00/15/30/45) in `_evaluate_strategy_lines` prevents statarb_m15 evaluation on non-boundary M5 cycles. M15-resampled close price routed to StatArbStrategy instead of raw tick mid_price. `_mtf_price_service` field on LiveCycleState, bootstrapped from MT5 historical M5 closes. See also FIX-20260523-004 in market-mtf. | RC-06, RC-07 |
| FIX-20260518-039 | 2026-05-18 | cursor-agent | — | Feature freshness check timezone fix: `ts.timestamp()` on naive UTC datetime from feature store interpreted as local time (UTC+8) → 8h artificial staleness. Fix: `ts.replace(tzinfo=UTC)` before `.timestamp()`. Cleaned 36,341 future-timestamp records (78,971→42,630) from feature store. | timezone-naive |
| FIX-20260518-038 | 2026-05-18 | cursor-agent | — | Single merge dispatch: trail SL + breakeven + trail TP merged into one modify_sltp per position per cycle (eliminates MT5 retcode 10006 back-to-back rejections). Ticket param added to 12 position_manager methods (30+ call sites updated). State path unified across load/save/shutdown. | contract-violation |
| FIX-20260518-033 | 2026-05-18 | cursor-agent | — | Tier 3 √N correlation discount: `_evaluate_strategy_lines()` calls `apply_sqrt_n_discount()` on all active decisions after strategy evaluation loop. Dropped strategies updated in execution_queue (verdict→REJECTED) and current_positions (removed). Audit log via `sqrt_n_discount` JSON event. | missing-feature |
| FIX-20260518-026 | 2026-05-18 | cursor-agent | — | Phase 2: daily_ops scheduler hardened — save _last_daily_ops_utc BEFORE execution (edge-reentry fix) + elapsed-time trigger replaced with fixed UTC 22:00-23:00 window using date-based already-ran-today guard. | edge-reentry, race-condition |
| FIX-20260517-022 | 2026-05-17 | cursor-agent | — | Phase 3: ExitWatchdog旁路补缺 — partial TP + force_close_dd + legacy dd + net_out 4 gaps wired to watchdog. ExecutionQueue flush() callback injection for net-out interception. | missing-error-handling, contract-violation |
| FIX-20260517-018 | 2026-05-17 | cursor-agent | — | Path B deprecation: `elif config.multi_brain:` marked DEPRECATED — unreachable with multi_strategy_enabled=True (default). Retained as rollback reference only. No runtime change. | dead-code |
| FIX-20260517-013 | 2026-05-17 | cursor-agent | — | Friction completeness: added slippage=0.10 to all settle_all() and record_signal() calls (3 sites). Previously only spread was passed; slippage defaulted to 0.0, undercounting friction by 0.10 USD/side. | contract-violation |
| FIX-20260517-014 | 2026-05-17 | cursor-agent | — | PnL global settlement anchor: moved settle_all() from post-price-fetch to after all safety guards (cooldown, SL streak, MT5 connection, market-closed) and before strategy evaluation. Eliminates settlement on skipped cycles where guards return early — single canonical call site per active cycle. | state-leak |
| FIX-20260517-003 | 2026-05-17 | cursor-agent | — | Route C+ live deployment: bootstrap_v9.py switched from v1 filter config (47-dim OOF-distorted, frozen confidence) to v3 (59-dim PiT, LGB+MLP 0.6/0.4 ensemble, Platt calibration, conformal prediction). All new filter params passed through. | config-drift |
| FIX-20260517-004 | 2026-05-17 | cursor-agent | — | MetaSignalFilter DevOps hardening: load_state() after init for crash recovery, save_state() in periodic save + shutdown. time-decayed conformal (conformal_max_age_days=14.0 from config). State path: meta_filter_state.json. | state-leak, boundary-error |
| FIX-20260515-015 | 2026-05-15 | cursor-agent | — | brain_votes consensus_confidence recording fix: replaced misleading _rough_conf with real consensus values, removed legacy path max(0.30) confidence floor | contract-violation |
| FIX-20260515-016 | 2026-05-15 | cursor-agent | — | Phase1 revival: 6 zombie strategies disabled (daily/m15/m30/h4 swing, micro_m15/h1), thresholds recalibrated, neutral penalty lowered | config-drift |
| FIX-20260515-017 | 2026-05-15 | cursor-agent | — | live.yaml enabled flag was ignored: _build_strategy_lines() now checks _cfg(name, enabled) for all 11 strategy types | config-drift |
| FIX-20260516-002 | 2026-05-16 | cursor-agent | — | ENGINE_STALL false positive: _check_stall() now uses live_trade_journal.jsonl (which live trading writes) instead of data/decisions/ (shadow-only) | config-drift |
| FIX-20260516-003 | 2026-05-16 | cursor-agent | — | Data-backed Strategy Parameter Reference: analyzed 7,216 brain_votes + 1,230 trade journal + 3 brain PnL ledgers. Documented per-strategy signal distributions, exit effectiveness, SL/TP calibration, consensus dilution, and critical LightGBM frozen confidence finding. | contract-violation |
| FIX-20260515-014 | 2026-05-15 | cursor-agent | — | Brain config restoration: 8 brain configs restored from accidental deletion, barrier_12bar now has full 5-type brain coverage (was 1/5) | stale-data |
| FIX-20260515-013 | 2026-05-15 | cursor-agent | — | Three-knife OU exit wired: Alpha Handoff check before OU close, Drift Lock set on PnL<0 exit, Drift Lock entry filter in queue processing alongside re-entry guard | missing-feature |
| FIX-20260515-008 | 2026-05-15 | cursor-agent | — | Watchdog cleanup: deleted scripts/hourly_watchdog.py (deprecated May 5 experiment), data/watchdog.log. Updated ADR-006 to document live_launcher as sole production runtime. Fixed verify.py deleted-file filtering (exists() check). | config-drift |
| FIX-20260515-006 | 2026-05-15 | cursor-agent | a4a1005 | Schema ID mismatch: swing_24 not recognized in brain re-evaluation path. Added swing_24 alias alongside daily_swing_24 in both position-management inference routes. Also fixed _STRATEGY_CONTRACT_TYPES to use timeframe-prefix matching (m15_swing etc) for broader training_contract compatibility. | config-drift |
| FIX-20260514-008 | 2026-05-14 | cursor-agent | a4a1005 | Add raw_proposals to defensive initialization block to prevent UnboundLocalError in single-brain mode | state-leak |
| FIX-20260511-001 | 2026-05-14 | cursor-agent | a4a1005 | Fixed multiple issues found during surgical audit of daily_ops, governance training, and execution risk controls | missing-validation |
| FIX-20260514-001 | 2026-05-14 | cursor-agent | a4a1005 | Blueprint mechanism upgrade: modular fix tracking with automated markers | contract-violation |
| FIX-20260514-002 | 2026-05-14 | cursor-agent | a4a1005 | Blueprint mechanism upgrade: modular fix tracking (retry after hyphen fix) | contract-violation |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: bootstrap_v9.py (remove type:ignore, switch to LightGBM V3) + signal_pipeline.py (empty ENSEMBLE_GROUPS). Previously registered as FIX-20260517-003, FIX-20260518-031. | process-violation |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `LiveCycle.run_cycle()` → `RuntimePipelineResult` | main.py, live_launcher | Stable |
| `RuntimeExecutionPipeline.execute(strategies)` → `RuntimePipelineResult` | LiveCycle | Stable |
| `signal_health.FeatureGate.check(snapshot)` → `GateResult` | LiveCycle | Stable |
| `record_brain_votes(proposals, cycle_id)` → `None` | strategy_line | Stable |

## Strategy Parameter Reference (DATA-BACKED, 2026-05-16, UPDATED 2026-05-24)

> **Purpose**: Permanent reference for all strategy parameters, backed by live trading data analysis.
> **Data sources**: `data/brain_votes/2026-05-15.jsonl` (7,216 records), `data/live_trade_journal.jsonl` (1,230 entries, Apr 29–May 16), `data/brain_pnl_ledger.json` (773 trades), `configs/brains/*.json` (training metrics).
> **Update this section whenever thresholds, SL/TP, or exit logic are adjusted.**
>
> **⚠️ 2026-05-24 UPDATE (FIX-20260524-036)**: barrier_12bar SL/TP corrected from 2.0/3.5 → 3.0/1.5 to match retrained calibration surface (EV=0.2004R). Brain roster completely changed since 2026-05-16 — see current state below. Full section rewrite pending next data collection cycle.

### Active Strategy Parameters

#### barrier_12bar (mode=shadow, magic=90001) — UPDATED 2026-05-24

| Parameter | Old Value | New Value | Data Justification | Health |
|-----------|-----------|-----------|-------------------|--------|
| `mode` | live | **shadow** | All voting brains frozen/shadow; zero capital deployed | ⚠️ Dormant |
| `sl.base_atr_mult` | 2.0 | **3.0** | FIX-20260524-036: aligned with retrained calibration (EV=0.2004R at SL=3.0/TP=1.5) | ✅ Matches training |
| `tp.base_atr_mult` | 3.5 | **1.5** | FIX-20260524-036: TP=1.5 has positive EV; TP≥2.5 all negative at SL=3.0 | ✅ Matches training |
| `confidence_threshold` | 0.40 | 0.40 | Dictator Protocol threshold; moot while shadow | — |
| `min_valid_brains` | 1 | 1 | Only Binary_Cls_V1 active (vote_weight=0.0) | ⚠️ No voting brain |

**Brain-level diagnostics (2026-05-24 — current)**:
| Brain | Status | Directionality | Vote Weight | Issue |
|-------|--------|---------------|-------------|-------|
| Meta_Stage1_Huber_V1 | **frozen** | — | 0.0 | Regression collapse with corrected costs; SL=2.0/TP=3.5 unprofitable |
| Meta_Stage1_Binary_Cls_V1 | shadow | TBD | 0.0 | OOF bimodal at [0.0, 0.7-0.8]; unsafe for live voting |
| Online_MLP_V1 | shadow | — | — | Not in live.yaml allowlist; unvalidated for live voting |

#### barrier_12bar_meta (mode=probation, magic=90014) — NEW 2026-05-24

| Parameter | Value | Data Justification | Health |
|-----------|-------|-------------------|--------|
| `mode` | probation | Promoted from shadow 2026-05-24; minimal capital (0.01) | ✅ Monitoring |
| `sl.base_atr_mult` | 3.0 | Matches MetaLabel_Binary_V1 training contract | ✅ |
| `tp.base_atr_mult` | 1.5 | Matches MetaLabel_Binary_V1 training contract | ✅ |
| `confidence_threshold` | 0.40 | OU-triggered signals; binary classifier confidence | ✅ |
| `base_volume` | 0.01 | Probation: minimal live capital | ✅ |
| `brain_types` | [lightgbm_v1] | Meta_Stage1_MetaLabel_Binary_V1 — OU signal meta-labeler | ✅ |

**Brain-level diagnostics (2026-05-24)**:
| Brain | Status | Train Sharpe | CPCV Sharpe | Forward Sharpe | Health |
|-------|--------|-------------|-------------|----------------|--------|
| Meta_Stage1_MetaLabel_Binary_V1 | probation | 13.71 | 12.94 | 8.10 | ✅ Healthy; 43-dim (40 V9 + 3 OU) |

#### statarb_dynamic (enabled, magic=90003)

| Parameter | Value | Data Justification | Health |
|-----------|-------|-------------------|--------|
| `confidence_threshold` | 0.25 | 22.8% of cycles above 0.25; P50=0.000, P75=0.1702, P90=0.5417 | ✅ OU signals naturally sparse |
| `sl.base_atr_mult` | 1.5 | OU mean-reversion: tighter SL than trend-following | ✅ Mean-reversion expects quick snapback |
| `tp.base_atr_mult` | 3.0 | Wider TP to capture full mean-reversion move | ✅ 2:1 RR ratio |
| `tp.partial_tp_r` | 2.0 | Lock profit at 2R on OU snapback | ✅ |
| `exit.trail_atr_mult` | 1.5 | Tighter trail for lower-hold-time OU trades | ✅ |
| `exit.zscore_exit_enabled` | true | Exit when z-score returns to mean | ✅ Core OU exit logic |
| `exit.confidence_decay_exit` | false | OU confidence naturally decays with z-score normalization | ✅ Correct for OU |
| `budget.daily_loss_limit_pct` | -0.015 | 1.5% daily cap | ✅ Conservative for arb strategy |

**Brain-level diagnostics (2026-05-15)**:
| Brain | Status | Directionality | Live PnL | Win Rate | Health |
|-------|--------|---------------|----------|----------|--------|
| OU_Params_V6_Sniper | probation | 17.7% directional (27L/77S/482N) | **+119.91 bps** | **49.7%** | ✅ Only healthy brain |

#### h1_swing (enabled, magic=90330)

| Parameter | Value | Data Justification | Health |
|-----------|-------|-------------------|--------|
| `confidence_threshold` | 0.25 | Only 8.4% of cycles above 0.25; P50=0.000, P75=0.112, P95=0.501 | 🔴 Threshold at P91 — blocks 91.6% of signals |
| `sl.base_atr_mult` | 2.0 | Training contract SL | ✅ |
| `tp.base_atr_mult` | 3.5 | Training contract TP | ✅ |
| `budget.max_consecutive_losses` | 3 | Very tight for single-directional brain (all LONG) | ⚠️ All-LONG brain will hit consecutive loss quickly in downtrend |

**Brain-level diagnostics (2026-05-15)**:
| Brain | Status | Directionality | Confidence | Live PnL | Train Sharpe | Issue |
|-------|--------|---------------|------------|----------|-------------|-------|
| lightgbm_h1_swing | probation | 100% LONG | **FROZEN at 0.612** | **-305.62 bps** | fw=8.21 | 🔴 8.3 Sharpe gap: train vs live |
| xgboost_h1_swing | shadow | 0% (all neutral) | 0.500 (neutral) | — | — | 🔴 Dead |

### Critical Diagnosis: LightGBM ML Inference Pipeline Broken

**Both LightGBM brains exhibit identical failure signatures:**
1. **Frozen confidence** — every prediction returns the identical value (0.5519 / 0.6120)
2. **All-LONG bias** — zero SHORT signals generated
3. **Live PnL deeply negative** despite stellar training metrics (fw_sharpe=8.21 → live_sharpe=-0.10)

**Root cause hypothesis**: The LightGBM adapter (`core/brains/adapters/`) receives constant/zero feature vectors at inference time. Tree-based models fed constant input collapse to a single leaf value. OU_Params_V6_Sniper (non-ML) works fine → problem is in the ML feature resolution pipeline, specifically:
- `feature_schema_id` resolution in brain factory
- `normalization_config_path` loading
- LightGBM adapter's `_predict()` feature vector construction

### Consensus Dilution Analysis

**barrier_12bar structural problem**: `ContractGroupConsensus._compute_weighted()` averages 8 brain votes. With 7 brains 100% neutral (up=0.5, down=0.5), a single directional brain's signal is diluted:

| Scenario | Raw Direction Signal | Neutral Penalty (0.15×) | Final Consensus | |
|----------|---------------------|------------------------|-----------------|---|
| 1 LONG + 7 neutral | up=0.5625, down=0.4375 | ×0.87 | conf≈0.109 | Before fix: conf≈0.022 |
| 2 LONG + 6 neutral | up=0.625, down=0.375 | ×0.83 | conf≈0.208 | |
| 3 LONG + 5 neutral | up=0.6875, down=0.3125 | ×0.78 | conf≈0.293 | |

The 0.10 barrier threshold only passes with at least 1 directional brain. With current state (1 direction/7 neutral), consensus barely exceeds the threshold 29% of the time.

### Threshold-to-Distribution Mapping

| Strategy | Current Threshold | Above % | P50 | P75 | P90 | P95 | Recommended Range |
|----------|------------------|---------|-----|-----|-----|-----|-------------------|
| barrier_12bar | 0.10 | 29.2% | 0.0003 | 0.1010 | 0.1148 | 0.6500 | 0.08–0.15 (once ML brains fixed) |
| statarb_dynamic | 0.25 | 22.8% | 0.000 | 0.1702 | 0.5417 | 0.8173 | 0.20–0.30 |
| h1_swing | 0.25 | 8.4% | 0.000 | 0.1120 | 0.1120 | 0.5010 | 0.10–0.15 (until ML brains fixed) |

### Exit Effectiveness (from live_trade_journal.jsonl)

| Exit Reason | Count | Win Rate | Total PnL | Avg PnL |
|-------------|-------|----------|-----------|---------|
| tp_hit | 10 | **100%** | +1.52 | +0.152 |
| sl_hit | 46 | **2.2%** | -4.92 | -0.107 |
| unknown | 142 | 43.7% | +0.43 | +0.003 |
| unknown_close | 22 | 27.3% | +0.02 | +0.001 |

**SL:TP hit ratio = 4.6:1** — SL triggers 4.6× more frequently than TP. The per-trade R:R (~1.4:1) is sound, but frequency mismatch destroys net profitability.

**125 of 369 closes (34%) lack PnL confirmation** — journal close-confirmation gap.

### Historical Loss Attribution

**magic=90004** (unregistered/unmapped strategy, May 5–7 only): 27 trades, 7.4% win rate, **-2.46 PnL** — represents **79% of all journal PnL losses**. Already resolved (magic removed in current config).

## Verification
```bash
python -m pytest tests/ -k "runtime or live or cycle" -q
```

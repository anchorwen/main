# Execution / Guards

## Purpose
Pre-trade safety checks that execute before any order is sent: session detection, VaR limits, position sizing, data quality validation, intraday drawdown kill switch, and SL streak breaker.

## Key Files
| File | Role |
|------|------|
| `core/execution/pre_trade_guards.py` | `detect_session()`, `check_var()`, `compute_position_size()`, data quality checks |
| `core/execution/strategy_budget.py` | `StrategyBudget` — per-strategy risk budget with graduated SL cooldown |
| `core/execution/market_efficiency.py` | `compute_kaufman_er()`, `check_market_normalized()` |
| `core/constants.py` | `INTRADAY_DD_KILL_PCT`, `INTRADAY_DD_FORCE_CLOSE_PCT`, `SL_STREAK_BREAK_COUNT` |

## Data Flow
```
Market data → detect_session() → check_var() → compute_position_size()
                                              ↓
                              StrategyBudget.check() → approved/rejected
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained utilities; no core imports |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/strategy_line | compute_position_size | Position sizing in strategy evaluation |
| runtime/live_cycle | detect_session, StrategyBudget | Pre-trade guard execution |
| runtime/order_dispatch | SL streak tracking | Re-entry blocking |

## Known Issues

### KI-003: Layer 3 Auto-Calibration (IN PROGRESS)

**目标**: 激活 ConformalCalibrator Q10 FIFO 自适应阈值。

**进度**: FIX-20260525-015 已解决全部三个阻塞条件：
1. ~~Brain 层 `max_half_life=42`~~ → 已恢复为 58
2. ~~Gate 层 5-way 乘法评分坍缩~~ → 已改为几何平均
3. ~~数据闭环鸡生蛋~~ → 已实现 Explore-then-Commit 暖启调度

**FIX-20260526-031 新增**: z_depth 硬否决权。几何平均评分存在"掩盖效应"——theta_q (0.95) 和 vel_q (1.0) 可拉高 composite_score 至 0.40+, 即使 z_depth_q 仅 0.12 (|z|≈0.16)。均值回归的物理基础是价格偏离; 无偏离=无利润空间。z_depth_q<0.25 → composite_score 归零, 任何其他维度无权拯救。效果: z_entry=1.3 → 实际 |z| 必须 > 0.325 才可能通过。

**剩余工作**: 等待实盘累积 ≥50 个闭环样本后 calibrator 自动进入 WARM 阶段。

**预估**: COLD→WARM 1-2 周, WARM→HOT 额外 2-3 周。

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260629-188 | 2026-06-29 | cursor-agent | — | **P1-3: Time-based session gating** (DQAF-175). LiveCycleConfig.blocked_entry_hours + strategy_evaluator.py per-cycle check blocks ALL new entries when UTC hour∈[0,20] (-$88.15 of -$89.03 total swing PnL). Existing positions continue to be managed. | RC-12 — missing time-based entry gating |
| FIX-20260629-174 | 2026-06-29 | cursor-agent | — | **DQAF-174 L2: Strategy mode enforcement Phase 2 (completes FIX-20260629-171)**: `evaluate()` 入口注入 config.mode→regime_gate_mode 推导逻辑 — mode=shadow→强制 shadow, mode=probation 且无 governance probation+ brain→强制 shadow. 物理剥夺调用方盲目传 "full" 的越权路径. 同时容量分配路径 (group_consensus.py) 对齐 base_weight×dynamic_scale 统一合约. | L2 — evaluate() 不推导 regime_gate_mode; 容量分配裸用 get_weights() |
| FIX-20260629-171 | 2026-06-29 | cursor-agent | — | **Strategy mode runtime enforcement (Phase 1 — YAML→Config)**: StrategyLineConfig新增`mode`字段 (live/probation/shadow), strategy_builder从YAML读取. Phase 2 强制执行由 FIX-20260629-174 完成. 闭合DQAF-20260609-011中Cut 4微量化交易仍允许candidate brain实盘交易的缺口. | L2 — YAML `mode`字段纯文档化, 零运行时门禁 |
| FIX-20260628-166 | 2026-06-28 | cursor-agent | — | **XAU MetaFilter V2 — ML signal quality gate trained on 1,339 PIT-aligned V9 samples**. 40-dim LGBMClassifier with strong regularization (max_depth=3, num_leaves=7, L1=0.5/L2=1.0). 3-Fold TimeSeriesSplit CV AUC=0.5316±0.0234. Post-filter WR=70.6% (baseline=34.3%, +36.3%). New model `data/models/meta_filter_v4/` with sklearn-compatible pickle + feature_names.json. Training script `scripts/train_xau_metafilter.py`. Config `configs/brains/meta_stage2_filter_v3.json` updated to V2. Replaces V1 (715 samples, AUC=0.64) which was trained on feature set incompatible with live build_meta_filter_array(). | L2 — V1 model trained on derived features (oof_pred, session_sin, etc.) not producible by live pipeline; feature parity gap closed by training on V9_INSTITUTIONAL_40_FEATURES + PnL>0 labels |
| FIX-20260624-095 | 2026-06-24 | cursor-agent | — | **UGR-A08: CapResult migration — StrategyBudget + live_cycle budget pipeline**. Added record_trade_checked()/record_sl_checked()/load_state_checked() CapResult-wrapped methods with input validation. Replaced fail_open_guard("BudgetStateRestore") + 2× log_and_continue in live_cycle Phase 7 with CapResult pattern matching. 17 new tests. | RC-12 — missing-feature: no CapResult integration in budget pipeline |
| FIX-20260623-084 | 2026-06-23 | cursor-agent | — | **DQAF-084: BTC ConformalCalibrator p_win=0.5 Collapse — 4-Layer p_win Propagation Pipeline Fix**. `position_registration.py`→`position_close_adapter.py`→`PositionClosed`→`live_cycle.py` pipeline didn't carry p_win → calibrator fed hardcoded 0.5 for every trade. 360/500 entries contaminated → Q10 collapsed to 0.5. Fix: (a) `PositionClosed` +p_win field+journal; (b) known_open_tickets stores p_win/sl/tp; (c) `_build_event()` propagates p_win; (d) live_cycle uses `_evt.p_win` + proper -1/0/1; (e) calibrator auto-detects contamination. ReB: `CALIBRATOR_PWIN_HARDCODED_CONTAMINATION`. | L3 — p_win not propagated through close event pipeline |
| FIX-20260623-076 | 2026-06-23 | cursor-agent | — | **DQAF-076: BLE001 P0 Hot Path — Blind Exception Catch Removal (pwin_chain 1)**. Replaced `except Exception` at `get_metrics()` with `except (KeyError, ValueError)`. Removed double-nested fail_open_guard wrapping bare raise→continue pattern. | L3 — except Exception anti-pattern |
| FIX-20260622-054 | 2026-06-22 | cursor-agent | — | **DQAF-054 Inference Pipeline Scaler Safe-Loading: `MicrostructureFeatureAdapter` JSON scaler + fail-closed**. Replaced broken `joblib.load()` (fails on JSON-format scalers) with `_load_scaler_json()` that reconstructs sklearn StandardScaler from `{mean_, scale_, var_, n_features_in_, feature_names_in_}`. Added `require_scaler` flag: `True` → `DataIntegrityError` (live mode halt); `False` → DEGRADE warning (shadow/testing). XAU training scaler now correctly loads via JSON path (previously would have crashed). | L3 — architecture defect: `joblib.load()` ↔ JSON format mismatch + hardcoded `scaler_path=None` → normalization permanently bypassed |
| FIX-20260622-053 | 2026-06-22 | cursor-agent | — | **DQAF-053: `ConformalCalibrator.reset_history()` + `AlphaPerformanceStore.remove_alpha()` + `list_ids()` APIs**. Added 3 domain API methods enabling physical state sanitization without raw JSON manipulation (Iron Law #0 compliance). `reset_history()` clears rolling history + resets counters + forces cold-start. `remove_alpha()` deletes all snapshots for an alpha_id (idempotent). `list_ids()` returns every alpha_id with recorded snapshots. Part of Phase 1 Global State Reconciliation. | L3 — no programmatic purge API existed; FIFO and performance store mutations required raw JSON editing |
| FIX-20260622-050 | 2026-06-22 | cursor-agent | — | **DQAF-050 Cold-Start Double Deadlock — governance fast-track + cold-start snapshot backfill**. (1) `lifecycle_service.py`: VALID_TRANSITIONS extended — CANDIDATE→PROBATION_LIVE (live fast-track) + CANDIDATE→PAPER_TRADING (probation fast-track). (2) `daily_ops.py` `_step_alpha_lifecycle()`: governance fast-track with federated trust (live→probation_live, probation→paper_trading), leaderboard verification (trade_count≥50, WR≥0.45), cold-start AlphaPerformanceSnapshot backfill from leaderboard+governance data to activate allocator PnL fallback path. (3) `daily_ops.py` `_step_alpha_registration()`: strategy_class inference + assets population + ghost cleanup guard. Result: 5 brains promoted, allocatable_count 0→5. | L3 — architectural deadlock: promotion gate execution metrics vs feed PnL metrics (zero schema overlap) |
| FIX-20260622-049 | 2026-06-22 | cursor-agent | — | **DQAF-049 G3 Alpha Allocation Vacuum — nomination bridge + data integrity fixes**. (1) Sev 1: CLI `_save_alpha_registry()` backdoor welded shut → StateWriter gate. (2) `AlphaRecord.to_dict()` + `AlphaRegistry.load()` now correctly round-trip `strategy_class`, `assets`, `risk_profile`. (3) New `_step_alpha_registration()` nomination bridge: reads leaderboard + governance, nominates qualifying brains (trade_count≥50, WR≥0.45, sharpe≥0.30, live/probation) as CANDIDATE. Multi-source field resolution for both leaderboard formats. Result: 3 XAU brains nominated (Swing_V9_M15_V2/M30_V2/H1_V2). | RC-12 (missing-feature: no bridge) + RC-07 (missing-validation: CLI backdoor) |
| FIX-20260620-004 | 2026-06-20 | cursor-agent | — | **P2: btc_swing_h1 family_spacing TF exemption + P3: breakeven rate pilot**: (P2) Removed `btc_swing_h1` from `_SWING_FAMILY` — M5 entries every ~15min perpetually blocked H1 (3600s gap) via shared "swing" family, making btc_swing_h1 mathematically deadlocked. Different brain architectures (XGBoost vs LightGBM) + timeframes (M5 vs H1) = genuinely different signals, not echo trades. (P3) `live_btc.yaml`: btc_swing `trail_activation_atr` 0.3→0.5 — reduce premature trail activation and lower breakeven rate. DQAF-20260620-002. | RC-05 (boundary-error — family gap blocking higher-TF entries) / RC-09 (config-drift — trail_activation too aggressive for M5 BTC) |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染: MetaFilterGate(model_dir) + build_meta_filter_array(feature_names_path) 移除默认值** | L3 — base_dir="data" 默认值 |
| FIX-20260613-083 | 2026-06-13 | cursor-agent | f5c9e30 | R1 Gate 4h silence protection: if all trades blocked by RegimeDirectionGate for >48 consecutive cycles (~4h), relax from block to penalty-only. Prevents BTC zero-open silence when trend=LONG and all brains are SHORT. Silence counter resets when any trade passes (trend-aligned or above penalty threshold). | boundary-error |
| FIX-20260613-079 | 2026-06-13 | cursor-agent | 53cf419 | RegimeDirectionGate: counter-trend confidence penalty when trend is confirmed (long/short). Ranging markets full passthrough. Applied as Cut 1a in strategy_evaluator.py — opposing brain signals get 0.5x confidence multiplier, blocked if <0.35. Eliminates ALL-LONG brains voting in downtrend and ALL-SHORT in uptrend. | missing-validation |
| FIX-20260612-005 | 2026-06-12 | cursor-agent | 10635d1 | P5: ConformalCalibrator cold_started transition fix. cold_started now transitions to False when history >= warmup_samples (50) instead of staying True forever. _load_state() backfills transition for existing state files. Fixes CONFORMAL_COLD_STALLED false positive — calibrator was operating correctly (51 history entries) but flagged as stalled because cold_started never cleared. | state-leak |
| FIX-20260612-003 | 2026-06-12 | cursor-agent | 9a36b40 | P0+P1: Close-flood phantom guard + trail-aware SL label. PositionManager: added _close_attempt_count tracker with PENDING_CLOSE_FLOOD_THRESHOLD=3 to permanently lock tickets after repeated close failures (prevents 76-close/80min flood pattern). PENDING_CLOSE_MAX_CYCLES extended 3→10. Reconciliation: sl_hit_trailed when trail_advances>0 (closes TRAIL_TELEMETRY_BLINDSPOT). | contract-violation |
| FIX-20260611-020 | 2026-06-11 | cursor-agent | 331f996 | Code Blue: Fail-Closed SL/TP assertion + Governance manual whitelist + mypy type fix. (1) strategy_evaluator.py: SL/TP Fail-Closed check rejects any should_trade=True decision with sl<=0 or tp<=0 in non-shadow mode. (2) scheduler_service.py: _GOVERNANCE_MANUAL_MODE disables PnP→governance injection and execute_transitions, logging pending decisions for human review. (3) live_intent_loop.py: fix mypy attr-defined errors — iterate _decisions (BrainPromotionDecision) instead of _applied (list[str]). Fixes ANOM-005 (BTC naked trading) + ANOM-002 (governance backtest illusion). | contract-violation |
| FIX-20260610-008 | 2026-06-10 | cursor-agent | — | **出场原因分类强化**: `_classify_exit_reason()`补全7种模式→3个新规范类别: kalman_velocity→kalman_flip, meta_exit子类型6种(全部归入meta_exit), net_out, exit_watchdog→watchdog, grace_period_emergency→emergency_close, partial_tp→tp_hit. 14种预存模式回归测试全通过+17种新模式测试. DQAF-20260610-002. | RC-07 |
| FIX-20260610-006 | 2026-06-10 | cursor-agent | — | **ATR freeze guard**: MetaSignalFilter 连续5周期浮点全等检测→_atr_frozen标志+JSON event+持久化; 值波动→自动解除. | RC-12 |
| FIX-20260612-001 | 2026-06-12 | cursor-agent | — | **Phase 0: 静默降级可观测性注入 (KI-004 收口)**: pwin_chain.py fallback paths now emit structured WARNING logs; BLE001→fail_open_guard; p_win_source + p_win_degraded flow through StrategyDecision → dispatch → journal. | RC-06 |
| FIX-20260610-007 | 2026-06-10 | cursor-agent | — | **Budget解冻+Calibrator+数据加速+XAU方向分离**: budget跨日unpause; calibrator merge; hesitation 0.08→0.05; cold_explore扩展swing(绕过MetaFilter+趋势隔离+vol*0.5); MetaFilter V1止血+V2 PIT; XAU方向分离(LONG 381 AUC=0.68, SHORT 334 AUC=0.67)+路由; 字典同构; 趋势门禁绕过(逆势冷探索). | RC-03, RC-06, RC-12 |
| FIX-20260609-011 | 2026-06-09 | cursor-agent | — | **Governance degradation gate**: When zero live brains in a strategy, confidence floor 0.50 + volume cap 0.01. candidate brains penalised vote_weight×0.5. DQAF-20260609-011. | RC-07 |
| FIX-20260609-010 | 2026-06-09 | cursor-agent | — | **Budget counter reset every cycle**: `_build_strategy_lines()` (live_cycle.py) creates fresh StrategyBudget objects every cycle, but `restore_execution_state()` only ran on cycle 1. Cycles 2+ ran with zeroed daily PnL, consecutive losses, and SL cooldown counters → all cumulative circuit breakers permanently disabled after first cycle. Fix: restore budget state from disk EVERY cycle before pending records are fed. DQAF-20260609-001. | RC-03 |
| FIX-20260608-007 | 2026-06-08 | agent | — | **S3 Functional Core extraction**: `pwin_chain.py` (new) — extracted `resolve_p_win_from_brains()` from `kelly_sizer.py` and `adjust_p_win_for_regime()` from `strategy_line.py` into shared pure-function module. kelly_sizer delegates to pwin_chain. MODULE_SOURCE_MAP updated. | RC-12 |
| FIX-20260605-127 | 2026-06-05 | cursor-agent | d9d9f49 | **discover_probe_specs() hardened**: Now skips brains with status=archived/frozen or vote_weight=0.0. Prevents Meta Pipeline from wiring dead probes after FIX-125 archival. | RC-11 |
| FIX-20260603-072 | 2026-06-03 | cursor-agent | — | **Global Execution State Hydration**: StrategyBudget now has `get_state()` / `load_state()` for restart persistence. Budget state (daily PnL, SL cooldown, consecutive losses, paused) survives process restart. | RC-03 |
| FIX-20260601-037 | 2026-06-01 | cursor-agent | — | **PortfolioRiskController contract_size**: `_to_notional()` inflated BTC exposure 100× (XAU default 100.0). Passed `LiveCycleConfig.contract_size` to controller. `live_btc.yaml` portfolio_max_net 0.05→0.30. | RC-06 |
| FIX-20260601-032 | 2026-06-01 | cursor-agent | — | **contract_size auto-resolution**: `compute_position_size` + `check_pre_trade_var` now accept optional `symbol` → auto-resolve `contract_size` from ASSET_REGISTRY. Callers updated (live_cycle.py, strategy_line.py). Forgetting `contract_size` no longer silently defaults to XAU 100.0. | RC-06 |
| FIX-20260531-012 | 2026-05-31 | cursor-agent | — | pre_trade_guards.py: `check_tick_sanity()` now uses ASSET_REGISTRY for price bounds (Defense 1), falling back to legacy XAU check for unregistered symbols. `check_pre_trade_var()` accepts `contract_size` parameter instead of hardcoded XAUUSD_CONTRACT_SIZE. | RC-06 |
| FIX-20260530-082 | 2026-05-30 | cursor-agent | — | BTC 24/7 session support: detect_session() market_type parameter | RC-05 |
| FIX-20260529-043 | 2026-05-29 | cursor-agent | — | MetaFilter fail-closed | RC-07 |
| FIX-20260529-030 | 2026-05-29 | cursor-agent | — | SL/TP spread cost mechanism: added `spread_points`/`tick_size` kwargs to `compute_sl_tp_levels()`. When enabled, TP tightened by spread (exit fills at bid/ask, not mid), SL widened (stop fills suffer adverse slippage). Aligns live order placement with training `label_contract.py` barrier adjustments. Default `0.0` preserves backward compat; enable after price basis audit. Also updated `meta_pipeline.py` call site to pass config-driven spread params. | RC-06 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: replaced positional slicing `[:40]`/`[40:49]` in MetaFilter with feature-name-indexed lookup (V9 prefix M5_/M15_/M30_/H1_ → v9_indices, rest → micro_indices). Eliminates fragile assumption that V9/micro boundary is at position 40. | RC-06 |
| FIX-20260622-064 | 2026-06-22 | IC_MANDATE | — | **DQAF-064 P0-1 LIVE-Brain Governance Gate**: `resolve_p_win_from_brains()` now accepts `live_brain_ids` — only status=="live" brains contribute to p_win median. Retired/frozen/archived brains physically excluded with diagnostic logging (FALLBACK_PATH_3c). `resolve_p_win()` + both `strategy_line.py` call sites pass through the filter. Closes zombie-data contamination Sev 1 vulnerability. | L3 — no governance alignment in p_win resolution |
| FIX-20260528-013 | 2026-05-28 | cursor-agent | — | meta_signal_filter.py: redirected 3 `print()` calls (conformal_warning, calibrator_load_error, meta_filter_unavailable) from stdout to `sys.stderr`. JSON diagnostic events were polluting stdout and breaking CLI `--json` output parsing when calibrator loading failed. | RC-06 |
| FIX-20260528-012 | 2026-05-28 | cursor-agent | — | ConformalCalibrator cold_start_from_journal() data gap: p_win was recorded on accepted (open) entries but cold-start only scanned closed entries — p_win always None → 0 samples loaded. Fix: two-pass journal scan — Pass 1 builds `{message_id: p_win}` from accepted entries, Pass 2 JOINs closed.open_message_id → accepted.message_id to recover p_win. Result: 27 samples loaded (vs 0 before). Remaining gap: p_win only recorded since 2026-05-24, so 704/731 closed trades predate the field and cannot be recovered. | RC-06 |
| FIX-20260526-031 | 2026-05-26 | cursor-agent | — | Phase 6: (Fix 3) z_depth hard veto in ConformalOUGate.filter() — when z_depth_q<0.25, composite_score forced to 0.0 before geometric mean to cut masking effect; (Fix 2) resolve_p_win_from_brains() fallback 0.50→0.40 (Fail-Closed) + diagnostic skip logging; (Fix 1) _adjust_p_win_for_regime() thresholds: ADX 20→15, \|z\| 1.5→0.8, z_amplification baseline 1.0→0.5. Fix 3 centerpiece: mean-reversion physics demands deviation — |z| must exceed 0.325×z_entry before any trade can pass. | RC-05, RC-12 |
| FIX-20260525-015 | 2026-05-25 | cursor-agent | — | Layer 3 bootstrap: break chicken-and-egg deadlock via (1) max_half_life 42→58 restore in M5 artifact, (2) geometric mean scoring replacing multiplicative product — `(∏ clip(c,0,1))^(1/5)` prevents dimensional collapse while preserving hard veto, (3) Explore-then-Commit warmup schedule: COLD phase (samples<50) fixed threshold=0.20 + force_min_volume=0.01, WARM (50≤n<100) Q10 floored at 0.20, HOT (n≥100) full Q10 [0.25,0.65]. | RC-05, RC-06, RC-12 |
| FIX-20260525-012 | 2026-05-25 | cursor-agent | — | Phase 4 Dynamic SL/TP Calibration: `StrategyFamily` enum (mean_reversion | trend_following) for asymmetric volatility regime response. `_compute_regime_factors()`: MR → SL widens ×√vol_ratio, TP tightens ×vol_ratio^-0.25; TF → both widen ×√vol_ratio. Hard clipping: SL [0.8, 4.0], TP [1.0, 6.0]. Dynamic ref_atr from RegimeDetector EWMA. `compute_dynamic_sl_tp()` gains `strategy_family` param + new clamping defaults. 26 tests (14 new). | RC-12 |
| FIX-20260524-042 |
| FIX-20260524-043 | 2026-05-24 | cursor-agent | — | T2-H3: OU/Meta gate exceptions now block trades instead of non-blocking pass — ML quality filters failing silently was allowing trades through unguarded. T2-H7: compute_position_size returns 0.0 for invalid ATR (was min_lot) — data feed failure should not result in positive position size. | RC-06, RC-07 |
| FIX-20260523-008 | 2026-05-24 | cursor-agent | — | Track 3d Conformal OU Gate: ConformalCalibrator with Q10 FIFO quantile threshold for OU MetaFilterGate. Replaces fixed 0.40 threshold with adaptive [0.35, 0.70]. Cold-start from journal history, FIFO deque(maxlen=500), clamp-hit-rate monitoring. MetaFilterGate.filter() now uses adaptive threshold when calibrator is warm. | RC-09 (config-drift — fixed threshold doesn't adapt to regime changes) |
| FIX-20260522-024 | 2026-05-22 | cursor-agent | — | Config-driven MetaPipeline architecture: replaces hardcoded `_try_meta_pipeline()` with declarative `MetaPipeline` class. Fixes cross-module cascade where FIX-20260522-015 (BrainSignal migration removed `extensions` attribute) silently broke FIX-20260520-028 (Executive Veto read `p.extensions.raw_outputs.raw_score`). Brain JSON declares `"roles": ["meta_probe"]`; MetaPipeline discovers, extracts, filters via stage-N registry. | RC-06 (cross-module cascade) |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260521-007 | 2026-05-21 | cursor-agent | — | MetaFilter gate: integrate meta_filter_gate.py for dual-track Meta Pipeline — Huber BPS regression probe → Stage 2 LGB+MLP+Platt+Conformal binary classifier gate | RC-06 |
| FIX-20260514-013 | 2026-05-14 | cursor-agent | a4a1005 | 最低持仓保护期(min_hold_cycles=3)+毒性流否决逃生舱(tick速度3倍阈值/逼近硬止损0.3ATR) | missing-null-check |
| FIX-20260514-012 | 2026-05-14 | cursor-agent | a4a1005 | 简化分级利润锁定：删除(+2R,0.5R)和(+4R,2.5R)易触发级别，仅保留灾难性保护(+3R,1.5R)和(+5R,3.5R) | boundary-error |
| FIX-20260514-011 | 2026-05-14 | cursor-agent | a4a1005 | 废弃R里程碑拖尾收紧，引入基于已实现波动率的自适应K：vol_ratio > 1.5 放宽K+0.8，vol_ratio < 0.7 收紧K-0.3 | boundary-error |
| FIX-20260514-010 | 2026-05-14 | cursor-agent | a4a1005 | EMA低通滤波替代离散信心下降检查：confidence_ema平滑信心得分，保留30s采样响应能力的同时数学过滤高频白噪声 | boundary-error |
| FIX-20260516-003 | 2026-05-16 | cursor-agent | — | Exit Effectiveness Data section added: SL:TP=4.6:1, per-strategy PnL breakdown, ML frozen confidence diagnostic. SL triggers 4.6x more than TP despite adequate per-trade R:R. | contract-violation |
| FIX-20260516-005 | 2026-05-16 | cursor-agent | — | check_feature_freshness() rejected negative age (future timestamps). Was: `age <= max_age` always True for future dates. Now: explicit `age < 0 → fresh:False, reason:future_timestamp` guard. | contract-violation |
| FIX-20260524-007 | 2026-05-24 | cursor-agent | — | Track 3d Conformal OU Gate: created ConformalOUGate — physics-based OU signal quality gate (Z-Depth, Z-Velocity, Half-life, Theta, ADX multiplicative scoring) replacing 47-dim LightGBM MetaFilterGate for statarb_dynamic + statarb_m15. Strategy-aware OU parameter loading from brain artifacts (V6 M5: z_entry=3.9, V7 M15: z_entry=1.2). Shared ConformalCalibrator (Q10 FIFO adaptive threshold). Wired into live_cycle lazy init + strategy_line.evaluate(). | RC-06, RC-12 |
| FIX-20260522-013 | 2026-05-22 | cursor-agent | — | (1) Sign-flip bug: _score_to_direction() in 5 adapters flipped weak signals (|score|<0.549). Fixed with 0.5±conf/2 anchoring. (2) Counter-trend bypass for barrier_12bar: Dictator Protocol's Huber BPS probe IS the trend signal — blocking its output when H1/H4 trend disagrees would silence the only voter. | RC-06 |
| FIX-20260522-014 | 2026-05-22 | cursor-agent | — | Defense-in-depth: meta_exit_engine load failure now emits `meta_exit_engine_load_failed` JSON event instead of silent pass. Config hot_reload failure emits `config_hot_reload_failed` JSON event. Position manager save_state uses atomic write (.tmp+os.replace). MetaSignalFilter save_state uses atomic write. | RC-01, RC-06 |
| FIX-20260517-002 | 2026-05-17 | cursor-agent | — | Route C+ Protocol 2+3: Platt scaling calibration (smooth sigmoid, coef=2.44/intercept=-0.84) + conformal prediction thresholding (80th pctile of 500-pred window, 0.50 floor). MetaSignalFilter extended with calibrator_path/conformal_mode/window/percentile/min_threshold. Fixed P(class=1) extraction bug. | missing-feature |
| FIX-20260517-004 | 2026-05-17 | cursor-agent | — | MetaSignalFilter DevOps hardening: state persistence (save_state/load_state JSON), time-decayed conformal (14d max_age_days), Platt safety clamp (eps 1e-4 + max/min output clamp). Integrated into live_intent_loop. | state-leak, boundary-error |
| FIX-20260517-010 | 2026-05-17 | cursor-agent | — | Fixed inverse-volatility SL/TP formula: `sl_mult = base_sl_mult / vol_ratio` mathematically cancelled to fixed distance regardless of ATR, causing SL to shrink to 1.25 ATR in high vol (noise-triggered). Changed to direct multiplication: `sl_mult = base_sl_mult`, `sl_distance = sl_mult * current_atr` — SL always spans exactly base_sl_mult ATRs. Updated ref_atr from 5.0 to 7.0 (current XAUUSD M5). | RC-05 |
| FIX-20260518-030 | 2026-05-18 | cursor-agent | — | MetaSignalFilter feature_names fallback: when .meta.json is missing (e.g. meta_stage2_lgb_pit_v3.meta.json), _feature_names stayed empty [] causing 0-length feature vector → LightGBM fatal. Now falls back to booster.feature_name() after model load — reads 59 feature names directly from the trained model file. | missing-file |
| FIX-20260518-032 | 2026-05-18 | cursor-agent | — | Tier 2 Kelly/Edge sizing: `compute_kelly_mult(p_win, rr_ratio)` computes fractional Kelly multiplier. When kf≤0, hard EV veto (`fractional_mult=0.0` → `should_trade=False`). `resolve_p_win_from_brains()` uses rolling 100-trade win rate from BrainPnLStore with cold-start guard (empty→0.5) and min 10-sample threshold. | missing-feature |
| FIX-20260518-033 | 2026-05-18 | cursor-agent | — | Tier 3 √N correlation discount: `apply_sqrt_n_discount()` groups decisions by direction, applies 1/√n decay to each cluster with lot_step rounding. Strategies below min_lot after discounting are dropped and removed from execution queue + current_positions snapshot. Drops are logged via `sqrt_n_discount` event for audit trail. | missing-feature |
| FIX-20260518-034 | 2026-05-18 | cursor-agent | — | Kelly discretization fix: moved `kelly_mult` into `_compute_volume()` BEFORE `round(size, 2)` — previously applied to already-rounded value, destroying Kelly effect through premature discretization. Added `kelly_diag` (MetaFilter p_win capture) + `kelly_sizing` (three-way volume: base/raw_target/final_stepped) JSON events. `multi_strategy_eval` now includes `p_win`/`kelly_mult` per strategy. | boundary-error |
| FIX-20260518-035 | 2026-05-18 | cursor-agent | — | NET_OUT config wiring: `portfolio_netting_mode` added to `LiveCycleConfig` (default `"net_out"`) and passed to `PortfolioRiskController`. Previously `netting_mode` defaulted to `"allow_coexist"` — the entire netting path was dead code. Also fixed ExecutionQueue ACK polling to extract `new_ticket` from partial close receipt and reassign `known_open_tickets` to prevent orphan positions without trailing stop. | config-drift |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: MetaSignalFilter feature_names fallback from booster. Previously registered as FIX-20260518-030. | process-violation |
| FIX-20260519-003 | 2026-05-19 | cursor-agent | — | New file: kelly_sizer.py — Tier 2 Kelly/Edge position sizing with EV veto. Previously registered as FIX-20260518-032. | missing-feature |
| FIX-20260520-023 | 2026-05-20 | cursor-agent | — | Dual-Track Router: Meta Pipeline (Huber→Stage 2 LGB+MLP+Platt+Conformal) decoupled from Parliament. Added `_try_meta_pipeline()` method to StrategyLine — when Parliament fails (confidence < 0.45), Track 2 independently evaluates Huber raw_score (±0.30 threshold) → Stage 2 filter → SL/TP/Kelly → execution. Deleted dead V1 filter config (`meta_stage2_filter_v1.json`). Created `scripts/test_meta_pipeline.py` for mock signal injection validation — confirmed full chain electrically connected with P(win) varying 0.37-0.68 per signal quality. | RC-06 (serial deadlock) |
| FIX-20260520-024 | 2026-05-20 | cursor-agent | — | Hesitation exit killed profitable positions: `should_exit_hesitation` only checked `breakeven_triggered` (binary, needs 1.5 ATR), ignoring current PnL. Added Pillar 3 Current-Profit Guard (`r_now > 0` → no exit). Lowered Profit Pardon: 0.30R → 0.15R. Added `mid` parameter for current R computation. Increased m15_swing hesitation_cycles 2→4, m30_swing 2→3. | RC-05 (boundary-error) |
| FIX-20260520-025 | 2026-05-20 | cursor-agent | — | Absolute Refractory Period (Cut 1) + Cross-Strategy Family Entry Spacing (Cut 2): Added `CooldownRegistry` (passive exits=1×timeframe, active exits=60s, reverse-direction override) and `FamilyEntryTracker` (swing family same-direction ≥15min gap) to `pre_trade_guards.py`. Integrated into `live_cycle.py`: exit recording in `_dispatch_managed_close`, pre-evaluate cooldown+spacing checks in `_evaluate_strategy_lines`, family entry recording after dispatch. Re-enabled h1_swing (60% WR best performer). | RC-06 (missing-feature) |
| FIX-20260520-026 | 2026-05-20 | cursor-agent | — | Dynamic Exit Manager: Per-strategy exit params (`trail_atr_mult`, `trail_atr_mult_low`, `trail_atr_mult_high`, `breakeven_threshold_atr`) added to `ActivePosition` dataclass. `register_position()` accepts and stores per-strategy overrides; `_adjust_trail_for_regime()`, `should_breakeven()`, `compute_trail_tp()` now read from `pos.*` instead of `self.*`. `live_cycle.py` passes per-strategy exit params at registration. `live.yaml` expanded: statarb=1.5/1.2/2.5+0.8x be, m15_swing=1.5/1.3/2.5+1.2x be, h1_swing=2.5/2.0/3.5+1.5x be, barrier_12bar=2.0/1.8/3.0+1.5x be. | RC-06 (config-drift) |
| FIX-20260520-028 | 2026-05-20 | cursor-agent | — | Meta Pipeline Executive Veto: removed `not parliament_passed` precondition from Track 2 activation. Meta_Stage1_Huber_V1 now gets first-refusal on every barrier_12bar evaluation — if Huber detects extreme counter-consensus signal (|raw_score|>0.30) and passes Stage 2 LGB+MLP+Platt+Conformal→RR→Kelly chain, it overrides parliament. Fixes "tyranny of the majority" where 8 long-biased brains created spurious LONG consensus, silencing the only short-biased brain before Track 2 could evaluate it. | RC-06 (serial deadlock) |
| FIX-20260524-042 | 2026-05-24 | cursor-agent | — | T1-H2: vol_ratio envelope check now uses raw ATR before √t scaling — previously sqrt(timeframe_mult) inflated vol_ratio 3.46× for H1 strategies, making envelope_warning fire every cycle. T1-H3: ConformalOUGate._extract_ou_diagnostics() now validates brain contract_group matches strategy name via BrainRegistry before accepting OU brain match. Fallback requires BOTH theta+half_life in diagnostics. | RC-05 (boundary-error), RC-06 (contract-violation) |
| FIX-20260608-148 | 2026-06-08 | cursor-agent | — | **S3 — p_win chain extracted as pure functions (Functional Core)**: `resolve_p_win_from_brains()` and `adjust_p_win_for_regime()` moved from kelly_sizer.py + strategy_line.py to new `pwin_chain.py`. Both functions are pure (no I/O, same input → same output). Verified by Golden Master replay (911 cycles, behavior unchanged). Enables Hypothesis property-based testing. | RC-06 |
| FIX-20260613-058 | 2026-06-13 | cursor-agent | — | **Extreme Value Gate BTC False Positive**: Threshold 10.0 was blocking all btc_swing trades (BTC co_ratio=221 is legitimate). Raised to 1e6. | RC-05 |
| FIX-20260613-090 | 2026-06-13 | cursor-agent | 35c7213 | **Fail-Closed Budget Latch + Physics Override**: (1) StrategyBudget.cooldown_minutes 30→0 — paused strategies never auto-unpause, budget breach cascades to global circuit_breaker via block_new_entries with auto-reset immunity. (2) RegimeDirectionGate._resolve_trend() Priority 0 physics override: OU Theta > 0.5 AND Hurst < 0.48 → "ranging" regardless of ADX. NaN-guarded, physically range-bounded. (3) MT5 backfill hydrates _recent_mid_prices on startup for instant physics availability. TODO: phase out ADX gating when brains retrained with V9_Micro features. | RC-06, RC-02 |
| FIX-20260623-066 | 2026-06-23 | cursor-agent | — | **DQAF-066: p_win Cold-Start Triple-Break Repair**. P0-1: `resolve_p_win_from_brains()` gains `governance_state` cold-start fallback — when PnL store is empty, computes median win_rate from governance `performance_metrics` (all-time WR from immutable labels ledger). P0-2: `resolve_p_win()` cold_explore step now uses governance fallback instead of blind 0.50. P0-3: Cold explore entry gate requires ≥2 LIVE brains with governance win_rate>0 before allowing bounded exploration. Fixes -34.84R/36h cold-start spiral (0% XAU WR, 13% BTC WR) where DQAF-065 MetaFilter excision routed all swing strategies through blind p_win=0.50. ReB: `COLD_EXPLORE_TRAP`. | L2 — cold_explore p_win resolution had no governance alignment; L3 — PnL store amnesia after restart created chicken-and-egg deadlock |
| FIX-20260616-100 | 2026-06-16 | cursor-agent | — | **L3 Architecture Fix — Reentry Guard Strategy-Type Awareness (DQAF-20260616-001)**: 三层修复: (L1/L2) exit_reason.classify() 词汇表补全 + restart_state _reason 用 label 而非 MT5 端原因. (L3) reentry_guard 新增 `is_rule_based` 参数 → rule-based 策略使用时间冷却 (max(120s, 40% bar)) 而非 ML 置信度阈值 → 永久消除 rule-based 策略被 ML 门禁死锁的结构性漏洞. Magic 90501 双侧死锁已确认解除. | RC-07 |
| FIX-20260619-023 | 2026-06-19 | cursor-agent | — | **TECH_DEBT-005 Phase 1: Dynamic tick-based session detection — SessionDetector deployed**. New `core/execution/session_detector.py` — tick-frequency state machine (NORMAL→ROLLOVER→CLOSED→NORMAL). Wired into `detect_session()` via optional `tick_time` parameter. When tick_time provided (live cycles): uses physical-state probe. When omitted (bootstrap/backtest): falls back to static _SESSIONS table. First call site in live_cycle passes `_tick_time` — subsequent calls use cached result. BTC (crypto_24_7) always normal. TECH_DEBT-005 Phase 2: validate 2 weeks, then delete static table. | RC-12 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `compute_position_size(account_equity, risk_per_trade, atr, sl_distance)` → `float` | strategy_line | Stable |
| `compute_kelly_mult(p_win, rr_ratio, fractional_k=0.5, floor=0.5, cap=1.5)` → `KellyResult` | strategy_line | Stable |
| `resolve_p_win_from_brains(brains, pnl_store, direction, live_brain_ids, governance_state)` → `float` | strategy_line | Stable |
| `apply_sqrt_n_discount(decisions, lot_step, min_lot)` → `(decisions, [ClusterResult])` | live_cycle | Stable |
| `detect_session(timestamp)` → `str` (asian/london/ny) | live_cycle | Stable |
| `StrategyBudget.check(strategy_id, sl_hit)` → `bool` | live_cycle | Stable |
| `CooldownRegistry.record_exit(strategy, direction, reason, timestamp)` → `dict` | live_cycle | Stable |
| `CooldownRegistry.check_cooldown(strategy, direction, now)` → `(bool, str)` | live_cycle | Stable |
| `FamilyEntryTracker.record_entry(family, direction, timestamp)` → `None` | live_cycle | Stable |
| `FamilyEntryTracker.check_spacing(family, direction, strategy, now, min_gap_sec)` → `(bool, str)` | live_cycle | Stable |
| `strategy_to_family(strategy)` → `str` | live_cycle | Stable |
| `strategy_timeframe_sec(strategy)` → `int` | live_cycle | Stable |
| `ActivePositionManager.register_position(*, trail_atr_mult, trail_atr_mult_low, trail_atr_mult_high, breakeven_threshold_atr)` → `ActivePosition` | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "guard" -q
```

## Exit Effectiveness Data (DATA-BACKED, 2026-05-16)

> **Data source**: `data/live_trade_journal.jsonl` — 238 confirmed closes with PnL data, spanning 2026-04-29 to 2026-05-16.
> **Purpose**: Permanent reference for SL/TP calibration and exit logic effectiveness.

### Exit Reason Performance (all strategies)

| Exit Reason | Count | Wins | Losses | BE | Win% | Total PnL | Avg PnL | Avg Hold |
|-------------|-------|------|--------|----|------|-----------|---------|----------|
| **tp_hit** | 10 | 10 | 0 | 0 | 100.0% | +1.52 | +0.152 | varies |
| **sl_hit** | 46 | 1 | 42 | 3 | 2.2% | -4.92 | -0.107 | varies |
| unknown | 142 | 62 | 57 | 23 | 43.7% | +0.43 | +0.003 | — |
| unknown_close | 22 | 6 | 13 | 3 | 27.3% | +0.02 | +0.001 | — |
| auto_orphan_stale | 7 | 0 | 0 | 7 | 0.0% | 0.00 | 0.000 | — |
| auto_orphan_rejected | 5 | 0 | 0 | 5 | 0.0% | 0.00 | 0.000 | — |
| sl_tp_auto_close | 5 | 0 | 2 | 3 | 0.0% | -0.14 | -0.028 | — |

### Key Metrics

| Metric | Value | Assessment |
|--------|-------|------------|
| **SL:TP hit ratio** | **4.6:1** (46 SL / 10 TP) | 🔴 SL triggers far too frequently |
| Avg TP win amount | +0.152 | ✅ Per-trade R:R ~1.4:1 |
| Avg SL loss amount | -0.107 | ✅ Individual SL sizing is reasonable |
| **Effective profit factor** | **0.31** (1.52 / 4.92) | 🔴 SL wins 5× more than TP earns |
| Closes missing PnL | 125/369 (33.9%) | ⚠️ Journal completeness gap |

### Per-Strategy Exit Performance

| Strategy | Trades | Win% | Total PnL | Avg Hold | Avg R/trade |
|----------|--------|------|-----------|----------|-------------|
| **barrier_12bar** | 116 | 38.8% | **+0.41** | 10.7h | -0.001 |
| statarb_dynamic | 57 | 36.8% | +0.05 | 9m | 0.000 |
| h1_swing | 5 | 40.0% | +0.21 | 7m | +0.005 |
| m15_swing | 20 | 25.0% | -0.44 | 11m | -0.003 |
| micro_3bar | 4 | 0.0% | -0.80 | N/A | -0.039 |

### SL/TP Calibration Assessment

**Current parameter R:R ratios vs actual market noise:**

| Strategy | SL (ATR×) | TP (ATR×) | Design R:R | Actual Win% | Breakeven Win% Needed |
|----------|-----------|-----------|------------|-------------|----------------------|
| barrier_12bar | 2.0 | 3.5 | 1.75:1 | 38.8% | 36.4% |
| statarb_dynamic | 1.5 | 3.0 | 2.0:1 | 36.8% | 33.3% |
| h1_swing | 2.0 | 3.5 | 1.75:1 | 40.0% | 36.4% |

**Finding**: Design R:R ratios are adequate — at current win rates, barrier_12bar (38.8% > 36.4% breakeven) should be profitable. The actual journal PnL supports this (+0.41 for barrier_12bar). However, the **SL trigger frequency (4.6× TP frequency)** is the dominant issue, not the per-trade sizing.

### Critical Diagnostic: ML Brains Frozen Confidence (2026-05-16)

**Both LightGBM brains produce IDENTICAL confidence on every prediction:**
- `LightGBM_V1_Institutional` (barrier_12bar): **0.5519 every signal**
- `lightgbm_h1_swing` (h1_swing): **0.6120 every signal**

This indicates the ML feature pipeline is broken — models receive constant/zero feature vectors, collapsing to a single leaf node. This explains:
1. Why both brains are 100% LONG (leaf value happens to be LONG class)
2. Why confidence never changes
3. Why training Sharpe (8.21) diverges completely from live Sharpe (-0.10)
4. Why 7/8 barrier brains are 100% neutral (same broken pipeline)

**Only the non-ML brain (OU_Params_V6_Sniper) produces valid, changing predictions** with 49.7% win rate and +119.91 bps live PnL.

### Historical Loss Attribution

**magic=90004** (unregistered strategy, May 5–7 only): 27 trades, 7.4% win rate, **-2.46 total PnL**. Accounts for **79% of all journal losses**. Already removed from current config. Not mapped to any current strategy magic.

### Data Gaps Identified

1. **60% of exits have "unknown" reason** (142/238) — exit reason not captured at journal-write time
2. **34% of closes lack PnL** (125/369) — MT5 order results not confirmed in journal
3. **No per-strategy magic mapping for historical trades** — magic=90004/90005/90009/90010 untraceable
4. **Meta filter v2 shows 74.4% kept win rate** on training data, but live achieves only 38.8% — feature pipeline mismatch suspected

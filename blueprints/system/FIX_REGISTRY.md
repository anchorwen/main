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
| FIX-20260518-037 | 2026-05-18 | execution-orders | Multi-position refactor: ActivePositionManager → dict[ticket→ActivePosition], register_position no longer blocks, recovery iterates ALL MT5 positions, management phase loops all positions, save/load v2 multi-position format | RC-05 |
| FIX-20260518-036 | 2026-05-18 | execution-orders | Phase A+B: Confidence Spring (Layer-2 confidence_ema modulates Chandelier trail K: +0.6 high-conf → -0.5 low-conf) + EV Trajectory Envelope (sqrt-law Alpha decay exit with grace period + 0.5R tolerance floor) replaces linear time-decay phases. Import math added. | RC-12 |

---
## Fix Details by Year

| Year | File | Count |
|------|------|-------|
| 2026 | [FIX_REGISTRY_2026.md](FIX_REGISTRY_2026.md) | 68 |

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

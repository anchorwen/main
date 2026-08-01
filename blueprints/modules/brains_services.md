# Brains / Services

## Purpose
Brain lifecycle services: factory construction, inference orchestration, leaderboard ranking, promotion evaluation, dynamic vote weighting, attribution, A/B testing, and stability monitoring.

## Key Files
| File | Role |
|------|------|
| `core/brains/services/brain_factory.py` | Builds adapter instances from brain_entry dicts |
| `core/brains/services/brain_run_service.py` | Routes feature sources to correct adapters, runs inference |
| `core/brains/services/brain_registry_service.py` | Loads active brain entries (auto-discovery from disk, optional live.yaml allowlist) |
| `core/brains/services/brain_leaderboard.py` | Composite scoring (Sharpe, WR, PF, PnL) for brain ranking |
| `core/brains/services/brain_promotion.py` | Automated candidate→probation→active/retire lifecycle |
| `core/brains/services/dynamic_brain_weighter.py` | Per-brain vote weights from P&L, redundancy penalty |
| `core/brains/services/brain_attribution_service.py` | Per-brain P&L attribution from trade journal |
| `core/brains/services/inference_guard.py` | Subprocess ONNX isolation with crash recovery |
| `core/brains/services/onnx_worker.py` | Standalone ONNX worker process |
| `core/brains/services/stability_monitor.py` | PSI/CSI drift monitoring |
| `core/brains/services/ab_test.py` | A/B experiment framework |

## Data Flow
```
DecisionCycleOrchestrator  ←  GovernanceService.get_all_states()
       │                              {brain_id: status}
       │  gov_state_filter dict       ("frozen"/"retired" excluded at source)
       ▼
BrainRegistryService.list_active_entries(gov_state_filter=None)
       │
       ▼
  brain_entries → BrainFactory → adapters
                                    ↓
                            BrainRunService.run()
                                    ↓
                            BrainSignal[]
                                    ↓
                  ┌─────────────────────────────────┴──────────────────────┐
                  ↓                      ↓                                 ↓
        DynamicBrainWeighter      BrainLeaderboard              BrainAttributionService
        (vote weights)            (rankings)                    (P&L breakdown)
                  ↓                      ↓
        BrainPromotionEvaluator  →  governance_state.json
```

**DQAF-20260624-058**: Governance status injection at water source. `gov_state_filter` dict
built from `GovernanceService.get_all_states()` by `DecisionCycleOrchestrator`, passed
through `RuntimeLoop` → `BrainRunService` → `BrainRegistryService.list_active_entries()`.
Frozen/retired brains excluded from ALL inference paths (live, shadow, contract-groups).
Zero governance import below orchestrator layer — pure dict dependency injection.
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/adapters | ADAPTER_REGISTRY, BRAIN_TYPE_MAP, BaseBrainAdapter | Factory construction |
| contracts/domain | BrainDecisionProposal | Inference output |
| schemas/trading_contracts | BrainSignal | Layer 1 output type |
| features/adapters | V9FeatureAdapter, MicrostructureFeatureAdapter | Feature normalization |
| feedback | BrainPerformanceTracker, BrainPnLStore, BrainQualityEngine | Quality/weight signals |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | DynamicBrainWeighter | Vote weight computation |
| deployment/lifecycle | BrainFactory, BrainRunService | Service wiring |
| execution/strategy_line | DynamicBrainWeighter | Per-strategy brain weighting |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260801-011 | 2026-08-01 | cursor-agent | — | **SSOT governance unification (DQAF-20260801-010)**: `apply_promotion_decisions()` marked `DEPRECATED — TODO: Remove in next cleanup`. Was the second writer in live_intent_loop's dual-track race (BrainPnLStore last-20 window, FIX-20260611-001). Kept ONLY for brain_promotion_runner.py + tests; new code MUST route through GovernanceRuleEngine.execute_transitions (sole writer, observation-hold aware). | L2 — deprecated direct-write helper outlived its sole purpose after launcher path amputation |
| FIX-20260627-147 | 2026-06-27 | cursor-agent | — | InferenceGuard.__del__: add AttributeError to except clause. __init__ raises FileNotFoundError before self._lock is set; __del__ calls shutdown() which accesses _lock → AttributeError. On Windows Server this causes PytestUnraisableExceptionWarning → exit code 1 in CI. | incomplete-error-handling |
| FIX-20260625-136 | 2026-06-25 | cursor-agent | bc9094a8 | Augment BrainPromotionEvaluator with journal-based PnL. Tracker profit_factor was composite_score proxy; override with actual MT5 trade PnL from journal. Closes data-source gap that caused high-PF brains to be undervalued by promotion evaluator. (DQAF-20260625-060 Phase 2) | missing-feature |
| FIX-20260625-135 | 2026-06-25 | cursor-agent | dcbe93b9 | Fix _compute_metrics_from_tracker() consecutive losses: lifetime max to tail (DQAF-20260625-060). 5 brains incorrectly flagged for freeze rescued. | contract-violation |
| FIX-20260613-074 | 2026-06-13 | cursor-agent | 6856291 | Promotion-before-throttle: reversed check order so probation→live promotion runs BEFORE throttle check. Previously recent_wr<38% throttle intercepted probation brains before they could reach promotion evaluation. Swing_V9_M15_V2 (PF=3.51) was stuck in promote→throttle→promote loop. | contract-violation |
| FIX-20260610-007 | 2026-06-10 | cursor-agent | — | **Leaderboard equal-weight fallback**: rank() vote_weights空/全零→1/N兜底, 彻底消除全局权重0瘫痪. | RC-06 |
| FIX-20260607-147 | 2026-06-07 | cursor-agent | — | **Vote weight decoupling**: `apply_weights()` stamps `p.dynamic_scale` instead of overwriting `p.vote_weight`. Config base_weight preserved as binary permission gate. Prevents shadow brains (config vote_weight=0.0) from accumulating collective dynamic weight to override voting brains. DQAF-011. | RC-09 |
| FIX-20260605-126 | 2026-06-05 | cursor-agent | — | **Brain_Rev_M30_V1/V2 archived + Brain_Trend_M30_V1 promoted shadow→candidate (vw=0.8)**: Rev killed by eval bug and SL/TP mismatch. Trend promoted based on 7-day signal activity. Final roster: 11 candidate, 2 live, 2 shadow, 5 archived. | RC-11 |
| FIX-20260605-125 | 2026-06-05 | cursor-agent | — | **Meta Pipeline probe trio archived**: Huber (1627 attr, -369.65R), Binary_Cls (540 attr, 100% LONG bias), MetaLabel (417 attr, 0 signals). All three had structurally negative expectancy. | RC-11 |
| FIX-20260604-088 | 2026-06-04 | cursor-agent | — | **brain_promotion.py bare-write eliminated**
| FIX-20260531-010 | 2026-05-31 | cursor-agent | — | brain_leaderboard.py docstring: added BTC usage example (data_btc/ paths). Actual code already supports base_dir parameter. | RC-09 |
| FIX-20260528-023 | 2026-05-28 | cursor-agent | — | train_swing_v9 brain config output: added schema_version/magic/artifact_path fields | RC-09 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: added strict-list feature order handshake in BrainFactory.build() — compares config `features` against model .meta.json `feature_names` using `!=` (NOT set()), raises BrainConfigError at first differing index. LightGBM uses positional indexing, scrambled features produce garbage predictions. | RC-06 |
| FIX-20260527-001 | 2026-05-27 | cursor-agent | — | Meta_Stage1_Binary_Cls_V1 vote_weight 0.0→0.8 + status shadow→probation in brain config. Previous vote_weight=0.0 (set for Plan B shadow OOF accumulation) meant even after governance unfreeze, brain had zero voting power — barrier_12bar probation mode could not produce trades. Governance probation penalty (0.5×) will reduce effective weight to ~0.4. Part of governance auto-freeze recovery. | RC-09, RC-11 |
| FIX-20260524-001 | 2026-05-24 | cursor-agent | — | Single source of truth: BrainRegistryService now auto-discovers brain configs from configs/brains/ when live.yaml registry_entries is empty. Redundant manual registration eliminated. | RC-09 |
| FIX-20260524-003 | 2026-05-24 | cursor-agent | — | P0-2 zombie brain removal: deleted LightGBM_V3_New and XGBoost_V11_New from governance_state.json. No config files, no model artifacts, no code references, 0% WR (8t, -0.01). Previously deleted in FIX-20260517-011, accidentally re-registered 2026-05-22. | RC-11 (stale-data) |
| FIX-20260524-004 | 2026-05-24 | cursor-agent | — | P2 OU governance gap: registered OU_Params_V7_M15 in governance_state.json. Brain had config+live.yaml entry but was never registered in governance — no transition tracking, no freeze count, no exposure limiting. Both V6+V7 share same artifact (arb_params_v7.json). Recent drawdown: V6 avg composite 0.472, V7 avg 0.483 (both below 0.50 breakeven). | RC-09 (config-drift) |
| FIX-20260524-006 | 2026-05-24 | cursor-agent | — | SSOT Dictator Governance Engine: 20 state contamination entries cleaned from governance_state.json (23→3). Online_MLP_V1 deleted (evicted from barrier_12bar Dictator Protocol 2026-05-22, brain_type=online_sgd not in any enabled strategy's brain_types). LightGBM_V1_Institutional + XGBoost_D1_Swing_5d deleted (zombies: probation status but no config/model/code refs). 16 frozen graveyard entries deleted (all without configs, batch re-registered 2026-05-23). 5 stale disk configs deleted. | RC-11 (state-contamination: auto_repair was one-way door) |
| FIX-20260524-005 | 2026-05-24 | cursor-agent | — | P2 OU timeframe parameter separation: created arb_params_v7_m5.json (Sharpe 3.27, theta_min=0.0027) and arb_params_v7_m15.json (Sharpe 2.76, theta_min=0.0186). Both brains previously shared arb_params_v7.json (M5-optimized, Sharpe 0.54). M15 theta_min is 6.9x higher — different timeframes need different OU parameters. V6→M5 artifact, V7→M15 artifact. | RC-05 (boundary-error: timeframe-invariant parameter assumption) |
| FIX-20260519-010 | 2026-05-19 | cursor-agent | — | Track 3: Confidence-Weighted Marginal Attribution. _attribute_trades() now splits brains into sponsors (voted with trade, P&L weighted by confidence) vs dissenters (voted against, exempted). _split_sponsors_dissenters() helper. BrainAttribution新增sponsor_count/dissenter_count字段. | RC-06 |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | BrainPromotionEvaluator role reduction: class docstring updated to "Auditor". apply_promotion_decisions() deprecated (use GovernanceRuleEngine.execute_transitions() instead). No functional change to evaluation logic. | contract-violation |
|--------|------|--------|--------|---------|------------|
| FIX-20260516-008 | 2026-05-16 | cursor-agent | — | BrainConfigValidator (7 checks at BrainFactory.build() time) + BrainAlert (structured JSON to stderr). BrainRunService extended with schema aliases, _failed_brain_ids tracking, run_single_brain/run_brain_type/run_brains_for_contract_group. 20 brain configs repaired with features field. Training pipelines auto-populate features. | RC-09 |
| FIX-20260516-007 | 2026-05-16 | cursor-agent | — | Base adapter run(): metadata-driven feature extraction from brain_entry["features"]. Strategy files + live_cycle unified to adapter.inference() convenience method. | RC-06 |
| FIX-20260524-040 | 2026-05-24 | cursor-agent | — | DEFERRED architecture debt: dual governance pipeline merge (BrainPromotionEvaluator vs GovernanceRuleEngine), leaderboard consumer gap, stability monitor unused, AB test framework not activated. No code changes — registered for future sprints. | RC-12 |
| FIX-20260516-006 | 2026-05-16 | cursor-agent | — | All adapters: dimension guards + brain_alert on fallback paths. V9_ONNX + Transformer: _num_features from ONNX input shape. OnlineLearner: alert on silent truncation. XGBoost + LightGBM: alert on dim mismatch + load failure. | RC-06 |
| FIX-20260516-004 | 2026-05-16 | cursor-agent | — | BrainRunService: replaced scattered feature_source/feature_vector params with single feature_blackboard: dict[str, dict] — each brain self-routes by looking up its feature_schema_id on the blackboard. Missing schema → empty dict → safe neutral. | RC-06 |
| FIX-20260515-015 | 2026-05-15 | cursor-agent | — | brain_votes recording fix: record_brain_votes moved to after _compute_consensus, now uses real ContractGroupConsensus confidence instead of misleading _rough_conf | contract-violation |
| FIX-20260515-016 | 2026-05-15 | cursor-agent | — | Phase1 revival: 3 viable brains promoted shadow→probation/live (OU_Params_V6, LightGBM_V1, lightgbm_h1_swing) | config-drift |
| FIX-20260515-014 | 2026-05-15 | cursor-agent | — | 8 accidentally deleted brain configs restored, contract_group added, artifact_paths remapped to institutional models, 4 barrier_12bar brains re-enabled | stale-data |
| FIX-20260514-009 | 2026-05-14 | cursor-agent | a4a1005 | Change resolve_ids_to_group fallback from barrier_12bar to unknown to prevent silent misattribution | contract-violation |
| FIX-20260514-007 | 2026-05-14 | cursor-agent | a4a1005 | Add new-brain protection period (min_signals_active=100), graduated retirement path (active->frozen->retired instead of direct retire) | missing-validation |
| FIX-20260522-018 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: BrainRunService output type updated from `BrainDecisionProposal[]` to `BrainSignal[]`. All consumers (live_cycle, shadow, verify) now receive typed frozen dataclasses with `direction`/`confidence`/`raw_score` fields instead of dict-based `prediction`. Backward-compat retained for callers expecting legacy dict access. | RC-06 |
| FIX-20260524-026 | 2026-05-24 | cursor-agent | — | Docstring fix: _compute_weight_from_metrics docstring claimed return range [0.0, 1.5] but clamp was max(0.0, min(3.0, weight)) → actual range [0.0, 3.0]. Docstring updated to match code. | RC-06 |
| FIX-20260524-035 | 2026-05-24 | cursor-agent | — | Meta_Stage1_Huber_V1 status alignment: brain config status shadow→frozen to match governance_state.json (frozen) and live.yaml (enabled:false). Three-way status inconsistency resolved. Formal baselines rebuilt. | RC-09 |
| FIX-20260524-038 | 2026-05-24 | cursor-agent | — | H1: Health tier handling: exceptional→healthy*1.2, marginal→base*0.5+sharpe (previously both fell through to else/stable branch). H2: composite_mean max(5.0,1.0)→min(max(sharpe,-5.0)/5.0,1.0) — old formula max(5.0,1.0) was always 5.0. H4: Low-signal-count protection (<20) added — bad perf now → probation instead of falling through unprotected. | RC-06 |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M4: BrainLeaderboard docstring formula fixed to match code (wr-0.35/0.55 not wr-0.40*2.0). M6: _split_sponsors_dissenters neutral vote exclusion documented. M7: brain_votes `or []` changed to explicit `is None` check to preserve empty lists. M10: apply_promotion_decisions now validates targets against VALID_TRANSITIONS before writing. H7-equivalent: pf>0 guard removed from auto-retire gate in dynamic_brain_weighter (pf==0 now caught). | boundary-error |
| FIX-20260524-036 | 2026-05-24 | cursor-agent | — | Brain SL/TP + magic audit: barrier_12bar SL/TP corrected 2.0/3.5→3.0/1.5 to match retrained calibration (EV=0.2004R). 4 brain magic numbers aligned to strategy magic for MT5 dispatch consistency. BARRIER_GROUP contract name + description updated. | RC-09 |
| FIX-20260620-024 | 2026-06-20 | cursor-agent | — | **DEFERRED: Governance hysteresis — promotion↔throttle oscillation prevention**: Missing hold-down period between promote and throttle allows ping-pong live↔probation transitions. Registered as L3 architecture debt; activate when any BTC brain accumulates ≥50 live trades. Trigger: `performance_metrics.trades ≥ 50` OR `2026-07-15`. | RC-12 (missing-feature) |
| FIX-20260624-122 | 2026-06-24 | cursor-agent (IC_MANDATE) | — | **DQAF-058 (Sev 1): Frozen Brain Inference Pipeline Bypass — L3 Governance Gate at BrainRegistryService.** Ghost brains excluded at water source via dependency-injected `gov_state_filter: dict[str, str] | None` parameter. Frozen/retired brains no longer consume inference compute. ReB: `GOVERNANCE_DISCONNECT`. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainFactory.build(brain_entry)` → `BaseBrainAdapter` | ServiceContainer | Stable |
| `BrainRunService.run(snapshot, feature_source)` → `list[BrainDecisionProposal]` | signal_pipeline | Stable |
| `DynamicBrainWeighter.compute_weights(metrics)` → `dict[brain_id, float]` | strategy_line | Evolving |

## Verification
```bash
python -m pytest tests/ -k "brain" -q
```

# Brains / Services

## Purpose
Brain lifecycle services: factory construction, inference orchestration, leaderboard ranking, promotion evaluation, dynamic vote weighting, attribution, A/B testing, and stability monitoring.

## Key Files
| File | Role |
|------|------|
| `core/brains/services/brain_factory.py` | Builds adapter instances from brain_entry dicts |
| `core/brains/services/brain_run_service.py` | Routes feature sources to correct adapters, runs inference |
| `core/brains/services/brain_registry_service.py` | Loads active brain entries from live.yaml |
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
BrainRegistryService → brain_entries → BrainFactory → adapters
                                                      ↓
                                              BrainRunService.run()
                                                      ↓
                                              BrainDecisionProposal[]
                                                      ↓
                    ┌─────────────────────────────────┴──────────────────────┐
                    ↓                      ↓                                 ↓
          DynamicBrainWeighter      BrainLeaderboard              BrainAttributionService
          (vote weights)            (rankings)                    (P&L breakdown)
                    ↓                      ↓
          BrainPromotionEvaluator  →  governance_state.json
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/adapters | ADAPTER_REGISTRY, BRAIN_TYPE_MAP, BaseBrainAdapter | Factory construction |
| contracts/domain | BrainDecisionProposal | Inference output |
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
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | BrainPromotionEvaluator role reduction: class docstring updated to "Auditor". apply_promotion_decisions() deprecated (use GovernanceRuleEngine.execute_transitions() instead). No functional change to evaluation logic. | contract-violation |
|--------|------|--------|--------|---------|------------|
| FIX-20260516-008 | 2026-05-16 | cursor-agent | — | BrainConfigValidator (7 checks at BrainFactory.build() time) + BrainAlert (structured JSON to stderr). BrainRunService extended with schema aliases, _failed_brain_ids tracking, run_single_brain/run_brain_type/run_brains_for_contract_group. 20 brain configs repaired with features field. Training pipelines auto-populate features. | RC-09 |
| FIX-20260516-007 | 2026-05-16 | cursor-agent | — | Base adapter run(): metadata-driven feature extraction from brain_entry["features"]. Strategy files + live_cycle unified to adapter.inference() convenience method. | RC-06 |
| FIX-20260516-006 | 2026-05-16 | cursor-agent | — | All adapters: dimension guards + brain_alert on fallback paths. V9_ONNX + Transformer: _num_features from ONNX input shape. OnlineLearner: alert on silent truncation. XGBoost + LightGBM: alert on dim mismatch + load failure. | RC-06 |
| FIX-20260516-004 | 2026-05-16 | cursor-agent | — | BrainRunService: replaced scattered feature_source/feature_vector params with single feature_blackboard: dict[str, dict] — each brain self-routes by looking up its feature_schema_id on the blackboard. Missing schema → empty dict → safe neutral. | RC-06 |
| FIX-20260515-015 | 2026-05-15 | cursor-agent | — | brain_votes recording fix: record_brain_votes moved to after _compute_consensus, now uses real ContractGroupConsensus confidence instead of misleading _rough_conf | contract-violation |
| FIX-20260515-016 | 2026-05-15 | cursor-agent | — | Phase1 revival: 3 viable brains promoted shadow→probation/live (OU_Params_V6, LightGBM_V1, lightgbm_h1_swing) | config-drift |
| FIX-20260515-014 | 2026-05-15 | cursor-agent | — | 8 accidentally deleted brain configs restored, contract_group added, artifact_paths remapped to institutional models, 4 barrier_12bar brains re-enabled | stale-data |
| FIX-20260514-009 | 2026-05-14 | cursor-agent | a4a1005 | Change resolve_ids_to_group fallback from barrier_12bar to unknown to prevent silent misattribution | contract-violation |
| FIX-20260514-007 | 2026-05-14 | cursor-agent | a4a1005 | Add new-brain protection period (min_signals_active=100), graduated retirement path (active->frozen->retired instead of direct retire) | missing-validation |

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

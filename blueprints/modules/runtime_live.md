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

## Data Flow
```
┌──────────────────────────────────────────────────────────┐
│                    LiveCycle.run_cycle()                  │
│                                                          │
│  1. market_ingress → ControlSnapshot                     │
│  2. FeatureService → feature vectors                     │
│  3. BrainRunService → BrainDecisionProposal[]            │
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
| FIX-20260515-008 | 2026-05-15 | cursor-agent | — | Watchdog cleanup: deleted scripts/hourly_watchdog.py (deprecated May 5 experiment), data/watchdog.log. Updated ADR-006 to document live_launcher as sole production runtime. Fixed verify.py deleted-file filtering (exists() check). | config-drift |
| FIX-20260515-006 | 2026-05-15 | cursor-agent | a4a1005 | Schema ID mismatch: swing_24 not recognized in brain re-evaluation path. Added swing_24 alias alongside daily_swing_24 in both position-management inference routes. Also fixed _STRATEGY_CONTRACT_TYPES to use timeframe-prefix matching (m15_swing etc) for broader training_contract compatibility. | config-drift |
| FIX-20260514-008 | 2026-05-14 | cursor-agent | a4a1005 | Add raw_proposals to defensive initialization block to prevent UnboundLocalError in single-brain mode | state-leak |
| FIX-20260511-001 | 2026-05-14 | cursor-agent | a4a1005 | Fixed multiple issues found during surgical audit of daily_ops, governance training, and execution risk controls | missing-validation |
| FIX-20260514-001 | 2026-05-14 | cursor-agent | a4a1005 | Blueprint mechanism upgrade: modular fix tracking with automated markers | contract-violation |
| FIX-20260514-002 | 2026-05-14 | cursor-agent | a4a1005 | Blueprint mechanism upgrade: modular fix tracking (retry after hyphen fix) | contract-violation |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `LiveCycle.run_cycle()` → `RuntimePipelineResult` | main.py, live_launcher | Stable |
| `RuntimeExecutionPipeline.execute(strategies)` → `RuntimePipelineResult` | LiveCycle | Stable |
| `signal_health.FeatureGate.check(snapshot)` → `GateResult` | LiveCycle | Stable |
| `record_brain_votes(proposals, cycle_id)` → `None` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "runtime or live or cycle" -q
```

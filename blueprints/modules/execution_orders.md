# Execution / Orders

## Purpose
Order lifecycle management (creation → ack → fill → close), position tracking with multi-layer exits, dynamic SL/TP computation, capital allocation across strategy groups, and broker gateway abstraction.

## Key Files
| File | Role |
|------|------|
| `core/execution/order_state_machine.py` | `OrderStateMachine` — canonical state transitions |
| `core/execution/execution_manager.py` | `ExecutionManager` — order lifecycle, venue event processing |
| `core/execution/position_manager.py` | `ActivePositionManager` — 3-layer exits (Chandelier, consensus flip, time-decay) |
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
| FIX-20260515-013 | 2026-05-15 | cursor-agent | — | Three-knife OU exit refactor: (1) Smart Entry inflection gate z_entry 1.5→2.0 + volume climax, (2) Drift Lock spatial re-entry lock after mean-drift exit, (3) Alpha Handoff OU→trailing-stop on profit+trend | missing-feature |
| FIX-20260514-003 | 2026-05-14 | cursor-agent | a4a1005 | Fixed raw_proposals UnboundLocalError: elif indentation error caused multi-strategy evaluation to be unreachable | type-confusion |
| FIX-20260513-001 | 2026-05-14 | cursor-agent | a4a1005 | PnL recording moved before approval gate: each proposal gets isolated PnL record to prevent missing ledger entries | state-leak |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OrderStateMachine.transition(current, event)` → `OrderState` | ExecutionManager | Stable |
| `dispatch_live_order(envelope)` → `bool` | ExecutionQueue | Stable |
| `ActivePositionManager.evaluate_exits(market_data)` → `list[ExitEvaluation]` | live_cycle | Stable |
| `compute_dynamic_sl_tp(atr, regime)` → `DynamicSLTP` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "execution or order or fill" -q
```

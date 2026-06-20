# Execution / Strategy Line

## Purpose
Abstract base class for all strategy lines. Defines the `StrategyDecision` dataclass, `StrategyLineConfig` immutable configuration, and the 8-phase `evaluate()` pipeline that every ML strategy inherits.

## Key Files
| File | Role |
|------|------|
| `core/execution/strategy_line.py` | `StrategyDecision`, `StrategyLineConfig`, `StrategyLine` base class + `evaluate()` (1,900+ lines) |

## Architecture

### StrategyDecision (23 fields)
Immutable output of one strategy evaluation. Key fields: `should_trade`, `direction`, `confidence`, `volume`, `sl`, `tp`, `brain_ids`, `p_win`, `p_win_source`, `kelly_mult`, `decision_hash` (audit), `evaluated_at` (audit), `code_version` (audit).

### StrategyLine.evaluate() — 8-Phase Pipeline
```
Phase 1:  Regime gate (ADX/ATR volatility → full/reduced/shadow/blocked)
Phase 1c: Spread gate (max_spread_points)
Phase 2:  Budget check (daily loss + consecutive losses)
Phase 3:  Brain inference (all brain adapters run in parallel)
Phase 4:  Group consensus (ContractGroupConsensus voting)
Phase 4ab: MetaFilter gate (direction-specific model filter)
Phase 4aa: Trend isolation gates (counter-trend blocking/penalising)
Phase 5:  Dynamic SL/TP computation
Phase 6:  Volume: base→Kelly→UCB→regime_adjust→MVS→lot_step
Phase 7:  StrategyDecision assembly
```

### Subclasses
| Class | File | Strategy Type |
|-------|------|---------------|
| `BarrierStrategy` | barrier_strategy.py | Survival-barrier ML |
| `MicroStrategy` | micro_strategy.py | Microstructure ML |
| `StatArbStrategy` | statarb_strategy.py | OU mean-reversion ML |
| `SwingStrategy` | swing_strategy.py | Swing ML (M15/M30/H1/H4/D1) |
| `RuleEngineStrategyWrapper` | rule_engine_strategy.py | Zero-ML pure-rule (StructuralSwingV1) |

## Strangler Fig Extractions
| Extraction | Destination | Lines |
|-----------|-------------|-------|
| MetaFilter gate routing | meta_filter_routing.py | -172 |
| Trend isolation gates | trend_isolation_gates.py | -384 |
| PWin chain | pwin_chain.py | -50+ |
| **_make_decision() factory (FIX-014)** | strategy_line.py (private method) | Centralized strategy_name/magic |
| **_counter_trend_action() (FIX-015)** | trend_volume_guard.py | -208 |
| **Conformal OU Gate block (FIX-016)** | conformal_ou_gate.py (apply_conformal_ou_gate) | -103 |
| **p_win resolution chain (FIX-017)** | pwin_chain.py (resolve_p_win + PWinResolution) | -108 |
| **Current file size** | | **1,641 lines** (from 2,037, –19.4%) |

## Inbound Dependencies
| Module | What is imported |
|--------|-----------------|
| brains/adapters/* | All brain inference adapters |
| execution/conformal_* | ConformalOU gate, calibrator |
| execution/meta_* | MetaFilter, MetaPipeline |
| execution/pwin_chain | Kelly + PWin computation |
| execution/dynamic_sl_tp | Dynamic SL/TP |
| execution/trend_isolation_gates | Counter-trend blocking |

## Outbound Dependents
| Module | What it imports |
|--------|-----------------|
| runtime/strategy_builder | StrategyLine, StrategyLineConfig |
| runtime/strategy_evaluator | StrategyDecision |

## Fix History
See [execution_orders.md](execution_orders.md) for consolidated Fix History.

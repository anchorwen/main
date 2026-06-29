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
| **OFI gate + volume finalization (FIX-018)** | ofi_gate.py + StrategyLine._finalize_volume() | -79 |
| **Current file size** | | **1,650 lines** (from 2,037, –19.0%) |

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
See [execution_guards.md](execution_guards.md) for consolidated Fix History.

| FIX-20260630-197 | 2026-06-30 | cursor-agent | — | **L3: Remove √t ATR scaling** (DQAF-20260630-197). `dynamic_sl_tp.py` — removed `current_atr *= sqrt(timeframe_mult)`. SL/TP distances inflated 3.46–16.97× for non-M5 strategies. See FIX_REGISTRY for forensic evidence. | L3 — √t double-counts ATR volatility |
| FIX-20260622-064d | 2026-06-23 | cursor-agent | — | **DQAF-064d: XAU 3-Brain LIVE Promotion (IC_MANDATE)**. Swing_V10_H1_Directional (30 trades +107.33R PF=81.10 Sharpe=2.66), Swing_V9_M30_V2 (42 trades +62.53R PF=6.48 Sharpe=1.69), Swing_V9_H4_V2 (11 trades +15.87R PF=18.06 Sharpe=1.56) promoted candidate/probation → live. Brain config JSONs + governance_state updated with IC_MANDATE authority. | L2 — governance scheduler failed to promote brains with strong PnL metrics |
## Data Flow
See [StrategyLine.evaluate() — 8-Phase Pipeline](#strategylineevaluate--8-phase-pipeline) above — the 8-phase pipeline from Regime gate to StrategyDecision assembly serves as this module's Data Flow documentation.

## Known Issues
No known issues.

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `StrategyLine.evaluate(market_state, brain_proposals)` → `StrategyDecision` | strategy_evaluator, live_cycle | Stable |
| `StrategyDecision` dataclass (23 fields, immutable) | All strategy subclasses, live_cycle | Stable |
| `StrategyLineConfig` immutable configuration | strategy_builder | Stable |

## Verification
```bash
python -m pytest tests/ -k "strategy_line or strategy_decision" -q
```

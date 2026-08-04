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

| FIX-20260802-001 | 2026-08-02 | cursor-agent | d04b88fd | DQAF-20260802-002 R1 (IC Ruling: Voting Boundary == EV Boundary): exclude zero-vote brains (vote_weight<=0) from the p_win median pool in pwin_chain.py. BTC_Swing_V4_LGB (probation, vote=0.0, WR=0.4141) silently anchored the btc_swing trio cold_explore pool at 0.4141 — a muted brain's historical WR must not drag ensemble EV. New pool = median of VOTING brains only (0.41,0.4948 -> 0.4524). Applied to BOTH PnL store pool and governance cold-start pool + _compute_live_sample_total. Mirrors brain_gates.count_valid_voters (missing vote_weight defaults to voting, fail-open). | boundary-error |
| FIX-20260802-002 | 2026-08-02 | cursor-agent | — | DQAF-20260802-003 R2 (IC ruling): align strategy_builder btc_swing SL/TP DEFAULTS (2.0/2.5 → 1.5/1.5) to the live_btc.yaml SSOT. Behavior-neutral; kills the misleading RR 1.25 default that masked the symmetric SL=TP RR collapse. | config-taxonomy |
| FIX-20260630-197 | 2026-06-30 | cursor-agent | — | **L3: Remove √t ATR scaling** (DQAF-20260630-197). `dynamic_sl_tp.py` — removed `current_atr *= sqrt(timeframe_mult)`. SL/TP distances inflated 3.46–16.97× for non-M5 strategies. See FIX_REGISTRY for forensic evidence. | L3 — √t double-counts ATR volatility |
| FIX-20260729-001b | 2026-07-30 | cursor-agent | — | **L2: Volume floor hardening — base_volume replaces lot_step/hardcoded 0.01 (companion to FIX-20260729-001).** `_compute_volume()` line 1608: `max(lot_step, ...)` → `max(base_volume, ...)` — after all multipliers crush the risk-budget-computed volume, floor at user's explicit base_volume (0.05) instead of lot_step (0.01). `_finalize_volume()` line 2030: hardcoded `volume=0.01` → `volume=self.config.base_volume` — COLD safety override now respects config. Both changes are defense-in-depth: the real bottleneck was YAML not loading (FIX-20260729-001), but these remove the last two hardcoded 0.01 fallbacks. | L2 — volume pipeline had 3 independent paths to 0.01; YAML fix removed the root cause, these remove the backup traps |
| FIX-20260622-064d | 2026-06-23 | cursor-agent | — | **DQAF-064d: XAU 3-Brain LIVE Promotion (IC_MANDATE)**. Swing_V10_H1_Directional (30 trades +107.33R PF=81.10 Sharpe=2.66), Swing_V9_M30_V2 (42 trades +62.53R PF=6.48 Sharpe=1.69), Swing_V9_H4_V2 (11 trades +15.87R PF=18.06 Sharpe=1.56) promoted candidate/probation → live. Brain config JSONs + governance_state updated with IC_MANDATE authority. | L2 — governance scheduler failed to promote brains with strong PnL metrics |
| FIX-20260804-010 | 2026-08-05 | cursor-agent | — | **L2: 测试 base_dir='data' 污染实盘投票台账 (TEST_LIVE_LEDGER_POLLUTION)**. `record_brain_votes()` (strategy_line.py:899) 每 evaluate() cycle 写 `{base_dir}/brain_votes/{date}.jsonl`; 8 测试文件 + 2 factory 以 base_dir="data"/"data_btc" 构造 StrategyLineConfig → 全量 pytest 将 test_brain_01 测试投票写入实盘台账 (08-04 62 行). 修: `tests/mock_kit/config_factory.py::TEST_BASE_DIR` (OS temp 隔离目录) 单收敛点, 全部测试 base_dir → TEST_BASE_DIR. 62 污染行清洗 (quarantine 备份 `data/_quarantine_brain_votes_test_pollution_20260804.jsonl`), 0 实盘行损失. | boundary-error |
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

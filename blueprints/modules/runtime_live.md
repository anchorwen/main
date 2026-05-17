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

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `LiveCycle.run_cycle()` → `RuntimePipelineResult` | main.py, live_launcher | Stable |
| `RuntimeExecutionPipeline.execute(strategies)` → `RuntimePipelineResult` | LiveCycle | Stable |
| `signal_health.FeatureGate.check(snapshot)` → `GateResult` | LiveCycle | Stable |
| `record_brain_votes(proposals, cycle_id)` → `None` | strategy_line | Stable |

## Strategy Parameter Reference (DATA-BACKED, 2026-05-16)

> **Purpose**: Permanent reference for all strategy parameters, backed by live trading data analysis.
> **Data sources**: `data/brain_votes/2026-05-15.jsonl` (7,216 records), `data/live_trade_journal.jsonl` (1,230 entries, Apr 29–May 16), `data/brain_pnl_ledger.json` (773 trades), `configs/brains/*.json` (training metrics).
> **Update this section whenever thresholds, SL/TP, or exit logic are adjusted.**

### Active Strategy Parameters

#### barrier_12bar (enabled, magic=90001)

| Parameter | Value | Data Justification | Health |
|-----------|-------|-------------------|--------|
| `confidence_threshold` | 0.10 | 29.2% of cycles above 0.10; P50=0.0003, P90=0.1148, P95=0.6500 | ⚠️ Too many neutral brains (7/8) dilute consensus |
| `sl.base_atr_mult` | 2.0 | Training contract uses 2.0 ATR SL; trade data: avg SL loss=-0.107, avg TP win=+0.152 | ✅ RR ratio ~1.4:1 per trade |
| `tp.base_atr_mult` | 3.5 | Training contract uses 3.5 ATR TP | ✅ Consistent with training |
| `tp.partial_tp_r` | 1.5 | Partial TP at 1.5R locks profit while leaving runner | ✅ |
| `budget.max_consecutive_losses` | 5 | 116 trades, win_rate=38.8%, max observed consecutive loss=7 | ⚠️ May need 6–7 buffer |
| `budget.daily_loss_limit_pct` | -0.03 | 3% daily cap on strategy drawdown | ✅ |
| `exit.trail_atr_mult` | 2.0 | Trailing stop distance | ✅ Standard |
| `exit.confidence_decay_exit` | true | Brain confidence naturally decays as barrier approaches | ✅ |
| `min_valid_brains` | 1 | Only 1 brain is directional; higher value would block all trades | ⚠️ Symptom of frozen ML brains |

**Brain-level diagnostics (2026-05-15)**:
| Brain | Status | Directionality | Confidence | Issue |
|-------|--------|---------------|------------|-------|
| LightGBM_V1_Institutional | probation | 100% LONG | **FROZEN at 0.552** | 🔴 ML inference pipeline broken |
| DeepResMLP_V2_New | shadow | 0% (all neutral) | 0.876 (neutral conf) | 🔴 train_acc=0.841 but live=100% neutral |
| Online_MLP_V1 | shadow | 0% (all neutral) | 0.908 (neutral conf) | 🔴 100% neutral |
| CRT, V9, XGBoost, etc. | shadow | 0% (all neutral) | ~0.86–0.99 | 🔴 All dead |

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

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

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260514-013 | 2026-05-14 | cursor-agent | a4a1005 | 最低持仓保护期(min_hold_cycles=3)+毒性流否决逃生舱(tick速度3倍阈值/逼近硬止损0.3ATR) | missing-null-check |
| FIX-20260514-012 | 2026-05-14 | cursor-agent | a4a1005 | 简化分级利润锁定：删除(+2R,0.5R)和(+4R,2.5R)易触发级别，仅保留灾难性保护(+3R,1.5R)和(+5R,3.5R) | boundary-error |
| FIX-20260514-011 | 2026-05-14 | cursor-agent | a4a1005 | 废弃R里程碑拖尾收紧，引入基于已实现波动率的自适应K：vol_ratio > 1.5 放宽K+0.8，vol_ratio < 0.7 收紧K-0.3 | boundary-error |
| FIX-20260514-010 | 2026-05-14 | cursor-agent | a4a1005 | EMA低通滤波替代离散信心下降检查：confidence_ema平滑信心得分，保留30s采样响应能力的同时数学过滤高频白噪声 | boundary-error |
| FIX-20260516-003 | 2026-05-16 | cursor-agent | — | Exit Effectiveness Data section added: SL:TP=4.6:1, per-strategy PnL breakdown, ML frozen confidence diagnostic. SL triggers 4.6x more than TP despite adequate per-trade R:R. | contract-violation |
| FIX-20260516-005 | 2026-05-16 | cursor-agent | — | check_feature_freshness() rejected negative age (future timestamps). Was: `age <= max_age` always True for future dates. Now: explicit `age < 0 → fresh:False, reason:future_timestamp` guard. | contract-violation |
| FIX-20260517-002 | 2026-05-17 | cursor-agent | — | Route C+ Protocol 2+3: Platt scaling calibration (smooth sigmoid, coef=2.44/intercept=-0.84) + conformal prediction thresholding (80th pctile of 500-pred window, 0.50 floor). MetaSignalFilter extended with calibrator_path/conformal_mode/window/percentile/min_threshold. Fixed P(class=1) extraction bug. | missing-feature |
| FIX-20260517-004 | 2026-05-17 | cursor-agent | — | MetaSignalFilter DevOps hardening: state persistence (save_state/load_state JSON), time-decayed conformal (14d max_age_days), Platt safety clamp (eps 1e-4 + max/min output clamp). Integrated into live_intent_loop. | state-leak, boundary-error |
| FIX-20260517-010 | 2026-05-17 | cursor-agent | — | Fixed inverse-volatility SL/TP formula: `sl_mult = base_sl_mult / vol_ratio` mathematically cancelled to fixed distance regardless of ATR, causing SL to shrink to 1.25 ATR in high vol (noise-triggered). Changed to direct multiplication: `sl_mult = base_sl_mult`, `sl_distance = sl_mult * current_atr` — SL always spans exactly base_sl_mult ATRs. Updated ref_atr from 5.0 to 7.0 (current XAUUSD M5). | RC-05 |
| FIX-20260518-030 | 2026-05-18 | cursor-agent | — | MetaSignalFilter feature_names fallback: when .meta.json is missing (e.g. meta_stage2_lgb_pit_v3.meta.json), _feature_names stayed empty [] causing 0-length feature vector → LightGBM fatal. Now falls back to booster.feature_name() after model load — reads 59 feature names directly from the trained model file. | missing-file |
| FIX-20260518-032 | 2026-05-18 | cursor-agent | — | Tier 2 Kelly/Edge sizing: `compute_kelly_mult(p_win, rr_ratio)` computes fractional Kelly multiplier. When kf≤0, hard EV veto (`fractional_mult=0.0` → `should_trade=False`). `resolve_p_win_from_brains()` uses rolling 100-trade win rate from BrainPnLStore with cold-start guard (empty→0.5) and min 10-sample threshold. | missing-feature |
| FIX-20260518-033 | 2026-05-18 | cursor-agent | — | Tier 3 √N correlation discount: `apply_sqrt_n_discount()` groups decisions by direction, applies 1/√n decay to each cluster with lot_step rounding. Strategies below min_lot after discounting are dropped and removed from execution queue + current_positions snapshot. Drops are logged via `sqrt_n_discount` event for audit trail. | missing-feature |
| FIX-20260518-034 | 2026-05-18 | cursor-agent | — | Kelly discretization fix: moved `kelly_mult` into `_compute_volume()` BEFORE `round(size, 2)` — previously applied to already-rounded value, destroying Kelly effect through premature discretization. Added `kelly_diag` (MetaFilter p_win capture) + `kelly_sizing` (three-way volume: base/raw_target/final_stepped) JSON events. `multi_strategy_eval` now includes `p_win`/`kelly_mult` per strategy. | boundary-error |
| FIX-20260518-035 | 2026-05-18 | cursor-agent | — | NET_OUT config wiring: `portfolio_netting_mode` added to `LiveCycleConfig` (default `"net_out"`) and passed to `PortfolioRiskController`. Previously `netting_mode` defaulted to `"allow_coexist"` — the entire netting path was dead code. Also fixed ExecutionQueue ACK polling to extract `new_ticket` from partial close receipt and reassign `known_open_tickets` to prevent orphan positions without trailing stop. | config-drift |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `compute_position_size(account_equity, risk_per_trade, atr, sl_distance)` → `float` | strategy_line | Stable |
| `compute_kelly_mult(p_win, rr_ratio, fractional_k=0.5, floor=0.5, cap=1.5)` → `KellyResult` | strategy_line | Stable |
| `resolve_p_win_from_brains(brains, pnl_store, direction)` → `float` | strategy_line | Stable |
| `apply_sqrt_n_discount(decisions, lot_step, min_lot)` → `(decisions, [ClusterResult])` | live_cycle | Stable |
| `detect_session(timestamp)` → `str` (asian/london/ny) | live_cycle | Stable |
| `StrategyBudget.check(strategy_id, sl_hit)` → `bool` | live_cycle | Stable |

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

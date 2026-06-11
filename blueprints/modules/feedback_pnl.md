# Feedback / PnL

## Purpose
Per-brain counterfactual P&L tracking: signal settlement against close prices, rolling Sharpe/WR/PF/PnL/drawdown metrics, and long/short breakdown statistics.

## Key Files
| File | Role |
|------|------|
| `core/feedback/brain_pnl_ledger.py` | `BrainPnLStore` — per-brain P&L ledger; `BrainPnLMetrics` — rolling metrics |
| `core/constants.py` | `PERFORMANCE_WINDOW` (100 trades) |

## Data Flow
```
cycle N:     record_signal(expected_horizon=H, ttl=H)  → pending queue
cycle N+1..N+H-1:  update_pending(mid_price)  → decrement TTL, track MFE/MAE
cycle N+H:   settle_all(mid_price)  → only ttl=0 settled at horizon bar
                                                                             ↓
                                                                   BrainPnLMetrics (Sharpe, WR, PF, PnL, DD, MFE/MAE)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/brain_registry | BrainRegistry | training_horizon per brain |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/dynamic_brain_weighter | BrainPnLStore, BrainPnLMetrics | P&L-based vote weighting |
| brains/services/brain_leaderboard | BrainPnLStore | Leaderboard scoring |
| brains/services/brain_attribution_service | BrainPnLStore | Layer-1 counterfactual report |
| execution/position_manager | BrainPnLStore | Exit decision context |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260611-022 | 2026-06-11 | cursor-agent | b106eb2 | Consumer migration: shadow_pnl_loop startup now tries load_from_stream() first, falls back to old JSON. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | 49610cd | Activate dual-write: BrainPnLStore.load() + constructor accept event_writer parameter for EventWriter injection. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | 520b371 | Event Sourcing Foundation: Optional EventWriter hook in BrainPnLStore (dual-write to ledger_events.jsonl). Zero-risk transition — hook is None by default. | contract-violation |
| FIX-20260604-077 | 2026-06-04 | cursor-agent | — | **PnL ledger every-cycle save**: moved `pnl_ledger.save()` outside 60-cycle block. Recent trades (and their p_win impact) no longer lost on crash/restart. | RC-03 |
| FIX-20260530-081 | 2026-05-30 | cursor-agent | — | PnL ledger retention: added `retention_prune(retention_days=90)` to `BrainPnLStore`. Called nightly from `daily_ops.py` after SSOT reconcile. Removes entries older than 90 days to prevent hot ledger unbounded growth. Returns per-brain prune counts for audit log. | RC-08 (incomplete-cleanup) |
| FIX-20260529-041 | 2026-05-29 | cursor-agent | — | Phase B: O(1) event-driven accumulators in BrainPnLStore | RC-12 |
| FIX-20260529-035 | 2026-05-29 | cursor-agent | — | P0+P1 Visibility Fix: BrainPnLMetrics extended with `recent_win_rate` + `consecutive_losses` fields, computed in `get_metrics_calibrated()`. `compute_performance_from_ledger()` deprecated in favor of `BrainPnLStore.get_all_metrics()` as SSOT. Fixes dual-pipeline metric divergence (Gap B). | RC-06, RC-09 |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M2: Eliminated ~50-line duplicate metrics computation — get_metrics() now delegates to get_metrics_calibrated(). M3: Deprecated _assess_health() call replaced with assess_health_calibrated() for unified health tiering. | RC-06 |
| FIX-20260524-033 | 2026-05-24 | cursor-agent | — | Batch mypy type safety: shadow_pnl_loop.py (1→0 — remove nonexistent rolling_norm.update() call, normalize() already does EWMA update internally). | api-confusion |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260519-010 | 2026-05-19 | cursor-agent | — | Track 1+2: Horizon-matched counterfactual PnL + MFE/MAE profiling. record_signal() accepts expected_horizon→TTL, update_pending() tracks MFE/MAE per cycle, settle_all() only settles ttl=0. _settle() computes MFE/MAE R-multiples from tracked prices. | RC-06 |
| FIX-20260517-013 | 2026-05-17 | cursor-agent | — | shadow_pnl_loop.py: added slippage=0.10 to settle_all() and record_signal() calls. Previously slippage defaulted to 0.0, undercounting shadow PnL friction by 0.10 USD/side. | contract-violation |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainPnLStore.record_signal(brain_id, direction, entry, expected_horizon=H)` → `str` | live_cycle | Stable |
| `BrainPnLStore.update_pending(mid_price)` → `int` | live_cycle | Stable |
| `BrainPnLStore.settle_all(mid_price, force_all=False)` → `dict` | live_cycle | Stable |
| `BrainPnLStore.get_metrics(brain_id)` → `BrainPnLMetrics` | DynamicBrainWeighter | Stable |

## Verification
```bash
python -m pytest tests/ -k "pnl or ledger" -q
```

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

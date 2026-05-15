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
Trade journal → BrainPnLStore.settle(brain_id, direction, entry, exit) → BrainPnLMetrics
                                                                             ↓
                                                                   Sharpe, WR, PF, PnL, DD
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/dynamic_brain_weighter | BrainPnLStore, BrainPnLMetrics | P&L-based vote weighting |
| brains/services/brain_leaderboard | BrainPnLStore | Leaderboard scoring |
| execution/position_manager | BrainPnLStore | Exit decision context |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainPnLStore.settle(brain_id, direction, entry, exit)` → `None` | online_feedback_hook | Stable |
| `BrainPnLStore.get_metrics(brain_id)` → `BrainPnLMetrics` | DynamicBrainWeighter | Stable |

## Verification
```bash
python -m pytest tests/ -k "pnl or ledger" -q
```

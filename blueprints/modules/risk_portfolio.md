# Risk / Portfolio

## Purpose
Cross-strategy portfolio-level risk control: VaR/CVaR computation, correlation penalty, net-out detection, and capital allocation optimization across concurrent positions.

## Key Files
| File | Role |
|------|------|
| `core/execution/portfolio_risk.py` | `PortfolioRiskController` — cross-strategy limits, VaR, correlation |
| `core/execution/capital_allocator.py` | `AllocationDecision`, `compute_optimal_group_weights()` |
| `core/metrics/portfolio_optimizer.py` | Portfolio optimization math |

## Data Flow
```
Active positions + pending intents → PortfolioRiskController.evaluate()
                                          ↓
                              RiskVerdict (APPROVED/REJECTED/REDUCED/NET_OUT)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/enums | RiskDecisionStatus | Verdict status |
| metrics/portfolio_optimizer | Optimization functions | Capital allocation |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | PortfolioRiskController | Cross-strategy risk gate |
| execution/strategy_line | RiskVerdict | Strategy-level gating |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260517-007 | 2026-05-17 | cursor-agent | — | CapitalAllocator: capacity-aware position sizing with two defense lines — max_concentration (50% default, prevents single-brain hot-streak dominance) + min_lot_size gating (prevents sub-minimum-lot micro-orders). Proportional allocation based on DynamicBrainWeighter brain weights. | missing-feature |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `PortfolioRiskController.evaluate(intents, positions)` → `RiskResult` | live_cycle | Stable |
| `compute_optimal_group_weights(groups, constraints)` → `dict[group, weight]` | capital_allocator | Evolving |

## Verification
```bash
python -m pytest tests/ -k "portfolio or risk" -q
```

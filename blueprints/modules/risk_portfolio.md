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
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260519-008 | 2026-05-19 | cursor-agent | — | Global Directional Cooldown: 新增net_out_cooldown_seconds(默认600s)+last_net_out_timestamp/last_net_out_direction追踪。net_out强制平仓后记录被平仓方向,cooldown期间拦截该方向所有新开单(任意策略),阻断net_out→新开仓→反向net_out的死亡连锁。Cooldown检查在策略重复检查之后、总敞口检查之前执行。 | RC-12 |
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

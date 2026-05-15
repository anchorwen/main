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

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `compute_position_size(account_equity, risk_per_trade, atr, sl_distance)` → `float` | strategy_line | Stable |
| `detect_session(timestamp)` → `str` (asian/london/ny) | live_cycle | Stable |
| `StrategyBudget.check(strategy_id, sl_hit)` → `bool` | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "guard" -q
```

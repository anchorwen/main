# Risk / Policies

## Purpose
Concrete risk policy checks: position limits, drawdown limits, exposure limits, and concentration limits. Each policy evaluates a `DecisionIntent` against configurable thresholds and returns a `RiskVerdict`.

## Key Files
| File | Role |
|------|------|
| `core/risk/risk_policies.py` | `PositionLimitPolicy`, `DrawdownPolicy`, `ExposurePolicy`, `ConcentrationPolicy` |
| `core/risk/risk_evaluation_service.py` | `RiskEvaluationService` — chains policies, produces final `RiskVerdict` |
| `core/risk/schema_versions.py` | `SCHEMA_RISK_VERDICT` version constant |
| `core/constants.py` | `MAX_DRAWDOWN_PCT`, `INTRADAY_DD_KILL_PCT` |

## Data Flow
```
DecisionIntent → RiskEvaluationService.evaluate()
                      ↓
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
  PositionLimit  DrawdownPolicy  ExposurePolicy  ConcentrationPolicy
        ↓             ↓             ↓             ↓
        └─────────────┼─────────────┘
                      ↓
                 RiskVerdict
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | RiskVerdict | Output type |
| contracts/enums | RiskDecisionStatus | Verdict status enum |
| contracts/ids | new_verdict_id | Verdict ID generation |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/order_dispatch | RiskEvaluationService | Pre-dispatch risk check |
| protocol/services | RiskVerdict | Communication pipeline |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260524-042 | 2026-05-24 | cursor-agent | — | T2-H5: ExposurePolicy now checks current + proposed exposure (was only current — 999,999 current could pass any new trade past 1,000,000 limit) | RC-06 |
| FIX-20260524-043 | 2026-05-24 | cursor-agent | — | T2-C1: RiskEvaluationService fail-closed hard assertion (len(_policies)==0 → DENY) + default policy set (ModePolicy + PositionLimitPolicy). T2-C2: execution_queue price guard exception now rejects instead of silently passing. T2-H1: VaR/CVaR exception logged + equity fallback 10k→100k. T2-H2: correlation exception returns 1.0 (fully correlated) instead of 0.0 (no penalty). T2-H3: OU/Meta gate exceptions now block trades instead of non-blocking pass. T2-H4: portfolio stop-loss method added. T2-H6: exposure check skipped when price unavailable (was dimensional error comparing lots vs %). T2-H7: compute_position_size returns 0.0 for invalid ATR (was min_lot). T2-H8: removed hardcoded skip_price_guard=True from dispatch path. T2-H9: VaR data insufficiency now warns conservatively instead of returning 0.0. | RC-06, RC-07, RC-05 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RiskPolicy.evaluate(intent, context)` → `RiskVerdict` | RiskEvaluationService | Stable |
| `RiskEvaluationService.evaluate(intent)` → `RiskVerdict` | order_dispatch | Stable |

## Verification
```bash
python -m pytest tests/ -k "risk" -q
```

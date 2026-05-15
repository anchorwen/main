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

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RiskPolicy.evaluate(intent, context)` → `RiskVerdict` | RiskEvaluationService | Stable |
| `RiskEvaluationService.evaluate(intent)` → `RiskVerdict` | order_dispatch | Stable |

## Verification
```bash
python -m pytest tests/ -k "risk" -q
```

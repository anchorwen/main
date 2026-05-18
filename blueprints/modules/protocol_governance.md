# Protocol / Governance

## Purpose
Brain lifecycle governance: state machine (candidate → live → probation → frozen → retired), rule engine for automated decisions, and persistence of governance state.

## Key Files
| File | Role |
|------|------|
| `core/governance/governance_service.py` | `GovernanceService` — lifecycle management, persistence, audit log |
| `core/governance/governance_rule_engine.py` | `GovernanceRuleEngine` — rule-based decision engine (5 rules) |

## Data Flow
```
BrainQualityVerdict → GovernanceRuleEngine.evaluate() → lifecycle_action
                              ↓
                    GovernanceService.apply(action) → governance_state.json
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/exceptions | BrainNotFoundError, InvalidTransitionError | Error handling |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_promotion | apply_promotion_decisions() | Writes governance state |
| deployment/lifecycle | GovernanceService | Service wiring |
| apps/monitor/ | governance_service | Dashboard status |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260517-015 | 2026-05-17 | cursor-agent | — | health_signal unblock: ShadowTracker.build_shadow_summary() health_signal changed from hardcoded "unknown" to "healthy". The old value blocked GovernanceRuleEngine auto-promotion rules (auto_promote_healthy requires health_signal=="healthy"), preventing candidate→probation transitions. | missing-feature |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor/Executor separation: GovernanceRuleEngine.execute_transitions(report) added as single Executor. BrainPromotionEvaluator reduced to pure Auditor (evaluate_all returns report, no state writes). scheduler_service wired as: evaluator.evaluate_all() → engine.execute_transitions(). | contract-violation |
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260515-009 | 2026-05-15 | cursor-agent | — | Auto-shadow mechanism: ShadowTracker (counts candidate signals), auto_promote_shadow_to_probation rule (50+ signals→probation), auto_promote_probation_to_live rule (100+ signals+quality→live). New file core/governance/shadow_tracker.py. | missing-feature |
| FIX-20260514-015 | 2026-05-14 | cursor-agent | a4a1005 | 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live | contract-violation |
| FIX-20260514-006 | 2026-05-14 | cursor-agent | a4a1005 | Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging | missing-validation |
| FIX-20260514-005 | 2026-05-14 | cursor-agent | a4a1005 | Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100) | contract-violation |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: governance_rule_engine.py (execute_transitions) + shadow_tracker.py (health_signal). Previously registered as FIX-20260517-017, FIX-20260517-015. | process-violation |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `GovernanceService.transition(brain_id, new_status)` → `bool` | BrainPromotionEvaluator | Stable |
| `GovernanceRuleEngine.evaluate(brain_id, metrics)` → `list[GovernanceAction]` | GovernanceService | Stable |
| Brain lifecycle states: candidate → probation → live → frozen → retired | All consumers | Stable |

## Verification
```bash
python -m pytest tests/ -k "governance" -q
```

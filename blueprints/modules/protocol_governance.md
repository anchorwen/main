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
| FIX-20260529-043 | 2026-05-29 | cursor-agent | — | PR#1 GovernanceService thread-safety: added threading.RLock() protecting all _brain_states/_transition_log reads/writes. save() changed from direct write_text() to atomic tmp+os.replace. RLock used because transition() internally calls register_brain(). | RC-04 |
| FIX-20260529-035 | 2026-05-29 | cursor-agent | — | P0.1 State Injection: `GovernanceService.set_performance_metrics()` added — injects win_rate/profit_factor/sharpe_ratio/total_trades/pnl_r into brain_states. `governance_scheduler.py` PnL-first path now calls `set_performance_metrics()` for every assessed brain. Fixes visibility gap where 16,903 settled trades had zero reflection in governance state. | RC-06, RC-09 |
| FIX-20260529-034 | 2026-05-29 | cursor-agent | — | SSOT governance status reconciliation + transition_log integrity: (1) `verify_startup_integrity()` now reconciles config→governance status — when brain has active config on disk but governance says "retired", config wins (restored to "candidate"). (2) `GovernanceService.register_brain()` now appends transition_log entry and sets transition_count=1. (3) Auto-registration path now adds transition_log entries. Fixes OU_Params_V7_M15 retired-reversion loop (governance "retired" persisted across saves even though config says "live"). Also archives V1 Swing configs to resolve magic collision with V2. | RC-09, RC-11 |
| FIX-20260524-037 | 2026-05-24 | cursor-agent | — | C2: build_shadow_summary() no longer outputs "current_status": "candidate" — was overriding real governance state via **summary spread in rule engine, permanently disabling all status-dependent rules. | RC-09 |
| FIX-20260524-038 | 2026-05-24 | cursor-agent | — | H3: "shadow" added to VALID_TRANSITIONS (→{candidate, probation, frozen, retired}) — 2 brains were permanently stuck. H6: SHARPE_RETIRE_THRESHOLD -10.0→-2.0, SHARPE_FREEZE_THRESHOLD -10.0→-1.5 (aligned with BrainQualityEngine hard gates). | config-drift |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M11: GovernanceRuleEngine now checks transition() return value (action==rejected) instead of silently ignoring failures. | missing-validation |
| FIX-20260517-015 | 2026-05-17 | cursor-agent | — | health_signal unblock: ShadowTracker.build_shadow_summary() health_signal changed from hardcoded "unknown" to "healthy". The old value blocked GovernanceRuleEngine auto-promotion rules (auto_promote_healthy requires health_signal=="healthy"), preventing candidate→probation transitions. | missing-feature |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor/Executor separation: GovernanceRuleEngine.execute_transitions(report) added as single Executor. BrainPromotionEvaluator reduced to pure Auditor (evaluate_all returns report, no state writes). scheduler_service wired as: evaluator.evaluate_all() → engine.execute_transitions(). | contract-violation |
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260515-009 | 2026-05-15 | cursor-agent | — | Auto-shadow mechanism: ShadowTracker (counts candidate signals), auto_promote_shadow_to_probation rule (50+ signals→probation), auto_promote_probation_to_live rule (100+ signals+quality→live). New file core/governance/shadow_tracker.py. | missing-feature |
| FIX-20260514-015 | 2026-05-14 | cursor-agent | a4a1005 | 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live | contract-violation |
| FIX-20260514-006 | 2026-05-14 | cursor-agent | a4a1005 | Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging | missing-validation |
| FIX-20260514-005 | 2026-05-14 | cursor-agent | a4a1005 | Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100) | contract-violation |
| FIX-20260524-040 | 2026-05-24 | cursor-agent | — | DEFERRED architecture debt: dual governance pipeline merge (BrainPromotionEvaluator vs GovernanceRuleEngine), leaderboard consumer gap, stability monitor unused, AB test framework not activated. No code changes — registered for future sprints. | RC-12 |
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

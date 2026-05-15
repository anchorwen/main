# Execution / Reentry

## Purpose
Re-entry quality gates that prevent overtrading after stop-loss hits. Implements cooldown periods, confidence improvement requirements, and exit reason classification.

## Key Files
| File | Role |
|------|------|
| `core/execution/reentry_guard.py` | `_classify_exit_reason()`, `check_reentry_quality()` |
| `core/constants.py` | `SL_REENTRY_COOLDOWN_SECONDS`, `SL_REENTRY_CONFIDENCE_IMPROVEMENT` |

## Data Flow
```
Trade close event → _classify_exit_reason() → check_reentry_quality()
                                                   ↓
                                          approved / cooldown / confidence_blocked
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | ReentryGuard checks | Pre-trade gate sequence |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `check_reentry_quality(brain_id, exit_reason, current_confidence, last_confidence)` → `(bool, str)` | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "reentry" -q
```

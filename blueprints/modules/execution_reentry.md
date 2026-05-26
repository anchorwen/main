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
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 2: Fixed 3 missing exit classifications (hesitation_*, bleed_stop_*, ev_trajectory→time_expired). Wave 2: Tightened time_expired re-entry from unconditional to gated (60s cooldown + confidence may not decay >0.05). Wave 2: Added hesitation + bleed_stop quality gate handlers (180s cooldown, confidence improvement, price confirmation). Wave 3: Micro-lot volume decay defense — hard block when stepped_vol >= original_volume after min_lot discretization. | RC-05 |
| FIX-20260525-045 | 2026-05-25 | cursor-agent | — | T1-L4: Replaced last_direction="" sentinel with None for clarity | — |
| FIX-20260525-024 | 2026-05-25 | cursor-agent | — | MIA/manual close exits permanently blocked same-direction reentry. `_classify_exit_reason()` had no pattern for "mia_close"/"unknown_close"/"manual_close" — all fell into "unknown" category which had NO timeout (permanent block). Fix: (a) added "unknown_close" category matching mia/manual/unknown_close patterns, (b) added 900s timeout for unknown_close category, (c) converted catch-all "unknown" from permanent block to 900s timeout with confidence check. | RC-05 (missing-classification), RC-07 (no-timeout) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `check_reentry_quality(brain_id, exit_reason, current_confidence, last_confidence)` → `(bool, str)` | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "reentry" -q
```

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
| FIX-20260605-120 | 2026-06-05 | cursor-agent | — | **Per-asset reentry parameterization**: `check_reentry_quality` and `check_and_record_entry` now accept `sl_cooldown_override`, `sl_penalty_override`, `bleed_cooldown_override`, `bleed_penalty_override` from config. Hardcoded BTC values replaced with XAU defaults (180s/0.10) as fallback. BTC gets 300s/0.15 from live_btc.yaml. | RC-09 |
| FIX-20260605-117 | 2026-06-05 | cursor-agent | — | **Reentry absolute ceiling**: All positive-margin reentry thresholds capped at `_MAX_THRESHOLD = 0.82`. Prevents permanent deadlock when exit confidence is extreme (e.g. exit=0.91, brain_flip +0.10 → 1.01 mathematically unreachable). Applied to brain_flip, sl_hit, ou_revert, unknown_close. XAU h1_swing threshold: 0.921 → 0.820. | RC-05 |
| FIX-20260605-116 | 2026-06-05 | cursor-agent | — | **Momentum pause reentry channel**: Split `confidence_decay`/`confidence_drop` from `brain_flip` (which required +0.10 confidence improvement) into new `momentum_pause` category (−0.05 tolerance, 60s cooldown). Fixes "semantic conflation" where same-direction conviction dips were punished as direction reversals. Also: `_derive_label` in bridge now uses PnL for close label instead of comment text. Bootstrap comment-borrowing (Phase 1: filtered entries, Phase 2: raw journal) restores SW-side exit reasons across restarts. BTC reentry unblocked. | RC-06 |
| FIX-20260528-011 | 2026-05-28 | cursor-agent | — | Reentry guard TTL hard unlock for `sl_hit` category: previously `sl_recovery_price_not_confirming` had NO maximum lock duration — a single SL hit could permanently block same-direction reentry (statarb_dynamic SHORT locked for >4 hours = 45+ blocked signals). Fix: `check_reentry_quality()` now accepts `entry_half_life` + `timeframe_minutes`, computes TTL = half_life × timeframe × 2.5 × 60s before existing price/confidence checks — when elapsed > TTL, force unlock with reason `sl_ttl_expired`. For OU_Params_V6_Sniper (half_life=58, M5): TTL = 58 × 5 × 2.5 × 60 = 43,500s ≈ 12.1h. Architect directive: if 2.5 half-lives pass without price recovery, the mean has shifted to a new regime — stale lock blocks new-regime trading opportunities. Also added `entry_half_life` + `ttl_seconds` to reentry_check diagnostic log. | RC-06 |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 2: Fixed 3 missing exit classifications (hesitation_*, bleed_stop_*, ev_trajectory→time_expired). Wave 2: Tightened time_expired re-entry from unconditional to gated (60s cooldown + confidence may not decay >0.05). Wave 2: Added hesitation + bleed_stop quality gate handlers (180s cooldown, confidence improvement, price confirmation). Wave 3: Micro-lot volume decay defense — hard block when stepped_vol >= original_volume after min_lot discretization. | RC-05 |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 2: Fixed 3 missing exit classifications (hesitation_*, bleed_stop_*, ev_trajectory→time_expired). Wave 2: Tightened time_expired re-entry from unconditional to gated (60s cooldown + confidence may not decay >0.05). Wave 2: Added hesitation + bleed_stop quality gate handlers (180s cooldown, confidence improvement, price confirmation). Wave 3: Micro-lot volume decay defense — hard block when stepped_vol >= original_volume after min_lot discretization. | RC-05 |
| FIX-20260525-045 | 2026-05-25 | cursor-agent | — | T1-L4: Replaced last_direction="" sentinel with None for clarity | — |
| FIX-20260525-024 | 2026-05-25 | cursor-agent | — | MIA/manual close exits permanently blocked same-direction reentry. `_classify_exit_reason()` had no pattern for "mia_close"/"unknown_close"/"manual_close" — all fell into "unknown" category which had NO timeout (permanent block). Fix: (a) added "unknown_close" category matching mia/manual/unknown_close patterns, (b) added 900s timeout for unknown_close category, (c) converted catch-all "unknown" from permanent block to 900s timeout with confidence check. | RC-05 (missing-classification), RC-07 (no-timeout) |
| FIX-20260529-039 | 2026-05-29 | cursor-agent | — | Stale exit deadlock + bootstrap counter accumulation. (a) `check_reentry_quality()`: exits older than 24h (86400s) auto-allowed — e.g. unknown_close from 9.5 days ago no longer permanently blocks same-direction reentry. (b) `check_and_record_entry()`: when stale_exit_allowed reason detected, reset `consecutive_same_direction` to 0 — bootstrap replay of 20+ historical exits inflated counter causing `volume_decay_blocked_consecutive_20+` on first live trade. | RC-05 (stale-state), RC-07 (no-timeout) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `check_reentry_quality(brain_id, exit_reason, current_confidence, last_confidence)` → `(bool, str)` | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "reentry" -q
```

# Execution / Reentry

## Purpose
Re-entry quality gates that prevent overtrading after stop-loss hits. Implements cooldown periods, confidence improvement requirements, and exit reason classification.

## Key Files
| File | Role |
|------|------|
| `core/execution/reentry_guard.py` | `_classify_exit_reason()`, `check_reentry_quality()` |
| `core/execution/exit_reason.py` | `ExitReason` enum, `classify()` — canonical SSOT (FIX-20260613-039) |
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
| FIX-20260613-039 | 2026-06-13 | cursor-agent | — | **ExitReason enum — canonical SSOT**: Extracted 15-category taxonomy from `_classify_exit_reason()` substring matcher into `ExitReason` enum (str subclass) in `core/execution/exit_reason.py`. Each member carries cooldown_tier + is_model_driven/is_risk_driven/is_structural metadata. `classify()` replaces post-hoc string matching. `reentry_guard.py` migrated to use enum. Backward-compatible `_classify_exit_reason` shim preserved. | RC-06, RC-12 |
| FIX-20260610-008 | 2026-06-10 | cursor-agent | — | **Exit reason classification hardening**: `_classify_exit_reason()`补全7种模式→3个新规范类别(kalman_flip/net_out/watchdog/emergency_close). meta_exit子类型6种(pnl_urgency/time_decay/regime_misalignment/consensus_drift/vol_expansion/ml_p_win)全部归入meta_exit. partial_tp→tp_hit. 31/31 pattern tests pass. DQAF-20260610-002. | RC-07 |
| FIX-20260609-010 | 2026-06-09 | cursor-agent | — | **Hesitation threshold BTC calibration**: FIX-001 added `_MAX_THRESHOLD=0.82` + 2h TTL but +0.15 margin with floor 0.70 still produced unreachable thresholds for BTC tree models (P99≈0.685). 150 consecutive cycles blocked 2026-06-08/09. Fix: margin +0.15→+0.08, floor 0.70→0.65. Ordering: brain_flip+0.05 < hesitation+0.08 < sl_hit+0.10. DQAF-20260609-001. | RC-05 |
| FIX-20260609-001 | 2026-06-09 | cursor-agent | — | **Hesitation permanent deadlock: TTL + _MAX_THRESHOLD ceiling**: `hesitation` was the ONLY exit category lacking both a TTL hard unlock and the `_MAX_THRESHOLD` ceiling (FIX-117 added it to brain_flip/sl_hit/ou_revert/unknown_close but MISSED hesitation). When exit_confidence was high (BTC 0.7668), threshold `max(0.7668+0.15, 0.70)=0.9168` exceeded BTC model P99 (~0.685), creating a MATHEMATICAL DEADLOCK with no time-based escape — BTC blocked 23h/148 cycles. Fix: (a) add `_MAX_THRESHOLD=0.82` ceiling → `min(max(exit_confidence+0.15, 0.70), _MAX_THRESHOLD)`, (b) add TTL hard unlock: after `max(2h, hl×tf×2.0×60)`, only confidence>0.50 required. Same proven pattern as brain_flip (FIX-127/130), sl_hit (FIX-011), meta_exit (FIX-127). ReB Pattern: ReB-20260609-001. | RC-05 |
| FIX-20260606-130 | 2026-06-06 | cursor-agent | — | **brain_flip TTL recalibration**: TTL 4h→2h, addition +0.10→+0.05, floor 0.70→0.65. Cross-validated with BTC 100-trade confidence distribution: model P99≈0.685, old floor 0.70 guaranteed 4h deadlock after every brain_flip (13.6% of BTC closes). New floor 0.65 reachable at model P99. Worst case (exit > 0.65): TTL=2h vs old 4h. | RC-05 |
| FIX-20260606-127 | 2026-06-06 | cursor-agent | — | **Reentry TTL hard unlock for brain_flip + meta_exit**: Linear margin (+0.10/+0.05) is mathematically unreachable when exit_confidence is near tree-model output ceiling. After TTL expires (brain_flip: max(4h, hl×tf×2.5×60), meta_exit: max(2h, hl×tf×2.0×60)), only confidence > 0.50 required. Proven pattern from sl_hit TTL (FIX-20260528-011). Unblocks BTC (exit=0.6875, need 0.787→TTL expired→need 0.50, current=0.60 passes) and XAU h1_swing (meta_exit). | RC-05 |
| FIX-20260605-123 | 2026-06-05 | cursor-agent | 6110bc6 | **Core test长城**: 29 precision-strike tests added. `test_trail_stop_engine.py` (16 tests) and `test_execution_state.py` (13 tests) harden the two highest-bug-density subsystems (restart amnesia + trail stop). | RC-12 |
| FIX-20260605-121 | 2026-06-05 | cursor-agent | 5892b3f | **Restart verification restored**: 5 reentry guard tests updated for FIX-116 momentum_pause behavior (confidence_drop→momentum_pause, 60s cooldown, -0.05 tolerance). `verify.py --full` returned to 2767 passed. | RC-06 |
| FIX-20260605-120 | 2026-06-05 | cursor-agent | — | **Per-asset reentry parameterization**: `check_reentry_quality` and `check_and_record_entry` now accept `sl_cooldown_override`, `sl_penalty_override`, `bleed_cooldown_override`, `bleed_penalty_override` from config. Hardcoded BTC values replaced with XAU defaults (180s/0.10) as fallback. BTC gets 300s/0.15 from live_btc.yaml. | RC-09 |
| FIX-20260605-117 | 2026-06-05 | cursor-agent | — | **Reentry absolute ceiling**: All positive-margin reentry thresholds capped at `_MAX_THRESHOLD = 0.82`. Prevents permanent deadlock when exit confidence is extreme (e.g. exit=0.91, brain_flip +0.10 → 1.01 mathematically unreachable). Applied to brain_flip, sl_hit, ou_revert, unknown_close. XAU h1_swing threshold: 0.921 → 0.820. | RC-05 |
| FIX-20260605-116 | 2026-06-05 | cursor-agent | — | **Momentum pause reentry channel**: Split `confidence_decay`/`confidence_drop` from `brain_flip` (which required +0.10 confidence improvement) into new `momentum_pause` category (−0.05 tolerance, 60s cooldown). Fixes "semantic conflation" where same-direction conviction dips were punished as direction reversals. Also: `_derive_label` in bridge now uses PnL for close label instead of comment text. Bootstrap comment-borrowing (Phase 1: filtered entries, Phase 2: raw journal) restores SW-side exit reasons across restarts. BTC reentry unblocked. | RC-06 |
| FIX-20260528-011 | 2026-05-28 | cursor-agent | — | Reentry guard TTL hard unlock for `sl_hit` category: previously `sl_recovery_price_not_confirming` had NO maximum lock duration — a single SL hit could permanently block same-direction reentry (statarb_dynamic SHORT locked for >4 hours = 45+ blocked signals). Fix: `check_reentry_quality()` now accepts `entry_half_life` + `timeframe_minutes`, computes TTL = half_life × timeframe × 2.5 × 60s before existing price/confidence checks — when elapsed > TTL, force unlock with reason `sl_ttl_expired`. For OU_Params_V6_Sniper (half_life=58, M5): TTL = 58 × 5 × 2.5 × 60 = 43,500s ≈ 12.1h. Architect directive: if 2.5 half-lives pass without price recovery, the mean has shifted to a new regime — stale lock blocks new-regime trading opportunities. Also added `entry_half_life` + `ttl_seconds` to reentry_check diagnostic log. | RC-06 |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 2: Fixed 3 missing exit classifications (hesitation_*, bleed_stop_*, ev_trajectory→time_expired). Wave 2: Tightened time_expired re-entry from unconditional to gated (60s cooldown + confidence may not decay >0.05). Wave 2: Added hesitation + bleed_stop quality gate handlers (180s cooldown, confidence improvement, price confirmation). Wave 3: Micro-lot volume decay defense — hard block when stepped_vol >= original_volume after min_lot discretization. | RC-05 |
| FIX-20260518-040 | 2026-05-18 | cursor-agent | — | Wave 2: Fixed 3 missing exit classifications (hesitation_*, bleed_stop_*, ev_trajectory→time_expired). Wave 2: Tightened time_expired re-entry from unconditional to gated (60s cooldown + confidence may not decay >0.05). Wave 2: Added hesitation + bleed_stop quality gate handlers (180s cooldown, confidence improvement, price confirmation). Wave 3: Micro-lot volume decay defense — hard block when stepped_vol >= original_volume after min_lot discretization. | RC-05 |
| FIX-20260618-003 | 2026-06-18 | cursor-agent | — | **P0: bleed_stop + unknown_close TTL hard unlock**: Added TTL (time-to-live) to the two remaining exit categories that lacked it. bleed_stop now auto-unlocks after max(7200s, half_life×tf×2.0×60) with conf>0.50; unknown_close same pattern. Previously only brain_flip/sl_hit/hesitation/meta_exit had TTL — bleed_stop and unknown_close were accidentally omitted, creating permanent deadlocks (observed: XAU m30_swing 8h drought, 0.77 < 0.81 with no expiry). Pattern-consistent with FIX-127/130/001/011. | RC-07 (missing TTL) |
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

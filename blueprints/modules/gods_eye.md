# Execution / GodsEye (God's Eye)

## Purpose
Cross-instrument, multi-timeframe regime consensus engine — sits ABOVE per-instrument RegimeGate. Answers "is the market structure healthy enough to trade?" and gates the entry path via a hard veto (`_GODS_EYE_HARD_BLOCK_HEALTH = 0.55`). Three pillars: multi-TF alignment, cross-instrument consistency, regime chop/anomaly detection. **Chop must measure the current rolling window — never session-cumulative uptime** (DQAF-20260822-001 / FIX-20260822-001).

## Key Files
| File | Role |
|------|------|
| `core/execution/gods_eye.py` | `GodsEye` engine + `GodsEyeVerdict` — health computation, verdict lifecycle, serialization (`to_dict`) |
| `core/runtime/gods_eye_bridge.py` | `feed_gods_eye()` — RegimeGate→GodsEye translation bridge (Strangler Fig, owned by `runtime_live`) |
| `core/runtime/strategy_evaluator.py` | Hard-veto read path — `if _ge_health < 0.55 or _ge_chop: should_trade=False, volume=0.0` (Cut 7) |
| `tests/test_gods_eye.py` | Unit + regression locks (incl. rolling-window chop recovery) |

## Data Flow
```
RegimeGate.classify() → gods_eye_bridge.extract_gods_eye_snapshot()
    │  (M5/H1/H4/D1 regime+direction+strength per instrument)
    ▼
GodsEye.update_instrument(symbol, snapshot)
    │  ├── _track_regime_history  (rolling deque maxlen=_chop_window — SOLE chop source)
    │  └── _track_combo           (anomaly combo frequencies)
    ▼
GodsEye.verdict()
    ├── _check_multi_tf_alignment   (dynamic ladder contraction, FIX-20260731-004)
    ├── _check_cross_instrument     (consensus 1.0 when single-instrument instance)
    ├── _check_chop                 (rolling-window switch count → chop_score, FIX-20260822-001)
    ├── _check_anomaly
    ├── _resolve_macro_bias
    ├── _compute_health             (product of 4 floored factors)
    └── _resolve_mode               (normal/cautious/defensive/shadow)
    ▼
gods_eye_cycle JSON event (intent log) + GodsEyeVerdict → strategy_evaluator Cut 7 hard veto
```

Health formula (`_compute_health`):
```
health = max(0.1, alignment) × max(0.1, consensus) × max(0.1, 1-chop_score) × max(0.1, 1-anomaly_score)
```
Each factor floored at 0.1 → a single saturated factor caps health at 0.1 × the rest (multiplicative penalty).

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| (stdlib) | collections.deque, dataclasses, typing | Rolling regime window + verdict dataclass |
| numpy | np.prod | Multiplicative health combination |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/gods_eye_bridge | GodsEye, update_instrument, verdict | Feed pipeline (lazy init in live_cycle) |
| runtime/strategy_evaluator | verdict.health_score, verdict.chop_detected | Cut 7 hard veto — `_ge_health < 0.55 or _ge_chop → BLOCKED_BY_GODSEYE` |

## Known Issues

### KI-001: Single-instrument instances report consensus=1.0 unconditionally
Both BTC and XAU pipelines ingest only their primary instrument (no partner correlations fed) → `_check_cross_instrument` `total_checks=0` → consensus=1.0 by design. This is NOT a defect — the cross-instrument pillar is inert until multi-instrument ingestion is wired. DQAF-20260822-001 audit confirmed consensus==1.0 in all 8145 BTC + 2599 XAU cycles.

### KI-002: Health proxies session length when chop is mis-measured (RESOLVED)
Pre-FIX-20260822-001, `_check_chop` read a session-cumulative counter → chop_score saturated to 1.0 ~2-4h into any session → health permanently locked at the 0.1 chop floor regardless of market truth. Audit: 0/866 monotonic drops, 77-86% of cycles floor-locked, Pearson(cycles/day, health) = −0.521 (BTC) / −0.369 (XAU). Fixed by rolling-window chop. Regression locked in `test_rolling_window_recovers_after_prolonged_stability`.

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260822-001 | 2026-08-22 | cursor-agent | (this change) | **DQAF-20260822-001 — Rolling Window Restoration: chop_score 剥离会话累积累加器**. `_check_chop` no longer reads `_regime_change_counter` (deleted) — `chop_score`/`chop_detected`/`switches_per_hour` now derive from the rolling `_regime_history` deque (adjacent-label switch count within the last `_chop_window` bars). `_track_regime_history` reduced to pure append; `to_dict` drops the counter key. Magic numbers untouched (`_chop_window=24`, `_chop_threshold=6`, 0.55 hard veto). Regression lock: `test_rolling_window_recovers_after_prolonged_stability` (chop → >24 stable bars → chop_score==0.0, not 1.0). | L2 — logic defect: cumulative counter used where rolling-window rate was intended (RC-06 contract-violation); score became a proxy for process uptime, not market chop |
| FIX-20260731-004 | 2026-07-31 | cursor-agent | — | Dynamic ladder contraction — missing TFs excluded from alignment (NaN-as-zero anti-pattern resolved). | L3 — design defect: hardcoded 6-TF ladder penalized absent TFs |
| FIX-20260625-124 | 2026-06-25 | cursor-agent | — | God's Eye Phase 2 — live pipeline integration (`core/runtime/gods_eye_bridge.py`, Strangler Fig). | RC-12 |
| FIX-20260625-090 | 2026-06-25 | cursor-agent | `1fa175a2` | God's Eye created — cross-instrument regime consensus engine (multi-TF alignment, consensus, chop, anomaly). | RC-12 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `GodsEye.verdict()` → `GodsEyeVerdict` (health_score, chop_detected, chop_score, multi_tf_alignment, consensus, anomaly, mode) | gods_eye_bridge.feed_gods_eye → strategy_evaluator Cut 7 | Stable |
| `GodsEye.update_instrument(symbol, regime_map)` → None | gods_eye_bridge (per-cycle) | Stable |
| `GodsEye.to_dict()` → `{schema_version:"gods_eye.v1", primary_instrument, instruments, regime_history, combo_counts, total_updates}` | checkpoint/restore (no `from_dict` producer yet) | Stable (v1 — `regime_change_counter` removed in FIX-20260822-001) |
| `_GODS_EYE_HARD_BLOCK_HEALTH = 0.55` | strategy_evaluator Cut 7 | Frozen red line — do NOT modify |

## Verification
```bash
python -m pytest tests/test_gods_eye.py -q
```
Covers: multi-TF alignment, cross-instrument consistency, chop (rolling-window + recovery regression lock), anomaly, macro bias, health/mode, serialization roundtrip, correlations.

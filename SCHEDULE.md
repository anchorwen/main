# Deferred Task Registry

> **Purpose**: Track deferred tasks, conditions, and time-based triggers so nothing falls through the cracks.
> **Format**: Each task has a unique ID, priority, trigger condition, deadline, and status.
> **Review cadence**: Every Monday UTC 00:00 — check all PENDING tasks for trigger conditions.
>
> **Last reviewed**: 2026-05-27

## Status Codes

| Status | Meaning |
|--------|---------|
| `PENDING` | Deferred, waiting for trigger condition |
| `READY` | Condition met, ready for execution |
| `IN_PROGRESS` | Currently being worked on |
| `DONE` | Completed (moved to Fix History) |
| `CANCELLED` | No longer relevant |

---

## Active Tasks

### TASK-20260527-001 — P1: barrier_12bar Full Pipeline Rebuild (SL=2.0/TP=2.0)

- **Priority**: P1 (Medium-term)
- **Status**: PENDING
- **Created**: 2026-05-27
- **Trigger**: Manual — schedule when 4-6 hours of engineering time available
- **Target deadline**: 2026-06-10 (before next major strategy activation)
- **Description**:
  barrier_12bar training contract uses SL=3.0/TP=1.5 (RR=0.5), requiring p_win > 66.7% for positive EV.
  MetaFilter outputs p_win ~55-58%, so Kelly is structurally negative regardless of model quality.
  Full Pipeline Rebuild needed: relabel → rebuild calibrated_dataset → rebuild meta_features →
  retrain Stage 1 LGB → retrain Stage 2 MetaFilter (48-dim LGB+MLP) → rebuild Platt → update configs.
  Target: SL=2.0/TP=2.0 (RR=1.0, breakeven 50%) or SL=2.0/TP=2.5 (RR=1.25, breakeven 44.4%).
- **Files affected**:
  - Training: `scripts/training/build_calibrated_dataset.py`, `build_meta_features.py`, `train.py`
  - Config: `meta_stage1_binary_cls_v1.json`, `meta_stage2_filter_v3.json`, `live.yaml`
  - Brains: `meta_stage1_metalabel_binary_v1.json` (if also redoing MetaLabel)
- **Dependencies**: None
- **Notes**: Architect directive — do NOT lower min_rr_ratio as a shortcut. Fix the physics, not the gate.

### TASK-20260527-002 — P2: statarb_m15 Reentry Guard TTL (half_life × 5)

- **Priority**: P2 (Short-term, after P0 deployed)
- **Status**: PENDING
- **Created**: 2026-05-27
- **Trigger**: P0 deployed + 24h of stable running
- **Target deadline**: 2026-05-30
- **Description**:
  `reentry_guard.py` `sl_hit` category has no maximum lock duration. statarb_m15 currently locked
  for 13.7 days by `sl_recovery_price_not_confirming_long`. Add TTL: `max_lock_duration = half_life * 5`
  (e.g. OU M15 max_half_life=58 → 290 M15 bars = 72.5h ≈ 3 days). After TTL expires, clear the
  recovery price lock and allow reentry based on fresh OU signal quality.
- **Files affected**: `core/execution/reentry_guard.py`, `core/execution/strategy_line.py` (pass `entry_half_life`)
- **Dependencies**: `entry_half_life` already tracked via FIX-20260525-021
- **Notes**: Simple change (~10 lines). P0's regime_map fix (statarb→false in trending) partially
  mitigates this already by preventing trading in bad regimes.

### TASK-20260527-003 — P3: MetaLabel_Binary_V1 Calibration Probe

- **Priority**: P3 (Data collection)
- **Status**: PENDING
- **Created**: 2026-05-27
- **Trigger**: collect ≥ 200 live raw_score samples from MetaLabel_Binary_V1
- **Target deadline**: 2026-06-10 (review collected data)
- **Description**:
  MetaLabel_Binary_V1 (Forward Sharpe 8.10 in training) outputs p_win=0.47-0.49 in live trading,
  consistently below the 0.50 threshold. Diagnose by adding a probe that records raw LightGBM
  scores (before any calibration) to compare against training distribution via KS test.
  Root cause candidates: feature distribution shift, insufficient training samples (445 signals
  for 43-dim model), or concept drift in OU signal trigger conditions.
- **Files affected**: New probe in `core/brains/adapters/` or `core/feedback/`, analytics script
- **Dependencies**: P0 deployment for stable running
- **Notes**: barrier_12bar_meta strategy does NOT use Platt calibration (MetaFilter path) — the
  p_win comes directly from LightGBM raw probability. If KS test confirms distribution shift,
  options are: (a) add Platt calibrator to MetaLabel path, (b) retrain with more/updated data,
  (c) adjust min_p_win threshold to match live distribution.

### TASK-20260527-004 — barrier_12bar Short-term Bridge (probation at zero volume)

- **Priority**: P2 (Immediate)
- **Status**: PENDING
- **Created**: 2026-05-27
- **Trigger**: P0 deployed + confirmed working
- **Description**:
  Per architect directive: keep barrier_12bar in probation at 0.00 volume (or shadow), collecting
  MetaFilter p_win data for analysis while preventing -EV trades. Current RR=0.5 requires
  p_win > 66.7% which MetaFilter cannot deliver. Do NOT relax min_rr_ratio — fix the physics
  via TASK-20260527-001 instead.
- **Files affected**: `configs/live.yaml` — barrier_12bar section
- **Dependencies**: None (config-only change)
- **Notes**: Verify that regime_map correctly sets barrier_12bar → "full" in trending after
  P0 deployment (was "reduced" due to global override).

---

## Completed Tasks

*(Moved to respective module Fix History upon completion)*

---

## Condition Monitors

| Monitor | What to check | Current state |
|---------|--------------|---------------|
| P0 deploy health | 24h of stable running with no regime-related dispatch errors | DEPLOYED 2026-05-27 11:50 UTC — monitoring |
| MetaLabel raw_score count | `count(live meta_label raw_score samples) >= 200` | 0 |
| OU COLD phase exit | `ConformalCalibrator.sample_count >= 50` (exit COLD, enter WARM) | 0 |
| statarb_m15 reentry timeout | `elapsed_since_exit for statarb_m15 sl_hit lock` | 13.7 days (should be ≤ 3 days with TTL) |
| OFI toxicity threshold | Review OFI z-score distribution after 1 week live data → calibrate ±2.0 gate | 0 bars collected (need ~500+ for stable z-score) |
| Phase 1 live verification | 24h monitoring: check `regime_gate_failed` count = 0, `circuit OPEN` count = 0, exit management continues for existing positions. If CircuitBreaker trips > 2× during normal hours → widen threshold. Verify `stale_counter` always resets to 0 after classify success. | DEPLOYED 2026-05-27 13:24 UTC — 3 cycles clean. Review 2026-05-28 |

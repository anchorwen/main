# Execution / Trail Stop Engine

## Purpose
Physically isolated Risk Exit subsystem. Computes Chandelier trailing stop levels with ATR-adaptive multipliers, graduated profit locking at R-milestones, and nonlinear dynamic decay to prevent breakeven floor deadlock. Explicitly agnostic to strategy type, model confidence, and brain identity.

## Key Files
| File | Role |
|------|------|
| `core/execution/trail_stop_engine.py` | `TrailPolicy` dataclass + `TrailStopEngine` — pure calculator (no I/O) |

## Architecture

### TrailPolicy (immutable per-strategy config)
- `atr_mult`, `trail_activation_atr`, `breakeven_threshold_atr`
- `graduated_lock_levels`: R-milestone → SL floor
- `decay_start_r`, `decay_full_r`, `decay_enabled`: nonlinear decay from base→min
- `ratchet_enabled`, `ratchet_arm_r`, `ratchet_giveback_r`, `ratchet_breakeven_floor_r`: profit ratchet floor (FIX-20260708-004)
- `tp_proximity_ratio`, `tp_min_distance_atr`, `tp_min_step`: TP trailing structural parity (FIX-20260713-008) — active by default (0.7 / 1.5 / 0.15), set to 0.0 to opt out per-strategy
- `tp_min_rr_ratio`: RR coupling contract (TECH_DEBT-019, FIX-20260819-001) — 0.0 = disabled (structural/legacy zero-change); >0 (e.g. 0.85) arms the three-mechanism RR contract: ① RR hard floor (trailing TP distance stays ≥ this × current SL distance, entry reference) ② symmetric SL_Volatility_Trail (ATR contraction ≤0.80 that tightens TP also tightens SL by the same ratio) ③ elastic expansion (ATR recovery ≥0.85 re-expands TP outward up to `initial_tp`, hysteresis dead-band 0.80–0.85). Injected at registration from `sl.min_rr_ratio` (`position_registration.py`), persisted via save_state v3 (`trail_policy`).

### Chandelier Formula
```
Long:  max(current_sl, highest_high - mult × ATR)
Short: min(current_sl, lowest_low + mult × ATR)
```
Capped at `max_lock_atr × entry_atr` to respect model training contract.

### Activation Watermark (FIX-20260603-064)
Trail only starts after unrealized profit exceeds `trail_activation_atr × entry_atr`. Previous default (1.0 ATR) left positions unprotected for too long.

### Nonlinear Dynamic Decay (DQAF-20260609-001)
`trail_mult` decays from regime base to `min_trail_mult` as R grows from 0.5R to 2.0R. Prevents "breakeven floor deadlock" where Chandelier trail could never exceed entry_price.

### Regime Adaptation
- Low vol: widen trail (let profits run)
- High vol: tighten trail (protect against reversal)
- Brain live Sharpe: widen only (1.0-1.5×), never tighten

## Inbound Dependencies
None — pure calculation engine with zero imports beyond numpy.

## Outbound Dependents
| Module | What it imports |
|--------|-----------------|
| execution/position_manager | TrailStopEngine (compute_trail_stop, should_breakeven) |
| runtime/trail_dispatch | TrailStopEngine (per-cycle trail/breakeven/TP dispatch) |
| runtime/live_cycle | TrailPolicy (per-strategy construction) |

## Fix History
See [execution_orders.md](execution_orders.md) for consolidated Fix History.

## Data Flow
See [Chandelier Formula](#chandelier-formula) and [Regime Adaptation](#regime-adaptation) above — the trail computation pipeline (Market State → Regime Multiplier → Chandelier Formula → Activation Watermark → Nonlinear Decay → TrailResult) serves as this module's Data Flow documentation.

## Known Issues

### RESOLVED — TP/SL dynamic-trail RR collapse (TECH_DEBT-019, DQAF-20260817-001, Sev 2)

`compute_trail_tp` (FIX-20260713-008) tightened TP inward when
`atr_ratio ≤ 0.80`, but the tighten floor was **decoupled from the SL
distance/RR** — the TP could collapse to a reward:risk < 1.  Live evidence:
h1_swing RR 1.73→0.527, m15_swing RR 0.98→0.385 (both `min_rr_ratio=0.85`
breached; SL never moved while profit reached +2.1R).  Fixed in
FIX-20260819-001 (three mechanisms gated behind `tp_min_rr_ratio > 0`; zero
change when disabled).  See `TECH_DEBT_REGISTRY.md` #019 and DQAF docket.

- **Remaining**: elastic expansion's `_EXPAND_THRESHOLD = 0.85` (dead-band
  upper edge) is a tuning constant — Deferred calibration, monitor via
  `rr_current` / `tp_rr_floor_fired` snapshot telemetry.

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `TrailStopEngine.compute_trail_stop(pos, market_state)` → `TrailResult` | position_manager, trail_dispatch | Stable |
| `TrailStopEngine.should_breakeven(pos, market_state)` → `bool` | position_manager | Stable |
| `TrailPolicy` dataclass (immutable per-strategy config) | live_cycle, trail_dispatch | Stable |
| `compute_rr_floor_price(side, entry, sl, min_rr)` → `float\|None` | position_manager, trail_dispatch (single RR-distance convergence point, FIX-20260819-001) | Stable |
| `TrailStopEngine.compute_volatility_trail_sl(pos, atr_ratio)` → `float\|None` | position_manager wrapper → trail_dispatch (symmetric SL tightening, FIX-20260819-001) | Stable |

## Verification
```bash
python -m pytest tests/ -k "trail_stop or trail" -q
```

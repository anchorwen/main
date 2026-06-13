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

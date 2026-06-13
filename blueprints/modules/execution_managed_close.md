# Execution / Managed Close

## Purpose
Strangler Fig extraction from live_cycle.py. Handles all managed position closes: PnL estimation, ExitRecord for re-entry guard, CooldownRegistry update, ghost-volume audit (MT5 ground truth vs system volume), trail contribution metadata, watchdog-protected dispatch, and post-close cleanup.

## Key Files
| File | Role |
|------|------|
| `core/execution/managed_close.py` | `dispatch_managed_close()` — single entry point for all managed exits |

## Architecture

### Close Flow
```
1. PnL estimation (mid-based, corrected later by reconciliation)
2. ExitRecord → ReentryState (for re-entry quality gate)
3. CooldownRegistry.record_exit() (strategy/direction/reason → cooldown)
4. Ghost-volume audit: pos.volume vs expected_remaining_volume vs MT5 truth
5. Trail contribution metadata: initial_sl, final_sl, trail_advances
6. Watchdog-protected dispatch (or bare dispatch if no watchdog)
7. Post-close cleanup: known_open_tickets removal, PnL/budget recording,
   AlertHub close notification
```

### Ghost-Volume Audit (Pillar 4)
Compares `pos.volume` (system belief) vs `expected_remaining_volume` (after partial_tp/net_out) vs MT5 `positions_get(ticket=).volume` (ground truth). MT5 always wins.

## Inbound Dependencies
| Module | What is imported |
|--------|-----------------|
| execution/live_order_sender | dispatch_live_order |
| execution/reentry_guard | ExitRecord, ensure_reentry_state |
| execution/exit_watchdog | ExitWatchdog (optional watchdog wrapping) |
| runtime/fault_handler | FaultLevel, FaultTolerantContext, log_and_continue |

## Outbound Dependents
| Module | What it imports |
|--------|-----------------|
| runtime/live_cycle | dispatch_managed_close (all managed exit paths) |

## Fix History
See [execution_orders.md](execution_orders.md) for consolidated Fix History.

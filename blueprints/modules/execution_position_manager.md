# Execution / Position Manager

## Purpose
Central position state machine for all active positions. Maintains mutable per-position state (ActivePosition dataclass), orchestrates 4-layer exit priority system, and persists minimal intent-state (v3 SSOT) for crash recovery.

## Key Files
| File | Role |
|------|------|
| `core/execution/position_manager.py` | `ActivePosition` dataclass + `ActivePositionManager` — exit orchestration (1,931 lines) |

## Architecture

### ActivePosition (40+ fields)
Physical state synced from MT5 (authoritative source). Python persists only intent-state (4 fields: ticket, cycles_held, breakeven_triggered, partial_tp_done).

### Exit Evaluation Order (per management cycle)
```
Layer 0:  Trail SL/Breakeven/TP modification (every cycle)
Layer 1:  Bleed stop — N consecutive negative-PnL bars
Layer 2:  OU mean-reversion exit (statarb only)
Layer 3:  Brain ensemble flip + EMA confidence decay
          └─ _toxicity_veto during protected period
Layer 4:  MetaExit multi-factor urgency (shadow/telemetry only)
Layer 5:  Hesitation — no breakeven within N cycles
Layer 6:  Time-based exit — gamma-parameterized EV trajectory
```

### Pending Close Lock
Prevents cross-cycle retry avalanche: `_pending_close` dict (ticket→dispatch_cycle), auto-expire 10 cycles, flood threshold 3 → permanent lock.

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| execution/trail_stop_engine | TrailStopEngine, TrailPolicy | Chandelier trailing stop computation |
| execution/meta_exit_engine | MetaExitEngine | Multi-factor exit urgency scoring |
| runtime/fault_handler | FaultTolerantContext | MT5 IPC fault tolerance |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | ActivePositionManager | Management phase orchestration |
| runtime/trail_dispatch | ActivePositionManager | Trail/breakeven/TP computation |

## Known Issues
- **Strangler Fig candidates**: `load_state()` and `register_position()` identified for extraction
- **Ghost-volume audit**: `expected_remaining_volume` vs MT5 ground truth — string-based comparison, fragile

## Fix History
See [execution_orders.md](execution_orders.md) for the consolidated Fix History covering all execution/ modules.

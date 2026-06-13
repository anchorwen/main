# Execution / Exit Watchdog

## Purpose
Heartbeat exit-order watchdog ensuring no managed close is silently dropped. Wraps every exit order with persistent retry (exponential backoff), slippage escalation, and L2 forced liquidation fallback after total duration exhaustion.

## Key Files
| File | Role |
|------|------|
| `core/execution/exit_watchdog.py` | `ExitWatchdog` class — retry loop + escalation + alerting |

## Architecture

### Retry Strategy
```
Attempt 1-2: normal dispatch, 2 pip slippage, 1-2s backoff
Attempt 3-4: escalated slippage (50 pts), 4-8s backoff
Attempt 5:   emergency slippage (200 pts), 16s backoff
After 30s:   L2 forced liquidation via broker.close_position()
```

### Multi-stage Confirmation
1. Dispatch → `dispatch_fn(payload)` 
2. ACK poll → `resolve_ack()` (ZMQ fast path, file fallback)
3. Position verification → `get_position_open(ticket)`
4. L2 fallback → `l2_broker.close_position()`

### Final Statuses
`closed`, `escalated`, `critical_timeout`, `cancelled`, `already_closed`, `closed_l2_forced`

### Health Monitoring
`is_healthy()` checks for CRITICAL alerts in last hour. Alerts logged to `data/reports/exit_watchdog_alerts.jsonl`.

## Inbound Dependencies
| Module | What is imported |
|--------|-----------------|
| protocol/services/zmq_receipt_listener | resolve_ack (ZMQ fast path + file fallback) |
| runtime/fault_handler | fail_open_guard, log_and_continue |

## Outbound Dependents
| Module | What it imports |
|--------|-----------------|
| execution/managed_close | ExitWatchdog (dispatch_managed_close wrapper) |
| runtime/live_cycle | ExitWatchdog (net_out close + managed exit) |

## Fix History
See [execution_orders.md](execution_orders.md) for consolidated Fix History.

| FIX-20260613-086 | 2026-06-13 | cursor-agent | ad6795e | Watchdog Encapsulation: multi-dimensional evaluator with time_decay + price_decay triggers. Model-independent structural exits. | missing-validation |
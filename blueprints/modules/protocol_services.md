# Protocol / Services

## Purpose
Communication layer: message dispatch, adapter registry, intent building, decision compilation, venue routing, circuit breaking, and idempotency tracking.

## Key Files
| File | Role |
|------|------|
| `core/protocol/services/communication_dispatcher.py` | `CommunicationDispatcher` — main dispatch entry point |
| `core/protocol/services/communication_adapter_registry.py` | `CommunicationAdapterRegistry` — adapter resolution |
| `core/protocol/services/communication_adapter.py` | `CommunicationAdapter(Protocol)` — adapter interface |
| `core/protocol/services/intent_message_builder.py` | `IntentMessageBuilder` — DecisionIntent → CommunicationEnvelope |
| `core/protocol/services/decision_compiler.py` | `DecisionCompiler` — DecisionCandidate → DecisionIntent |
| `core/protocol/services/venue_router.py` | `VenueAdapter`, `StubVenueAdapter` — venue dispatch |
| `core/protocol/services/resilience.py` | `CircuitBreaker` — CLOSED/OPEN/HALF_OPEN circuit breaker |
| `core/protocol/services/idempotency.py` | `IdempotencyStore` — file-backed idempotency keys |
| `core/protocol/services/override_resolver.py` | `OverrideResolver` — active override resolution |
| `core/protocol/event_bar_sync.py` | `BarSyncPoller` — event-driven M5 bar synchronization with MT5 |

## Data Flow
```
DecisionIntent → DecisionCompiler → IntentMessageBuilder → CommunicationEnvelope
                                                                  ↓
                                                      CommunicationDispatcher
                                                                  ↓
                                                    AdapterRegistry → VenueAdapter
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | DecisionIntent, DecisionCandidate, CommunicationEnvelope, DispatchRequest, DispatchResult | Domain types |
| contracts/enums | DecisionAction, DecisionSide, SystemMode, DispatchStatus, CommunicationMessageType | Enum values |
| contracts/ids | new_intent_id, new_message_id, new_dispatch_id | ID generation |
| execution | gateway_contracts | Order state tracking |
| observability | metric_names | Dispatch metrics |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/live_order_sender | CommunicationEnvelope, CommunicationDispatcher | Order dispatch |
| deployment/lifecycle | CommunicationDispatcher | Service wiring |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260519-019 | 2026-05-19 | cursor-agent | — | BarSyncPoller 92.8% timeout rate fix: added `fetch_synthetic_bar()` — when M5 bar hasn't formed, aggregates last 6 M1 bars into synthetic M5 OHLC(V) instead of blind `time.sleep()`. Eliminates 120s data misalignment window where stale features were used against real-time prices. Caller in live_intent_loop.py uses synthetic bar on timeout instead of falling back to interval sleep. | RC-06 (data-misalignment, sampling-blind-spot) |
| FIX-20260522-006 | 2026-05-22 | cursor-agent | — | BarSyncPoller MT5 transient error retry: copy_rates_from_pos() fails after ~104s of polling despite successful initialize(). Added MAX_MT5_ERROR_RETRIES=3 with re-init+retry loop before degrading to fallback_poll + synthetic bar. Resets error count on successful poll or new bar detection. | RC-05 (transient-error) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `CommunicationDispatcher.dispatch(envelope)` → `DispatchResult` | live_order_sender | Stable |
| `DecisionCompiler.compile(candidate, policies)` → `DecisionIntent` | live_cycle | Stable |
| `CircuitBreaker` states: CLOSED → OPEN (5 failures) → HALF_OPEN (30s) → CLOSED | CommunicationDispatcher | Stable |

## Verification
```bash
python -m pytest tests/ -k "protocol or dispatch or circuit" -q
```

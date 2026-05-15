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

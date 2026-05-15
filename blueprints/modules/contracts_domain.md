# Contracts / Domain

## Purpose
Domain model dataclasses that form the message-passing backbone of the system. Every cross-module communication uses these types: proposals, intents, verdicts, envelopes, dispatch requests/results, and execution events.

## Key Files
| File | Role |
|------|------|
| `core/contracts/domain/brain_decision_proposal.py` | `BrainDecisionProposal` — uniform AI output |
| `core/contracts/domain/decision_intent.py` | `DecisionIntent` — compiled trading intent |
| `core/contracts/domain/decision_candidate.py` | `DecisionCandidate` — parliament output |
| `core/contracts/domain/risk_verdict.py` | `RiskVerdict` — risk evaluation result |
| `core/contracts/domain/communication_envelope.py` | `CommunicationEnvelope` — inter-service message |
| `core/contracts/domain/communication_record.py` | `CommunicationRecord` — message ledger entry |
| `core/contracts/domain/decision_record.py` | `DecisionRecord` — shadow decision ledger entry |
| `core/contracts/domain/dispatch_request.py` | `DispatchRequest` — order dispatch request |
| `core/contracts/domain/dispatch_result.py` | `DispatchResult` — dispatch outcome |
| `core/contracts/domain/execution_event.py` | `ExecutionEvent` — order lifecycle event |
| `core/contracts/domain/replay_execution_record.py` | `ReplayExecutionRecord` — replay verification |
| `core/contracts/domain/protocol_override.py` | Protocol override domain |
| `core/contracts/domain/system_mode_state.py` | `SystemModeState` — system mode snapshot |
| `core/contracts/enums.py` | All enums: BrainRole, BrainStatus, DecisionAction, DecisionSide, SystemMode, etc. |
| `core/contracts/exceptions.py` | `DomainError`, `RiskError`, `BrainNotFoundError`, etc. |
| `core/contracts/domain_keys.py` | Shared string constants for evidence, payloads, pipeline stages |
| `core/contracts/validators.py` | `ContractViolation` — data integrity checks |

## Data Flow
```
All data flows through these dataclasses:
  BrainDecisionProposal → DecisionCandidate → DecisionIntent → RiskVerdict
       (AI output)         (parliament)        (compiled)       (risk check)
                                                                    ↓
                                                            DispatchRequest
                                                                    ↓
                                                            ExecutionEvent
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| ledger | (replay_execution_record only) | Schema version for replay |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| **Every other module** | Various domain types | Core message types used everywhere |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| All domain dataclasses are immutable (frozen=True) where possible | All consumers | Highly stable |
| Schema version constants in each domain file | Serialization layer | Stable |
| `new_*_id()` functions in contracts/ids | All producers | Stable |

## Verification
```bash
python -m pytest tests/ -k "contract" -q
```

# Contracts / Domain

## Purpose
Domain model dataclasses that form the message-passing backbone of the system. Every cross-module communication uses these types: proposals, intents, verdicts, envelopes, dispatch requests/results, and execution events.

## Key Files
| File | Role |
|------|------|
| `core/schemas/trading_contracts.py` | **Layer 1 immutable contracts**: `BrainSignal`, `ConsensusResult`, `StrategyDecision`, `DegradedResult`, `Direction`, `TradeDirection` — frozen dataclasses replacing dict-based communication |
| `core/contracts/domain/brain_decision_proposal.py` | `BrainDecisionProposal` — uniform AI output (legacy, being phased out by BrainSignal) |
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
  BrainSignal → ConsensusResult → StrategyDecision → RiskVerdict
  (AI output)    (parliament)     (strategy line)    (risk check)
                                                                   ↓
                                                           DispatchRequest
                                                                   ↓
                                                           ExecutionEvent

Layer 1 immutable contracts (core/schemas/trading_contracts.py):
  BrainAdapters ─BrainSignal→ Parliament ─ConsensusResult→ StrategyLines ─StrategyDecision→ Guards → Dispatch

Failure contract: DegradedResult replaces every `except Exception: pass`
so downstream modules can decide whether to degrade, skip, or circuit-break.
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
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues across all changed modules | type-confusion |
| FIX-20260522-022 | 2026-05-22 | cursor-agent | 24ff517 | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility | contract-violation |
| FIX-20260522-017 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: Created `core/schemas/trading_contracts.py` — single source of truth for inter-module data contracts. Four frozen dataclasses (`BrainSignal`, `ConsensusResult`, `StrategyDecision`, `DegradedResult`) replace untyped dicts at all 4 module boundaries. `DegradedResult` replaces every `except:pass` with explicit degradation signal enabling circuit breaker. All fields use `frozen=True, slots=True` for immutability. | RC-06 |

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

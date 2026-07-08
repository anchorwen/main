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
| FIX-20260708-001 | 2026-07-08 | cursor-agent | 97ec551d | Canonical immutable-identity join authority (DQAF-20260708-001 L3): pair open<->close on position_identifier (stable across MT5 re-ticketing) not the mutable position_ticket. Adds resolve_identity() used by JournalGate/auditor/reconciliation/label_builder; PositionOpened now emits the anchor. Fixes recurring orphan-close quarantine (~17/day) + dual-track label loss. BTC orphans 157->119, $126 PnL recovered. | type-confusion |
| FIX-20260623-084 | 2026-06-23 | cursor-agent | — | **DQAF-084: PositionClosed +p_win field — signal quality through close-event contract**. `PositionClosed` had no `p_win` (unlike `PositionOpened` which carries p_win/confidence/kelly_mult). Added `p_win: float = 0.5` + `to_journal_entry()` output. Backward-compatible via default. | L3 — signal quality metadata not propagated through close-event contract |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | 520b371 | Event Sourcing Foundation: PnLEvent + GovernanceTransitionEvent Pydantic models with extra=forbid, frozen=True, allow_inf_nan=False. | contract-violation |
| FIX-20260605-122 | 2026-06-05 | cursor-agent | ae0d006 | **Orphan domain key cleanup**: Removed 4 unused constants from domain_keys.py — PAYLOAD_KEY_CIRCUIT_STATE, PAYLOAD_KEY_FROZEN_BRAIN_COUNT, PAYLOAD_KEY_POSITION_UTILIZATION, CIRCUIT_STATE_OPEN. Zero references in codebase. | RC-11 |
| FIX-20260525-014 | 2026-05-25 | cursor-agent | — | Gate audit observability: StrategyDecision.gate_diag field for per-gate diagnostics. ConformalOU gate captures z_score/theta/half_life/composite_score; parliament captures confidence/threshold; counter-trend captures trend info. Supports structured gate_audit JSONL recording. | RC-12 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing, and suppressors for pre-existing pattern issues across all changed modules | type-confusion |
| FIX-20260522-022 | 2026-05-22 | cursor-agent | 24ff517 | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility | contract-violation |
| FIX-20260522-017 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: Created `core/schemas/trading_contracts.py` — single source of truth for inter-module data contracts. Four frozen dataclasses (`BrainSignal`, `ConsensusResult`, `StrategyDecision`, `DegradedResult`) replace untyped dicts at all 4 module boundaries. `DegradedResult` replaces every `except:pass` with explicit degradation signal enabling circuit breaker. All fields use `frozen=True, slots=True` for immutability. | RC-06 |
| FIX-20260621-036 | 2026-06-21 | cursor-agent | — | **DQAF-033 P1: position_identifier 注入两路径对账主键。** PositionClosed 新增 position_identifier 字段，PCA 从 MT5 deal.position_id 捕获，bridge worker detail + journal record 同步注入。0 行路由/资金逻辑变更。 | RC-08 |
| FIX-20260624-106 | 2026-06-24 | cursor-agent | — | test_stub_sequence_gap_detectable: fix field tracking — test used record.seq (WAL sequential numbering) instead of record.payload.recorded_at_wal_seq (where the intentional gap at seq 4 lives in the stub payload). | RC-06 |
| FIX-20260531-005 | 2026-05-31 | cursor-agent | — | **Architectural Defense 1**: Global Asset Registry `core/config/asset_registry.py` — SSOT for symbol physical properties. XAUUSDc + BTCUSDc registered. Adding new asset = 1 line. | RC-09 |
| FIX-20260625-139 | 2026-06-25 | cursor-agent | — | **BrainSignal vote_weight contract gap — shadow brain bypass**: Added `vote_weight: float = 1.0` field to `BrainSignal` frozen dataclass in `trading_contracts.py`. Config-level binary permission gate (0.0=muted) now carried through pipeline to `_compute_weighted()` fail-fast gate. Previously missing → shadow brains (vote_weight=0.0) bypassed consensus mute. Part of multi-file fix: 7 adapters + base + shadow_decision_recorder updated. | L3 — contract gap: BrainSignal replaced BrainDecisionProposal but omitted vote_weight field |

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

# Protocol / Parliament

## Purpose
Multi-brain deliberation and consensus building: groups brains by contract type, computes within-group consensus (union or weighted-average voting), and produces `DecisionCandidate` for downstream risk evaluation.

## Key Files
| File | Role |
|------|------|
| `core/parliament/parliament_service.py` | `ParliamentService` — (DEPRECATED for live) multi-brain deliberation |
| `core/parliament/contract_groups.py` | `ContractGroupConsensus`, `GroupSignal`, contract group definitions |
| `core/parliament/schema_versions.py` | `SCHEMA_DECISION_CANDIDATE` version constant |

## Data Flow
```
BrainDecisionProposal[] → ContractGroupConsensus.compute_all_group_signals()
                                    ↓
                          GroupSignal[] (per-group consensus)
                                    ↓
                          StrategyLine → DecisionCandidate
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/brain_registry | BrainRegistry | Brain-to-group assignment |
| contracts/domain | BrainDecisionProposal, DecisionCandidate | Input/output types |
| contracts/enums | BrainRole | Role-based filtering |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/strategy_line | ContractGroupConsensus, get_group_for_contract_group | Group signal computation |
| runtime/live_cycle | contract_groups | Strategy group definitions |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260512-001 | 2026-05-14 | cursor-agent | a4a1005 | Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading | contract-violation |
| FIX-20260517-008 | 2026-05-17 | cursor-agent | — | Added explicit type annotations (dict[str, Any]) to BARRIER_GROUP, MICRO_GROUP, and all contract group dicts for mypy strict compliance | type-safety |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BARRIER_GROUP`, `MICRO_GROUP`, `MICRO_M15_GROUP`, etc. | strategy_line, live_cycle | Stable |
| `ContractGroupConsensus.compute(proposals, mode)` → `GroupSignal` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "parliament or consensus or group" -q
```

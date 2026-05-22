# Protocol / Parliament

## Purpose
Multi-brain deliberation and consensus building: groups brains by contract type, computes within-group consensus (union or weighted-average voting), and produces `DecisionCandidate` for downstream risk evaluation.

## Key Files
| File | Role |
|------|------|
| `core/parliament/parliament_service.py` | `ParliamentService` — (DEPRECATED for live) multi-brain deliberation |
| `core/parliament/contract_groups.py` | `ContractGroupConsensus`, `ConsensusResult`, contract group definitions (GroupSignal replaced by `ConsensusResult` from trading_contracts, Layer 1) |
| `core/parliament/schema_versions.py` | `SCHEMA_DECISION_CANDIDATE` version constant |

## Data Flow
```
BrainSignal[] → ContractGroupConsensus.compute_all_group_signals()
                                    ↓
                          ConsensusResult[] (per-group consensus, immutable)
                                    ↓
                          StrategyLine → DecisionCandidate
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/brain_registry | BrainRegistry | Brain-to-group assignment |
| contracts/domain | BrainDecisionProposal, DecisionCandidate | Input/output types |
| schemas/trading_contracts | BrainSignal, ConsensusResult | Layer 1 immutable contracts |
| contracts/enums | BrainRole | Role-based filtering |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/strategy_line | ContractGroupConsensus, get_group_for_contract_group | Group signal computation |
| runtime/live_cycle | contract_groups | Strategy group definitions |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260522-022 | 2026-05-22 | cursor-agent | 24ff517 | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility. Fixes 32 v9 shadow tests. | contract-violation |
| FIX-20260519-013 | 2026-05-19 | cursor-agent | — | Consensus direction bug: _compute_weighted()全neutral组(up==down==0.5)之前伪造direction="long"+confidence=0.2486。修复后全neutral→direction="neutral",confidence=0.0。brain_votes中statarb_dynamic组不再出现虚假long共识。 | contract-violation |
| FIX-20260517-013 | 2026-05-17 | cursor-agent | — | BARRIER_GROUP brain_types trimmed: removed onnx_v9, deepresmlp, online_sgd, xgboost_v4.5 (no active brains of these types). Kept xgboost_v9 + lightgbm_v1. live.yaml synced. | stale-data |
| FIX-20260512-001 | 2026-05-14 | cursor-agent | a4a1005 | Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading | contract-violation |
| FIX-20260517-008 | 2026-05-17 | cursor-agent | — | Added explicit type annotations (dict[str, Any]) to BARRIER_GROUP, MICRO_GROUP, and all contract group dicts for mypy strict compliance | type-safety |
| FIX-20260520-028 | 2026-05-20 | cursor-agent | — | Meta Pipeline Executive Veto: Track 2 (Huber→Stage 2) upgraded from deadlock-only fallback to independent first-refusal. When 8/11 long-biased brains create spurious LONG majority, Huber's counter-consensus short signal now evaluates BEFORE parliament deadlock check, not after. | RC-06 |
| FIX-20260522-016 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: `GroupSignal` (10-field mutable dict-like) replaced with frozen `ConsensusResult` from `core/schemas/trading_contracts.py`. `_compute_weighted()` redesigned with direction-count voting: each brain votes its decided direction weighted by `confidence × vote_weight × (0.5 if fallback else 1.0)`, highest total wins. Added `supporting_brains`/`dissenting_brains` lists for audit trail. Backward-compat via `getattr(p, "direction", None)` for legacy `BrainDecisionProposal` inputs. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BARRIER_GROUP`, `MICRO_GROUP`, `MICRO_M15_GROUP`, etc. | strategy_line, live_cycle | Stable |
| `ContractGroupConsensus.compute(proposals, mode)` → `ConsensusResult` | strategy_line | Stable (Layer 1) |

## Verification
```bash
python -m pytest tests/ -k "parliament or consensus or group" -q
```

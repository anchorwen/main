# Parliament / Consensus

## Purpose
Contract-group-based multi-brain consensus computation. Groups brain proposals by training contract type, computes per-group voting (weighted-average or union mode), resolves inter-group conflicts, and produces a unified `ConsensusResult` for downstream execution. Ensures models trained on the SAME contract vote together and models on DIFFERENT contracts do not cross-contaminate.

## Key Files
| File | Role |
|------|------|
| `core/parliament/group_consensus.py` | `compute_contract_group_consensus()` — orchestrator: dynamic weighting → per-group signals → conflict resolution → capital allocation |
| `core/parliament/contract_groups.py` | `ContractGroupConsensus`, all contract group definitions (barrier_12bar, micro, swing, statarb, BTC), `compute_all_group_signals()`, AB router |
| `core/parliament/parliament_service.py` | `ParliamentService` — (DEPRECATED for live hot path, replaced by `ContractGroupConsensus`) |
| `core/parliament/schema_versions.py` | `SCHEMA_DECISION_CANDIDATE` version constant |

## Data Flow
```
BrainDecisionProposal[] → DynamicBrainWeighter.apply_weights()
                              ↓
              compute_all_group_signals(brain_proposal_pairs)
                              ↓
                    group_signals: dict[group_name → ConsensusResult|None]
                              ↓
                    resolve_conflicts(group_signals) → AllocationDecision
                              ↓
                    compute_volume() → dynamic_volume
                              ↓
                    compute_contract_group_consensus() → {direction, confidence, dynamic_volume, proposals, consensus_extra}
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/services/dynamic_brain_weighter | DynamicBrainWeighter | Vote weight computation from PnL performance |
| execution/capital_allocator | CapitalAllocator, compute_volume, resolve_conflicts | Conflict resolution and position sizing |
| parliament/contract_groups | compute_all_group_signals | Per-group consensus signals |
| runtime/fault_handler | FaultTolerantContext, log_and_continue, FaultLevel | Graceful degradation |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | compute_contract_group_consensus | Live trading cycle orchestration |

## Contract Groups (15 groups)
| Group | Voting Mode | Horizon | Brains |
|-------|------------|---------|--------|
| barrier_12bar | weighted | 12 M5 bars | lightgbm_v1, xgboost_v9 |
| barrier_12bar_meta | weighted | 12 M5 bars | lightgbm_v1 (OU meta-labeler) |
| micro_3bar | union | 8 M5 bars | xgboost_v4.5, transformer_v4.3, transformer_v5 |
| micro_m15 | union | 5 M15 bars | xgboost_v4.5_m15, transformer_v5_m15 |
| micro_h1 | union | 4 H1 bars | xgboost_v4.5_h1, transformer_v5_h1 |
| micro_h4 | — | 3 H4 bars | xgboost_v4.5_h4, transformer_v5_h4 (gate only) |
| statarb_dynamic | weighted | dynamic | ou_params_v6 |
| statarb_m15 | weighted | dynamic | ou_params_v6 |
| daily_swing | weighted | 1440 M5 cycles | xgboost_v9, lightgbm_v1 |
| m15_swing | weighted | 72 M5 cycles | xgboost_v9 |
| m30_swing | weighted | 36 M5 cycles | xgboost_v9 |
| h1_swing | weighted | 288 M5 cycles | xgboost_v9 |
| h4_swing | weighted | 864 M5 cycles | xgboost_v9 |
| btc_swing | weighted | 36 M5 cycles | xgboost_v9 |
| btc_swing_h1 | weighted | 144 M5 cycles | lightgbm_v1 |

## Known Issues
- `compute_contract_group_consensus()` has 6 lazy imports inside function body (mitigating circular deps) — if any import fails, function is fully unavailable. Consider extracting to module-level with Protocol interfaces.
- Correlation penalty path uses `FaultTolerantContext(DEGRADE)` — if correlation tracker is misconfigured, penalty is silently skipped.

## Fix History
| Fix ID | Date | Summary | Root Cause |
|--------|------|---------|------------|
| FIX-20260607-011 | 2026-06-07 | Vote weight decoupling: base_weight × dynamic_scale with fail-fast gate | RC-09 |
| FIX-20260602-052 | 2026-06-02 | Single-brain consensus self-normalization bug (conf 0.34→1.0) | RC-06 |
| FIX-20260603-062 | 2026-06-03 | Unanimous multi-brain self-normalization bug | RC-06 |
| FIX-20260530-060 | 2026-05-30 | Strangler Fig #2: extract compute_contract_group_consensus | RC-08 |
| FIX-20260522-016 | 2026-05-22 | GroupSignal→ConsensusResult immutable contract migration | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `compute_contract_group_consensus()` signature | live_cycle | Stable — params only added, never removed |
| `ConsensusResult` dataclass | strategy_line, capital_allocator | Stable — frozen dataclass |
| `ContractGroupConsensus.compute()` | contract_groups | Stable |

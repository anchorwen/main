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
| FIX-20260726-006 | 2026-07-26 | cursor-agent | — | **M30_SWING_GROUP: add lightgbm_v1 to brain_types.** V6_lgb and Swing_LGB_M30_V1 (both lightgbm_v1) were in the m30_swing group via contract_group routing but brain_types set only had xgboost_v9. Alignment fix — brain_types used by barrier_brains filtering + legacy _TYPE_TO_GROUP mapping. | RC-09 |
| FIX-20260713-005 | 2026-07-13 | cursor-agent | (pending) | BTC multi-TF shadow tracer bullets: 4 new contract groups (btc_swing_m15/m30/h1_v2/h4) added to ALL_GROUPS — brains were silently dropped as unknown_contract_group_at_build. | RC-09 |
| FIX-20260629-179 | 2026-06-29 | cursor-agent | — | **Register h1_directional contract group**: H1_DIRECTIONAL_GROUP added to contract_groups.py + ALL_GROUPS. Fixes Swing_V10_H1_Directional (PF=81.10) being silently skipped every cycle as unknown_contract_group_at_build. h1_directional strategy now has a contract group bucket. | RC-09 |
| FIX-20260629-174 | 2026-06-29 | cursor-agent | — | **DQAF-174 L2: Unified weighting contract — capacity allocation aligned from bare get_weights() (PnL-only) to base_weight × dynamic_scale.** vote_weight=0 brains now receive 0 capacity allocation. Same contract as contract_groups.py voting path. | RC-06 |
| FIX-20260607-147 | 2026-06-07 | cursor-agent | — | **Vote weight decoupling**: `contract_groups._compute_weighted()` now uses `base_weight × dynamic_scale` with fail-fast gate at base_weight≤0. Config vote_weight preserved as binary permission, dynamic_scale from PnL performance as multiplier. DQAF-011. | RC-09 |
| FIX-20260530-087 | 2026-05-30 | cursor-agent | — | BTC_SWING_GROUP: added `BTC_SWING_GROUP` contract group to `ALL_GROUPS` in `contract_groups.py`. BTC swing strategy (`btc_swing`) isolated with magic=90410, brain_type=swing_v9, contract_group=btc_swing_v1. Prevents cross-contamination between gold and BTC brain voting. | RC-09 (config-drift) |
| FIX-20260529-051 | 2026-05-30 | cursor-agent | — | Last Mile Protocol Phase 2: contract_groups.py 4 LOG sites converted to log_and_continue() | RC-07 |
| FIX-20260529-048 | 2026-05-29 | cursor-agent | — | PR#3 Phase 2: contract_groups.py 4 sequential brain_group_resolution silent-pass handlers → LOG via structured logging (registry/brain_type/source_type/metadata_type probes). | RC-07 |
| FIX-20260522-022 | 2026-05-22 | cursor-agent | 24ff517 | Phase 2b: ParliamentService _normalize_proposal adapter — maps BrainSignal frozen dataclass to legacy BrainDecisionProposal interface for v9 shadow compatibility. Fixes 32 v9 shadow tests. | contract-violation |
| FIX-20260519-013 | 2026-05-19 | cursor-agent | — | Consensus direction bug: _compute_weighted()全neutral组(up==down==0.5)之前伪造direction="long"+confidence=0.2486。修复后全neutral→direction="neutral",confidence=0.0。brain_votes中statarb_dynamic组不再出现虚假long共识。 | contract-violation |
| FIX-20260517-013 | 2026-05-17 | cursor-agent | — | BARRIER_GROUP brain_types trimmed: removed onnx_v9, deepresmlp, online_sgd, xgboost_v4.5 (no active brains of these types). Kept xgboost_v9 + lightgbm_v1. live.yaml synced. | stale-data |
| FIX-20260512-001 | 2026-05-14 | cursor-agent | a4a1005 | Strategy ping-pong: added allow_coexist + min_hold_cycles to prevent conflicting strategies from overtrading | contract-violation |
| FIX-20260517-008 | 2026-05-17 | cursor-agent | — | Added explicit type annotations (dict[str, Any]) to BARRIER_GROUP, MICRO_GROUP, and all contract group dicts for mypy strict compliance | type-safety |
| FIX-20260520-028 | 2026-05-20 | cursor-agent | — | Meta Pipeline Executive Veto: Track 2 (Huber→Stage 2) upgraded from deadlock-only fallback to independent first-refusal. When 8/11 long-biased brains create spurious LONG majority, Huber's counter-consensus short signal now evaluates BEFORE parliament deadlock check, not after. | RC-06 |
| FIX-20260522-016 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: `GroupSignal` (10-field mutable dict-like) replaced with frozen `ConsensusResult` from `core/schemas/trading_contracts.py`. `_compute_weighted()` redesigned with direction-count voting: each brain votes its decided direction weighted by `confidence × vote_weight × (0.5 if fallback else 1.0)`, highest total wins. Added `supporting_brains`/`dissenting_brains` lists for audit trail. Backward-compat via `getattr(p, "direction", None)` for legacy `BrainDecisionProposal` inputs. | RC-06 |
| FIX-20260524-034 | 2026-05-24 | cursor-agent | — | BARRIER_12BAR_META_GROUP added: new contract group for meta-labeling binary classifier (Meta_Stage1_MetaLabel_Binary_V1). Routes OU-triggered signal bars to barrier_12bar_meta strategy line, using brain_type=lightgbm_v1 with weighted voting mode on barrier_12bar_meta_binary_cls contract. Added to ALL_GROUPS. | feature |
| FIX-20260524-036 | 2026-05-24 | cursor-agent | — | BARRIER_GROUP contract name updated survival_barrier_2.0sl_3.5tp_12bar→survival_barrier_3.0sl_1.5tp_12bar. Description updated: Huber frozen, Binary_Cls_V1 shadow monitoring. Brain_types comment updated. | RC-09 |
| FIX-20260530-060 | 2026-05-30 | cursor-agent | — | Strangler Fig #2: _compute_contract_group_consensus (151 lines) → core/parliament/group_consensus.py. | RC-08 |
| FIX-20260602-052 | 2026-06-02 | cursor-agent | — | **Single-brain consensus bug**: `contract_groups.py _compute_weighted()` self-normalization — when only 1 brain votes, `consensus_base = weight/weight = 1.0` regardless of raw confidence (e.g. 0.34→1.0). `confidence_threshold` gate bypassed for all single-brain strategies (BTC, m15_swing until V3). Fix: single-brain path uses raw confidence directly. | RC-06 |
| FIX-20260602-060 | 2026-06-02 | cursor-agent | — | M15_SWING_GROUP brain_types documented lightgbm_v1 but actual brain is xgboost_v9. Corrected. | RC-09 |
| FIX-20260603-062 | 2026-06-03 | cursor-agent | — | **Unanimous consensus self-normalization**: FIX-052 only covered single-brain. Multi-brain unanimous (2/2 SHORT) still self-normalized to conf=1.0. Now uses weighted-average confidence across agreeing brains. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BARRIER_GROUP`, `MICRO_GROUP`, `MICRO_M15_GROUP`, etc. | strategy_line, live_cycle | Stable |
| `ContractGroupConsensus.compute(proposals, mode)` → `ConsensusResult` | strategy_line | Stable (Layer 1) |

## Verification
```bash
python -m pytest tests/ -k "parliament or consensus or group" -q
```

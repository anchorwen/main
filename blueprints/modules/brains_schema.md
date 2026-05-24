# Brains / Schema

## Purpose
Brain schema version constants and the brain registry (config-driven brain catalog). Defines which brains are available, their types, artifact paths, and deployment scopes.

## Key Files
| File | Role |
|------|------|
| `core/brains/schema_versions.py` | `SCHEMA_BRAIN_DECISION_PROPOSAL` version constant |
| `core/schemas/trading_contracts.py` | `BrainSignal` — Layer 1 frozen dataclass replacing dict-based brain output |
| `core/brains/brain_registry.py` | `BrainEntry` dataclass, `BrainRegistry` singleton |
| `core/brains/online_mlp_model.py` | `OnlineMLP` — PyTorch MLP (40→32→16→3) for online learning |

## Data Flow
```
configs/brains/*.json → BrainRegistry.load() → {brain_id: BrainEntry}
                                                 ↓
                                          BrainFactory.build()
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained; only uses stdlib for BrainRegistry |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/adapters | SCHEMA_BRAIN_DECISION_PROPOSAL | Schema stamp on proposals |
| brains/services | BrainRegistry, BrainEntry | Registry loading and lookup |
| parliament/contract_groups | BrainRegistry | Group brain assignments |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260521-001 | 2026-05-21 | cursor-agent | — | Meta_Stage1_Huber_V1 vote_weight 0.0→0.8:解除物理阻断。V3脑禁用后Huber成为barrier_12bar唯一投票者，但vote_weight=0导致total_weight=0→parliament返回None→策略永远无法开单。0.8保留未来第二脑权重空间。 | RC-09 |
| FIX-20260520-027 | 2026-05-20 | cursor-agent | — | BrainEntry added training_params field (dict[str, Any]) with sl_atr_mult, tp_atr_mult, horizon_bars, min_rr_ratio. BrainRegistry._load_all() parses training_params from JSON. Brain config schema extended: all 14 registry entries backfilled. | RC-09 |
| FIX-20260522-021 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: Brain schema reference updated — `BrainSignal` supersedes `BrainDecisionProposal.prediction` dict. `Direction` type (`Literal["long","short","neutral"]`) and `TradeDirection` type (`Literal["long","short"]`) from `trading_contracts.py` replace loose string direction fields. Schema version constant `SCHEMA_BRAIN_DECISION_PROPOSAL` retained for backward compat. | RC-06 |
| FIX-20260524-023 | 2026-05-24 | cursor-agent | — | BrainRegistry._by_type: changed from dict[str, BrainEntry] to dict[str, list[BrainEntry]] — multiple brains sharing the same brain_type (e.g., multiple lightgbm_v1) no longer overwrite each other. Added get_first_by_type() convenience method for single-entry lookup. Audited all downstream callers (BrainFactory, consensus/voting, leaderboard, dynamic weighter) to iterate list. | RC-06 |
| FIX-20260524-036 | 2026-05-24 | cursor-agent | — | Brain magic→strategy magic alignment: 4 brain configs updated (OU_Params_V6: 90010→90003, MetaLabel: 90013→90014, Huber: 90011→90001, Binary_Cls: 90012→90001). dispatch_magic from brain config is the actual MT5 order magic — misalignment breaks trade journal attribution. | RC-09 |
| FIX-20260524-020 | 2026-05-24 | cursor-agent | — | MEDIUM: Meta_Stage1_Huber_V1 status aligned to probation (was shadow in config, live in comment). Updated configs/brains/meta_stage1_huber_v1.json + configs/live.yaml comment. | RC-09 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainRegistry.get(brain_id)` → `BrainEntry` | BrainFactory, ParliamentService | Stable |
| `BrainEntry` fields: brain_id, brain_type, artifact_path, vote_weight, deployment_scope | All consumers | Stable |
| `SCHEMA_BRAIN_DECISION_PROPOSAL = "brain_decision_proposal.v1"` | All adapters | Stable |

## Verification
```bash
python -c "from core.brains.brain_registry import BrainRegistry; r = BrainRegistry(); print(len(r._entries))"
```

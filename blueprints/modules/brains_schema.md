# Brains / Schema

## Purpose
Brain schema version constants and the brain registry (config-driven brain catalog). Defines which brains are available, their types, artifact paths, and deployment scopes.

## Key Files
| File | Role |
|------|------|
| `core/brains/schema_versions.py` | `SCHEMA_BRAIN_DECISION_PROPOSAL` version constant |
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

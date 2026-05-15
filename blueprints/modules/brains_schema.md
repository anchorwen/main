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

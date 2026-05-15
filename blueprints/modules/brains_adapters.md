# Brains / Adapters

## Purpose
Model inference adapters that wrap diverse brain backends (XGBoost, LightGBM, Transformer, ONNX, OnlineMLP, SGD) behind a uniform `BaseBrainAdapter` interface. Each adapter handles loading artifacts, running inference, and producing `BrainDecisionProposal` outputs.

## Key Files
| File | Role |
|------|------|
| `core/brains/adapters/__init__.py` | Registry: `ADAPTER_REGISTRY`, `BRAIN_TYPE_MAP` |
| `core/brains/adapters/base_adapter.py` | Abstract interface: `load()`, `infer()`, `get_signal()`, `run()` |
| `core/brains/adapters/xgboost_brain_adapter.py` | XGBoost JSON booster adapter |
| `core/brains/adapters/lightgbm_brain_adapter.py` | LightGBM .txt booster adapter |
| `core/brains/adapters/v9_onnx_brain_adapter.py` | V9 institutional ONNX adapter (classification + regression) |
| `core/brains/adapters/transformer_brain_adapter.py` | QuantTransformer ONNX with 64-bar rolling buffer |
| `core/brains/adapters/online_learner_adapter.py` | Dual-backend online learner (SGDClassifier / OnlineMLP) with drift protection |
| `core/brains/adapters/params_brain_adapter.py` | OU process Z-Score from arb_params.json |

## Data Flow
```
brain_entry (config dict) → BrainFactory → adapter.load() → adapter.infer(feature_vector) → adapter.get_signal() → BrainDecisionProposal
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | BrainDecisionProposal | Output type for all adapters |
| contracts/ids | new_proposal_id | Proposal ID generation |
| brains/schema | SCHEMA_BRAIN_DECISION_PROPOSAL | Schema version stamp |
| brains/services | InferenceGuard, run_worker | Subprocess isolation (optional) |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_factory | ADAPTER_REGISTRY, BRAIN_TYPE_MAP | Builds adapters from config |
| brains/services/brain_run_service | BaseBrainAdapter | Runs inference across adapters |
| runtime/signal_pipeline | BrainDecisionProposal | Aggregates brain outputs |

## Known Issues
<!-- Add known issues here with tracking IDs -->

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BaseBrainAdapter.load()` → sets `self._backend` | BrainFactory | Stable |
| `BaseBrainAdapter.infer(feature_vector)` → `dict[str, Any]` | BrainRunService | Stable |
| `BaseBrainAdapter.get_signal(raw_output)` → `BrainDecisionProposal` | BrainRunService | Stable |
| `ADAPTER_REGISTRY` dict format: `{registry_key: adapter_class}` | BrainFactory | Stable |

## Verification
```bash
python -m pytest tests/ -k "brain" -q
python scripts/verify_all_brains.py
```

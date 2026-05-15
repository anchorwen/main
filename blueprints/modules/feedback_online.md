# Feedback / Online

## Purpose
Bridges closed trades from the journal to the online learner for live model updates. Handles feature-label extraction, validation sample collection, and partial_fit triggering with drift protection.

## Key Files
| File | Role |
|------|------|
| `core/feedback/online_feedback_hook.py` | `OnlineFeedbackHook` — journal→feature_store→partial_fit bridge |
| `core/feedback/param_optimizer.py` | `suggest_parameters()` — hyperparameter tuning suggestions |

## Data Flow
```
Trade journal (closed trades) → OnlineFeedbackHook
    ↓
LocalFeatureStore (get features at entry time)
    ↓
OnlineLearnerAdapter.partial_fit(features, label)
    ↓
Drift check → snapshot / rollback / freeze
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| features | LocalFeatureStore | Feature retrieval at trade entry time |
| brains/adapters | OnlineLearnerAdapter | Model update target |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | OnlineFeedbackHook | Post-trade feedback cycle |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OnlineFeedbackHook.process_closed_trades(journal_entries)` → `int` (updates applied) | live_cycle | Stable |
| `suggest_parameters(brain_id, degradation_metrics)` → `dict` | daily_ops | Evolving |

## Verification
```bash
python -m pytest tests/ -k "online or feedback" -q
```

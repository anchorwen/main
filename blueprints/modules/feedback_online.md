# Feedback / Online

## Purpose
Bridges closed trades from the journal to the online learner for live model updates. Handles feature-label extraction, validation sample collection, and partial_fit triggering with drift protection.

## Key Files
| File | Role |
|------|------|
| `core/feedback/online_feedback_hook.py` | `OnlineFeedbackHook` — journal→feature_store→partial_fit bridge |
| `core/feedback/experience_replay.py` | `ExperienceReplayBuffer` — R-weighted shuffle buffer for mini-batch SGD |
| `core/feedback/param_optimizer.py` | `suggest_parameters()` — hyperparameter tuning suggestions |

## Data Flow
```
Trade journal (closed trades) → OnlineFeedbackHook
    ↓
LocalFeatureStore (get features at entry time)
    ↓
ExperienceReplayBuffer (collect 20 trades → R-weight → Fisher-Yates shuffle)
    ↓
OnlineLearnerAdapter.partial_fit(features, label) × N shuffled samples
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
| FIX-20260523-008 | 2026-05-24 | cursor-agent | — | Track 3d: OnlineFeedbackHook now accepts ConformalCalibrator — updates (p_win, label) on each closed trade to feed the adaptive OU threshold | calibrator data pipeline for Track 3d |
| FIX-20260523-007 | 2026-05-23 | cursor-agent | — | Mini-batch online learning: ExperienceReplayBuffer with EMA R-weighting, Fisher-Yates shuffle, class imbalance warning | single-sample SGD ignored trade magnitude; consecutive duplicates risked catastrophic forgetting |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OnlineFeedbackHook.process_closed_trades(journal_entries)` → `int` (updates applied) | live_cycle | Stable |
| `suggest_parameters(brain_id, degradation_metrics)` → `dict` | daily_ops | Evolving |

## Verification
```bash
python -m pytest tests/ -k "online or feedback" -q
```

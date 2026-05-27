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
| FIX-20260527-003 | 2026-05-27 | cursor-agent | — | Remove hardcoded brain ID fallback: `scripts/online_feedback_hook.py` `brain_entry.get("brain_id", "Online_SGD_V1")` → `brain_entry["brain_id"]` — direct key access. If config lacks required `brain_id` field, KeyError surfaces immediately. Also registered `scripts/online_feedback_hook.py` in MODULE_SOURCE_MAP under `feedback_online`. | RC-09 |
| FIX-20260523-008 | 2026-05-24 | cursor-agent | — | Track 3d: OnlineFeedbackHook now accepts ConformalCalibrator — updates (p_win, label) on each closed trade to feed the adaptive OU threshold | calibrator data pipeline for Track 3d |
| FIX-20260523-007 | 2026-05-23 | cursor-agent | — | Mini-batch online learning: ExperienceReplayBuffer with EMA R-weighting, Fisher-Yates shuffle, class imbalance warning | single-sample SGD ignored trade magnitude; consecutive duplicates risked catastrophic forgetting |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260524-027 | 2026-05-24 | cursor-agent | — | Latent bug: ExperienceReplayBuffer.flush() computed avg_weight AFTER self._buffer.clear() (always 0). Moved avg_weight computation before clear, removed dead `if False` guard. | RC-03 |
| FIX-20260524-037 | 2026-05-24 | cursor-agent | — | C1: Look-ahead bias — feature lookup now uses open order entry_time (via open_message_id→recorded_at index) instead of close_time. ExperienceReplayBuffer.reset() added to discard contaminated samples. C4: Timestamp comparison upgraded from string to datetime for accurate trade ordering. | RC-03 |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M1: _invert_score() now inverts dimension scores (sharpe/wr/pf/pnl/dd) when composite is inverted, keeping sub-scores consistent. | boundary-error |
| FIX-20260524-041 | 2026-05-24 | cursor-agent | — | EMA circular reference fix: _compute_weight() now computes weight against the *previous* running mean before updating it. Previously r_abs pulled the mean toward itself then divided by it — self-bias loop. | RC-03 |
| FIX-20260524-028 | 2026-05-24 | cursor-agent | — | Perf: _find_feature_vector() O(n)→O(log n) — replaced per-trade full-file linear scan with pre-built in-memory index (symbol→sorted list of (unix_ts, values)) + bisect_left nearest-neighbor lookup. Eliminates 100×10K=1M iterations in typical feedback cycle. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OnlineFeedbackHook.process_closed_trades(journal_entries)` → `int` (updates applied) | live_cycle | Stable |
| `suggest_parameters(brain_id, degradation_metrics)` → `dict` | daily_ops | Evolving |

## Verification
```bash
python -m pytest tests/ -k "online or feedback" -q
```

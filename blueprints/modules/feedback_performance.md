# Feedback / Performance

## Purpose
Brain performance tracking and quality scoring: composite score computation, execution outcome recording, and unified quality verdicts consumed by brain weighting, leaderboard, and governance.

## Key Files
| File | Role |
|------|------|
| `core/feedback/brain_performance_tracker.py` | `BrainPerformanceTracker` — rolling composite score + outcome records |
| `core/feedback/brain_quality_engine.py` | `BrainQualityEngine` — unified quality verdict (tier, score, vote weight) |
| `core/feedback/decision_scorer.py` | `DecisionScorer` — 4-dim scoring (fill, timing, accuracy, risk compliance) |
| `core/feedback/outcome_collector.py` | `OutcomeCollector` — collects execution outcomes |
| `core/feedback/feedback_loop.py` | `FeedbackLoop` — orchestrates outcome→score→track→governance chain |
| `core/feedback/performance_analytics.py` | Performance aggregation utilities |
| `core/constants.py` | `PERFORMANCE_WINDOW` (100 trades) |

## Data Flow
```
Execution events → OutcomeCollector → DecisionScorer → BrainPerformanceTracker
                                                              ↓
                                                     BrainQualityEngine
                                                              ↓
                                             BrainQualityVerdict (tier, score, weight)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained; all imports intra-feedback |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/dynamic_brain_weighter | BrainPerformanceTracker, BrainPnLMetrics | Vote weight computation |
| brains/services/brain_leaderboard | BrainQualityEngine | Composite rankings |
| brains/services/brain_promotion | BrainQualityEngine (indirect) | Promotion decisions |
| runtime/live_cycle | FeedbackLoop | Post-trade feedback cycle |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add scripts/trade_quality_report.py. Mypy fix (1→0 — Counter[str] annotation for rejected_reasons). | RC-02 |
| FIX-20260524-011 | 2026-05-24 | cursor-agent | — | Variable shadowing fix: renamed outcome→resolved in accepted/rejected label blocks. Mypy inferred outcome as str from earlier loop, breaking dict assignment. | RC-02 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260514-004 | 2026-05-14 | cursor-agent | a4a1005 | Add marginal tier (score 10-20), fix WR cliff with smooth ramp, fix DD component when PnL<=0, add marginal to all tier mappings | boundary-error |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainQualityEngine.evaluate(brain_id)` → `BrainQualityVerdict` | DynamicBrainWeighter, BrainLeaderboard | Stable |
| `BrainPerformanceTracker.record(brain_id, outcome)` → `None` | FeedbackLoop | Stable |

## Verification
```bash
python -m pytest tests/ -k "feedback or performance or tracker" -q
```

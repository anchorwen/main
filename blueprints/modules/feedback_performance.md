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
| FIX-20260601-033 | 2026-06-01 | cursor-agent | — | **feedback_loop symbol threading**: `feedback_loop.py` + `daily_ops.py` now accept/forward `--symbol`/`symbol` param. Feedback ingestion no longer hardcoded to XAUUSDc. | RC-09 |
| FIX-20260524-037 | 2026-05-24 | cursor-agent | — | C3: Probation floor (0.5 cap) reordered to after Sharpe adjustment and drawdown penalty — previously Sharpe bonus could push weight above the 0.5 cap. | RC-03 |
| FIX-20260524-038 | 2026-05-24 | cursor-agent | — | H7: Auto-retire hard gate fixed — pf==0 edge case (previously `pf > 0 and pf < 0.60` missed pf==0, now `pf < 0.60`). | boundary-error |
| FIX-20260524-041 | 2026-05-24 | cursor-agent | — | Sharpe annualization fix: returns are per-trade not daily, but _sharpe_ratio/_sortino_ratio hardcoded *sqrt(252) and /252. Now derives annual_factor from actual trade timestamps (N/span_days*365); falls back to 1.0 without timestamps. | RC-06 |
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add scripts/trade_quality_report.py. Mypy fix (1→0 — Counter[str] annotation for rejected_reasons). | RC-02 |
| FIX-20260524-011 | 2026-05-24 | cursor-agent | — | Variable shadowing fix: renamed outcome→resolved in accepted/rejected label blocks. Mypy inferred outcome as str from earlier loop, breaking dict assignment. | RC-02 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260514-004 | 2026-05-14 | cursor-agent | — | Add marginal tier (score 10-20), fix WR cliff with smooth ramp, fix DD component when PnL<=0, add marginal to all tier mappings. | RC-05 |
| FIX-20260527-002 | 2026-05-27 | cursor-agent | — | Brain performance data contamination fix: `ingest_journal_to_tracker()` replaced `_find_brains_by_time()` (which grouped ALL consensus brains → identical records for 5 brains) with per-strategy `brain_ids` from open journal entries. Governance path upgraded to PnL-first via BrainPnLStore. Contaminated tracker records for 5 brains cleaned. | RC-11 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainQualityEngine.evaluate(brain_id)` → `BrainQualityVerdict` | DynamicBrainWeighter, BrainLeaderboard | Stable |
| `BrainPerformanceTracker.record(brain_id, outcome)` → `None` | FeedbackLoop | Stable |

## Verification
```bash
python -m pytest tests/ -k "feedback or performance or tracker" -q
```

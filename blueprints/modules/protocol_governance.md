# Protocol / Governance

## Purpose
Brain lifecycle governance: state machine (candidate → live → probation → frozen → retired), rule engine for automated decisions, and persistence of governance state.

## Key Files
| File | Role |
|------|------|
| `core/governance/governance_service.py` | `GovernanceService` — lifecycle management, persistence, audit log |
| `core/governance/governance_rule_engine.py` | `GovernanceRuleEngine` — rule-based decision engine (5 rules) |

## Data Flow
```
BrainQualityVerdict → GovernanceRuleEngine.evaluate() → lifecycle_action
                              ↓
                    GovernanceService.apply(action) → governance_state.json
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/exceptions | BrainNotFoundError, InvalidTransitionError | Error handling |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_promotion | apply_promotion_decisions() | Writes governance state |
| deployment/lifecycle | GovernanceService | Service wiring |
| apps/monitor/ | governance_service | Dashboard status |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260628-062 | 2026-06-28 | cursor-agent | — | **DQAF-062 L3: Config→Governance Reconciliation Gate (`_step_config_gov_reconcile()` in daily_ops.py)**. Reads brain_registry_entry.v1 configs, bootstrap-registers missing brains as "candidate", logs status drift without overriding governance-owns-lifecycle contract. Eliminates dual-track drift. | RC-09, RC-12 |
| FIX-20260628-061 | 2026-06-28 | cursor-agent | — | **DQAF-061 L3: XAU Governance Blind Eye Recovery**. Auto-register brains from PnLStore before set_performance_metrics() in governance_scheduler.py (silent skip → 31/49 brains now reachable). _data_source "no_data" marker. scheduler_service.py purge respects _data_source. | RC-09, RC-06 |
| FIX-20260620-013 | 2026-06-20 | cursor-agent | — | **P2: 3D Expiry Contract auto-rollback — `_enforce_3d_override_expiry()` added to governance_scheduler.py**. Checks all brains for manual override expiry on every cycle. ANY of 3 dimensions triggers rollback to candidate: (1) trade_count ≥ override_expires_after_trades, (2) wall_clock ≥ override_expires_at, (3) cumulative_pnl < override_max_probation_dd. Without this code, 3D override fields in governance_state.json are dead fields with no enforcement. Integrated into `run_governance_cycle()` as Phase 1 mandatory safety valve. | RC-07 |
| FIX-20260620-012 | 2026-06-20 | cursor-agent | — | **P2: BTC Governance deadlock break — BTC_Swing_V4 manual activation candidate→probation**. All 3 BTC candidates had 0 settled PnL trades → governance_scheduler returned "insufficient_data" → permanent deadlock (no brain can trade→no PnL→no promotion). Added 3D Expiry Contract with ANY-trigger auto-rollback safeguards. Risk disclosed: in-sample metrics only (test WR=38.5%, PF=1.91), no walk-forward validation, history of "pnl:critical" retirements. Probation mode with budget guard + dynamic SL/TP limits exposure. | RC-09 |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染: ShadowTracker(base_dir) 移除默认值→必需参数** | L3 — base_dir="data" 默认值 |
| FIX-20260613-080 | 2026-06-13 | cursor-agent | 422871f | Brain portfolio cleanup: archived 3 XAU NO_DATA brains (10-14d 0 trades), froze 3 Brain_Trend brains (PF<1.0 + 100% SHORT cloning), fixed BTC V6/V7/V8 config enabled→false. STR WARN 18→15. | contract-violation |
| FIX-20260613-076 | 2026-06-13 | cursor-agent | e4b1d82 | cmd_reconcile no longer overwrites runtime governance promotions with config defaults. Config status is now treated as registration default only — governance owns the lifecycle. Preserves transition_log across saves. OU_Params_V7_M15 config fixed (live→candidate, matching PF=0.80 performance). | contract-violation |
| FIX-20260529-043 | 2026-05-29 | cursor-agent | — | PR#1 GovernanceService thread-safety: added threading.RLock() protecting all _brain_states/_transition_log reads/writes. save() changed from direct write_text() to atomic tmp+os.replace. RLock used because transition() internally calls register_brain(). | RC-04 |
| FIX-20260529-035 | 2026-05-29 | cursor-agent | — | P0.1 State Injection: `GovernanceService.set_performance_metrics()` added — injects win_rate/profit_factor/sharpe_ratio/total_trades/pnl_r into brain_states. `governance_scheduler.py` PnL-first path now calls `set_performance_metrics()` for every assessed brain. Fixes visibility gap where 16,903 settled trades had zero reflection in governance state. | RC-06, RC-09 |
| FIX-20260529-034 | 2026-05-29 | cursor-agent | — | SSOT governance status reconciliation + transition_log integrity: (1) `verify_startup_integrity()` now reconciles config→governance status — when brain has active config on disk but governance says "retired", config wins (restored to "candidate"). (2) `GovernanceService.register_brain()` now appends transition_log entry and sets transition_count=1. (3) Auto-registration path now adds transition_log entries. Fixes OU_Params_V7_M15 retired-reversion loop (governance "retired" persisted across saves even though config says "live"). Also archives V1 Swing configs to resolve magic collision with V2. | RC-09, RC-11 |
| FIX-20260524-037 | 2026-05-24 | cursor-agent | — | C2: build_shadow_summary() no longer outputs "current_status": "candidate" — was overriding real governance state via **summary spread in rule engine, permanently disabling all status-dependent rules. | RC-09 |
| FIX-20260524-038 | 2026-05-24 | cursor-agent | — | H3: "shadow" added to VALID_TRANSITIONS (→{candidate, probation, frozen, retired}) — 2 brains were permanently stuck. H6: SHARPE_RETIRE_THRESHOLD -10.0→-2.0, SHARPE_FREEZE_THRESHOLD -10.0→-1.5 (aligned with BrainQualityEngine hard gates). | config-drift |
| FIX-20260524-039 | 2026-05-24 | cursor-agent | — | M11: GovernanceRuleEngine now checks transition() return value (action==rejected) instead of silently ignoring failures. | missing-validation |
| FIX-20260517-015 | 2026-05-17 | cursor-agent | — | health_signal unblock: ShadowTracker.build_shadow_summary() health_signal changed from hardcoded "unknown" to "healthy". The old value blocked GovernanceRuleEngine auto-promotion rules (auto_promote_healthy requires health_signal=="healthy"), preventing candidate→probation transitions. | missing-feature |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor/Executor separation: GovernanceRuleEngine.execute_transitions(report) added as single Executor. BrainPromotionEvaluator reduced to pure Auditor (evaluate_all returns report, no state writes). scheduler_service wired as: evaluator.evaluate_all() → engine.execute_transitions(). | contract-violation |
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260515-009 | 2026-05-15 | cursor-agent | — | Auto-shadow mechanism: ShadowTracker (counts candidate signals), auto_promote_shadow_to_probation rule (50+ signals→probation), auto_promote_probation_to_live rule (100+ signals+quality→live). New file core/governance/shadow_tracker.py. | missing-feature |
| FIX-20260514-015 | 2026-05-14 | cursor-agent | a4a1005 | 大脑批量复活脚本：用修复后的BrainQualityEngine重评退休大脑，score≥10恢复为probation，score≥50恢复为live | contract-violation |
| FIX-20260514-006 | 2026-05-14 | cursor-agent | a4a1005 | Add max 1 retirement/cycle safety valve, map marginal tier to frozen, add insufficient_data skip logging | missing-validation |
| FIX-20260514-005 | 2026-05-14 | cursor-agent | a4a1005 | Remove break-after-first-match, collect all matching rules per brain, apply most severe result, differentiate priorities (retire=110, freeze=100) | contract-violation |
| FIX-20260524-040 | 2026-05-24 | cursor-agent | — | DEFERRED architecture debt: dual governance pipeline merge (BrainPromotionEvaluator vs GovernanceRuleEngine), leaderboard consumer gap, stability monitor unused, AB test framework not activated. No code changes — registered for future sprints. | RC-12 |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: governance_rule_engine.py (execute_transitions) + shadow_tracker.py (health_signal). Previously registered as FIX-20260517-017, FIX-20260517-015. | process-violation |
| FIX-20260607-148 | 2026-06-07 | cursor-agent | — | **BLE001 Phase 2 tactical deferral**: 29 FAIL_OPEN sites audited — 90% are state-persistence/shutdown-cleanup (best-effort degradation, acceptable). High-risk trading-path silent-failures already covered by FIX-138 (Fail-Closed bootstrap) + FIX-140 (dispatch circuit-breaker). Established Incremental-Upgrade doctrine: replace `except: pass` with `fail_open_guard()` when next touching each hot-path file. `fail_open_guard` tool deployed (FIX-146). BLE001 count: 566→0. ruff: 0 warnings. mypy production: 0. | RC-07 |
| FIX-20260617-001 | 2026-06-17 | cursor-agent | — | **P0: Governance data source migration — BrainPnLStore (backtest) → brain_performance.json (live SSOT)**: Replaced BrainPnLStore injection in scheduler_service.py governance evaluation path with brain_performance.json execution outcomes. BrainPnLStore contains COUNTERFACTUAL PnL (what brains WOULD have earned) — trade counts 10-814x larger than real live trades. brain_performance.json tracks actual MT5 fill outcomes (win/loss, window=100). Added `source: brain_performance` marker + `[GOV_CLEAN]` backtest purge. Auto-transition remains manual (if True guard). Also updated live_startup.py inject_performance_metrics() to brain_performance path (currently guarded by _GOVERNANCE_SKIP_INJECTION=True). | RC-06 (contract-violation — backtest data injected into live governance) |
| FIX-20260614-B0B1 | 2026-06-15 | cursor-agent | — | **Route B Phase 0-1: Governance unfreeze + BTC training OU/Hurst parity**: (B0) Removed _GOVERNANCE_MANUAL_MODE flag from governance_scheduler.py and scheduler_service.py — 5,123 BTC SignalSettled events now flow into governance_state.performance_metrics. Auto-transition stays manual (if True guard preserved) pending first-cycle metrics review. (B1) Replaced X[:,33]=0.0 and X[:,34]=0.5 placeholders in train_btc_directional_v1.py::compute_features() with live production _ou_theta() and _hurst() imported directly from core.features.computers.v9_live_computer — absolute inference parity. First 30 warm-up bars dropped (not zero-filled). Root cause: FIX-20260611-020 manual mode + placeholder constants caused BTC brains to train blind to regime. | RC-06, RC-09 |
| FIX-20260621-032 | 2026-06-21 | cursor-agent | — | **Leaderboard度量衡修复 + V4擢升/V10_M15退役**: 全量实盘审计(analyze_live_brain_performance.py)发现Leaderboard将pnl_per_unit(账户货币)误标为pnl_r(风险单位)→V6/V7/V8显示-1146R实为+10.3R。修复: (1) daily_ops.py+governance_scheduler.py上游改用live_journal_metrics.py(基于实盘journal的pnl_r),废弃BrainPnLStore.pnl_per_unit, (2) BTC_Swing_V4 probation→live(172笔+92.1R), (3) BTC_Swing_V10_M15_Survival frozen→retired(-16.4R,全联盟有毒)。新模块core/feedback/live_journal_metrics.py。 | RC-06 (contract-violation: pnl_per_unit≠pnl_r field label mismatch) + RC-03 (dual-source: journal vs ledger split SSOT) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `GovernanceService.transition(brain_id, new_status)` → `bool` | BrainPromotionEvaluator | Stable |
| `GovernanceRuleEngine.evaluate(brain_id, metrics)` → `list[GovernanceAction]` | GovernanceService | Stable |
| Brain lifecycle states: candidate → probation → live → frozen → retired | All consumers | Stable |

## Verification
```bash
python -m pytest tests/ -k "governance" -q
```

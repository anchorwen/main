# Governance / Rules Engine

## Purpose
Declarative brain lifecycle governance: evaluates brain health signals against 9 built-in rules and automatically transitions brains through the lifecycle state machine (candidate → probation → live → frozen → retired). Separates audit (read) from execution (write): `BrainPromotionEvaluator` reads, `GovernanceRuleEngine.execute_transitions()` writes.

## Key Files
| File | Role |
|------|------|
| `core/governance/governance_rule_engine.py` | `GovernanceRuleEngine` — declarative rule engine with 9 default rules; `GovernanceRule` — single rule (condition_fn + action_fn + priority) |
| `core/governance/governance_service.py` | `GovernanceService` — thread-safe state machine with valid transition map, persistence (save/load), and feedback signal processing |
| `core/governance/shadow_tracker.py` | `ShadowTracker` — tracks shadow brain signal statistics for promotion eligibility |

## Data Flow
```
BrainPerformanceTracker → brain_summaries (per-brain health metrics)
                              ↓
              GovernanceRuleEngine.evaluate(brain_summaries, system_context)
                              ↓
              Each rule: condition_fn(ctx) → True? → action_fn(ctx)
                              ↓
              All matching rules → _most_severe() picks winning transition
                              ↓
              GovernanceService.transition(brain_id, new_status)
                              ↓
              Audit log: log_governance_signal() per fired rule
```

## Lifecycle State Machine
```
  shadow ──→ candidate ──→ probation ──→ live ──→ retired
    │           │              │            │          ↑
    │           │              │            │          │
    └───────────┴──────────────┴────────────┴──────────┘
                    frozen (can re-enter probation)
```

Valid transitions:
- shadow → {candidate, probation, frozen, retired}
- candidate → {live, probation, retired}
- live → {probation, frozen, retired}
- probation → {live, frozen, retired}
- frozen → {probation, retired}
- retired → {} (terminal)

## Built-in Rules (9 rules, sorted by priority)
| Priority | Rule | Condition | Action |
|:--------:|------|-----------|--------|
| 120 | auto_retire_repeated_frozen | freeze_count ≥ 3 | → retired |
| 110 | auto_freeze_negative_sr | sharpe < -1.0, ≥50 samples, status=live | → frozen |
| 100 | auto_freeze_critical | health_signal=critical, ≥10 samples | → frozen |
| 90 | auto_demote_degraded | health_signal=degraded, ≥15 samples, status=live | → probation |
| 85 | auto_promote_shadow_to_probation | ≥50 shadow signals, min 5 long/5 short, avg conf≥0.50 | → probation |
| 80 | auto_demote_probation_to_frozen | status=probation, health degraded/critical, freeze_count<3 | → frozen |
| 75 | auto_promote_probation_to_live | status=probation, ≥100 samples, healthy/stable, composite≥0.55 | → live |
| 50 | auto_promote_healthy | health_signal=healthy, composite≥0.75, ≥30 samples | → live |
| 40 | unfreeze_recovered | status=frozen, health stable/healthy, recommendation≠freeze | → probation |

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/exceptions | BrainNotFoundError, InvalidTransitionError | Strict transition errors |
| infrastructure/distributed_lock | FileLock | Cross-process safe state persistence |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | GovernanceRuleEngine.with_default_rules() | Daily ops governance evaluation |
| scripts/daily_ops | GovernanceService | Brain lifecycle administration |
| deployment/brain_lifecycle_manager | GovernanceService | Startup brain registration and validation |

## Known Issues
- `with_default_rules()` closures capture `gs` (governance_service) from outer scope — if service is replaced without re-creating rules, stale reference persists.
- `_most_severe()` returns the first rule at max severity; if two rules have equal severity but conflicting transitions, the higher-priority rule wins silently.

## Fix History
| Fix ID | Date | Summary | Root Cause |
|--------|------|---------|------------|
| FIX-20260629-182 | 2026-06-29 | **P0-2: Promote 5 candidate brains → probation** (DQAF-175). Swing_V9_H1_V2 (PF=1.93, n=8,419), Barrier_V9_12B_V2 (PF=2.42, n=1,007), Brain_Trend_V10_M30 (PF=7.42, n=1,048), Brain_Trend_M30_V2 (PF=9.94, n=862), Brain_Trend_M30_V1 (PF=1.32, n=437). All met shadow promotion criteria but stuck at candidate due to 4-layer governance blockage (FIX-183). Manual catch-up promotion; governance pipeline restored for future transitions. | L2 — 4-layer governance pipeline blockage: hardcoded guard + missing shadow data + SSOT reconciliation + chicken-egg live samples |
| FIX-20260621-029 | 2026-06-21 | **Minimum Live Sample Gate (N=50) — 终结 live↔probation 死循环**: 统一数据源为 brain_performance.json，低于 50 条实盘执行记录的脑旁路所有升/降级决策。消除 governance_state.json 全时 PnL vs brain_performance.json 滚窗双轨数据源冲突。Supersedes FIX-024 (deferred hysteresis). | RC-12 (insufficient-data — small-sample noise) + RC-03 (dual-source — split SSOT) |
| FIX-20260801-012 | 2026-08-01 | **Observation hold (grace period)**: `observation_hold_until` in brain config `governance` block (L1 SSOT). `GovernanceRuleEngine.set_observation_holds()` + `_hold_blocked()` refuse ANY automated demotion during an active hold — on the sole writer `execute_transitions()` AND the declarative rule path `evaluate()`. `load_observation_holds()`/`resolve_brains_dir()` in governance_evaluator.py; attached once per cycle in both deployment paths. BTC_Swing_V4 hold until 2026-08-03T23:59:59Z. | L3 — policy conflict: evaluator throttle (PF<0.80) vs config floor vs daily_ops pull-back oscillated V4; IC observation window needs explicit code priority |
| FIX-20260801-011 | 2026-08-01 | **SSOT governance unification**: extract brain_performance Auditor→Executor into `governance_evaluator.evaluate_governance_state()` shared by container scheduler_service + bare-metal launcher 60s thread. Amputate live_intent_loop's DEPRECATED apply_promotion_decisions block. Route daily_ops PnL transitions through `execute_transitions()` (sole writer). Mark apply_promotion_decisions TODO. Migrate governance_events.jsonl to SSOT runner. | L2 — integration gap: FIX-20260611-001 patched launcher with direct writer; daily_ops direct transition = third rail; multiple data sources → V4 oscillation |: `run_governance_cycle()` was PnL-first only — brains without settled PnL (shadow/candidate) were never evaluated and never promoted. Added `_promote_shadow_brains()` helper + call sites in both PnL-first and fallback paths. Scans shadow/candidate brains via `ShadowTracker.all_candidate_metrics()`, applies Rule 85 thresholds (≥50 signals, ≥5 long/short, avg conf≥0.50), and transitions eligible brains to probation. Closes the local deployment gap: cloud `scheduler_service.py` already calls `GovernanceRuleEngine.with_default_rules()` → Rule 85, but local `daily_ops.py` bypassed the rule engine entirely. | L2 — integration gap: ShadowTracker existed but was never wired into local governance path |
| FIX-20260629-173 | 2026-06-29 | **V4 RR-Adjusted Sharpe 阈值校准 0.8→0.4**: 趋势跟踪策略 (高 R:R 低 WR) 的实盘 Sharpe 天然衰减是回测过拟合+执行摩擦的结构性结果，非策略失效. SHARPE_RR_ADJUSTED_MIN=0.4 是对实盘摩擦的让步 (PF≥1.1+Sharpe>0+≥50 trades 三重防护). V4 (WR=35.5% PF=1.147 SR=0.545 +42.4R 298笔) 分类从 probation/degraded→live/stable. 闭合 2026-06-28 3次 live↔probation 振荡 (DQAF-063 治标不治本). | L2 — 回测 SR=1.08 衰减至实盘 SR=0.545 被误判为 degraded, RR-adjusted 通道 0.8 阈值基于回测数据未考虑实盘摩擦 |
| FIX-20260629-171 | 2026-06-29 | **Ghost registration defense-in-depth**: Cleaned 83/96 ghost entries from BTC governance transition_log. Added `_valid_brain_ids` whitelist + `resolve_valid_brain_ids()` static method to GovernanceService. `register_brain()` now rejects non-whitelisted brain_ids. `load()` auto-resolves valid IDs from config SSOT. Closes FIX-20260628-168 gap (only covered governance_scheduler.py). | L2 — defense gap: 5+ registration paths (daily_ops, scheduler_service, brain_lifecycle_manager) bypassed FIX-168 ghost filter |
| FIX-20260627-152 | 2026-06-27 | **RR-adjusted governance channel for low-WR high-RR strategies**. Added exemption before probation catch-all: PF≥1.3 + SR≥0.8 + N≥50 → live, bypassing WR≥45% threshold. V4 (WR=39.1%, PF=1.36, SR=1.08, +81.21R) auto-promoted probation→live on first cycle post-fix. ReB: `WR_THRESHOLD_ONE_SIZE_FITS_ALL`. | L3 — design defect: one-size-fits-all WR threshold incompatible with swing strategies (avg_win/avg_loss > 2:1) |
| FIX-20260626-141 | 2026-06-26 | **V4 governance state correction: probation→live (DQAF-20260626-002)**. Governance log-replay bug: stale live→probation entry at 2026-06-25T10:02:45 was replayed after FIX-20260625-136 IC_MANDATE promotion (12:00). brain_states used log-position order instead of timestamp order. Added IC_MANDATE corrective transition restoring live status. | L2 — append-order vs timestamp-order in governance state rebuild |
| FIX-20260617-001a | 2026-06-17 | add save() + auto-register to governance pipeline | RC-07 |
| FIX-20260617-001 | 2026-06-17 | P0 data integrity — governance backtest purge | RC-03 |
| FIX-20260611-017 | 2026-06-11 | auto_freeze_negative_sr: hard stop-loss for negative Sharpe | RC-07 |
| FIX-20260529-034 | 2026-05-29 | register_brain() appends transition_log entry for audit trail | RC-07 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `GovernanceService.transition(brain_id, new_status, reason)` | rule_engine, daily_ops | Stable |
| `GovernanceRuleEngine.evaluate(brain_summaries, system_context)` | live_cycle, daily_ops | Stable |
| `GovernanceRuleEngine.execute_transitions(report, dry_run)` | deployment | Stable |

## Verification
```bash
python -m pytest tests/ -k "governance" -q
```

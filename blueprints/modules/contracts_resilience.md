# Contracts / Resilience (UGR v3.1)

## Purpose
Zero-tolerance resilience architecture: CapResult[T] with scope-gated proof tokens,
Phantom Contracts for production audit, TypedClock with three incompatible types,
hash-chained WAL, and dual-isolation scheduler. This module implements the UGR
v3.1 architectural transformation described in the master plan.

## Key Files
| File | Role | Status |
|------|------|--------|
| `core/contracts/cap_result.py` | CapResult[T] + _SuccessProof (scope-gated proof token) | ✅ UGR-A01 |
| `core/contracts/phantom_contract.py` | Phantom Contracts decorator + PhantomStub | ✅ UGR-B01 |
| `core/runtime/typed_clock.py` | MonotonicInstant, WallInstant, Duration — three incompatible time types | ✅ UGR-A02 |
| `core/data/write_ahead_log.py` | Hash-chained WAL with checkpoint + rotation support | ✅ UGR-A03 + UGR-B02 |
| `core/data/lifecycle_manager.py` | In-memory eviction lifecycle | PLANNED |
| `core/runtime/supervised_scheduler.py` | THREAD + PROCESS dual-isolation scheduler | ✅ UGR-A04 |
| `core/contracts/adapters.py` | Adapter bridges — CapResult ↔ legacy, TypedClock ↔ float, fault tolerance helpers | ✅ UGR-A07 |
| `core/observability/invariant_engine.py` | 15 binary predicate invariants + WAL integrity check | ✅ UGR-A06 |
| `core/observability/live_alert_hub.py` | AlertBus — AlertStormDetector + get_health_status (storm protection + self-monitoring) | ✅ UGR-A05 |
| `scripts/verify_capresult_ast.py` | AST scanner: 5 detectors (DynamicCall, CapResultOk, RawAccess, FailOpenGuard, ProofLeak) with --enforce mode | ✅ UGR-B03 |
| `scripts/verify_phantom_contracts.py` | Phantom offline verifier (WAL state reconstruction + replay) — state-aware with StateProjector, incremental mode | ✅ UGR-B04 |

## Data Flow
```
Kernel.success_scope() → _SuccessProof
    ↓
CapResult[T].ok(value, proof) — proof must be valid
    ↓
Phantom Contracts (__debug__: assert; -O: stub → WAL)
    ↓
WriteAheadLog (hash-chained, fsync'd)
    ↓
verify_phantom_contracts.py (offline replay against WAL state)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| data_infrastructure | WriteAheadLog, WALConfig | Phantom stubs written to WAL |
| runtime_live | typed_clock, supervised_scheduler | Clock types + task scheduling |
| monitor_dashboard | live_alert_hub | AlertBus integration for phantom violations |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime_live | CapResult, Kernel, Clock | Core resilience types |
| execution_guards | CapResult | Gate results use CapResult |
| execution_orders | CapResult | Strategy execution results |
| deployment_lifecycle | CapResult | Lifecycle operations |

## Known Issues
- Phantom Contracts state reconstruction protocol: ✅ UGR-B04 — StateProjector implemented with state completeness assertions, handler priority/conflict detection, timeout/overflow protection.
- InvariantEngine currently shadow-only — no circuit-breaking integration yet.
- AlertStormDetector rate decay is window-based; may need persistent state for long-running storms.
- Production @phantom decorator wiring deferred to UGR-A09 (predicate signatures require per-site function adaptation).

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260624-085 | 2026-06-24 | cursor-agent | — | **UGR-A05: AlertBus Storm Protection + Self-Monitoring**. Added AlertStormDetector (sliding-window rate limiter with NORMAL→WARNING→STORM states), storm summary emission, self-monitoring via get_health_status(), and integrated into evaluate_and_dispatch(). 40 tests. | RC-12 — missing-feature: no storm protection or self-monitoring in alert bus |
| FIX-20260624-088 | 2026-06-24 | cursor-agent | — | **UGR-B01: Phantom Contracts prototype — PhantomStub + @phantom decorator + offline verifier**. Created phantom_contract.py (PhantomStub, PhantomSerializer, PredicateRegistry, @phantom decorator with hot_path flag, MVP predicate risk_budget_non_negative) + verify_phantom_contracts.py (offline WAL replay verifier with dedup, version check, violation detection). 27 tests. | RC-12 — missing-feature: no phantom contract audit mechanism existed for production -O mode |
| FIX-20260624-086 | 2026-06-24 | cursor-agent | — | **UGR-A06: InvariantEngine — 15 binary predicate invariants**. Created InvariantEngine with 15 shadow-mode invariants (WAL integrity, circuit breaker, position count, risk budget, duplicate tickets, feature freshness, live brain count, governance state, calibrator health, alert queue pressure, supervisor heartbeat, clock monotonicity, journal/ledger alignment, cycle health, data dir writability). check_all_and_alert() routes violations to alert hub. 45 tests. | RC-12 — missing-feature: no systematic invariant checking in shadow mode |
| FIX-20260624-090 | 2026-06-24 | cursor-agent | — | **UGR-A01: CapResult[T] + _SuccessProof — scope-gated proof token**. Created cap_result.py (CapResult with ok/err, _SuccessProof lifecycle, match/map/flat_map, Kernel.success_scope, CapProofExpired). ~30 tests. | RC-12 — missing-feature: no scope-gated capability-based Result type |
| FIX-20260624-091 | 2026-06-24 | cursor-agent | — | **UGR-A02: TypedClock — three incompatible time types**. Created typed_clock.py (MonotonicInstant, WallInstant, Duration — arithmetic only on MonotonicInstant+Duration, mypy-enforced incompatibility). ~30 tests. | RC-12 — missing-feature: no type-safe clock types |
| FIX-20260624-092 | 2026-06-24 | cursor-agent | — | **UGR-A04: SupervisedScheduler — dual-isolation task execution**. Created supervised_scheduler.py (THREAD tasks with cancellation+heartbeat, PROCESS tasks with SIGTERM→SIGKILL). ~30 tests. | RC-12 — missing-feature: no dual-isolation scheduler |
| FIX-20260624-093 | 2026-06-24 | cursor-agent | — | **UGR-P02/P03/P04: Spec docs + AST scanner baseline**. Created phantom_state_replay.md, wal_checkpoint_design.md, verify_capresult_ast.py. | RC-12 — missing artifacts for v3.1 architecture |
| FIX-20260624-094 | 2026-06-24 | cursor-agent | — | **UGR-Phase0-1 CI Red-X repair**. Committed missing Phase 0-1 files that were created locally but never git-added. CI ImportError on cap_result + typed_clock → Red X on A05-B02 commits. Added ruff/mypy fixes. | L1 — git add omitted |
| FIX-20260624-095 | 2026-06-24 | cursor-agent | — | **UGR-A08: CapResult migration — StrategyBudget + live_cycle budget pipeline**. Added record_trade_checked(), record_sl_checked(), load_state_checked() CapResult-wrapped methods to StrategyBudget. Replaced fail_open_guard("BudgetStateRestore") + log_and_continue at 3 budget pipeline sites in live_cycle Phase 7 with CapResult pattern matching. 17 new tests. | RC-12 — missing-feature: no CapResult integration in budget pipeline |
| FIX-20260624-096 | 2026-06-24 | cursor-agent | — | **UGR-B03: AST Scanner full enforcement — 5 detectors**. Upgraded verify_capresult_ast.py from baseline (1 detector) to full enforcement (5 detectors): CapResultOkPlacementDetector (ok() outside success_scope→violation), RawAccessDetector (._raw on TypedClock types outside whitelist), FailOpenGuardDetector (fail_open_guard/log_and_continue DEPRECATED), ProofLeakDetector (proof stored to persistent location). 41 tests. | RC-12 — missing-feature: no AST enforcement for CapResult.ok() placement, ._raw access, or proof leakage |
| FIX-20260624-097 | 2026-06-24 | cursor-agent | — | **UGR-B04: Phantom Contracts full implementation — 9 predicates + StateProjector + state-aware verifier**. Upgraded from B01 prototype (1 input-only predicate) to full implementation: StateProjector with state completeness assertions, handler priority/conflict detection, timeout/overflow protection; 8 additional predicates (4 hot-path + 5 non-hot-path); snapshot_for() per-contract key validation; PhantomSerializer NaN/Inf/numpy/Decimal support; PredicateRegistry.reset() + required_state_keys; _alert_violation LiveAlertHub integration + violation counter; offline verifier state-aware mode + incremental (since_seq) + exit code 4 for projection errors. 39 new tests (66 total). Production @phantom wiring deferred to UGR-A09. | RC-12 — missing-feature: only 1 prototype predicate, no StateProjector, no state-aware verification |
| FIX-20260624-098 | 2026-06-24 | cursor-agent | — | **_alert_violation LiveAlertHub.instance() AttributeError fix**. LiveAlertHub is a regular class, not a singleton. Calling .instance() raises AttributeError which bypasses except ImportError. Widen to except (ImportError, AttributeError). 1 line, 4 tests fixed. | L1 — incorrect API assumption |
| FIX-20260624-099 | 2026-06-24 | cursor-agent | — | **_SuccessProof cross-thread protection**. _create() records thread_id; new _verify_thread() checks caller matches creator in __debug__ mode. Integrated into ok/map/flat_map. 13 lines, 5 new tests (38 total). | L2 — logic gap: no cross-thread guard |
| FIX-20260624-101 | 2026-06-24 | cursor-agent | — | **UGR-A09a: Atomic write migration — deployment state files**. Added `atomic_write_text`/`atomic_write_json` convenience functions to `AtomicFileWriter`. Migrated 12 `write_text` calls → `atomic_write_json`/`atomic_write_text` in 7 deployment state files (state_persistence, blue_green, brain_lifecycle_manager, release_registry, release_gate, release_pipeline, release_certification). Replaced 12 `fail_open_guard`/`log_and_continue` with specific exception types in 5 deployment files. All 144 related tests pass. Remaining files deferred to UGR-A09b. | RC-12 — write_text vulnerable to partial-write corruption; fail_open_guard DEPRECATED but not yet migrated |

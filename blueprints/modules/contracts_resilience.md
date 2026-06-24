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
| `scripts/verify_capresult_ast.py` | AST scanner: CapResult placement + dynamic call detection | ✅ UGR-P03 |
| `scripts/verify_phantom_contracts.py` | Phantom offline verifier (WAL state reconstruction + replay) | ✅ UGR-B01 |

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
- Phantom Contracts state reconstruction protocol TBD (see docs/specs/phantom_state_replay.md).
- InvariantEngine currently shadow-only — no circuit-breaking integration yet.
- AlertStormDetector rate decay is window-based; may need persistent state for long-running storms.

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

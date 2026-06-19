# Dependency Graph

> Auto-generated from `from core.<x> import` statements
> Last updated: 2026-05-14

## Core Module Dependency Matrix

```
                    ┌──────────────────────────────────────────────────┐
                    │                  runtime/live                     │
                    │  (orchestrates: brains, execution, risk,          │
                    │   feedback, parliament, features, state,          │
                    │   deployment, alpha, governance, ledger,          │
                    │   observability, market, strategies)              │
                    └────┬────┬────┬────┬────┬────┬────┬───────────────┘
                         │    │    │    │    │    │    │
    ┌────────────────────┘    │    │    │    │    │    └──────────────┐
    ▼                         ▼    ▼    ▼    ▼    ▼                   ▼
┌──────────┐  ┌──────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐
│deployment│  │ execution│ │ protocol │ │parliament│ │ feedback │
│(13 deps) │  │(8 deps)  │ │(3 deps)  │ │(2 deps) │ │(0 deps)  │
└────┬─────┘  └────┬─────┘ └────┬─────┘ └───┬────┘ └──────────┘
     │             │             │           │
     └──────┬──────┘             │           └──────┐
            ▼                    ▼                  ▼
     ┌──────────┐        ┌──────────┐       ┌──────────┐
     │  brains  │        │ contracts │       │ features │
     │(feedback │        │  domain,  │       │ service, │
     │ features │        │ ids, enums│       │ rolling  │
     │contracts)│        └──────────┘       └──────────┘
     └──────────┘
```

## Per-Module Dependencies (Imports From)

| Module | Imports From |
|--------|-------------|
| **runtime/live** | alpha, brains, contracts, deployment, execution, features, feedback, governance, infrastructure, ledger, market, observability, parliament, state, strategies |
| **deployment/lifecycle** | brains, contracts, execution, features, feedback, governance, ledger, market, observability, parliament, protocol, risk, state |
| **execution/orders** | brains, contracts, deployment, metrics, observability, parliament, protocol, runtime |
| **execution/guards** | contracts |
| **execution/reentry** | — (self-contained) |
| **brains/adapters** | contracts, features (via factory) |
| **brains/services** | contracts, features, feedback |
| **brains/schema** | — |
| **brains/validation** | brains, deployment |
| **protocol/services** | contracts, execution, observability |
| **protocol/governance** | contracts |
| **protocol/parliament** | brains, contracts |
| **risk/policies** | contracts |
| **risk/regime** | — |
| **risk/portfolio** | contracts, metrics |
| **feedback/performance** | — |
| **feedback/pnl** | — |
| **feedback/online** | features, brains |
| **features/service** | contracts |
| **features/rolling** | — |
| **contracts/domain** | ledger (replay_execution_record only) |
| **contracts/ids** | — |
| **contracts/training** | — (stdlib dataclasses + yaml only) |
| **training** | contracts/training, numpy |
| **state** | contracts |
| **runtime/state** | contracts |
| **monitor/dashboard** | feedback, governance, observability |
| **deployment/config** | contracts |
| **deployment/lifecycle** | contracts, state |
| **market/mtf** | — (self-contained, stdlib datetime only) |

## Apps → Core Dependencies

| App | Imports From (core modules) |
|-----|---------------------------|
| apps/engine/ | alpha, brains, contracts, deployment, execution, features, feedback, ledger, observability, parliament, protocol, risk, runtime, strategies |
| apps/monitor/ | feedback, governance, observability |
| scripts/training/train.py | contracts/training, training (full) |
| scripts/training/trainers/* | contracts/training, training |

| execution/exit_watchdog | execution, runtime |
| execution/managed_close | execution, runtime |
| execution/position_manager | execution, runtime |
| execution/trail_stop | execution, runtime |

## Known Cycles

| Cycle | Files | Risk | Status |
|-------|-------|------|--------|
| execution ↔ runtime | execution/strategy_line.py ↔ runtime/shadow_recorder.py | Low — shadow_recorder is write-only | Open |
| execution ↔ deployment | execution/live_order_sender.py ↔ deployment/service_container.py | Medium — DI container; stable interface | Open |
| strategy_line ↔ meta_filter_routing | ~~execution/strategy_line.py ↔ execution/meta_filter_routing.py~~ | ~~Low~~ | **RESOLVED**: Strangler Fig #18 (strategy_decision.py) |
| strategy_line ↔ trend_isolation_gates | ~~execution/strategy_line.py ↔ execution/trend_isolation_gates.py~~ | ~~Low~~ | **RESOLVED**: Strangler Fig #18 (strategy_decision.py) |

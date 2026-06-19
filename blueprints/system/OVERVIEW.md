# Blueprint System Overview

> Last regenerated: 2026-06-19
> FIX count: 22 | BLE001: 560→0 | 圈导入: 2→0 | 零测试模块: 6→0 | 纯函数模块: 9

## Architecture Layers

```
┌─────────────────────────────────────────────────────────┐
│                    apps/ (CLI + Dashboard)                │
│  main.py | live_launcher | bridge_worker | dashboard     │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│               core/runtime/ (Live Cycle Orchestration)    │
│  live_cycle | signal_pipeline | execution_pipeline       │
│  market_ingress | order_dispatch | shadow_recorder        │
└──┬──────┬──────┬──────┬──────┬──────┬──────┬────────────┘
   │      │      │      │      │      │      │
   ▼      ▼      ▼      ▼      ▼      ▼      ▼
┌──────┐┌─────┐┌────┐┌────┐┌────┐┌─────┐┌──────────┐
│Brains││Exec ││Risk││Feed││Parl││Proto││Features   │
│      ││ution││    ││back││    ││col  ││           │
└──┬───┘└──┬──┘└──┬─┘└──┬─┘└──┬─┘└──┬──┘└────┬─────┘
   │       │      │     │     │      │        │
   └───────┴──────┴─────┴─────┴──────┴────────┘
                      │
┌─────────────────────┴───────────────────────────────────┐
│               Foundation Layer                           │
│  contracts/ | state/ | governance/ | deployment/         │
│  ledger/ | observability/ | infrastructure/              │
└──────────────────────────────────────────────────────────┘
```

## Module Map

| # | Module | Blueprint File | Layer | Depends On |
|---|--------|---------------|-------|------------|
| 1 | brains/adapters | [brains_adapters.md](../modules/brains_adapters.md) | AI | contracts |
| 2 | brains/services | [brains_services.md](../modules/brains_services.md) | AI | contracts, features, feedback |
| 3 | brains/schema | [brains_schema.md](../modules/brains_schema.md) | AI | — |
| 4 | execution/guards | [execution_guards.md](../modules/execution_guards.md) | Execution | contracts |
| 5 | execution/orders | [execution_orders.md](../modules/execution_orders.md) | Execution | contracts, deployment, protocol |
| 6 | execution/reentry | [execution_reentry.md](../modules/execution_reentry.md) | Execution | — |
| 7 | risk/policies | [risk_policies.md](../modules/risk_policies.md) | Risk | contracts |
| 8 | risk/regime | [risk_regime.md](../modules/risk_regime.md) | Risk | — |
| 9 | risk/portfolio | [risk_portfolio.md](../modules/risk_portfolio.md) | Risk | contracts, metrics |
| 10 | feedback/performance | [feedback_performance.md](../modules/feedback_performance.md) | Feedback | — |
| 11 | feedback/pnl | [feedback_pnl.md](../modules/feedback_pnl.md) | Feedback | — |
| 12 | feedback/online | [feedback_online.md](../modules/feedback_online.md) | Feedback | features, brains |
| 13 | protocol/governance | [protocol_governance.md](../modules/protocol_governance.md) | Protocol | contracts |
| 14 | protocol/parliament | [protocol_parliament.md](../modules/protocol_parliament.md) | Protocol | brains, contracts |
| 15 | protocol/services | [protocol_services.md](../modules/protocol_services.md) | Protocol | contracts, execution |
| 16 | contracts/domain | [contracts_domain.md](../modules/contracts_domain.md) | Foundation | — |
| 17 | contracts/ids | [contracts_ids.md](../modules/contracts_ids.md) | Foundation | — |
| 18 | deployment/config | [deployment_config.md](../modules/deployment_config.md) | Foundation | contracts |
| 19 | deployment/lifecycle | [deployment_lifecycle.md](../modules/deployment_lifecycle.md) | Foundation | contracts, state |
| 20 | features/rolling | [features_rolling.md](../modules/features_rolling.md) | Features | — |
| 21 | features/service | [features_service.md](../modules/features_service.md) | Features | contracts |
| 22 | runtime/live | [runtime_live.md](../modules/runtime_live.md) | Orchestration | brains, execution, risk, feedback, parliament, features, state |
| 23 | runtime/state | [runtime_state.md](../modules/runtime_state.md) | Foundation | contracts |
| 24 | parliament/consensus | [parliament_consensus.md](../modules/parliament_consensus.md) | AI | brains, execution |
| 25 | governance/rules | [governance_rules.md](../modules/governance_rules.md) | Foundation | contracts, infrastructure |
| 26 | training/pipeline | [training_pipeline.md](../modules/training_pipeline.md) | AI | brains, metrics, features, contracts |

## Cross-Cutting Concerns

| Concern | Owned By | Enforced By |
|---------|---------|-------------|
| Schema versioning | contracts/domain | Schema version constants in each module |
| ID generation | contracts/ids | `new_*_id()` functions |
| Error handling | contracts/exceptions | DomainError hierarchy |
| Serialization | contracts/serialization | json_codec.py |
| Health checking | deployment/lifecycle | HealthCheckService |
| Config hot-reload | deployment/config | ConfigHotReload |
| State persistence | deployment/lifecycle | StatePersistence |
| Circuit breaking | protocol/services | CircuitBreaker |
| Metric names | observability | metric_names.py |
| Audit logging | observability | audit_log.py |

## Dependency Graph

See [DEPENDENCY_GRAPH.md](DEPENDENCY_GRAPH.md) for the full cross-module dependency matrix.

## Fix Registry

See [FIX_REGISTRY.md](FIX_REGISTRY.md) for the master fix ledger with root cause analysis.

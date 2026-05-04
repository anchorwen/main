# DEPENDENCY GRAPH — 模块依赖关系

> **自动生成**: 2026-05-04T01:27:54Z

## Package-Level Dependencies

### `apps/engine/`

- `backtest_runner.py` → `core.deployment.environment_config`, `core.deployment.replay_isolation`, `core.deployment.service_container`, `core.feedback.performance_analytics`
- `batch_processor.py` → `core.observability.metric_names`
- `bootstrap_v9.py` → `core.brains.services.brain_registry_loader`, `core.deployment.environment_config`, `core.deployment.service_container`
- `cli.py` → `apps.engine.backtest_runner`, `apps.engine.diagnostics_cli`, `apps.engine.system_facade`, `core.alpha`, `core.alpha.schema_versions`, `core.deployment.domain_keys`, `core.deployment.environment_config`, `core.deployment.lifecycle_manager`, `core.deployment.operational_support`, `core.deployment.scheduler_service`, `core.deployment.schema_versions`, `core.deployment.service_container`, `core.deployment.state_persistence`, `core.execution`, `core.ledger.storage.jsonl_ledger_store`, `core.observability.alert_service`, `core.runtime`, `core.runtime.schema_versions`, `core.strategies.examples`, `core.strategies.registry`, `scripts.trade_quality_report`
- `communication_ops_cli.py` → `apps.engine.communication_summary_contract`, `core.deployment.domain_keys`, `core.ledger.services.communication_inspection_service`, `core.ledger.services.communication_operations_service`, `core.ledger.services.communication_record_reader`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_replay_service`, `core.ledger.services.replay_execution_reader`, `core.protocol.services.file_queue_receipt_reader`
- `communication_summary_contract.py` → `core.deployment.domain_keys`
- `diagnostics_cli.py` → `core.deployment.domain_keys`
- `main_v9_shadow.py` → `apps.engine.bootstrap_v9`, `apps.engine.communication_summary_contract`, `apps.engine.v9_shadow_sse`, `core.deployment.domain_keys`, `core.features.schemas.v9_institutional_schema`
- `orchestrator.py` → `core.contracts.ids`, `core.observability.metric_names`, `core.observability.tracing`
- `runtime_loop.py` → `core.contracts.domain.risk_verdict`, `core.contracts.enums`, `core.contracts.ids`, `core.risk.schema_versions`
- `system_facade.py` → `core.deployment.domain_keys`, `core.deployment.schema_versions`, `core.observability.metric_names`
- `v9_shadow_sse.py` → (无内部依赖)
- `v9_shadow_support.py` → `apps.engine.runtime_loop`, `core.contracts.domain.decision_candidate`, `core.contracts.ids`, `core.parliament.schema_versions`

### `core/alpha/`

- `contracts.py` → (无内部依赖)
- `lifecycle_service.py` → `core.alpha.contracts`, `core.alpha.registry`, `core.alpha.schema_versions`
- `performance_store.py` → `core.alpha.schema_versions`
- `portfolio_allocator.py` → `core.alpha.contracts`, `core.alpha.performance_store`, `core.alpha.registry`, `core.alpha.schema_versions`
- `promotion_gate.py` → `core.alpha.contracts`, `core.alpha.lifecycle_service`, `core.alpha.performance_store`, `core.alpha.schema_versions`
- `registry.py` → `core.alpha.contracts`, `core.alpha.schema_versions`
- `risk_budget.py` → `core.alpha.schema_versions`
- `schema_versions.py` → (无内部依赖)

### `core/brains/`

- `schema_versions.py` → (无内部依赖)

### `core/brains/adapters/`

- `base_adapter.py` → `core.contracts.domain.brain_decision_proposal`
- `params_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.brains.schema_versions`, `core.contracts.domain.brain_decision_proposal`, `core.contracts.ids`
- `v9_onnx_brain_adapter.py` → `core.contracts.domain.brain_decision_proposal`, `core.contracts.ids`
- `xgboost_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.brains.schema_versions`, `core.contracts.domain.brain_decision_proposal`, `core.contracts.ids`

### `core/brains/services/`

- `brain_factory.py` → `core.brains.adapters`, `core.features.adapters.v9_feature_adapter`
- `brain_registry_loader.py` → (无内部依赖)
- `brain_registry_service.py` → `core.brains.services.brain_registry_loader`
- `brain_run_service.py` → `core.brains.adapters.base_adapter`

### `core/contracts/`

- `enums.py` → (无内部依赖)
- `exceptions.py` → (无内部依赖)
- `ids.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)
- `validators.py` → (无内部依赖)

### `core/contracts/domain/`

- `brain_decision_proposal.py` → `core.contracts.enums`, `core.deployment.domain_keys`
- `communication_envelope.py` → `core.contracts.enums`, `core.deployment.domain_keys`
- `communication_record.py` → `core.contracts.domain.communication_envelope`, `core.contracts.domain.dispatch_result`, `core.contracts.schema_versions`, `core.deployment.domain_keys`
- `decision_candidate.py` → `core.deployment.domain_keys`
- `decision_intent.py` → `core.contracts.enums`, `core.deployment.domain_keys`
- `decision_record.py` → `core.deployment.domain_keys`
- `dispatch_request.py` → `core.contracts.domain.communication_envelope`, `core.deployment.domain_keys`
- `dispatch_result.py` → `core.contracts.enums`, `core.deployment.domain_keys`
- `execution_event.py` → `core.deployment.domain_keys`
- `protocol_override.py` → `core.contracts.enums`
- `replay_execution_record.py` → `core.contracts.schema_versions`, `core.deployment.domain_keys`, `core.ledger.governance_sources`, `core.ledger.services.replay_trace_refs`
- `risk_verdict.py` → `core.contracts.enums`, `core.deployment.domain_keys`
- `system_mode_state.py` → `core.contracts.enums`

### `core/contracts/serialization/`

- `json_codec.py` → (无内部依赖)

### `core/deployment/`

- `capability_registry.py` → `core.deployment.domain_keys`
- `compliance_audit.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `compliance_control_matrix.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `config_hot_reload.py` → `core.deployment.domain_keys`
- `deployment_executor.py` → `core.deployment.domain_keys`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `deployment_plan.py` → `core.deployment.domain_keys`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `domain_keys.py` → (无内部依赖)
- `environment_config.py` → `core.deployment.domain_keys`
- `evidence_bundle.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `feature_update_producer.py` → `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`
- `final_audit.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `governance_summary.py` → `core.deployment.domain_keys`
- `health_check.py` → `core.deployment.domain_keys`
- `lifecycle_manager.py` → `core.deployment.domain_keys`, `core.deployment.state_persistence`, `core.observability.metric_names`
- `operational_support.py` → `core.deployment.domain_keys`
- `operations_timeline.py` → `core.deployment.domain_keys`, `core.deployment.schema_versions`
- `ops_maturity.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `postmortem_report.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_certification.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`
- `release_gate.py` → `core.deployment.domain_keys`, `core.deployment.operational_support`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_pipeline.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_readiness.py` → `core.deployment.capability_registry`, `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_registry.py` → `core.deployment.domain_keys`, `core.deployment.governance_summary`, `core.deployment.schema_versions`
- `replay_isolation.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.deployment.domain_keys`, `core.protocol.schema_versions`, `core.protocol.services.communication_dispatcher`
- `rollback_drill.py` → `core.deployment.domain_keys`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `runbook_engine.py` → `core.deployment.domain_keys`, `core.deployment.operational_support`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `scheduler_service.py` → `core.deployment.domain_keys`, `core.observability.metric_names`
- `schema_versions.py` → (无内部依赖)
- `service_container.py` → `apps.engine.orchestrator`, `apps.engine.runtime_loop`, `core.brains.services.brain_factory`, `core.brains.services.brain_run_service`, `core.contracts.domain.system_mode_state`, `core.contracts.enums`, `core.deployment.compliance_audit`, `core.deployment.compliance_control_matrix`, `core.deployment.config_hot_reload`, `core.deployment.deployment_executor`, `core.deployment.deployment_plan`, `core.deployment.domain_keys`, `core.deployment.environment_config`, `core.deployment.evidence_bundle`, `core.deployment.final_audit`, `core.deployment.health_check`, `core.deployment.operations_timeline`, `core.deployment.ops_maturity`, `core.deployment.postmortem_report`, `core.deployment.release_certification`, `core.deployment.release_gate`, `core.deployment.release_pipeline`, `core.deployment.release_readiness`, `core.deployment.release_registry`, `core.deployment.rollback_drill`, `core.deployment.runbook_engine`, `core.deployment.schema_versions`, `core.execution.execution_manager`, `core.execution.fix_contracts`, `core.features.feature_service`, `core.feedback.brain_performance_tracker`, `core.feedback.decision_scorer`, `core.feedback.feedback_loop`, `core.feedback.outcome_collector`, `core.governance.governance_rule_engine`, `core.governance.governance_service`, `core.ledger.services.communication_inspection_service`, `core.ledger.services.communication_operations_service`, `core.ledger.services.communication_record_reader`, `core.ledger.services.communication_record_writer`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_replay_service`, `core.ledger.services.decision_record_writer`, `core.ledger.services.execution_event_reader`, `core.ledger.services.execution_event_writer`, `core.ledger.services.execution_reconciliation_service`, `core.ledger.storage.jsonl_ledger_store`, `core.market.position_tracker`, `core.observability.alert_service`, `core.observability.audit_log`, `core.observability.diagnostics_dashboard`, `core.observability.metric_names`, `core.observability.metrics_collector`, `core.observability.slo_service`, `core.parliament.parliament_service`, `core.protocol.services.communication_dispatcher`, `core.protocol.services.decision_compiler`, `core.protocol.services.file_queue_communication_adapter`, `core.protocol.services.file_queue_receipt_reader`, `core.protocol.services.fix_communication_adapter`, `core.protocol.services.idempotency`, `core.protocol.services.intent_message_builder`, `core.protocol.services.mt5_communication_adapter`, `core.protocol.services.override_resolver`, `core.protocol.services.stub_communication_adapter`, `core.protocol.services.venue_router`, `core.risk.risk_evaluation_service`, `core.risk.risk_policies`, `core.state.schema_versions`, `core.state.services.control_snapshot_service`, `core.state.stores.override_store`, `core.state.stores.system_mode_store`
- `state_persistence.py` → `core.deployment.domain_keys`
- `validation_mode.py` → `core.deployment.domain_keys`

### `core/execution/`

- `execution_manager.py` → `core.contracts.exceptions`, `core.observability.metric_names`
- `fill_simulator.py` → `core.contracts.ids`, `core.execution.gateway_contracts`
- `fix_contracts.py` → (无内部依赖)
- `fix_execution_mapper.py` → `core.contracts.ids`, `core.execution.fix_contracts`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`
- `fix_gateway_adapter.py` → `core.execution.fix_contracts`, `core.execution.fix_execution_mapper`, `core.execution.fix_message_builder`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`
- `fix_message_builder.py` → `core.execution.fix_contracts`, `core.execution.gateway_contracts`
- `gateway_contracts.py` → (无内部依赖)
- `order_state_machine.py` → `core.execution.gateway_contracts`
- `paper_gateway.py` → `core.execution.fill_simulator`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`, `core.observability.metric_names`
- `quality_analyzer.py` → `core.execution.gateway_contracts`, `core.execution.quality_contracts`, `core.execution.schema_versions`
- `quality_contracts.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)

### `core/features/`

- `feature_service.py` → `apps.engine.runtime_loop`, `core.contracts.ids`, `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`
- `feature_snapshot.py` → `core.features.store_contracts`
- `local_feature_store.py` → `core.features.store_contracts`
- `store_contracts.py` → (无内部依赖)
- `update_job.py` → `core.features.store_contracts`

### `core/features/adapters/`

- `v9_feature_adapter.py` → `core.features.schemas.v9_institutional_schema`

### `core/features/computers/`

- `v9_live_computer.py` → (无内部依赖)

### `core/features/schemas/`

- `v9_institutional_schema.py` → (无内部依赖)

### `core/feedback/`

- `brain_performance_tracker.py` → (无内部依赖)
- `decision_scorer.py` → (无内部依赖)
- `feedback_loop.py` → (无内部依赖)
- `outcome_collector.py` → (无内部依赖)
- `performance_analytics.py` → (无内部依赖)

### `core/governance/`

- `governance_rule_engine.py` → (无内部依赖)
- `governance_service.py` → `core.contracts.exceptions`

### `core/ledger/`

- `governance_sources.py` → `core.deployment.domain_keys`
- `schema_versions.py` → (无内部依赖)
- `stream_names.py` → (无内部依赖)

### `core/ledger/services/`

- `communication_inspection_service.py` → `core.deployment.domain_keys`
- `communication_operations_service.py` → `core.deployment.domain_keys`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_trace_refs`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`, `core.ledger.services.replay_record_refs`
- `communication_record_reader.py` → `core.deployment.domain_keys`, `core.ledger.stream_names`
- `communication_record_writer.py` → `core.contracts.domain.communication_record`, `core.contracts.ids`, `core.ledger.stream_names`
- `communication_replay_executor.py` → `core.contracts.enums`, `core.deployment.domain_keys`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`, `core.ledger.services.replay_record_refs`, `core.ledger.services.replay_trace_refs`
- `communication_replay_gate.py` → `core.contracts.enums`, `core.deployment.domain_keys`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`
- `communication_replay_service.py` → `core.deployment.domain_keys`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_trace_refs`
- `communication_trace_refs.py` → `core.deployment.domain_keys`
- `decision_record_writer.py` → `core.contracts.domain.decision_record`, `core.contracts.ids`, `core.ledger.schema_versions`, `core.ledger.stream_names`
- `execution_event_reader.py` → `core.deployment.domain_keys`
- `execution_event_writer.py` → `core.contracts.domain.execution_event`, `core.contracts.ids`, `core.ledger.schema_versions`, `core.ledger.stream_names`
- `execution_reconciliation_service.py` → `core.deployment.domain_keys`
- `gate_decision_refs.py` → `core.deployment.domain_keys`
- `replay_execution_reader.py` → `core.deployment.domain_keys`, `core.ledger.stream_names`
- `replay_execution_writer.py` → `core.contracts.domain.replay_execution_record`, `core.contracts.ids`, `core.ledger.stream_names`
- `replay_plan_refs.py` → `core.deployment.domain_keys`
- `replay_record_refs.py` → `core.deployment.domain_keys`
- `replay_trace_refs.py` → `core.deployment.domain_keys`

### `core/ledger/storage/`

- `jsonl_ledger_store.py` → `core.contracts.serialization.json_codec`, `core.ledger.stream_names`

### `core/market/`

- `position_tracker.py` → (无内部依赖)
- `signal_processor.py` → (无内部依赖)

### `core/observability/`

- `alert_service.py` → `core.deployment.domain_keys`
- `audit_log.py` → (无内部依赖)
- `diagnostics_dashboard.py` → `core.observability.metric_names`
- `event_bus.py` → (无内部依赖)
- `metric_names.py` → (无内部依赖)
- `metrics_collector.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)
- `slo_service.py` → `core.deployment.domain_keys`, `core.observability.metric_names`, `core.observability.schema_versions`
- `tracing.py` → (无内部依赖)

### `core/parliament/`

- `parliament_service.py` → `core.contracts.domain.decision_candidate`, `core.contracts.ids`, `core.parliament.schema_versions`
- `schema_versions.py` → (无内部依赖)

### `core/protocol/`

- `live_execution_contract.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)

### `core/protocol/services/`

- `communication_adapter.py` → (无内部依赖)
- `communication_adapter_registry.py` → (无内部依赖)
- `communication_dispatcher.py` → `core.contracts.domain.dispatch_request`, `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.contracts.ids`, `core.deployment.domain_keys`, `core.protocol.schema_versions`, `core.protocol.services.communication_adapter_registry`
- `decision_compiler.py` → `core.contracts.domain.decision_intent`, `core.contracts.enums`, `core.contracts.ids`, `core.protocol.schema_versions`
- `file_queue_communication_adapter.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.contracts.serialization.json_codec`, `core.protocol.schema_versions`
- `file_queue_receipt_reader.py` → (无内部依赖)
- `fix_communication_adapter.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.execution.fix_contracts`, `core.execution.fix_gateway_adapter`, `core.execution.gateway_contracts`, `core.protocol.schema_versions`
- `idempotency.py` → (无内部依赖)
- `intent_message_builder.py` → `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.contracts.ids`, `core.protocol.schema_versions`
- `mt5_communication_adapter.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.contracts.serialization.json_codec`, `core.protocol.schema_versions`
- `override_resolver.py` → (无内部依赖)
- `resilience.py` → (无内部依赖)
- `stub_communication_adapter.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.protocol.schema_versions`
- `venue_router.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.protocol.schema_versions`

### `core/risk/`

- `risk_evaluation_service.py` → `core.contracts.domain.risk_verdict`, `core.contracts.enums`, `core.contracts.ids`, `core.risk.schema_versions`
- `risk_policies.py` → `core.contracts.enums`
- `schema_versions.py` → (无内部依赖)

### `core/runtime/`

- `alpha_budget_contracts.py` → `core.alpha.schema_versions`, `core.runtime.schema_versions`
- `alpha_budget_usage_reporter.py` → `core.runtime.alpha_budget_contracts`, `core.runtime.schema_versions`
- `alpha_budget_usage_store.py` → `core.runtime.alpha_budget_contracts`, `core.runtime.schema_versions`
- `alpha_risk_budget_gate.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.alpha_budget_contracts`, `core.runtime.alpha_budget_usage_store`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `approval_contracts.py` → `core.execution.gateway_contracts`, `core.runtime.schema_versions`, `core.strategies.contracts`
- `cycle_replay.py` → `core.runtime.evidence_reader`, `core.runtime.schema_versions`
- `evidence_contracts.py` → `core.runtime.integration_contracts`, `core.runtime.schema_versions`
- `evidence_reader.py` → `core.ledger.stream_names`
- `evidence_writer.py` → `core.contracts.ids`, `core.ledger.stream_names`, `core.runtime.evidence_contracts`, `core.runtime.integration_contracts`
- `execution_gates.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `execution_gateway_router.py` → `core.execution.gateway_contracts`
- `execution_pipeline.py` → `core.contracts.ids`, `core.execution.quality_analyzer`, `core.execution.quality_contracts`, `core.runtime.execution_gateway_router`, `core.runtime.integration_contracts`, `core.runtime.schema_versions`, `core.runtime.signal_order_builder`, `core.strategies.registry`
- `integration_contracts.py` → `core.execution.gateway_contracts`, `core.execution.quality_contracts`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `schema_versions.py` → (无内部依赖)
- `signal_order_builder.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.integration_contracts`, `core.strategies.contracts`
- `summary_service.py` → `core.runtime.evidence_reader`, `core.runtime.schema_versions`

### `core/state/`

- `schema_versions.py` → (无内部依赖)

### `core/state/services/`

- `control_snapshot.py` → (无内部依赖)
- `control_snapshot_service.py` → `core.state.services.control_snapshot`

### `core/state/stores/`

- `override_store.py` → `core.contracts.enums`
- `system_mode_store.py` → `core.contracts.domain.system_mode_state`, `core.contracts.enums`, `core.state.schema_versions`

### `core/strategies/`

- `contracts.py` → `core.strategies.schema_versions`
- `examples.py` → `core.contracts.ids`, `core.strategies.contracts`, `core.strategies.schema_versions`
- `registry.py` → `core.strategies.contracts`, `core.strategies.schema_versions`
- `schema_versions.py` → (无内部依赖)

### `scripts/`

- `ci_prepare_v9_shadow_fixtures.py` → `apps.engine.main_v9_shadow`
- `ingest_live_journal_to_alpha.py` → `core.alpha.performance_store`, `core.runtime.schema_versions`, `scripts.trade_quality_report`
- `live_auto_healthcheck.py` → `scripts.live_dispatch_policy`
- `live_daily_recap.py` → `scripts.live_data_quality_report`, `scripts.shadow_live_compare_report`, `scripts.trade_quality_report`
- `live_data_quality_report.py` → (无内部依赖)
- `live_dispatch_policy.py` → `scripts.guards.journal_quality`, `scripts.market_calendar`, `scripts.mt5_spread_probe`, `scripts.trade_quality_report`
- `live_intent_loop.py` → `core.brains.adapters.v9_onnx_brain_adapter`, `core.deployment.feature_update_producer`, `core.features.adapters.v9_feature_adapter`, `core.features.computers.v9_live_computer`, `core.features.feature_service`, `core.features.local_feature_store`, `scripts.send_live_order`
- `live_micro_rollout_gate.py` → `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.protocol.schema_versions`
- `live_read_only_preflight.py` → `apps.engine.system_facade`, `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.protocol.schema_versions`, `scripts.live_micro_rollout_gate`
- `live_shadow_intent_producer.py` → `core.features.live_feature_source`
- `live_stack_diagnostic.py` → `scripts.live_dispatch_policy`, `scripts.send_live_order`
- `market_calendar.py` → (无内部依赖)
- `mt5_bridge_healthcheck.py` → (无内部依赖)
- `mt5_bridge_worker.py` → `core.protocol.live_execution_contract`
- `mt5_positions_snapshot.py` → (无内部依赖)
- `mt5_spread_probe.py` → (无内部依赖)
- `runtime_protection_guard.py` → `scripts.guards.journal_quality`
- `send_live_order.py` → `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.protocol.live_execution_contract`, `core.protocol.schema_versions`
- `shadow_live_compare_report.py` → `scripts.trade_quality_report`
- `trade_quality_report.py` → (无内部依赖)

### `scripts/dev/`

- `fix_project.py` → (无内部依赖)

### `scripts/guards/`

- `journal_quality.py` → (无内部依赖)

### `scripts/training/`

- `batch_train_skeleton.py` → `scripts.training.crt_manifest`
- `crt_manifest.py` → (无内部依赖)
- `generate_batch_plan.py` → (无内部依赖)
- `monitor_training.py` → (无内部依赖)
- `run_train_batch.py` → (无内部依赖)
- `write_manifest_stub.py` → `scripts.training.crt_manifest`
- `your_trainer.py` → `scripts.training.crt_manifest`

### `scripts/training/trainers/`

- `arb_trainer.py` → (无内部依赖)
- `mtx_trainer.py` → (无内部依赖)
- `sur_trainer.py` → (无内部依赖)

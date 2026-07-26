# DEPENDENCY GRAPH — 模块依赖关系

> **自动生成**: 2026-07-26T13:13:08Z

## Package-Level Dependencies

### `apps/engine/`

- `backtest_runner.py` → `core.deployment.environment_config`, `core.deployment.replay_isolation`, `core.deployment.service_container`, `core.feedback.performance_analytics`, `core.runtime.fault_handler`
- `batch_processor.py` → `core.observability.metric_names`, `core.runtime.fault_handler`
- `bootstrap_v9.py` → `core.brains.services.brain_registry_loader`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.execution.meta_signal_filter`, `core.features.adapters.microstructure_feature_adapter`
- `cli.py` → `apps.engine.backtest_runner`, `apps.engine.diagnostics_cli`, `apps.engine.system_facade`, `core.alpha`, `core.alpha.schema_versions`, `core.contracts.domain_keys`, `core.deployment.environment_config`, `core.deployment.lifecycle_manager`, `core.deployment.operational_support`, `core.deployment.scheduler_service`, `core.deployment.schema_versions`, `core.deployment.service_container`, `core.deployment.state_persistence`, `core.execution`, `core.ledger.storage.jsonl_ledger_store`, `core.observability.alert_service`, `core.runtime`, `core.runtime.schema_versions`, `core.strategies.examples`, `core.strategies.registry`, `scripts.trade_quality_report`
- `communication_ops_cli.py` → `apps.engine.communication_summary_contract`, `core.contracts.domain_keys`, `core.ledger.services.communication_inspection_service`, `core.ledger.services.communication_operations_service`, `core.ledger.services.communication_record_reader`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_replay_service`, `core.ledger.services.replay_execution_reader`, `core.protocol.services.file_queue_receipt_reader`
- `communication_summary_contract.py` → `core.contracts.domain_keys`
- `diagnostics_cli.py` → `core.contracts.domain_keys`
- `main_v9_shadow.py` → `apps.engine.bootstrap_v9`, `apps.engine.communication_summary_contract`, `apps.engine.v9_shadow_sse`, `core.contracts.domain_keys`, `core.features.schemas.v9_institutional_schema`, `core.runtime.fault_handler`
- `orchestrator.py` → `core.contracts.ids`, `core.observability.metric_names`, `core.observability.tracing`, `core.runtime.fault_handler`
- `runtime_loop.py` → `core.contracts.domain.risk_verdict`, `core.contracts.enums`, `core.contracts.ids`, `core.risk.schema_versions`
- `system_facade.py` → `core.contracts.domain_keys`, `core.deployment.schema_versions`, `core.observability.metric_names`, `core.runtime.fault_handler`
- `v9_shadow_sse.py` → (无内部依赖)
- `v9_shadow_support.py` → `apps.engine.runtime_loop`, `core.contracts.domain.decision_candidate`, `core.contracts.ids`, `core.parliament.schema_versions`

### `apps/monitor/`

- `live_trading_dashboard.py` → `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.governance.governance_service`, `core.observability.alert_service`, `core.observability.audit_log`, `core.observability.slo_service`, `core.runtime.fault_handler`, `scripts.live_auto_healthcheck`, `scripts.live_dashboard`, `scripts.live_dispatch_policy`, `scripts.mt5_positions_snapshot`

### `core/`

- `constants.py` → (无内部依赖)

### `core/alpha/`

- `contracts.py` → (无内部依赖)
- `lifecycle_service.py` → `core.alpha.contracts`, `core.alpha.registry`, `core.alpha.schema_versions`
- `ou_optimizer.py` → (无内部依赖)
- `performance_store.py` → `core.alpha.schema_versions`, `core.state.catalog`, `core.state.writer`
- `portfolio_allocator.py` → `core.alpha.contracts`, `core.alpha.performance_store`, `core.alpha.registry`, `core.alpha.schema_versions`
- `promotion_gate.py` → `core.alpha.contracts`, `core.alpha.lifecycle_service`, `core.alpha.performance_store`, `core.alpha.schema_versions`
- `registry.py` → `core.alpha.contracts`, `core.alpha.schema_versions`, `core.state.catalog`, `core.state.writer`
- `risk_budget.py` → `core.alpha.schema_versions`
- `schema_versions.py` → (无内部依赖)

### `core/backtest/`

- `data_feed.py` → (无内部依赖)
- `engine.py` → `core.backtest.data_feed`, `core.backtest.execution_simulator`, `core.backtest.portfolio`
- `execution_simulator.py` → (无内部依赖)
- `metrics.py` → `core.backtest.engine`
- `portfolio.py` → (无内部依赖)
- `strategy_adapter.py` → `core.backtest.data_feed`, `core.backtest.portfolio`, `core.execution.strategy_line`

### `core/brains/`

- `brain_registry.py` → (无内部依赖)
- `online_mlp_model.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)

### `core/brains/adapters/`

- `base_adapter.py` → `core.schemas.trading_contracts`
- `lightgbm_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.deployment.brain_alert`, `core.schemas.trading_contracts`
- `meta_filter_adapter.py` → (无内部依赖)
- `online_learner_adapter.py` → `core.brains.adapters.base_adapter`, `core.brains.online_mlp_model`, `core.deployment.brain_alert`, `core.schemas.trading_contracts`
- `params_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.schemas.trading_contracts`
- `transformer_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.brains.services.inference_guard`, `core.deployment.brain_alert`, `core.schemas.trading_contracts`
- `v9_onnx_brain_adapter.py` → `core.brains.services.inference_guard`, `core.deployment.brain_alert`, `core.schemas.trading_contracts`
- `xgboost_brain_adapter.py` → `core.brains.adapters.base_adapter`, `core.deployment.brain_alert`, `core.schemas.trading_contracts`

### `core/brains/services/`

- `ab_test.py` → (无内部依赖)
- `brain_attribution_service.py` → (无内部依赖)
- `brain_factory.py` → `core.brains.adapters`, `core.deployment.brain_alert`, `core.deployment.brain_config_validator`, `core.features.adapters.microstructure_feature_adapter`, `core.features.adapters.v9_feature_adapter`
- `brain_leaderboard.py` → `core.contracts.exceptions`, `core.feedback.brain_quality_engine`
- `brain_promotion.py` → `core.governance.governance_service`
- `brain_registry_loader.py` → (无内部依赖)
- `brain_registry_service.py` → `core.brains.brain_registry`, `core.brains.services.brain_registry_loader`
- `brain_run_service.py` → `core.brains.adapters.base_adapter`, `core.deployment.brain_alert`, `core.deployment.brain_config_validator`
- `dynamic_brain_weighter.py` → `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.feedback.brain_quality_engine`
- `inference_guard.py` → (无内部依赖)
- `onnx_worker.py` → (无内部依赖)
- `stability_monitor.py` → (无内部依赖)

### `core/config/`

- `asset_registry.py` → (无内部依赖)
- `consistency.py` → (无内部依赖)

### `core/contracts/`

- `adapters.py` → `core.contracts.cap_result`, `core.runtime.typed_clock`
- `calibrator_contract.py` → (无内部依赖)
- `cap_result.py` → (无内部依赖)
- `domain_keys.py` → (无内部依赖)
- `enums.py` → (无内部依赖)
- `events.py` → (无内部依赖)
- `exceptions.py` → (无内部依赖)
- `ids.py` → (无内部依赖)
- `journal_contract.py` → (无内部依赖)
- `journal_sla.py` → (无内部依赖)
- `phantom_contract.py` → `core.data.write_ahead_log`, `core.observability.live_alert_hub`
- `position_events.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)
- `strategy_magic.py` → (无内部依赖)
- `validators.py` → (无内部依赖)

### `core/contracts/domain/`

- `brain_decision_proposal.py` → `core.contracts.domain_keys`, `core.contracts.enums`
- `communication_envelope.py` → `core.contracts.domain_keys`, `core.contracts.enums`
- `communication_record.py` → `core.contracts.domain.communication_envelope`, `core.contracts.domain.dispatch_result`, `core.contracts.domain_keys`, `core.contracts.schema_versions`
- `decision_candidate.py` → `core.contracts.domain_keys`
- `decision_intent.py` → `core.contracts.domain_keys`, `core.contracts.enums`
- `decision_record.py` → `core.contracts.domain_keys`
- `dispatch_context.py` → (无内部依赖)
- `dispatch_request.py` → `core.contracts.domain.communication_envelope`, `core.contracts.domain_keys`
- `dispatch_result.py` → `core.contracts.domain_keys`, `core.contracts.enums`
- `execution_event.py` → `core.contracts.domain_keys`
- `protocol_override.py` → `core.contracts.enums`
- `replay_execution_record.py` → `core.contracts.domain_keys`, `core.contracts.schema_versions`, `core.ledger.governance_sources`, `core.ledger.services.replay_trace_refs`
- `risk_verdict.py` → `core.contracts.domain_keys`, `core.contracts.enums`
- `system_mode_state.py` → `core.contracts.enums`

### `core/contracts/serialization/`

- `json_codec.py` → (无内部依赖)

### `core/contracts/training/`

- `label_contract.py` → (无内部依赖)
- `training_contract.py` → (无内部依赖)
- `training_recipe.py` → (无内部依赖)

### `core/data/`

- `event_writer.py` → (无内部依赖)
- `projections.py` → `core.contracts.events`
- `ticket_resolver.py` → (无内部依赖)
- `wap.py` → (无内部依赖)
- `write_ahead_log.py` → (无内部依赖)

### `core/deployment/`

- `atomic_file_writer.py` → (无内部依赖)
- `blue_green.py` → `core.deployment.atomic_file_writer`
- `brain_alert.py` → (无内部依赖)
- `brain_config_validator.py` → `core.brains.adapters`, `core.features.schemas.registry`
- `brain_lifecycle_manager.py` → `core.config.asset_registry`, `core.deployment.atomic_file_writer`, `core.deployment.path_defaults`, `core.features.feature_service`, `core.governance.governance_service`, `core.runtime.signal_pipeline`
- `brain_registration_gate.py` → `core.brains.adapters`, `core.brains.services.brain_factory`, `core.deployment.brain_config_validator`
- `capability_registry.py` → `core.contracts.domain_keys`
- `compliance_audit.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `compliance_control_matrix.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `compliance_export.py` → (无内部依赖)
- `config_hot_reload.py` → `core.contracts.domain_keys`
- `deployment_executor.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `deployment_plan.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.metric_names`
- `domain_keys.py` → `core.contracts.domain_keys`
- `environment_config.py` → `core.contracts.domain_keys`
- `evidence_bundle.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.diagnostics_dashboard`, `core.observability.metric_names`
- `feature_update_producer.py` → `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`
- `final_audit.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `governance_summary.py` → `core.contracts.domain_keys`
- `health_check.py` → `core.contracts.domain_keys`
- `lifecycle_manager.py` → `core.contracts.domain_keys`, `core.deployment.state_persistence`, `core.observability.metric_names`
- `operational_support.py` → `core.contracts.domain_keys`
- `operations_timeline.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.schema_versions`
- `ops_maturity.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `path_defaults.py` → (无内部依赖)
- `permission_audit.py` → `core.deployment.atomic_file_writer`
- `postmortem_report.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_certification.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`
- `release_gate.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.operational_support`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_pipeline.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_readiness.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.capability_registry`, `core.deployment.governance_summary`, `core.deployment.health_check`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `release_registry.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.governance_summary`, `core.deployment.schema_versions`
- `replay_isolation.py` → `core.contracts.domain.dispatch_result`, `core.contracts.domain_keys`, `core.contracts.enums`, `core.protocol.schema_versions`, `core.protocol.services.communication_dispatcher`
- `rollback_drill.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.schema_versions`, `core.deployment.validation_mode`
- `runbook_engine.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`, `core.deployment.health_check`, `core.deployment.operational_support`, `core.deployment.schema_versions`, `core.deployment.validation_mode`, `core.observability.diagnostics_dashboard`, `core.observability.metric_names`
- `scheduled_task_registry.py` → (无内部依赖)
- `scheduler_service.py` → `core.brains.services.brain_promotion`, `core.contracts.domain_keys`, `core.data.projections`, `core.deployment.atomic_file_writer`, `core.deployment.brain_alert`, `core.deployment.scheduled_task_registry`, `core.execution.pre_trade_guards`, `core.governance.shadow_tracker`, `core.observability.metric_names`
- `schema_versions.py` → (无内部依赖)
- `service_container.py` → `apps.engine.orchestrator`, `apps.engine.runtime_loop`, `core.brains.services.brain_factory`, `core.brains.services.brain_run_service`, `core.brains.services.dynamic_brain_weighter`, `core.contracts.domain.system_mode_state`, `core.contracts.domain_keys`, `core.contracts.enums`, `core.deployment.compliance_audit`, `core.deployment.compliance_control_matrix`, `core.deployment.config_hot_reload`, `core.deployment.deployment_executor`, `core.deployment.deployment_plan`, `core.deployment.environment_config`, `core.deployment.evidence_bundle`, `core.deployment.final_audit`, `core.deployment.health_check`, `core.deployment.operations_timeline`, `core.deployment.ops_maturity`, `core.deployment.postmortem_report`, `core.deployment.release_certification`, `core.deployment.release_gate`, `core.deployment.release_pipeline`, `core.deployment.release_readiness`, `core.deployment.release_registry`, `core.deployment.rollback_drill`, `core.deployment.runbook_engine`, `core.deployment.schema_versions`, `core.execution.execution_manager`, `core.execution.fix_contracts`, `core.features.feature_service`, `core.feedback.brain_performance_tracker`, `core.feedback.decision_scorer`, `core.feedback.feedback_loop`, `core.feedback.outcome_collector`, `core.governance.governance_rule_engine`, `core.governance.governance_service`, `core.ledger.services.communication_inspection_service`, `core.ledger.services.communication_operations_service`, `core.ledger.services.communication_record_reader`, `core.ledger.services.communication_record_writer`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_replay_service`, `core.ledger.services.decision_record_writer`, `core.ledger.services.execution_event_reader`, `core.ledger.services.execution_event_writer`, `core.ledger.services.execution_reconciliation_service`, `core.ledger.storage.jsonl_ledger_store`, `core.market.position_tracker`, `core.observability.alert_channels`, `core.observability.alert_service`, `core.observability.audit_log`, `core.observability.diagnostics_dashboard`, `core.observability.metric_names`, `core.observability.metrics_collector`, `core.observability.slo_service`, `core.parliament.parliament_service`, `core.protocol.services.communication_dispatcher`, `core.protocol.services.decision_compiler`, `core.protocol.services.file_queue_communication_adapter`, `core.protocol.services.file_queue_receipt_reader`, `core.protocol.services.fix_communication_adapter`, `core.protocol.services.idempotency`, `core.protocol.services.intent_message_builder`, `core.protocol.services.mt5_communication_adapter`, `core.protocol.services.override_resolver`, `core.protocol.services.stub_communication_adapter`, `core.protocol.services.venue_router`, `core.protocol.services.zmq_communication_adapter`, `core.risk.risk_evaluation_service`, `core.risk.risk_policies`, `core.state.schema_versions`, `core.state.services.control_snapshot_service`, `core.state.stores.override_store`, `core.state.stores.system_mode_store`
- `startup_validator.py` → `core.features.feature_service`, `core.features.local_feature_store`
- `state_persistence.py` → `core.contracts.domain_keys`, `core.deployment.atomic_file_writer`
- `validation_mode.py` → `core.contracts.domain_keys`

### `core/execution/`

- `barrier_strategy.py` → `core.execution.strategy_line`, `core.features.feature_assembler`, `core.features.schemas.v9_institutional_schema`
- `brain_gates.py` → (无内部依赖)
- `broker_adapter.py` → (无内部依赖)
- `capital_allocator.py` → `core.metrics.portfolio_optimizer`
- `conformal_calibrator.py` → (无内部依赖)
- `conformal_ou_gate.py` → `core.brains.brain_registry`
- `correlation_sizer.py` → (无内部依赖)
- `cross_strategy_coordinator.py` → (无内部依赖)
- `dynamic_sl_tp.py` → (无内部依赖)
- `execution_manager.py` → `core.contracts.exceptions`, `core.observability.metric_names`
- `execution_queue.py` → `core.contracts.strategy_magic`, `core.execution.live_order_sender`, `core.protocol.services.zmq_receipt_listener`
- `exit_reason.py` → (无内部依赖)
- `exit_watchdog.py` → `core.protocol.services.zmq_receipt_listener`
- `fill_simulator.py` → `core.contracts.ids`, `core.execution.gateway_contracts`
- `fix_contracts.py` → (无内部依赖)
- `fix_execution_mapper.py` → `core.contracts.ids`, `core.execution.fix_contracts`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`
- `fix_gateway_adapter.py` → `core.execution.fix_contracts`, `core.execution.fix_execution_mapper`, `core.execution.fix_message_builder`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`
- `fix_message_builder.py` → `core.execution.fix_contracts`, `core.execution.gateway_contracts`
- `gate_reachability.py` → (无内部依赖)
- `gateway_contracts.py` → (无内部依赖)
- `gods_eye.py` → (无内部依赖)
- `kelly_sizer.py` → `core.execution.pwin_chain`
- `limit_order_monitor.py` → (无内部依赖)
- `live_order_sender.py` → `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.contracts.strategy_magic`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.execution.broker_adapter`, `core.execution.mt5_broker_adapter`, `core.execution.mt5_worker`, `core.protocol.live_execution_contract`, `core.protocol.schema_versions`, `core.protocol.services.zmq_receipt_listener`
- `managed_close.py` → `core.contracts.strategy_magic`, `core.execution.live_order_sender`, `core.execution.reentry_guard`, `core.runtime.fault_handler`
- `market_efficiency.py` → (无内部依赖)
- `market_impact.py` → (无内部依赖)
- `meta_exit_engine.py` → (无内部依赖)
- `meta_filter_gate.py` → `core.brains.adapters.meta_filter_adapter`, `core.features.schemas.microstructure_schema`, `core.features.schemas.v9_institutional_schema`
- `meta_filter_routing.py` → `core.execution.meta_pipeline`, `core.execution.strategy_decision`
- `meta_pipeline.py` → `core.execution.dynamic_sl_tp`, `core.execution.kelly_sizer`, `core.schemas.trading_contracts`
- `meta_signal_filter.py` → `core.brains.online_mlp_model`, `core.features.adapters.microstructure_feature_adapter`, `core.features.schemas.microstructure_schema`
- `micro_strategy.py` → `core.execution.strategy_line`
- `microstructure_gate.py` → (无内部依赖)
- `mt5_broker_adapter.py` → `core.execution.mt5_worker`
- `mt5_worker.py` → `core.protocol.services.resilience`
- `net_out_close_handler.py` → `core.execution.live_order_sender`
- `ofi_gate.py` → (无内部依赖)
- `ood_gateway.py` → (无内部依赖)
- `order_state_machine.py` → `core.execution.gateway_contracts`
- `paper_gateway.py` → `core.execution.fill_simulator`, `core.execution.gateway_contracts`, `core.execution.order_state_machine`, `core.observability.metric_names`
- `portfolio_netting.py` → (无内部依赖)
- `portfolio_risk.py` → `core.execution.capital_allocator`
- `position_manager.py` → `core.execution.meta_exit_engine`, `core.execution.trail_stop_engine`
- `pre_trade_guards.py` → `core.config.asset_registry`, `core.execution.session_detector`
- `pwin_chain.py` → (无内部依赖)
- `quality_analyzer.py` → `core.execution.gateway_contracts`, `core.execution.quality_contracts`, `core.execution.schema_versions`
- `quality_contracts.py` → (无内部依赖)
- `reentry_guard.py` → `core.execution.exit_reason`
- `regime_direction_gate.py` → (无内部依赖)
- `regime_gate.py` → `core.execution.trend_detector`
- `rule_engine_strategy.py` → `core.execution.strategy_context`, `core.execution.strategy_decision`, `core.execution.strategy_protocol`, `core.strategies.structural_swing_v1`
- `schema_versions.py` → (无内部依赖)
- `session_detector.py` → (无内部依赖)
- `statarb_strategy.py` → `core.execution.strategy_line`
- `strategy_budget.py` → `core.contracts.cap_result`
- `strategy_context.py` → (无内部依赖)
- `strategy_decision.py` → (无内部依赖)
- `strategy_line.py` → `core.brains.services.dynamic_brain_weighter`, `core.config.asset_registry`, `core.execution.brain_gates`, `core.execution.conformal_ou_gate`, `core.execution.dynamic_sl_tp`, `core.execution.kelly_sizer`, `core.execution.meta_filter_routing`, `core.execution.meta_pipeline`, `core.execution.ofi_gate`, `core.execution.pre_trade_guards`, `core.execution.pwin_chain`, `core.execution.strategy_context`, `core.execution.strategy_decision`, `core.execution.strategy_protocol`, `core.execution.trend_isolation_gates`, `core.execution.trend_volume_guard`, `core.parliament.contract_groups`, `core.runtime.fault_handler`, `core.runtime.shadow_recorder`
- `strategy_protocol.py` → `core.execution.strategy_context`, `core.execution.strategy_decision`
- `strategy_type.py` → (无内部依赖)
- `swing_strategy.py` → `core.execution.strategy_line`, `core.features.feature_assembler`, `core.runtime.h1_features`
- `trail_stop_engine.py` → `core.execution.position_manager`
- `trend_detector.py` → (无内部依赖)
- `trend_isolation_gates.py` → `core.execution.strategy_decision`, `core.execution.strategy_line`
- `trend_volume_guard.py` → (无内部依赖)

### `core/features/`

- `data_augmentation.py` → (无内部依赖)
- `feature_assembler.py` → `core.features.feature_router`, `core.features.schemas.registry`
- `feature_router.py` → `core.features.schemas.registry`
- `feature_service.py` → `apps.engine.runtime_loop`, `core.contracts.ids`, `core.deployment.brain_alert`, `core.execution.pre_trade_guards`, `core.features.schemas.registry`, `core.features.store_contracts`
- `feature_snapshot.py` → `core.features.store_contracts`
- `local_feature_store.py` → `core.features.store_contracts`
- `meta_feature_builder.py` → `core.brains.adapters.params_brain_adapter`, `core.features.schemas.registry`, `core.features.schemas.v9_institutional_schema`
- `ofi_collector.py` → (无内部依赖)
- `rolling_normalizer.py` → (无内部依赖)
- `store_contracts.py` → (无内部依赖)
- `update_job.py` → `core.features.store_contracts`

### `core/features/adapters/`

- `microstructure_feature_adapter.py` → `core.contracts.exceptions`, `core.features.schemas.microstructure_schema`
- `v9_feature_adapter.py` → `core.features.schemas.v9_institutional_schema`

### `core/features/computers/`

- `btc_feature_augmenter.py` → `core.execution.mt5_worker`
- `daily_computer.py` → `core.features.schemas.daily_swing_schema`
- `live_daily_provider.py` → `core.execution.mt5_worker`, `core.features.computers.daily_computer`
- `microstructure_computer.py` → `core.execution.mt5_worker`
- `v9_live_computer.py` → `core.execution.mt5_worker`
- `v9_micro_computer.py` → `core.execution.mt5_worker`, `core.features.computers.microstructure_computer`, `core.features.computers.v9_live_computer`, `core.features.schemas.microstructure_schema`

### `core/features/schemas/`

- `btc_macro_enhanced_schema.py` → (无内部依赖)
- `daily_swing_schema.py` → (无内部依赖)
- `microstructure_schema.py` → `core.features.store_contracts`
- `registry.py` → `core.features.feature_assembler`, `core.features.schemas.btc_macro_enhanced_schema`, `core.features.schemas.daily_swing_schema`, `core.features.schemas.microstructure_schema`, `core.features.schemas.swing_enhanced_schema`, `core.features.schemas.v9_institutional_schema`, `core.features.schemas.v9_micro_schema`
- `swing_enhanced_schema.py` → `core.features.schemas.daily_swing_schema`
- `v9_institutional_schema.py` → (无内部依赖)
- `v9_micro_schema.py` → `core.features.schemas.microstructure_schema`, `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`

### `core/feedback/`

- `brain_performance_tracker.py` → (无内部依赖)
- `brain_pnl_ledger.py` → `core.contracts.events`
- `brain_quality_engine.py` → (无内部依赖)
- `decision_scorer.py` → (无内部依赖)
- `experience_replay.py` → (无内部依赖)
- `feedback_loop.py` → (无内部依赖)
- `live_journal_metrics.py` → (无内部依赖)
- `online_feedback_hook.py` → (无内部依赖)
- `outcome_collector.py` → (无内部依赖)
- `param_optimizer.py` → (无内部依赖)
- `performance_analytics.py` → (无内部依赖)

### `core/governance/`

- `governance_rule_engine.py` → `core.brains.brain_registry`
- `governance_service.py` → `core.contracts.exceptions`, `core.infrastructure.distributed_lock`, `core.state.catalog`, `core.state.writer`
- `shadow_tracker.py` → (无内部依赖)

### `core/infrastructure/`

- `distributed_lock.py` → (无内部依赖)

### `core/ledger/`

- `governance_sources.py` → `core.contracts.domain_keys`
- `schema_versions.py` → (无内部依赖)
- `stream_names.py` → (无内部依赖)

### `core/ledger/services/`

- `communication_inspection_service.py` → `core.contracts.domain_keys`
- `communication_operations_service.py` → `core.contracts.domain_keys`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_trace_refs`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`, `core.ledger.services.replay_record_refs`
- `communication_record_reader.py` → `core.contracts.domain_keys`, `core.ledger.stream_names`
- `communication_record_writer.py` → `core.contracts.domain.communication_record`, `core.contracts.ids`, `core.ledger.stream_names`
- `communication_replay_executor.py` → `core.contracts.domain_keys`, `core.contracts.enums`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`, `core.ledger.services.replay_record_refs`, `core.ledger.services.replay_trace_refs`
- `communication_replay_gate.py` → `core.contracts.domain_keys`, `core.contracts.enums`, `core.ledger.services.gate_decision_refs`, `core.ledger.services.replay_plan_refs`
- `communication_replay_service.py` → `core.contracts.domain_keys`, `core.ledger.services.communication_replay_gate`, `core.ledger.services.communication_trace_refs`
- `communication_trace_refs.py` → `core.contracts.domain_keys`
- `decision_record_writer.py` → `core.contracts.domain.decision_record`, `core.contracts.ids`, `core.ledger.schema_versions`, `core.ledger.stream_names`
- `execution_event_reader.py` → `core.contracts.domain_keys`
- `execution_event_writer.py` → `core.contracts.domain.execution_event`, `core.contracts.ids`, `core.ledger.schema_versions`, `core.ledger.stream_names`
- `execution_reconciliation_service.py` → `core.contracts.domain_keys`, `core.observability.metric_names`
- `gate_decision_refs.py` → `core.contracts.domain_keys`
- `journal_cleanup.py` → `core.contracts.strategy_magic`, `core.infrastructure.distributed_lock`
- `journal_gate.py` → `core.contracts.journal_sla`, `core.data.ticket_resolver`
- `pnl_guard.py` → (无内部依赖)
- `replay_execution_reader.py` → `core.contracts.domain_keys`, `core.ledger.stream_names`
- `replay_execution_writer.py` → `core.contracts.domain.replay_execution_record`, `core.contracts.ids`, `core.ledger.stream_names`
- `replay_plan_refs.py` → `core.contracts.domain_keys`
- `replay_record_refs.py` → `core.contracts.domain_keys`
- `replay_trace_refs.py` → `core.contracts.domain_keys`

### `core/ledger/storage/`

- `jsonl_ledger_store.py` → `core.contracts.serialization.json_codec`, `core.ledger.stream_names`

### `core/market/`

- `calendar.py` → (无内部依赖)
- `mtf_price_service.py` → (无内部依赖)
- `position_tracker.py` → (无内部依赖)
- `signal_processor.py` → (无内部依赖)

### `core/metrics/`

- `brinson_attribution.py` → (无内部依赖)
- `factor_attribution.py` → (无内部依赖)
- `financial_metrics.py` → (无内部依赖)
- `portfolio_optimizer.py` → (无内部依赖)

### `core/observability/`

- `_health_helpers.py` → (无内部依赖)
- `alert_channels.py` → `core.observability.alert_service`, `core.observability.localization`
- `alert_runbook_bridge.py` → (无内部依赖)
- `alert_service.py` → (无内部依赖)
- `audit_log.py` → (无内部依赖)
- `data_health_schema.py` → (无内部依赖)
- `data_health_service.py` → `core.observability._health_helpers`, `core.observability.data_health_schema`, `core.observability.health_checks`, `core.state.catalog`, `core.state.writer`
- `data_loss.py` → (无内部依赖)
- `degradation.py` → (无内部依赖)
- `diagnostics_dashboard.py` → `core.observability.metric_names`
- `entry_context_guard.py` → `core.observability.data_health_service`, `core.observability.data_loss`
- `event_bus.py` → (无内部依赖)
- `event_schema.py` → (无内部依赖)
- `health_checks.py` → `core.observability._health_helpers`, `core.observability.data_health_schema`
- `invariant_engine.py` → `core.data.write_ahead_log`
- `live_alert_hub.py` → `core.observability.alert_channels`, `core.observability.alert_runbook_bridge`, `core.observability.alert_service`, `core.protocol.services.resilience`
- `localization.py` → (无内部依赖)
- `message_broker.py` → `core.observability.event_bus`
- `metric_names.py` → (无内部依赖)
- `metrics_collector.py` → (无内部依赖)
- `mlflow_bridge.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)
- `slo_service.py` → `core.contracts.domain_keys`, `core.observability.metric_names`, `core.observability.schema_versions`
- `tracing.py` → (无内部依赖)

### `core/parliament/`

- `contract_groups.py` → `core.brains.brain_registry`, `core.brains.services.ab_test`, `core.schemas.trading_contracts`
- `group_consensus.py` → `core.brains.services.dynamic_brain_weighter`, `core.execution.capital_allocator`, `core.parliament.contract_groups`, `core.runtime.fault_handler`
- `parliament_service.py` → `core.contracts.domain.decision_candidate`, `core.contracts.ids`, `core.parliament.contract_groups`, `core.parliament.schema_versions`
- `schema_versions.py` → (无内部依赖)

### `core/protocol/`

- `event_bar_sync.py` → `core.execution.mt5_worker`, `core.execution.pre_trade_guards`
- `live_execution_contract.py` → (无内部依赖)
- `schema_versions.py` → (无内部依赖)

### `core/protocol/services/`

- `communication_adapter.py` → (无内部依赖)
- `communication_adapter_registry.py` → (无内部依赖)
- `communication_dispatcher.py` → `core.contracts.domain.dispatch_request`, `core.contracts.domain.dispatch_result`, `core.contracts.domain_keys`, `core.contracts.enums`, `core.contracts.ids`, `core.observability.metric_names`, `core.protocol.schema_versions`, `core.protocol.services.communication_adapter_registry`
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
- `zmq_communication_adapter.py` → `core.contracts.domain.dispatch_result`, `core.contracts.enums`, `core.contracts.serialization.json_codec`, `core.protocol.schema_versions`, `core.protocol.services.resilience`
- `zmq_receipt_listener.py` → (无内部依赖)

### `core/risk/`

- `regime_detector.py` → (无内部依赖)
- `risk_evaluation_service.py` → `core.contracts.domain.risk_verdict`, `core.contracts.enums`, `core.contracts.ids`, `core.risk.risk_policies`, `core.risk.schema_versions`
- `risk_policies.py` → `core.contracts.enums`
- `schema_versions.py` → (无内部依赖)

### `core/runtime/`

- `alpha_budget_contracts.py` → `core.alpha.schema_versions`, `core.runtime.schema_versions`
- `alpha_budget_usage_reporter.py` → `core.runtime.alpha_budget_contracts`, `core.runtime.schema_versions`
- `alpha_budget_usage_store.py` → `core.runtime.alpha_budget_contracts`, `core.runtime.schema_versions`
- `alpha_risk_budget_gate.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.alpha_budget_contracts`, `core.runtime.alpha_budget_usage_store`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `approval_contracts.py` → `core.execution.gateway_contracts`, `core.runtime.schema_versions`, `core.strategies.contracts`
- `circuit_breaker_reset.py` → `core.runtime.time_utils`
- `cooldown.py` → (无内部依赖)
- `cycle_replay.py` → `core.runtime.evidence_reader`, `core.runtime.schema_versions`
- `daily_ops_scheduler.py` → `core.execution.pre_trade_guards`, `core.features.local_feature_store`, `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.governance.governance_service`, `core.runtime.live_cycle`, `core.runtime.time_utils`, `core.state.catalog`, `core.state.writer`, `scripts.daily_ops`, `scripts.training.governance_scheduler`
- `data_health_monitor.py` → `core.observability.data_health_service`
- `deal_selection.py` → (无内部依赖)
- `dispatch_post.py` → `core.execution.pre_trade_guards`, `core.runtime.order_dispatch`, `core.runtime.time_utils`
- `evidence_contracts.py` → `core.runtime.integration_contracts`, `core.runtime.schema_versions`
- `evidence_reader.py` → `core.ledger.stream_names`
- `evidence_writer.py` → `core.contracts.ids`, `core.ledger.stream_names`, `core.runtime.evidence_contracts`, `core.runtime.integration_contracts`
- `execution_gates.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `execution_gateway_router.py` → `core.execution.gateway_contracts`
- `execution_pipeline.py` → `core.contracts.ids`, `core.execution.quality_analyzer`, `core.execution.quality_contracts`, `core.runtime.execution_gateway_router`, `core.runtime.integration_contracts`, `core.runtime.schema_versions`, `core.runtime.signal_order_builder`, `core.strategies.registry`
- `execution_state.py` → `core.runtime.time_utils`, `core.state.catalog`, `core.state.writer`
- `fault_handler.py` → `core.runtime.fault_handler`
- `feature_freshness.py` → `core.execution.pre_trade_guards`, `core.runtime.time_utils`
- `gate_audit_recorder.py` → (无内部依赖)
- `gods_eye_bridge.py` → `core.execution.gods_eye`, `core.runtime.time_utils`
- `golden_master.py` → `core.runtime.fault_handler`
- `h1_features.py` → (无内部依赖)
- `integration_contracts.py` → `core.execution.gateway_contracts`, `core.execution.quality_contracts`, `core.runtime.approval_contracts`, `core.strategies.contracts`
- `legacy_dispatch_reference.py` → (无内部依赖)
- `live_bootstrap.py` → `core.deployment.feature_update_producer`, `core.features.adapters.microstructure_feature_adapter`, `core.features.adapters.v9_feature_adapter`, `core.features.computers.live_daily_provider`, `core.features.computers.microstructure_computer`, `core.features.computers.v9_live_computer`, `core.features.feature_service`, `core.features.local_feature_store`, `core.features.schemas.microstructure_schema`, `core.runtime.time_utils`
- `live_cycle.py` → `core.config.asset_registry`, `core.contracts.domain.dispatch_context`, `core.contracts.strategy_magic`, `core.deployment.feature_update_producer`, `core.execution.conformal_calibrator`, `core.execution.conformal_ou_gate`, `core.execution.cross_strategy_coordinator`, `core.execution.execution_queue`, `core.execution.live_order_sender`, `core.execution.managed_close`, `core.execution.market_efficiency`, `core.execution.meta_filter_gate`, `core.execution.microstructure_gate`, `core.execution.mt5_worker`, `core.execution.net_out_close_handler`, `core.execution.portfolio_netting`, `core.execution.portfolio_risk`, `core.execution.pre_trade_guards`, `core.execution.reentry_guard`, `core.execution.regime_gate`, `core.features.computers.btc_feature_augmenter`, `core.features.meta_feature_builder`, `core.features.schemas.registry`, `core.infrastructure.distributed_lock`, `core.market.mtf_price_service`, `core.observability.data_health_service`, `core.observability.degradation`, `core.parliament.contract_groups`, `core.parliament.group_consensus`, `core.runtime.circuit_breaker_reset`, `core.runtime.cooldown`, `core.runtime.daily_ops_scheduler`, `core.runtime.dispatch_post`, `core.runtime.execution_state`, `core.runtime.fault_handler`, `core.runtime.feature_freshness`, `core.runtime.gods_eye_bridge`, `core.runtime.golden_master`, `core.runtime.management_phase`, `core.runtime.market_ingress`, `core.runtime.mia_close`, `core.runtime.micro_persist`, `core.runtime.modify_trail_dispatch`, `core.runtime.order_dispatch`, `core.runtime.ou_hurst`, `core.runtime.pnl_recording`, `core.runtime.position_close_adapter`, `core.runtime.position_ownership`, `core.runtime.position_registration`, `core.runtime.pre_close_check`, `core.runtime.reconciliation`, `core.runtime.reentry_alert`, `core.runtime.reentry_recording`, `core.runtime.restart_state`, `core.runtime.session_guards`, `core.runtime.signal_health`, `core.runtime.signal_pipeline`, `core.runtime.signal_settlement`, `core.runtime.strategy_builder`, `core.runtime.strategy_evaluator`, `core.runtime.time_utils`, `core.runtime.trade_notify`
- `live_startup.py` → `core.governance.governance_service`, `core.risk.risk_evaluation_service`, `core.risk.risk_policies`, `core.runtime.fault_handler`
- `management_phase.py` → `core.contracts.domain.dispatch_context`, `core.contracts.strategy_magic`, `core.execution.capital_allocator`, `core.execution.live_order_sender`, `core.execution.managed_close`, `core.features.schemas.registry`, `core.parliament.contract_groups`, `core.runtime.fault_handler`, `core.runtime.live_cycle`, `core.runtime.market_ingress`, `core.runtime.mia_close`, `core.runtime.modify_trail_dispatch`, `core.runtime.ou_hurst`, `core.runtime.signal_health`, `core.runtime.time_utils`, `core.runtime.trail_dispatch`, `core.trading.position_lifecycle`, `core.trading.ratchet_risk`
- `market_ingress.py` → `core.config.asset_registry`, `core.execution.mt5_worker`, `core.runtime.fault_handler`
- `mia_close.py` → `core.contracts.strategy_magic`, `core.ledger.services.pnl_guard`, `core.runtime.deal_selection`, `core.runtime.time_utils`
- `micro_persist.py` → `core.features.local_feature_store`, `core.features.schemas.microstructure_schema`, `core.features.store_contracts`
- `modify_trail_dispatch.py` → `core.contracts.strategy_magic`, `core.execution.live_order_sender`
- `order_dispatch.py` → `apps.engine.runtime_loop`, `core.brains.brain_registry`, `core.contracts.domain.decision_intent`, `core.contracts.domain.system_mode_state`, `core.contracts.enums`, `core.contracts.ids`, `core.state.schema_versions`
- `ou_hurst.py` → (无内部依赖)
- `pnl_recording.py` → `core.brains.brain_registry`
- `position_close_adapter.py` → `core.contracts.position_events`, `core.execution.reentry_guard`, `core.ledger.services.journal_cleanup`, `core.runtime.deal_selection`
- `position_ownership.py` → (无内部依赖)
- `position_registration.py` → `core.execution.trail_stop_engine`, `core.runtime.fault_handler`
- `pre_close_check.py` → `core.market.calendar`
- `reconciliation.py` → `core.contracts.events`, `core.contracts.strategy_magic`, `core.data.event_writer`, `core.execution.reentry_guard`, `core.runtime.deal_selection`, `core.runtime.fault_handler`, `core.runtime.time_utils`
- `reentry_alert.py` → `core.runtime.time_utils`
- `reentry_recording.py` → `core.execution.reentry_guard`, `core.runtime.time_utils`
- `restart_state.py` → `core.contracts.strategy_magic`, `core.execution.reentry_guard`
- `schema_versions.py` → (无内部依赖)
- `session_guards.py` → `core.execution.live_order_sender`, `core.execution.pre_trade_guards`, `core.runtime.fault_handler`, `core.runtime.time_utils`
- `shadow_recorder.py` → `core.contracts.domain.decision_record`, `core.contracts.ids`, `core.ledger.schema_versions`, `core.ledger.storage.jsonl_ledger_store`
- `signal_health.py` → (无内部依赖)
- `signal_order_builder.py` → `core.contracts.ids`, `core.execution.gateway_contracts`, `core.runtime.integration_contracts`, `core.strategies.contracts`
- `signal_pipeline.py` → `core.brains.schema_versions`, `core.contracts.domain.brain_decision_proposal`, `core.contracts.ids`
- `signal_settlement.py` → `core.contracts.events`, `core.data.event_writer`, `core.runtime.reconciliation`
- `strategy_builder.py` → `core.config.asset_registry`, `core.execution.barrier_strategy`, `core.execution.meta_pipeline`, `core.execution.micro_strategy`, `core.execution.rule_engine_strategy`, `core.execution.statarb_strategy`, `core.execution.strategy_budget`, `core.execution.strategy_line`, `core.execution.swing_strategy`, `core.parliament.contract_groups`, `core.runtime.time_utils`, `core.strategies.structural_swing_v1`
- `strategy_config_validator.py` → (无内部依赖)
- `strategy_evaluator.py` → `core.execution.correlation_sizer`, `core.execution.cross_strategy_coordinator`, `core.execution.execution_queue`, `core.execution.ood_gateway`, `core.execution.portfolio_risk`, `core.execution.pre_trade_guards`, `core.execution.reentry_guard`, `core.execution.regime_direction_gate`, `core.execution.regime_gate`, `core.execution.strategy_context`, `core.observability.degradation`, `core.runtime.gate_audit_recorder`, `core.runtime.time_utils`
- `summary_service.py` → `core.runtime.evidence_reader`, `core.runtime.schema_versions`
- `supervised_scheduler.py` → (无内部依赖)
- `time_utils.py` → (无内部依赖)
- `timeframe_scaling.py` → (无内部依赖)
- `trade_notify.py` → `core.runtime.time_utils`
- `trail_dispatch.py` → (无内部依赖)
- `typed_clock.py` → (无内部依赖)

### `core/schemas/`

- `trading_contracts.py` → (无内部依赖)

### `core/simulation/`

- `spread_model.py` → (无内部依赖)

### `core/state/`

- `catalog.py` → (无内部依赖)
- `freshness_guard.py` → `core.state.catalog`
- `schema_versions.py` → (无内部依赖)
- `writer.py` → `core.state.catalog`

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
- `structural_swing_v1.py` → (无内部依赖)

### `core/trading/`

- `contracts.py` → (无内部依赖)
- `position_lifecycle.py` → `core.execution.position_manager`, `core.trading.contracts`
- `ratchet_risk.py` → (无内部依赖)
- `signal_refinement.py` → `core.trading.contracts`

### `core/training/`

- `brain_config.py` → `core.features.schemas.registry`
- `checkpoint.py` → (无内部依赖)
- `cpcv.py` → (无内部依赖)
- `custom_objectives.py` → (无内部依赖)
- `dataset.py` → (无内部依赖)
- `evaluation_report.py` → (无内部依赖)
- `experiment_tracker.py` → (无内部依赖)
- `model_card.py` → (无内部依赖)
- `model_hashing.py` → (无内部依赖)
- `profitability_calibrator.py` → `core.contracts.training.label_contract`
- `registries.py` → (无内部依赖)
- `trainer_protocol.py` → (无内部依赖)
- `training_registry.py` → `core.training.model_hashing`
- `utils.py` → (无内部依赖)

### `scripts/`

- `_analyze_btc_exits.py` → (无内部依赖)
- `_analyze_btc_improvement.py` → (无内部依赖)
- `_analyze_h1_swing_now.py` → (无内部依赖)
- `_analyze_h4_swing_now.py` → (无内部依赖)
- `_analyze_m15_swing_now.py` → (无内部依赖)
- `_evaluate_probation_m30_h1v2.py` → (无内部依赖)
- `_monitor_direction_concentration.py` → `core.deployment.brain_alert`, `core.deployment.scheduled_task_registry`
- `_train_h1_binary_final.py` → (无内部依赖)
- `_train_h4_binary_final.py` → (无内部依赖)
- `_train_m15_binary_final.py` → (无内部依赖)
- `alert_dispatcher.py` → (无内部依赖)
- `analyze_90501_institutional.py` → (无内部依赖)
- `analyze_deps.py` → (无内部依赖)
- `analyze_dual_symbol_trades.py` → (无内部依赖)
- `analyze_exit_optimization_effect.py` → (无内部依赖)
- `analyze_feature_shift.py` → (无内部依赖)
- `analyze_gate_activity.py` → (无内部依赖)
- `analyze_live_brain_performance.py` → (无内部依赖)
- `analyze_live_journal.py` → `core.data.ticket_resolver`
- `analyze_ou_pnl.py` → (无内部依赖)
- `analyze_recent_losses.py` → (无内部依赖)
- `analyze_shadow_exit.py` → (无内部依赖)
- `analyze_shadow_predictions.py` → (无内部依赖)
- `analyze_swing_pnl.py` → (无内部依赖)
- `analyze_trail_impact.py` → (无内部依赖)
- `analyze_xau_recent_entries.py` → (无内部依赖)
- `assess_system_health.py` → (无内部依赖)
- `audit_2day.py` → (无内部依赖)
- `audit_behavior_compliance.py` → (无内部依赖)
- `audit_brain_fleet.py` → (无内部依赖)
- `audit_btc_cross_validate.py` → (无内部依赖)
- `audit_btc_live_direction.py` → (无内部依赖)
- `audit_btc_v11_ledger.py` → (无内部依赖)
- `audit_cross_symbol_consistency.py` → `scripts.alert_dispatcher`
- `audit_data_exhaustive.py` → `core.data.ticket_resolver`
- `audit_data_final.py` → (无内部依赖)
- `audit_data_health_journal.py` → (无内部依赖)
- `audit_data_integrity.py` → `scripts.alert_dispatcher`
- `audit_data_module.py` → (无内部依赖)
- `audit_deferred_tasks.py` → (无内部依赖)
- `audit_entry_spread.py` → (无内部依赖)
- `audit_entry_spread_coverage.py` → (无内部依赖)
- `audit_full_pipeline.py` → (无内部依赖)
- `audit_institutional_performance.py` → (无内部依赖)
- `audit_live_brains.py` → (无内部依赖)
- `audit_live_health.py` → (无内部依赖)
- `audit_memory.py` → (无内部依赖)
- `audit_physics_thresholds.py` → (无内部依赖)
- `audit_pnl_ledger_integrity.py` → (无内部依赖)
- `audit_profitability.py` → (无内部依赖)
- `audit_state_of_system.py` → (无内部依赖)
- `audit_trade_quality.py` → (无内部依赖)
- `audit_xau_directional_bias.py` → (无内部依赖)
- `audit_xau_exits.py` → (无内部依赖)
- `augment_journal_strategy.py` → `core.contracts.strategy_magic`
- `backfill_fabricated_breakeven.py` → `core.infrastructure.distributed_lock`, `core.runtime.deal_selection`
- `backfill_journal_orphans.py` → (无内部依赖)
- `backfill_journal_pnl.py` → `core.infrastructure.distributed_lock`
- `backtest_rule_strategies.py` → `scripts.scan_barrier_params`
- `backtest_runner.py` → `core.backtest.data_feed`, `core.backtest.engine`, `core.backtest.metrics`, `core.backtest.strategy_adapter`, `core.contracts.strategy_magic`, `core.metrics.brinson_attribution`, `core.metrics.factor_attribution`
- `backtest_structural_swing.py` → `core.strategies.structural_swing_v1`
- `benchmark_zmq_latency.py` → (无内部依赖)
- `ble001_phase3b_migrate_hotpath.py` → (无内部依赖)
- `ble001_phase3c_fog_wrap.py` → (无内部依赖)
- `ble001_phase3d_coldpath_fog_wrap.py` → (无内部依赖)
- `ble001_phase3e_deferred_fog_wrap.py` → (无内部依赖)
- `brain.py` → `core.brains.brain_registry`, `core.deployment.brain_lifecycle_manager`, `core.governance.governance_service`
- `bridge_supervisor.py` → `scripts.mt5_bridge_worker`
- `build_btc_metafilter_v2_dataset.py` → `core.features.schemas.v9_institutional_schema`
- `build_metafilter_dataset.py` → `core.features.feature_router`
- `build_regime_snapshots.py` → (无内部依赖)
- `calibrate_binary_threshold.py` → (无内部依赖)
- `check_blueprint_compliance.py` → (无内部依赖)
- `check_data_health_contract.py` → (无内部依赖)
- `check_import_boundaries.py` → (无内部依赖)
- `check_omega_compliance.py` → (无内部依赖)
- `check_omega_pre_push.py` → (无内部依赖)
- `check_preconditions.py` → (无内部依赖)
- `check_symbol_liveness.py` → (无内部依赖)
- `check_training_readiness.py` → `core.state.catalog`, `core.state.writer`
- `check_training_triggers.py` → (无内部依赖)
- `ci_prepare_v9_shadow_fixtures.py` → `apps.engine.main_v9_shadow`
- `classify_ble001.py` → (无内部依赖)
- `clean_ledger_bloat.py` → (无内部依赖)
- `cleanup_claude_transcripts.py` → (无内部依赖)
- `commander_g2_metafilter_path.py` → (无内部依赖)
- `commander_g3_alpha_vacuum.py` → (无内部依赖)
- `commander_g4_g6_g7_coverage_xau.py` → (无内部依赖)
- `commander_guardrails_arch.py` → (无内部依赖)
- `coverage_baseline.py` → (无内部依赖)
- `daily_cost_report.py` → (无内部依赖)
- `daily_ops.py` → `core.alpha.contracts`, `core.alpha.lifecycle_service`, `core.alpha.performance_store`, `core.alpha.portfolio_allocator`, `core.alpha.promotion_gate`, `core.alpha.registry`, `core.brains.services.brain_leaderboard`, `core.brains.services.dynamic_brain_weighter`, `core.contracts.exceptions`, `core.contracts.journal_sla`, `core.contracts.strategy_magic`, `core.contracts.training.label_contract`, `core.data.ticket_resolver`, `core.deployment.scheduled_task_registry`, `core.execution.conformal_calibrator`, `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.feedback.live_journal_metrics`, `core.feedback.param_optimizer`, `core.governance.governance_service`, `core.infrastructure.distributed_lock`, `core.ledger.services.journal_cleanup`, `core.ledger.services.journal_gate`, `core.observability.data_health_service`, `core.state.catalog`, `core.state.freshness_guard`, `core.state.writer`, `scripts.augment_journal_strategy`, `scripts.brain`, `scripts.check_training_readiness`, `scripts.feature_store_maintenance`, `scripts.feedback_loop`, `scripts.live_daily_recap`, `scripts.live_shadow_ensemble`, `scripts.paper_trade_simulator`, `scripts.training.brain_leaderboard`, `scripts.training.champion_challenger`, `scripts.training.governance_scheduler`, `scripts.training.label_builder`, `scripts.training.retraining_trigger`
- `data_integrity_check.py` → (无内部依赖)
- `data_pipeline_audit.py` → (无内部依赖)
- `dedup_journal.py` → (无内部依赖)
- `dedup_journal_by_ticket.py` → `core.infrastructure.distributed_lock`
- `deep_audit_live_data.py` → (无内部依赖)
- `deep_audit_probes.py` → (无内部依赖)
- `deploy_blue_green.py` → `core.deployment.blue_green`
- `diagnose_data_health_failures.py` → (无内部依赖)
- `diagnose_feature_drift.py` → (无内部依赖)
- `diagnose_journal_mt5_sev2.py` → (无内部依赖)
- `diagnose_mypy_baseline.py` → (无内部依赖)
- `diagnose_process_health.py` → (无内部依赖)
- `diagnose_sl_performance.py` → (无内部依赖)
- `dqaf053_phase1_sanitize.py` → `core.alpha.performance_store`, `core.alpha.registry`, `core.execution.conformal_calibrator`
- `dqaf_collect.py` → (无内部依赖)
- `export_ood_params.py` → `core.execution.ood_gateway`
- `extract_health_checks.py` → (无内部依赖)
- `feature_store_maintenance.py` → `core.deployment.feature_update_producer`, `core.deployment.scheduled_task_registry`, `core.features.computers.v9_live_computer`, `core.features.local_feature_store`, `core.features.update_job`
- `feedback_loop.py` → `core.feedback.brain_performance_tracker`
- `forensic_feature_analysis.py` → (无内部依赖)
- `generate_btc_empirical_scaler.py` → `core.features.adapters.microstructure_feature_adapter`, `core.features.schemas.microstructure_schema`, `core.features.schemas.registry`
- `generate_micro_scaler.py` → `core.features.adapters.microstructure_feature_adapter`, `core.features.schemas.microstructure_schema`, `core.features.schemas.registry`
- `governance_promote_m15.py` → (无内部依赖)
- `guard_git_stash.py` → (无内部依赖)
- `health_check.py` → (无内部依赖)
- `hook_architecture_gate.py` → (无内部依赖)
- `hook_blueprint_precheck.py` → (无内部依赖)
- `hook_mypy_check.py` → (无内部依赖)
- `hook_pre_push.py` → (无内部依赖)
- `ingest_live_journal_to_alpha.py` → `core.alpha.performance_store`, `core.runtime.schema_versions`, `scripts.trade_quality_report`
- `inject_regime_to_labels.py` → (无内部依赖)
- `inspect_ofi_history.py` → (无内部依赖)
- `journal_freeze_gate.py` → (无内部依赖)
- `live_audit_realtime.py` → (无内部依赖)
- `live_auto_healthcheck.py` → `core.deployment.scheduled_task_registry`, `scripts.live_dispatch_policy`
- `live_daily_recap.py` → `core.brains.services.brain_attribution_service`, `core.brains.services.brain_leaderboard`, `core.brains.services.dynamic_brain_weighter`, `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.governance.governance_service`, `core.parliament.contract_groups`, `scripts.daily_ops`, `scripts.live_data_quality_report`, `scripts.live_feature_quality_report`, `scripts.live_shadow_ensemble`, `scripts.shadow_live_compare_report`, `scripts.trade_quality_report`, `scripts.training.brain_leaderboard`, `scripts.training.dataset_builder`, `scripts.training.eval_alignment`
- `live_dashboard.py` → `core.features.local_feature_store`, `core.feedback.brain_performance_tracker`, `core.governance.governance_service`, `scripts.training.brain_leaderboard`
- `live_data_quality_report.py` → `core.deployment.scheduled_task_registry`, `scripts.validators.journal_validator`
- `live_dispatch_policy.py` → `scripts.guards.journal_quality`, `scripts.market_calendar`, `scripts.mt5_spread_probe`, `scripts.trade_quality_report`
- `live_feature_quality_report.py` → `scripts.validators.feature_quality_validator`
- `live_intent_loop.py` → `core.brains.adapters.v9_onnx_brain_adapter`, `core.brains.brain_registry`, `core.brains.services.brain_factory`, `core.brains.services.brain_promotion`, `core.config.asset_registry`, `core.contracts.strategy_magic`, `core.data.event_writer`, `core.deployment.brain_lifecycle_manager`, `core.deployment.config_hot_reload`, `core.deployment.path_defaults`, `core.deployment.startup_validator`, `core.execution.capital_allocator`, `core.execution.exit_watchdog`, `core.execution.limit_order_monitor`, `core.execution.meta_exit_engine`, `core.execution.meta_signal_filter`, `core.execution.mt5_broker_adapter`, `core.execution.mt5_worker`, `core.execution.position_manager`, `core.features.adapters.microstructure_feature_adapter`, `core.features.local_feature_store`, `core.features.rolling_normalizer`, `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.infrastructure.distributed_lock`, `core.ledger.services.journal_gate`, `core.observability.entry_context_guard`, `core.observability.live_alert_hub`, `core.parliament.parliament_service`, `core.protocol.event_bar_sync`, `core.risk.regime_detector`, `core.runtime.data_health_monitor`, `core.runtime.execution_state`, `core.runtime.fault_handler`, `core.runtime.live_bootstrap`, `core.runtime.live_cycle`, `core.runtime.live_startup`
- `live_launcher.py` → `core.ledger.services.journal_cleanup`, `core.state.catalog`
- `live_micro_rollout_gate.py` → `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.protocol.schema_versions`
- `live_monitor.py` → `core.deployment.scheduled_task_registry`
- `live_read_only_preflight.py` → `apps.engine.system_facade`, `core.contracts.domain.communication_envelope`, `core.contracts.enums`, `core.deployment.environment_config`, `core.deployment.service_container`, `core.protocol.schema_versions`, `scripts.live_micro_rollout_gate`
- `live_shadow_ensemble.py` → `core.brains.services.brain_factory`, `core.features.adapters.microstructure_feature_adapter`, `core.features.computers.daily_computer`, `core.features.local_feature_store`, `core.features.schemas.btc_macro_enhanced_schema`, `core.features.schemas.v9_institutional_schema`, `core.ledger.storage.jsonl_ledger_store`, `scripts.shadow_decision_recorder`
- `live_shadow_intent_producer.py` → `core.features.live_feature_source`
- `live_stack_diagnostic.py` → `scripts.live_dispatch_policy`, `scripts.send_live_order`
- `market_calendar.py` → `core.market.calendar`
- `migrate_fog_live_cycle.py` → (无内部依赖)
- `monitor_feature_drift.py` → `scripts.alert_dispatcher`
- `monitor_pwin_fix.py` → (无内部依赖)
- `mt5_bridge_healthcheck.py` → (无内部依赖)
- `mt5_bridge_worker.py` → `core.contracts.strategy_magic`, `core.features.ofi_collector`, `core.infrastructure.distributed_lock`, `core.ledger.services.journal_gate`, `core.protocol.live_execution_contract`, `core.runtime.fault_handler`, `core.state.catalog`, `core.state.writer`
- `mt5_positions_snapshot.py` → (无内部依赖)
- `mt5_spread_probe.py` → (无内部依赖)
- `normalize_journal_pnl.py` → (无内部依赖)
- `omega_constants.py` → (无内部依赖)
- `omega_crash_snapshot.py` → (无内部依赖)
- `omega_gate.py` → `scripts.omega_constants`
- `online_feedback_hook.py` → `core.brains.adapters.online_learner_adapter`, `core.feedback.online_feedback_hook`
- `optimize_sl_tp.py` → `scripts.paper_trade_simulator`
- `optimize_sltp_params.py` → `scripts.training.train_btc_swing_v9`
- `paper_trade_simulator.py` → `core.simulation.spread_model`
- `phase4_final_audit.py` → (无内部依赖)
- `phase4_shadow_review.py` → (无内部依赖)
- `position_query.py` → (无内部依赖)
- `position_snapshot.py` → (无内部依赖)
- `pre_commit_blueprint.py` → (无内部依赖)
- `pre_commit_mypy.py` → (无内部依赖)
- `probe_xau_signal_generation.py` → `core.brains.services.brain_factory`, `core.features.local_feature_store`, `core.features.schemas.v9_institutional_schema`
- `purge_backtest_from_governance.py` → `core.feedback.live_journal_metrics`
- `reconcile_fix_registry.py` → (无内部依赖)
- `register_fix.py` → (无内部依赖)
- `repair_brain_configs.py` → `core.features.schemas.registry`
- `restore_btc_schema_41.py` → `core.features.schemas.registry`
- `run_data_health.py` → `core.observability.data_health_service`
- `runtime_protection_guard.py` → `scripts.guards.journal_quality`
- `scan_barrier_params.py` → (无内部依赖)
- `send_data_health_alert.py` → `core.observability.data_health_service`
- `send_live_order.py` → `core.contracts.domain.dispatch_context`, `core.execution.live_order_sender`, `core.execution.mt5_broker_adapter`
- `shadow_decision_recorder.py` → `core.contracts.domain.decision_record`, `core.contracts.ids`, `core.ledger.storage.jsonl_ledger_store`, `core.runtime.shadow_recorder`, `core.schemas.trading_contracts`
- `shadow_live_compare_report.py` → `scripts.trade_quality_report`
- `shadow_pnl_loop.py` → `core.brains.services.brain_factory`, `core.data.event_writer`, `core.deployment.feature_update_producer`, `core.features.adapters.microstructure_feature_adapter`, `core.features.adapters.v9_feature_adapter`, `core.features.computers.microstructure_computer`, `core.features.computers.v9_live_computer`, `core.features.local_feature_store`, `core.features.rolling_normalizer`, `core.features.schemas.microstructure_schema`, `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`, `core.feedback.brain_pnl_ledger`, `core.ledger.storage.jsonl_ledger_store`, `core.risk.regime_detector`, `scripts.shadow_decision_recorder`
- `shadow_rca.py` → (无内部依赖)
- `smoke_test_e2e.py` → `core.features.local_feature_store`, `core.features.store_contracts`, `core.feedback.brain_performance_tracker`, `core.governance.governance_service`, `core.ledger.storage.jsonl_ledger_store`, `scripts.daily_ops`, `scripts.feedback_loop`, `scripts.live_shadow_ensemble`, `scripts.shadow_decision_recorder`, `scripts.training.dataset_builder`, `scripts.training.governance_scheduler`
- `system_health.py` → `core.data.projections`, `core.observability.degradation`
- `system_trust_report.py` → `core.execution.pre_trade_guards`
- `task_a_directional_closure.py` → `scripts.training.build_calibrated_dataset`
- `task_b_regime_baseline.py` → `scripts.training.build_calibrated_dataset`
- `test_io_pipeline.py` → (无内部依赖)
- `test_meta_pipeline.py` → `core.execution.meta_signal_filter`
- `tombstone_orphans.py` → (无内部依赖)
- `trade_quality_report.py` → (无内部依赖)
- `train_btc_metafilter_v2.py` → (无内部依赖)
- `train_metafilter_path_b.py` → (无内部依赖)
- `train_regime_aware_btc.py` → (无内部依赖)
- `train_xau_metafilter.py` → (无内部依赖)
- `training_strategy_report.py` → (无内部依赖)
- `validate_artifacts.py` → (无内部依赖)
- `validate_blueprints.py` → (无内部依赖)
- `validate_brain_before_deploy.py` → `core.brains.services.brain_factory`
- `validate_commit_msg.py` → `scripts.omega_constants`
- `validate_journal_health_fix.py` → (无内部依赖)
- `validate_magic_sync.py` → `core.contracts.strategy_magic`
- `verify.py` → `core.runtime.golden_master`
- `verify_all_brains.py` → `core.brains.services.brain_factory`
- `verify_capresult_ast.py` → (无内部依赖)
- `verify_dqaf044_fix_effect.py` → (无内部依赖)
- `verify_dqaf_002_fix.py` → `core.execution.position_manager`
- `verify_event_stream.py` → `core.feedback.brain_pnl_ledger`
- `verify_health_check_coverage.py` → `core.observability.data_health_service`
- `verify_phantom_contracts.py` → `core.contracts.phantom_contract`, `core.data.write_ahead_log`
- `verify_pnl_data_integrity.py` → (无内部依赖)
- `verify_training_serving_parity.py` → `scripts.build_btc_metafilter_v2_dataset`
- `watchdog_daily_ops.py` → (无内部依赖)

### `scripts/audit/`

- `model_inventory.py` → (无内部依赖)
- `reference_integrity.py` → `core.deployment.brain_lifecycle_manager`, `core.deployment.path_defaults`

### `scripts/backtest/`

- `backtest_dynamic_exit.py` → (无内部依赖)
- `backtest_high_recall_precision.py` → `core.brains.adapters.meta_filter_adapter`
- `backtest_meta_filter.py` → `core.brains.adapters.meta_filter_adapter`
- `backtest_regime_2d.py` → (无内部依赖)
- `backtest_v3_combined.py` → (无内部依赖)

### `scripts/dev/`

- `fix_project.py` → (无内部依赖)

### `scripts/features/`

- `feature_store_warmer.py` → `core.features.local_feature_store`, `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`

### `scripts/guards/`

- `journal_quality.py` → (无内部依赖)

### `scripts/migration/`

- `migrate_to_event_stream.py` → `core.contracts.events`, `core.data.event_writer`

### `scripts/training/`

- `augment_h1_directional_features.py` → (无内部依赖)
- `batch_train_skeleton.py` → `scripts.training.crt_manifest`
- `brain_leaderboard.py` → (无内部依赖)
- `brain_promotion_runner.py` → `core.brains.services.brain_promotion`, `core.feedback.live_journal_metrics`, `core.governance.governance_service`
- `build_calibrated_dataset.py` → `core.features.schemas.v9_institutional_schema`
- `build_live_labeled_dataset.py` → (无内部依赖)
- `build_meta_features.py` → `core.contracts.training.training_contract`
- `build_meta_labeling_dataset.py` → `core.alpha.ou_optimizer`, `scripts.training.build_calibrated_dataset`
- `build_meta_labels.py` → (无内部依赖)
- `build_meta_learner.py` → (无内部依赖)
- `build_micro_barrier_dataset.py` → (无内部依赖)
- `build_micro_flat_features.py` → (无内部依赖)
- `build_profitable_labels.py` → `core.contracts.training.label_contract`, `core.training.profitability_calibrator`, `core.training.utils`
- `build_s1_regression_dataset.py` → `scripts.training.build_calibrated_dataset`
- `build_swing_enhanced_dataset.py` → `core.features.computers.daily_computer`
- `build_v9_micro_dataset.py` → (无内部依赖)
- `calibrate_labels.py` → `core.training.profitability_calibrator`
- `calibrate_meta_filter.py` → `core.brains.online_mlp_model`
- `calibrate_sl_tp.py` → (无内部依赖)
- `calibrate_v4_isotonic.py` → (无内部依赖)
- `champion_challenger.py` → `core.feedback.brain_performance_tracker`, `core.governance.governance_service`
- `crt_manifest.py` → (无内部依赖)
- `dataset_builder.py` → `core.contracts.training.label_contract`, `core.features.local_feature_store`, `core.features.schemas.v9_institutional_schema`
- `dataset_builder_d1.py` → `core.features.computers.daily_computer`, `core.features.schemas.daily_swing_schema`
- `download_mt5_ohlc.py` → (无内部依赖)
- `e2e_pipeline_validation.py` → `core.contracts.training.label_contract`, `core.features.local_feature_store`, `core.features.schemas.v9_institutional_schema`, `core.features.store_contracts`, `scripts.training.quality_gate`, `scripts.training.recipe_search`, `scripts.training.trainers.xgb_trainer`
- `eval_alignment.py` → (无内部依赖)
- `eval_ensemble_baselines.py` → (无内部依赖)
- `eval_regime.py` → (无内部依赖)
- `eval_tf_comparison.py` → (无内部依赖)
- `export_mt5_data.py` → (无内部依赖)
- `generate_batch_plan.py` → (无内部依赖)
- `generate_brain_config.py` → `core.features.schemas.registry`
- `governance_scheduler.py` → `core.brains.brain_registry`, `core.feedback.brain_performance_tracker`, `core.feedback.brain_pnl_ledger`, `core.feedback.live_journal_metrics`, `core.governance.governance_service`, `core.governance.shadow_tracker`, `core.training.utils`
- `label_builder.py` → `core.contracts.training.label_contract`, `core.data.ticket_resolver`
- `label_builder_d1.py` → (无内部依赖)
- `monitor_training.py` → (无内部依赖)
- `optimize_ensemble_weights.py` → (无内部依赖)
- `optimize_meta_threshold.py` → `core.contracts.training.training_contract`
- `quality_gate.py` → `scripts.training.trainers.xgb_trainer`
- `reactivate_brains.py` → `core.feedback.brain_pnl_ledger`, `core.feedback.brain_quality_engine`, `core.governance.governance_service`, `core.training.utils`
- `recipe_diff.py` → (无内部依赖)
- `recipe_search.py` → `core.features.data_augmentation`, `scripts.training.trainers.deep_res_mlp_trainer`, `scripts.training.trainers.lgb_trainer`, `scripts.training.trainers.online_mlp_trainer`, `scripts.training.trainers.xgb_trainer`
- `register_brain.py` → `core.deployment.brain_registration_gate`
- `retraining_trigger.py` → `scripts.training.champion_challenger`
- `run_promotion.py` → `core.brains.services.brain_promotion`, `core.governance.governance_service`
- `run_train_batch.py` → (无内部依赖)
- `scan_profitability_surface.py` → `core.training.profitability_calibrator`
- `train.py` → `core.contracts.training.training_contract`, `core.deployment.brain_registration_gate`, `core.training.brain_config`, `core.training.cpcv`, `core.training.custom_objectives`, `core.training.dataset`, `core.training.model_hashing`, `core.training.profitability_calibrator`, `core.training.training_registry`, `scripts.training.trainers.deep_res_mlp_trainer`, `scripts.training.trainers.lgb_trainer`, `scripts.training.trainers.online_mlp_trainer`, `scripts.training.trainers.transformer_trainer`, `scripts.training.trainers.xgb_trainer`
- `train_btc_binary_directional.py` → (无内部依赖)
- `train_btc_directional_v1.py` → `core.features.computers.v9_live_computer`
- `train_btc_directional_v10.py` → (无内部依赖)
- `train_btc_swing_v9.py` → `core.features.computers.v9_live_computer`
- `train_daily_swing.py` → (无内部依赖)
- `train_exit_metamodel.py` → (无内部依赖)
- `train_from_csv.py` → `core.contracts.training.label_contract`, `core.contracts.training.training_recipe`, `core.features.schemas.registry`
- `train_meta_filter.py` → (无内部依赖)
- `train_meta_model.py` → (无内部依赖)
- `train_online_init.py` → (无内部依赖)
- `train_stage2_lgb_pit.py` → (无内部依赖)
- `train_stage2_mlp_pit.py` → `core.brains.online_mlp_model`
- `train_swing_v9.py` → (无内部依赖)
- `train_xau_directional_v1.py` → (无内部依赖)
- `train_xau_directional_v2.py` → `core.features.schemas.swing_enhanced_schema`
- `write_manifest_stub.py` → `scripts.training.crt_manifest`
- `your_trainer.py` → `scripts.training.crt_manifest`

### `scripts/training/builders/`

- `arb.py` → `scripts.training.builders.base`
- `base.py` → (无内部依赖)
- `microstructure.py` → `scripts.training.builders.base`

### `scripts/training/trainers/`

- `arb_trainer.py` → `core.alpha.ou_optimizer`, `core.contracts.training.training_recipe`
- `deep_res_mlp_trainer.py` → `core.contracts.training.training_recipe`, `core.training.utils`
- `lgb_trainer.py` → `core.contracts.training.training_recipe`, `core.features.data_augmentation`, `core.training.utils`
- `mtx_trainer.py` → `core.contracts.training.training_recipe`
- `online_mlp_trainer.py` → `core.brains.online_mlp_model`, `core.training.utils`
- `sur_trainer.py` → `core.contracts.training.training_recipe`
- `transformer_trainer.py` → `core.contracts.training.training_recipe`, `core.training.utils`
- `xgb_trainer.py` → `core.contracts.training.training_recipe`, `core.features.data_augmentation`, `core.training.utils`

### `scripts/tuning/`

- `tune_btc_kalman_h4.py` → (无内部依赖)

### `scripts/validators/`

- `feature_quality_validator.py` → `core.features.schemas.v9_institutional_schema`
- `journal_validator.py` → (无内部依赖)

# MODULE INVENTORY — 模块清单与完成度

> **自动生成**: 2026-05-04T01:27:54Z
> **扫描模块数**: 219
> **图例**: ✅ active | 🧪 stub | 📄 config | ⬜ empty

## apps/engine

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_runner.py` | ✅ active | BacktestRunner, BacktestResult | 6 | 153 | — | |
| `batch_processor.py` | ✅ active | BatchProcessor | 3 | 77 | — | |
| `bootstrap_v9.py` | ✅ active | — | 5 | 70 | — | |
| `cli.py` | ✅ active | — | 42 | 1501 | — | |
| `communication_ops_cli.py` | ✅ active | — | 7 | 133 | — | |
| `communication_summary_contract.py` | ✅ active | — | 1 | 71 | — | |
| `diagnostics_cli.py` | ✅ active | DiagnosticsCLI | 10 | 129 | — | |
| `main_v9_shadow.py` | ✅ active | FeatureInputError, OutputPlan, StreamEnvelopePlan, SessionStreamPlan, BaselineSuiteSpec, FormalBaselineManifest, ShadowSessionManager | 91 | 2171 | — | |
| `orchestrator.py` | ✅ active | CycleOutcome, DecisionCycleOrchestrator | 6 | 297 | — | |
| `runtime_loop.py` | ✅ active | SimpleFeatureSnapshot, DecisionCycleResult, RuntimeLoop | 2 | 195 | — | |
| `system_facade.py` | ✅ active | SystemFacade, SystemSelfTest | 27 | 236 | — | |
| `v9_shadow_sse.py` | ✅ active | SessionStreamQueryError, SessionStreamResponseStartError, SessionSSEClientBuffer, ShadowSessionSSEHandler | 22 | 314 | — | |
| `v9_shadow_support.py` | ✅ active | StubFeatureService, V9ParliamentAdapter | 2 | 66 | — | |

## core/alpha

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | AlphaLifecycleState, AlphaRecord, AlphaTransitionRecord | 4 | 78 | — | |
| `lifecycle_service.py` | ✅ active | AlphaLifecycleService | 10 | 106 | — | |
| `performance_store.py` | ✅ active | AlphaPerformanceSnapshot, AlphaPerformanceStore | 12 | 181 | — | |
| `portfolio_allocator.py` | ✅ active | AlphaAllocationPolicy, AlphaAllocationRecommendation, AlphaPortfolioAllocator | 8 | 167 | — | |
| `promotion_gate.py` | ✅ active | AlphaPromotionPolicy, AlphaPromotionDecision, AlphaPromotionGate | 16 | 261 | — | |
| `registry.py` | ✅ active | AlphaRegistry | 8 | 51 | — | |
| `risk_budget.py` | ✅ active | AlphaRiskBudgetPolicy, AlphaRiskBudgetExporter | 3 | 56 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 10 | — | |

## core/brains

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/brains/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `base_adapter.py` | ✅ active | BaseBrainAdapter | 7 | 115 | — | |
| `params_brain_adapter.py` | ✅ active | ParamsBrainAdapter | 8 | 298 | — | |
| `v9_onnx_brain_adapter.py` | ✅ active | V9OnnxBrainAdapter | 9 | 230 | — | |
| `xgboost_brain_adapter.py` | ✅ active | XGBoostBrainAdapter | 6 | 175 | — | |

## core/brains/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_factory.py` | ✅ active | BrainFactory | 1 | 41 | — | |
| `brain_registry_loader.py` | ✅ active | BrainRegistryLoader | 1 | 7 | — | |
| `brain_registry_service.py` | ✅ active | BrainRegistryService | 3 | 54 | — | |
| `brain_run_service.py` | ✅ active | BrainRunService | 3 | 82 | — | |

## core/contracts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `enums.py` | ✅ active | BrainRole, BrainStatus, DecisionAction, DecisionSide, RiskDecisionStatus, SystemMode, OverrideStatus, CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision, ExecutionEventType, ReconciliationStatus | 0 | 109 | — | |
| `exceptions.py` | ✅ active | DomainError, RiskError, RiskPolicyViolation, GovernanceError, InvalidTransitionError, BrainNotFoundError, ExecutionError, OrderNotFoundError, DuplicateOrderError, ProtocolError, DispatchError, IdempotencyError, ConfigurationError, ContractViolationError | 9 | 136 | — | |
| `ids.py` | ✅ active | — | 14 | 57 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `validators.py` | ✅ active | ContractViolation, ContractValidator | 7 | 123 | — | |

## core/contracts/domain

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_decision_proposal.py` | ✅ active | BrainDecisionProposal | 2 | 51 | — | |
| `communication_envelope.py` | ✅ active | CommunicationEnvelope | 1 | 42 | — | |
| `communication_record.py` | ✅ active | CommunicationRecord | 2 | 85 | — | |
| `decision_candidate.py` | ✅ active | DecisionCandidate | 1 | 27 | — | |
| `decision_intent.py` | ✅ active | DecisionIntent | 5 | 64 | — | |
| `decision_record.py` | ✅ active | DecisionRecord | 4 | 45 | — | |
| `dispatch_request.py` | ✅ active | DispatchRequest | 1 | 27 | — | |
| `dispatch_result.py` | ✅ active | DispatchResult | 2 | 44 | — | |
| `execution_event.py` | ✅ active | ExecutionEvent | 3 | 95 | — | |
| `protocol_override.py` | ✅ active | ProtocolOverride | 0 | 21 | — | |
| `replay_execution_record.py` | ✅ active | ReplayExecutionRecord | 7 | 181 | — | |
| `risk_verdict.py` | ✅ active | RiskVerdict | 5 | 55 | — | |
| `system_mode_state.py` | ✅ active | SystemModeState | 0 | 19 | — | |

## core/contracts/serialization

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `json_codec.py` | ✅ active | — | 3 | 27 | — | |

## core/deployment

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `capability_registry.py` | ✅ active | CapabilitySpec, CapabilityRegistry | 5 | 112 | — | |
| `compliance_audit.py` | ✅ active | ComplianceAuditService | 9 | 567 | — | |
| `compliance_control_matrix.py` | ✅ active | ComplianceControlMatrixService | 11 | 424 | — | |
| `config_hot_reload.py` | ✅ active | ConfigHotReload | 7 | 116 | — | |
| `deployment_executor.py` | ✅ active | DeploymentExecutor | 8 | 329 | — | |
| `deployment_plan.py` | ✅ active | DeploymentPlanService | 7 | 300 | — | |
| `domain_keys.py` | 📄 config | — | 0 | 984 | — | |
| `environment_config.py` | ✅ active | Environment, EnvironmentConfig | 7 | 120 | — | |
| `evidence_bundle.py` | ✅ active | EvidenceBundleService | 10 | 289 | — | |
| `feature_update_producer.py` | ✅ active | — | 2 | 53 | — | |
| `final_audit.py` | ✅ active | FinalAuditService | 5 | 211 | — | |
| `governance_summary.py` | ✅ active | — | 4 | 56 | — | |
| `health_check.py` | ✅ active | HealthCheckService | 8 | 90 | — | |
| `lifecycle_manager.py` | ✅ active | LifecycleManager | 7 | 167 | — | |
| `operational_support.py` | ✅ active | RetryPolicy, ConfigValidator | 7 | 129 | — | |
| `operations_timeline.py` | ✅ active | OperationsTimelineService | 16 | 262 | — | |
| `ops_maturity.py` | ✅ active | OpsMaturityService | 4 | 164 | — | |
| `postmortem_report.py` | ✅ active | PostmortemReportService | 11 | 468 | — | |
| `release_certification.py` | ✅ active | ReleaseCertificationService | 12 | 289 | — | |
| `release_gate.py` | ✅ active | ReleaseGateService | 16 | 320 | — | |
| `release_pipeline.py` | ✅ active | ReleasePipelineService | 7 | 377 | — | |
| `release_readiness.py` | ✅ active | ReleaseReadinessService | 13 | 444 | — | |
| `release_registry.py` | ✅ active | ReleaseRegistryService | 17 | 351 | — | |
| `replay_isolation.py` | ✅ active | ReplayDispatchAdapter, NullDispatchAdapter, ReplayEnvironment | 11 | 138 | — | |
| `rollback_drill.py` | ✅ active | RollbackDrillService | 8 | 304 | — | |
| `runbook_engine.py` | ✅ active | RunbookEngine | 16 | 673 | — | |
| `scheduler_service.py` | ✅ active | ScheduledTask, SchedulerService | 14 | 190 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 29 | — | |
| `service_container.py` | ✅ active | ServiceContainer | 41 | 532 | — | |
| `state_persistence.py` | ✅ active | StatePersistence | 6 | 100 | — | |
| `validation_mode.py` | ✅ active | — | 1 | 10 | — | |

## core/execution

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `execution_manager.py` | ✅ active | ExecutionManager | 7 | 161 | — | |
| `fill_simulator.py` | ✅ active | FillSimulationConfig, FillSimulator | 7 | 88 | — | |
| `fix_contracts.py` | ✅ active | FixSessionConfig, FixMessage, FixExecutionReport | 4 | 69 | — | |
| `fix_execution_mapper.py` | ✅ active | FixExecutionReportMapper | 5 | 75 | — | |
| `fix_gateway_adapter.py` | ✅ active | FixGatewayAdapter | 12 | 136 | — | |
| `fix_message_builder.py` | ✅ active | FixMessageBuilder | 5 | 57 | — | |
| `gateway_contracts.py` | ✅ active | OrderRequest, Fill, OrderState, ExecutionGateway | 9 | 103 | — | |
| `order_state_machine.py` | ✅ active | OrderStateMachine | 9 | 101 | — | |
| `paper_gateway.py` | ✅ active | PaperExecutionGateway | 10 | 141 | — | |
| `quality_analyzer.py` | ✅ active | ExecutionQualityAnalyzer | 8 | 122 | — | |
| `quality_contracts.py` | ✅ active | ExecutionBenchmark, ExecutionQualityMetric, ExecutionQualityReport | 2 | 94 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/features

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `feature_service.py` | ✅ active | FeatureService, FeatureBrainRegistry, IntentExplainer | 11 | 184 | — | |
| `feature_snapshot.py` | ✅ active | StoredFeatureSnapshot | 2 | 33 | — | |
| `local_feature_store.py` | ✅ active | LocalFeatureStore | 12 | 115 | — | |
| `store_contracts.py` | ✅ active | FeatureSchema, FeatureRecord, FeatureQuery, FeatureStore | 8 | 87 | — | |
| `update_job.py` | ✅ active | FeatureUpdateResult, IncrementalFeatureUpdateJob | 3 | 63 | — | |

## core/features/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `v9_feature_adapter.py` | ✅ active | V9FeatureAdapter | 4 | 36 | — | |

## core/features/computers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `v9_live_computer.py` | ✅ active | V9LiveFeatureComputer | 15 | 320 | — | |

## core/features/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `v9_institutional_schema.py` | 📄 config | — | 0 | 42 | — | |

## core/feedback

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_performance_tracker.py` | ✅ active | BrainPerformanceTracker | 7 | 104 | — | |
| `decision_scorer.py` | ✅ active | DecisionScorer | 5 | 120 | — | |
| `feedback_loop.py` | ✅ active | FeedbackLoop | 4 | 99 | — | |
| `outcome_collector.py` | ✅ active | OutcomeCollector | 4 | 111 | — | |
| `performance_analytics.py` | ✅ active | PerformanceAnalytics | 11 | 162 | — | |

## core/governance

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_rule_engine.py` | ✅ active | GovernanceRule, GovernanceRuleEngine | 8 | 134 | — | |
| `governance_service.py` | ✅ active | GovernanceService | 14 | 161 | — | |

## core/ledger

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_sources.py` | ✅ active | — | 1 | 17 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `stream_names.py` | ✅ active | — | 2 | 15 | — | |

## core/ledger/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `communication_inspection_service.py` | ✅ active | CommunicationInspectionService | 15 | 346 | — | |
| `communication_operations_service.py` | ✅ active | CommunicationOperationsService | 13 | 445 | — | |
| `communication_record_reader.py` | ✅ active | CommunicationRecordReader | 6 | 46 | — | |
| `communication_record_writer.py` | ✅ active | CommunicationRecordWriter | 2 | 22 | — | |
| `communication_replay_executor.py` | ✅ active | CommunicationReplayExecutor | 9 | 381 | — | |
| `communication_replay_gate.py` | ✅ active | CommunicationReplayGate | 4 | 273 | — | |
| `communication_replay_service.py` | ✅ active | CommunicationReplayService | 9 | 310 | — | |
| `communication_trace_refs.py` | ✅ active | — | 14 | 95 | — | |
| `decision_record_writer.py` | ✅ active | DecisionRecordWriter | 2 | 57 | — | |
| `execution_event_reader.py` | ✅ active | ExecutionEventReader | 8 | 119 | — | |
| `execution_event_writer.py` | ✅ active | ExecutionEventWriter | 3 | 47 | — | |
| `execution_reconciliation_service.py` | ✅ active | ExecutionReconciliationService | 8 | 243 | — | |
| `gate_decision_refs.py` | ✅ active | — | 4 | 30 | — | |
| `replay_execution_reader.py` | ✅ active | ReplayExecutionReader | 5 | 35 | — | |
| `replay_execution_writer.py` | ✅ active | ReplayExecutionWriter | 2 | 26 | — | |
| `replay_plan_refs.py` | ✅ active | — | 11 | 85 | — | |
| `replay_record_refs.py` | ✅ active | — | 15 | 159 | — | |
| `replay_trace_refs.py` | ✅ active | — | 8 | 63 | — | |

## core/ledger/storage

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `jsonl_ledger_store.py` | ✅ active | JsonlLedgerStore | 2 | 19 | — | |

## core/market

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `position_tracker.py` | ✅ active | PositionTracker, MarketContextProvider | 12 | 125 | — | |
| `signal_processor.py` | ✅ active | SignalFilter, MarketSignalProcessor | 8 | 121 | — | |

## core/observability

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `alert_service.py` | 🧪 stub | AlertRule, AlertChannel, LogAlertChannel, InMemoryAlertChannel, AlertService | 15 | 179 | — | |
| `audit_log.py` | ✅ active | StructuredAuditLog | 11 | 180 | — | |
| `diagnostics_dashboard.py` | ✅ active | DiagnosticsDashboard | 6 | 135 | — | |
| `event_bus.py` | ✅ active | EventBus | 7 | 63 | — | |
| `metric_names.py` | ✅ active | — | 2 | 56 | — | |
| `metrics_collector.py` | ✅ active | MetricsCollector | 10 | 96 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `slo_service.py` | ✅ active | SloService | 9 | 189 | — | |
| `tracing.py` | ✅ active | Span, TracingContext | 18 | 128 | — | |

## core/parliament

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `parliament_service.py` | ✅ active | ParliamentService | 9 | 174 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/protocol

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `live_execution_contract.py` | ✅ active | — | 5 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 8 | — | |

## core/protocol/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `communication_adapter.py` | ✅ active | CommunicationAdapter | 1 | 7 | — | |
| `communication_adapter_registry.py` | ✅ active | CommunicationAdapterRegistry | 3 | 61 | — | |
| `communication_dispatcher.py` | ✅ active | CommunicationDispatcher | 4 | 283 | — | |
| `decision_compiler.py` | ✅ active | DecisionCompiler | 5 | 118 | — | |
| `file_queue_communication_adapter.py` | ✅ active | FileQueueCommunicationAdapter | 2 | 53 | — | |
| `file_queue_receipt_reader.py` | ✅ active | FileQueueReceiptReader | 5 | 28 | — | |
| `fix_communication_adapter.py` | ✅ active | FixCommunicationAdapter | 6 | 117 | — | |
| `idempotency.py` | ✅ active | IdempotencyStore, DuplicateDetector | 10 | 111 | — | |
| `intent_message_builder.py` | ✅ active | IntentMessageBuilder | 3 | 60 | — | |
| `mt5_communication_adapter.py` | ✅ active | MT5CommunicationAdapter | 2 | 81 | — | |
| `override_resolver.py` | ✅ active | OverrideResolver | 1 | 22 | — | |
| `resilience.py` | ✅ active | CircuitState, CircuitBreaker, RateLimiter | 12 | 137 | — | |
| `stub_communication_adapter.py` | ✅ active | StubCommunicationAdapter | 2 | 23 | — | |
| `venue_router.py` | 🧪 stub | VenueAdapter, StubVenueAdapter, VenueRouter | 14 | 116 | — | |

## core/risk

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `risk_evaluation_service.py` | ✅ active | RiskEvaluationService | 5 | 143 | — | |
| `risk_policies.py` | ✅ active | RiskPolicy, PositionLimitPolicy, DrawdownPolicy, ExposurePolicy, ConcentrationPolicy, ModePolicy | 12 | 135 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/runtime

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `alpha_budget_contracts.py` | ✅ active | AlphaBudgetContractError, AlphaRiskBudgetContractValidator, AlphaBudgetUsageContractValidator | 6 | 91 | — | |
| `alpha_budget_usage_reporter.py` | ✅ active | AlphaBudgetUsageReporter | 3 | 93 | — | |
| `alpha_budget_usage_store.py` | ✅ active | AlphaBudgetUsageStore | 9 | 57 | — | |
| `alpha_risk_budget_gate.py` | ✅ active | AlphaRiskBudgetGate | 9 | 111 | — | |
| `approval_contracts.py` | ✅ active | ExecutionApproval | 4 | 76 | — | |
| `cycle_replay.py` | ✅ active | RuntimeReplayReport, RuntimeCycleReplay | 4 | 124 | — | |
| `evidence_contracts.py` | ✅ active | RuntimeEvidenceRecord | 2 | 61 | — | |
| `evidence_reader.py` | ✅ active | RuntimeEvidenceReader | 5 | 42 | — | |
| `evidence_writer.py` | ✅ active | RuntimeEvidenceWriter | 2 | 30 | — | |
| `execution_gates.py` | ✅ active | RuntimeRiskGate, RuntimeGovernanceGate, RuntimeExecutionApprovalChain | 6 | 107 | — | |
| `execution_gateway_router.py` | ✅ active | ExecutionGatewayRouter | 5 | 30 | — | |
| `execution_pipeline.py` | ✅ active | RuntimeExecutionPipeline | 4 | 106 | — | |
| `integration_contracts.py` | ✅ active | OrderSizingPolicy, RuntimePipelineResult | 2 | 52 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 16 | — | |
| `signal_order_builder.py` | ✅ active | SignalOrderRequestBuilder | 3 | 52 | — | |
| `summary_service.py` | ✅ active | RuntimeSummaryService | 11 | 143 | — | |

## core/state

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/state/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `control_snapshot.py` | ✅ active | ControlSnapshot | 0 | 12 | — | |
| `control_snapshot_service.py` | ✅ active | ControlSnapshotService | 2 | 29 | — | |

## core/state/stores

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `override_store.py` | ✅ active | OverrideStore | 4 | 57 | — | |
| `system_mode_store.py` | ✅ active | SystemModeStore | 4 | 34 | — | |

## core/strategies

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | StrategyMetadata, RequiredFeature, Signal, StrategyHealth, AlphaAgent | 9 | 102 | — | |
| `examples.py` | ✅ active | ThresholdAlphaAgent | 8 | 86 | — | |
| `registry.py` | ✅ active | StrategyPluginRegistry, StrategyPluginRunner | 11 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |

## scripts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `ci_prepare_v9_shadow_fixtures.py` | ✅ active | — | 2 | 180 | — | |
| `ingest_live_journal_to_alpha.py` | ✅ active | — | 4 | 96 | — | |
| `live_auto_healthcheck.py` | ✅ active | — | 11 | 220 | — | |
| `live_daily_recap.py` | ✅ active | — | 12 | 299 | — | |
| `live_data_quality_report.py` | ✅ active | — | 10 | 261 | — | |
| `live_dispatch_policy.py` | ✅ active | — | 10 | 310 | — | |
| `live_intent_loop.py` | ✅ active | — | 11 | 467 | — | |
| `live_micro_rollout_gate.py` | ✅ active | — | 5 | 136 | — | |
| `live_read_only_preflight.py` | ✅ active | — | 5 | 139 | — | |
| `live_shadow_intent_producer.py` | ✅ active | — | 7 | 256 | — | |
| `live_stack_diagnostic.py` | ✅ active | — | 5 | 204 | — | |
| `market_calendar.py` | ✅ active | — | 3 | 69 | — | |
| `mt5_bridge_healthcheck.py` | ✅ active | — | 6 | 149 | — | |
| `mt5_bridge_worker.py` | ✅ active | — | 17 | 457 | — | |
| `mt5_positions_snapshot.py` | ✅ active | — | 4 | 91 | — | |
| `mt5_spread_probe.py` | ✅ active | — | 1 | 65 | — | |
| `runtime_protection_guard.py` | ✅ active | — | 1 | 22 | — | |
| `send_live_order.py` | ✅ active | — | 8 | 249 | — | |
| `shadow_live_compare_report.py` | ✅ active | — | 9 | 217 | — | |
| `trade_quality_report.py` | ✅ active | — | 6 | 109 | — | |

## scripts/dev

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `fix_project.py` | ✅ active | — | 13 | 457 | — | |

## scripts/guards

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `journal_quality.py` | ✅ active | — | 2 | 40 | — | |

## scripts/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `batch_train_skeleton.py` | ✅ active | — | 2 | 105 | — | |
| `crt_manifest.py` | ✅ active | CRTManifestV1 | 9 | 150 | — | |
| `generate_batch_plan.py` | ✅ active | — | 5 | 269 | — | |
| `monitor_training.py` | ✅ active | — | 18 | 421 | — | |
| `run_train_batch.py` | ✅ active | — | 6 | 268 | — | |
| `write_manifest_stub.py` | ✅ active | — | 2 | 60 | — | |
| `your_trainer.py` | ✅ active | — | 7 | 198 | — | |

## scripts/training/trainers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `arb_trainer.py` | ✅ active | — | 5 | 477 | — | |
| `mtx_trainer.py` | ✅ active | — | 7 | 352 | — | |
| `sur_trainer.py` | ✅ active | — | 5 | 267 | — | |

# MODULE INVENTORY — 模块清单与完成度

> **自动生成**: 2026-08-21T14:22:44Z
> **扫描模块数**: 828
> **图例**: ✅ active | 🧪 stub | 📄 config | ⬜ empty

## apps/engine

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_runner.py` | ✅ active | BacktestRunner, BacktestResult | 6 | 154 | — | |
| `batch_processor.py` | ✅ active | BatchProcessor | 3 | 78 | — | |
| `bootstrap_v9.py` | ✅ active | — | 7 | 259 | — | |
| `cli.py` | ✅ active | — | 42 | 1559 | — | |
| `communication_ops_cli.py` | ✅ active | — | 7 | 137 | — | |
| `communication_summary_contract.py` | ✅ active | — | 1 | 71 | — | |
| `diagnostics_cli.py` | ✅ active | DiagnosticsCLI | 10 | 129 | — | |
| `main_v9_shadow.py` | ✅ active | FeatureInputError, OutputPlan, StreamEnvelopePlan, SessionStreamPlan, BaselineSuiteSpec, FormalBaselineManifest, ShadowSessionManager | 91 | 2197 | — | |
| `orchestrator.py` | ✅ active | CycleOutcome, DecisionCycleOrchestrator | 6 | 310 | — | |
| `runtime_loop.py` | ✅ active | SimpleFeatureSnapshot, DecisionCycleResult, RuntimeLoop | 3 | 322 | — | |
| `system_facade.py` | ✅ active | SystemFacade, SystemSelfTest | 27 | 239 | — | |
| `v9_shadow_sse.py` | ✅ active | SessionStreamQueryError, SessionStreamResponseStartError, SessionSSEClientBuffer, ShadowSessionSSEHandler | 22 | 315 | — | |
| `v9_shadow_support.py` | ✅ active | StubFeatureService, V9ParliamentAdapter | 2 | 66 | — | |

## apps/monitor

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `live_trading_dashboard.py` | ✅ active | LiveDashboardHandler | 34 | 2486 | — | |

## core

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `constants.py` | 📄 config | — | 0 | 195 | — | |

## core/alpha

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | AlphaLifecycleState, AlphaRecord, AlphaTransitionRecord | 4 | 83 | — | |
| `lifecycle_service.py` | ✅ active | AlphaLifecycleService | 10 | 108 | — | |
| `ou_optimizer.py` | ✅ active | KalmanHalfLifeFilter | 12 | 535 | — | |
| `performance_store.py` | ✅ active | AlphaPerformanceSnapshot, AlphaPerformanceStore | 16 | 230 | — | |
| `portfolio_allocator.py` | ✅ active | AlphaAllocationPolicy, AlphaAllocationRecommendation, AlphaPortfolioAllocator | 8 | 195 | — | |
| `promotion_gate.py` | ✅ active | AlphaPromotionPolicy, AlphaPromotionDecision, AlphaPromotionGate | 16 | 261 | — | |
| `registry.py` | ✅ active | AlphaRegistry | 10 | 89 | — | |
| `risk_budget.py` | ✅ active | AlphaRiskBudgetPolicy, AlphaRiskBudgetExporter | 3 | 56 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 10 | — | |

## core/backtest

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `data_feed.py` | ✅ active | Bar, DataFeed | 11 | 160 | — | |
| `engine.py` | ✅ active | BacktestResult, BacktestEngine | 5 | 268 | — | |
| `execution_simulator.py` | ✅ active | SimulatedFill, ExecutionSimulator | 8 | 105 | — | |
| `metrics.py` | ✅ active | — | 1 | 72 | — | |
| `portfolio.py` | ✅ active | VirtualPosition, VirtualPortfolio | 13 | 186 | — | |
| `strategy_adapter.py` | ✅ active | StrategyLineAdapter | 9 | 265 | — | |

## core/brains

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_registry.py` | ✅ active | BrainEntry, BrainRegistry | 16 | 188 | — | |
| `online_mlp_model.py` | ✅ active | OnlineMLP, _TorchOnlineMLP, _Module | 15 | 269 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/brains/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `base_adapter.py` | ✅ active | BaseBrainAdapter | 14 | 371 | — | |
| `lightgbm_brain_adapter.py` | ✅ active | LightGBMBrainAdapter | 6 | 222 | — | |
| `meta_filter_adapter.py` | ✅ active | FeatureParityError, MetaFilterAdapter | 8 | 207 | — | |
| `online_learner_adapter.py` | ✅ active | OnlineLearnerAdapter | 18 | 597 | — | |
| `params_brain_adapter.py` | ✅ active | ParamsBrainAdapter | 9 | 271 | — | |
| `transfer_residual_brain_adapter.py` | ✅ active | TransferResidualBrainAdapter | 4 | 188 | — | |
| `transformer_brain_adapter.py` | ✅ active | TransformerBrainAdapter | 9 | 288 | — | |
| `v9_onnx_brain_adapter.py` | ✅ active | V9OnnxBrainAdapter | 9 | 316 | — | |
| `xgboost_brain_adapter.py` | ✅ active | XGBoostBrainAdapter | 6 | 295 | — | |

## core/brains/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `ab_test.py` | ✅ active | ExperimentConfig, TrafficSplitter, ExperimentResult, ExperimentTracker | 11 | 305 | — | |
| `brain_attribution_service.py` | ✅ active | BrainAttribution, AttributionReport, BrainAttributionService | 11 | 327 | — | |
| `brain_factory.py` | ✅ active | BrainFactory | 1 | 185 | — | |
| `brain_leaderboard.py` | ✅ active | BrainRanking, BrainLeaderboard | 10 | 370 | — | |
| `brain_promotion.py` | ✅ active | BrainPromotionDecision, BrainPromotionThresholds, BrainPromotionEvaluator | 8 | 522 | — | |
| `brain_registry_loader.py` | ✅ active | BrainRegistryLoader | 1 | 7 | — | |
| `brain_registry_service.py` | ✅ active | BrainRegistryService | 7 | 141 | — | |
| `brain_run_service.py` | ✅ active | BrainRunService | 15 | 281 | — | |
| `dynamic_brain_weighter.py` | ✅ active | DynamicBrainWeighter | 13 | 423 | — | |
| `inference_guard.py` | ✅ active | InferenceGuard | 11 | 223 | — | |
| `onnx_worker.py` | ✅ active | — | 1 | 77 | — | |
| `stability_monitor.py` | ✅ active | StabilityReport | 4 | 201 | — | |

## core/config

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `asset_registry.py` | ✅ active | AssetConfig | 2 | 68 | — | |
| `consistency.py` | ✅ active | ConfigConsistencyError, ConsistencyReport | 4 | 142 | — | |

## core/contracts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `adapters.py` | ✅ active | — | 11 | 210 | — | |
| `calibrator_contract.py` | ✅ active | CalibratorHistoryEntry, CalibratorState | 3 | 116 | — | |
| `cap_result.py` | ✅ active | CapProofExpired, CapProofReused, _SuccessProof, CapResult, Kernel | 19 | 289 | — | |
| `domain_keys.py` | 📄 config | — | 0 | 979 | — | |
| `enums.py` | ✅ active | BrainRole, BrainStatus, DecisionAction, DecisionSide, RiskDecisionStatus, SystemMode, OverrideStatus, CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision, ExecutionEventType, ReconciliationStatus | 0 | 109 | — | |
| `events.py` | ✅ active | DataSource, EventType, PnLEvent, GovernanceTransitionEvent | 0 | 168 | — | |
| `exceptions.py` | ✅ active | DomainError, RiskError, RiskPolicyViolation, GovernanceError, InvalidTransitionError, BrainNotFoundError, ExecutionError, OrderNotFoundError, DuplicateOrderError, ProtocolError, DispatchError, IdempotencyError, ConfigurationError, ContractViolationError, DataIntegrityError | 10 | 159 | — | |
| `ids.py` | ✅ active | — | 14 | 57 | — | |
| `journal_contract.py` | ✅ active | JournalAccepted, JournalClosed | 6 | 232 | — | |
| `journal_sla.py` | ✅ active | ReconStatus, JournalHealthSLA | 2 | 133 | — | |
| `phantom_contract.py` | ✅ active | PhantomStub, PhantomSerializer, PredicateRegistry, StateProjectionError, StateProjector, ContractViolation | 46 | 938 | — | |
| `position_events.py` | ✅ active | PositionClosed, PositionOpened | 2 | 167 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `strategy_magic.py` | ✅ active | UnattributedOrderRejected | 3 | 215 | — | |
| `validators.py` | ✅ active | ContractViolation, ContractValidator | 7 | 123 | — | |

## core/contracts/domain

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_decision_proposal.py` | ✅ active | BrainDecisionProposal | 2 | 53 | — | |
| `communication_envelope.py` | ✅ active | CommunicationEnvelope | 1 | 42 | — | |
| `communication_record.py` | ✅ active | CommunicationRecord | 2 | 85 | — | |
| `decision_candidate.py` | ✅ active | DecisionCandidate | 1 | 27 | — | |
| `decision_intent.py` | ✅ active | DecisionIntent | 5 | 64 | — | |
| `decision_record.py` | ✅ active | DecisionRecord | 4 | 45 | — | |
| `dispatch_context.py` | ✅ active | DispatchContext | 1 | 77 | — | |
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

## core/contracts/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `label_contract.py` | ✅ active | BarrierResult, LabelContract | 12 | 641 | — | |
| `label_from_live_yaml.py` | ✅ active | LiveLabelParams | 4 | 140 | — | |
| `training_contract.py` | ✅ active | DatasetSpec, LabelSpec, ArchitectureSpec, ValidationSpec, QualityGateSpec, OutputSpec, TrainingContract | 12 | 504 | — | |
| `training_recipe.py` | ✅ active | ModelIdentity, LabelContractRef, DataAugmentation, DataConfig, TrainingConfig, EvaluationConfig, TrainingRecipe | 6 | 386 | — | |

## core/data

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `event_writer.py` | ✅ active | EventWriter | 9 | 128 | — | |
| `projections.py` | ✅ active | — | 7 | 271 | — | |
| `ticket_resolver.py` | ✅ active | TicketResolutionError | 4 | 137 | — | |
| `wap.py` | ✅ active | WAPStore | 11 | 219 | — | |
| `write_ahead_log.py` | ✅ active | WALConfig, WALRecord, WriteAheadLog | 21 | 526 | — | |

## core/deployment

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `atomic_file_writer.py` | ✅ active | AtomicFileError, AtomicFileWriter | 14 | 172 | — | |
| `blue_green.py` | ✅ active | SlotState, SlotColor, DeploymentSlot, DeploymentTopology, CutoverResult, HealthProbe, BlueGreenManager | 26 | 521 | — | |
| `brain_alert.py` | ✅ active | — | 2 | 42 | — | |
| `brain_config_validator.py` | ✅ active | BrainConfigError, ValidationResult, BrainConfigValidator | 17 | 350 | — | |
| `brain_lifecycle_manager.py` | ✅ active | RetirementReport, RegistrationReport, IntegrityReport, ReferenceAuditReport, BrainLifecycleManager | 19 | 1283 | — | |
| `brain_registration_gate.py` | ✅ active | GateResult, BrainRegistrationGate | 23 | 548 | — | |
| `capability_registry.py` | ✅ active | CapabilitySpec, CapabilityRegistry | 5 | 112 | — | |
| `compliance_audit.py` | ✅ active | ComplianceAuditService | 9 | 567 | — | |
| `compliance_control_matrix.py` | ✅ active | ComplianceControlMatrixService | 11 | 424 | — | |
| `compliance_export.py` | ✅ active | TradeRecord, ComplianceReport | 9 | 368 | — | |
| `config_hot_reload.py` | ✅ active | ConfigHotReload | 7 | 128 | — | |
| `deployment_executor.py` | ✅ active | DeploymentExecutor | 8 | 330 | — | |
| `deployment_plan.py` | ✅ active | DeploymentPlanService | 7 | 300 | — | |
| `domain_keys.py` | ⬜ empty | — | 0 | 7 | — | |
| `environment_config.py` | ✅ active | Environment, EnvironmentConfig | 7 | 126 | — | |
| `evidence_bundle.py` | ✅ active | EvidenceBundleService | 10 | 288 | — | |
| `feature_update_producer.py` | ✅ active | — | 2 | 80 | — | |
| `final_audit.py` | ✅ active | FinalAuditService | 5 | 211 | — | |
| `governance_evaluator.py` | ✅ active | — | 3 | 292 | — | |
| `governance_summary.py` | ✅ active | — | 4 | 56 | — | |
| `health_check.py` | ✅ active | HealthCheckService | 9 | 117 | — | |
| `lifecycle_manager.py` | ✅ active | LifecycleManager | 7 | 166 | — | |
| `operational_support.py` | ✅ active | RetryPolicy, ConfigValidator | 7 | 129 | — | |
| `operations_timeline.py` | ✅ active | OperationsTimelineService | 16 | 263 | — | |
| `ops_maturity.py` | ✅ active | OpsMaturityService | 4 | 164 | — | |
| `path_defaults.py` | ✅ active | — | 2 | 85 | — | |
| `permission_audit.py` | ✅ active | AuditEntry, PermissionMatrix, AuditTrail | 20 | 335 | — | |
| `postmortem_report.py` | ✅ active | PostmortemReportService | 11 | 468 | — | |
| `release_certification.py` | ✅ active | ReleaseCertificationService | 12 | 294 | — | |
| `release_gate.py` | ✅ active | ReleaseGateService | 16 | 320 | — | |
| `release_pipeline.py` | ✅ active | ReleasePipelineService | 7 | 378 | — | |
| `release_readiness.py` | ✅ active | ReleaseReadinessService | 13 | 437 | — | |
| `release_registry.py` | ✅ active | ReleaseRegistryService | 17 | 352 | — | |
| `replay_isolation.py` | ✅ active | ReplayDispatchAdapter, NullDispatchAdapter, ReplayEnvironment | 11 | 138 | — | |
| `rollback_drill.py` | ✅ active | RollbackDrillService | 8 | 304 | — | |
| `runbook_engine.py` | ✅ active | RunbookEngine | 16 | 664 | — | |
| `scheduled_task_registry.py` | ✅ active | — | 4 | 36 | — | |
| `scheduler_service.py` | ✅ active | ScheduledTask, SchedulerService | 20 | 483 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 29 | — | |
| `service_container.py` | ✅ active | ServiceContainer | 41 | 650 | — | |
| `startup_validator.py` | ✅ active | — | 1 | 113 | — | |
| `state_persistence.py` | ✅ active | StatePersistence | 6 | 101 | — | |
| `validation_mode.py` | ✅ active | — | 1 | 10 | — | |

## core/execution

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `barrier_strategy.py` | ✅ active | BarrierStrategy | 2 | 132 | — | |
| `brain_gates.py` | ✅ active | — | 3 | 92 | — | |
| `broker_adapter.py` | ✅ active | BrokerAdapter | 9 | 68 | — | |
| `capital_allocator.py` | ✅ active | AllocationDecision, GroupCorrelationTracker, CapitalAllocator | 9 | 468 | — | |
| `conformal_calibrator.py` | ✅ active | ConformalCalibrator | 11 | 460 | — | |
| `conformal_ou_gate.py` | ✅ active | ConformalOUGate | 17 | 868 | — | |
| `correlation_sizer.py` | ✅ active | ClusterResult | 1 | 117 | — | |
| `cross_strategy_coordinator.py` | ✅ active | OpposingPosition, ConflictResolution, CrossStrategyCoordinator | 4 | 228 | — | |
| `dynamic_sl_tp.py` | ✅ active | StrategyFamily, DynamicSLTP | 3 | 279 | — | |
| `execution_manager.py` | ✅ active | ExecutionManager | 7 | 180 | — | |
| `execution_queue.py` | ✅ active | ExecutionQueueFatalError, QueuedDecision, DispatchResult, ExecutionQueue | 9 | 570 | — | |
| `exit_reason.py` | ✅ active | ExitReason | 6 | 342 | — | |
| `exit_watchdog.py` | ✅ active | ExitAttempt, ExitWatchdogResult, ExitWatchdog | 8 | 507 | — | |
| `fill_simulator.py` | ✅ active | FillSimulationConfig, FillSimulator | 8 | 125 | — | |
| `fix_contracts.py` | ✅ active | FixSessionConfig, FixMessage, FixExecutionReport | 4 | 69 | — | |
| `fix_execution_mapper.py` | ✅ active | FixExecutionReportMapper | 5 | 75 | — | |
| `fix_gateway_adapter.py` | ✅ active | FixGatewayAdapter | 12 | 136 | — | |
| `fix_message_builder.py` | ✅ active | FixMessageBuilder | 5 | 57 | — | |
| `gate_reachability.py` | ✅ active | GateReachabilityReport | 4 | 461 | — | |
| `gateway_contracts.py` | ✅ active | OrderRequest, Fill, OrderState, ExecutionGateway | 9 | 103 | — | |
| `gods_eye.py` | ✅ active | GodsEyeVerdict, GodsEye | 15 | 496 | — | |
| `kelly_sizer.py` | ✅ active | KellyResult | 2 | 127 | — | |
| `limit_order_monitor.py` | ✅ active | LimitOrderIntent, LimitFillResult, LimitOrderMonitor | 9 | 329 | — | |
| `live_order_sender.py` | ✅ active | FatalRiskViolation | 6 | 398 | — | |
| `managed_close.py` | ✅ active | — | 2 | 416 | — | |
| `market_efficiency.py` | ✅ active | — | 2 | 67 | — | |
| `market_impact.py` | ✅ active | MarketImpactEstimate | 3 | 168 | — | |
| `meta_exit_engine.py` | ✅ active | ExitFeatureSnapshot, ExitEvaluation, MetaExitEngine | 13 | 511 | — | |
| `meta_filter_gate.py` | ✅ active | MetaFilterGate | 7 | 220 | — | |
| `meta_filter_routing.py` | ✅ active | — | 1 | 248 | — | |
| `meta_pipeline.py` | ✅ active | MetaProbeSpec, MetaProbeResult, MetaPipeline | 8 | 494 | — | |
| `meta_signal_filter.py` | ✅ active | FilterResult, MetaSignalFilter | 19 | 980 | — | |
| `micro_strategy.py` | ✅ active | MicroStrategy | 1 | 86 | — | |
| `microstructure_gate.py` | ✅ active | MicrostructureResult, MicrostructureGate | 3 | 184 | — | |
| `mt5_broker_adapter.py` | ✅ active | MT5BrokerAdapter | 12 | 159 | — | |
| `mt5_worker.py` | ✅ active | MT5Worker | 20 | 406 | — | |
| `net_out_close_handler.py` | ✅ active | — | 1 | 181 | — | |
| `ofi_gate.py` | ✅ active | — | 1 | 90 | — | |
| `ood_gateway.py` | ✅ active | OODVerdict, OODConfig, OODGateway | 11 | 456 | — | |
| `order_state_machine.py` | ✅ active | OrderStateMachine | 9 | 101 | — | |
| `paper_gateway.py` | ✅ active | PaperExecutionGateway | 10 | 153 | — | |
| `portfolio_netting.py` | ✅ active | NettedDecision, PortfolioNettingConfig, PortfolioNettingGate | 10 | 403 | — | |
| `portfolio_risk.py` | ✅ active | RiskVerdict, RiskResult, PortfolioState, PortfolioRiskController | 14 | 531 | — | |
| `position_manager.py` | ✅ active | ActivePosition, ActivePositionManager | 61 | 2525 | — | |
| `pre_trade_guards.py` | ✅ active | IntradayDrawdownKill, CooldownRegistry, FamilyEntryTracker | 28 | 955 | — | |
| `pwin_chain.py` | ✅ active | PWinResolution | 6 | 712 | — | |
| `quality_analyzer.py` | ✅ active | SlippageTracker, ExecutionQualityAnalyzer | 15 | 351 | — | |
| `quality_contracts.py` | ✅ active | ExecutionBenchmark, ExecutionQualityMetric, ImplementationShortfall, ExecutionQualityReport | 3 | 138 | — | |
| `reentry_guard.py` | ✅ active | ExitRecord, ReentryState | 6 | 589 | — | |
| `regime_direction_gate.py` | ✅ active | RegimeDirectionGate | 4 | 220 | — | |
| `regime_gate.py` | ✅ active | RegimeModulation, OURegime2D, RegimeGate | 42 | 841 | — | |
| `rule_engine_strategy.py` | ✅ active | RuleEngineStrategyWrapper | 5 | 262 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `session_detector.py` | ✅ active | SessionDetector | 3 | 182 | — | |
| `statarb_strategy.py` | ✅ active | StatArbStrategy | 1 | 76 | — | |
| `strategy_budget.py` | ✅ active | StrategyBudget | 14 | 410 | — | |
| `strategy_context.py` | ✅ active | StrategyEvaluationContext | 0 | 85 | — | |
| `strategy_decision.py` | ✅ active | StrategyDecision | 2 | 89 | — | |
| `strategy_line.py` | 🧪 stub | StrategyLineConfig, StrategyLine | 19 | 2159 | — | |
| `strategy_protocol.py` | ✅ active | StrategyEvaluateProtocol | 1 | 44 | — | |
| `strategy_type.py` | ✅ active | StrategyType | 0 | 30 | — | |
| `swing_strategy.py` | ✅ active | SwingStrategy | 1 | 137 | — | |
| `trail_stop_engine.py` | ✅ active | TrailPolicy, TrailStopEngine | 12 | 556 | — | |
| `trend_detector.py` | ✅ active | KalmanTrendFilter, TrendDetector | 33 | 699 | — | |
| `trend_isolation_gates.py` | ✅ active | SpatialGateResult | 4 | 382 | — | |
| `trend_volume_guard.py` | ✅ active | — | 3 | 345 | — | |

## core/features

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `data_augmentation.py` | ✅ active | — | 4 | 141 | — | |
| `feature_assembler.py` | ✅ active | — | 3 | 310 | — | |
| `feature_router.py` | ✅ active | FeatureMissingError, SchemaNotFoundError, FeatureRouter | 4 | 299 | — | |
| `feature_service.py` | ✅ active | FeatureService, FeatureBrainRegistry, IntentExplainer | 14 | 479 | — | |
| `feature_snapshot.py` | ✅ active | StoredFeatureSnapshot | 2 | 33 | — | |
| `local_feature_store.py` | ✅ active | FeatureValidationError, LocalFeatureStore | 24 | 434 | — | |
| `meta_feature_builder.py` | ✅ active | — | 1 | 135 | — | |
| `ofi_collector.py` | ✅ active | OFICollector | 5 | 250 | — | |
| `rolling_normalizer.py` | ✅ active | RollingNormalizer | 15 | 234 | — | |
| `stale_feature_guard.py` | ✅ active | StaleFeatureException | 4 | 125 | — | |
| `store_contracts.py` | ✅ active | FeatureSchema, FeatureRecord, FeatureQuery, FeatureStore | 8 | 87 | — | |
| `update_job.py` | ✅ active | FeatureUpdateResult, IncrementalFeatureUpdateJob | 3 | 63 | — | |

## core/features/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `microstructure_feature_adapter.py` | ✅ active | MicrostructureFeatureAdapter | 12 | 280 | — | |
| `v9_feature_adapter.py` | ✅ active | V9FeatureAdapter | 6 | 103 | — | |

## core/features/computers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `btc_feature_augmenter.py` | ✅ active | BTCFeatureAugmenter | 14 | 686 | — | |
| `daily_computer.py` | ✅ active | DailyFeatureComputer | 22 | 726 | — | |
| `live_daily_provider.py` | ✅ active | LiveDailyFeatureProvider | 9 | 262 | — | |
| `microstructure_computer.py` | ✅ active | MicrostructureFeatureComputer | 21 | 687 | — | |
| `v9_live_computer.py` | ✅ active | V9LiveFeatureComputer | 15 | 339 | — | |
| `v9_micro_computer.py` | ✅ active | V9MicroComputer | 3 | 110 | — | |

## core/features/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `btc_macro_enhanced_schema.py` | 📄 config | — | 0 | 209 | — | |
| `daily_swing_schema.py` | 📄 config | — | 0 | 44 | — | |
| `microstructure_schema.py` | ✅ active | — | 1 | 57 | — | |
| `registry.py` | ✅ active | — | 4 | 253 | — | |
| `swing_enhanced_schema.py` | 📄 config | — | 0 | 44 | — | |
| `v9_institutional_schema.py` | 📄 config | — | 0 | 42 | — | |
| `v9_micro_schema.py` | ✅ active | — | 1 | 27 | — | |

## core/feedback

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_performance_tracker.py` | ✅ active | BrainPerformanceTracker | 9 | 137 | — | |
| `brain_pnl_ledger.py` | ✅ active | BrainPnLMetrics, BrainPnLStore | 25 | 1009 | — | |
| `brain_quality_engine.py` | ✅ active | BrainQualityVerdict, BrainQualityEngine | 13 | 432 | — | |
| `decision_scorer.py` | ✅ active | DecisionScorer | 5 | 120 | — | |
| `experience_replay.py` | ✅ active | ExperienceReplayBuffer | 10 | 246 | — | |
| `feedback_loop.py` | ✅ active | FeedbackLoop | 4 | 110 | — | |
| `live_journal_metrics.py` | ✅ active | — | 2 | 225 | — | |
| `online_feedback_hook.py` | ✅ active | OnlineFeedbackHook | 11 | 459 | — | |
| `outcome_collector.py` | ✅ active | OutcomeCollector | 4 | 111 | — | |
| `param_optimizer.py` | ✅ active | — | 5 | 283 | — | |
| `performance_analytics.py` | ✅ active | PerformanceAnalytics | 12 | 194 | — | |

## core/governance

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_rule_engine.py` | ✅ active | GovernanceRule, GovernanceRuleEngine | 17 | 486 | — | |
| `governance_service.py` | ✅ active | GovernanceService | 19 | 372 | — | |
| `shadow_tracker.py` | ✅ active | ShadowBrainMetrics, ShadowTracker | 9 | 132 | — | |

## core/infrastructure

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `distributed_lock.py` | 🧪 stub | LockAcquireResult, BaseLock, FileLock, DirectoryLock | 25 | 456 | — | |

## core/ledger

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_sources.py` | ✅ active | — | 1 | 17 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 7 | — | |
| `stream_names.py` | ✅ active | — | 2 | 15 | — | |

## core/ledger/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `communication_inspection_service.py` | ✅ active | CommunicationInspectionService | 15 | 345 | — | |
| `communication_operations_service.py` | ✅ active | CommunicationOperationsService | 13 | 446 | — | |
| `communication_record_reader.py` | ✅ active | CommunicationRecordReader | 6 | 46 | — | |
| `communication_record_writer.py` | ✅ active | CommunicationRecordWriter | 2 | 22 | — | |
| `communication_replay_executor.py` | ✅ active | CommunicationReplayExecutor | 9 | 382 | — | |
| `communication_replay_gate.py` | ✅ active | CommunicationReplayGate | 4 | 275 | — | |
| `communication_replay_service.py` | ✅ active | CommunicationReplayService | 9 | 312 | — | |
| `communication_trace_refs.py` | ✅ active | — | 14 | 95 | — | |
| `decision_record_writer.py` | ✅ active | DecisionRecordWriter | 2 | 59 | — | |
| `execution_event_reader.py` | ✅ active | ExecutionEventReader | 8 | 119 | — | |
| `execution_event_writer.py` | ✅ active | ExecutionEventWriter | 3 | 47 | — | |
| `execution_reconciliation_service.py` | ✅ active | ExecutionReconciliationService | 9 | 264 | — | |
| `gate_decision_refs.py` | ✅ active | — | 4 | 30 | — | |
| `journal_cleanup.py` | ✅ active | — | 13 | 943 | — | |
| `journal_gate.py` | ✅ active | JournalGate | 13 | 275 | — | |
| `pnl_guard.py` | ✅ active | PnlGuard | 2 | 138 | — | |
| `replay_execution_reader.py` | ✅ active | ReplayExecutionReader | 5 | 35 | — | |
| `replay_execution_writer.py` | ✅ active | ReplayExecutionWriter | 2 | 26 | — | |
| `replay_plan_refs.py` | ✅ active | — | 11 | 85 | — | |
| `replay_record_refs.py` | ✅ active | — | 15 | 159 | — | |
| `replay_trace_refs.py` | ✅ active | — | 8 | 63 | — | |

## core/ledger/storage

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `jsonl_ledger_store.py` | ✅ active | JsonlLedgerStore | 2 | 25 | — | |

## core/market

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `calendar.py` | ✅ active | — | 9 | 322 | — | |
| `mtf_price_service.py` | ✅ active | MTFPriceService | 11 | 166 | — | |
| `position_tracker.py` | ✅ active | PositionTracker, MarketContextProvider | 12 | 125 | — | |
| `signal_processor.py` | ✅ active | SignalFilter, MarketSignalProcessor | 8 | 121 | — | |

## core/metrics

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brinson_attribution.py` | ✅ active | BrinsonResult, BrinsonMultiPeriod | 8 | 202 | — | |
| `factor_attribution.py` | ✅ active | FactorAttributionReport | 4 | 222 | — | |
| `financial_metrics.py` | ✅ active | — | 13 | 278 | — | |
| `portfolio_optimizer.py` | ✅ active | EfficientFrontier | 10 | 276 | — | |

## core/observability

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_health_helpers.py` | ✅ active | — | 6 | 139 | — | |
| `alert_channels.py` | ✅ active | SlackAlertChannel, DingTalkAlertChannel, CompositeAlertChannel | 12 | 347 | — | |
| `alert_runbook_bridge.py` | ✅ active | RunbookAction, RunbookSOP, AlertRunbookBridge | 10 | 502 | — | |
| `alert_service.py` | 🧪 stub | AlertRule, AlertChannel, LogAlertChannel, InMemoryAlertChannel, BatchingAlertChannel, SeverityRouter, AlertService | 28 | 479 | — | |
| `audit_log.py` | ✅ active | StructuredAuditLog | 11 | 180 | — | |
| `data_health_schema.py` | ✅ active | Tier, SourceStatus, SourceCheckResult, CrossCheckResult, OrphanFinding, BehavioralMetrics, HealthReport, HealthCheckMeta, SourceHealthRecord | 5 | 306 | — | |
| `data_health_service.py` | ✅ active | DataHealthService | 9 | 320 | — | |
| `data_loss.py` | ✅ active | — | 1 | 103 | — | |
| `degradation.py` | ✅ active | DegradationLevel, DegradationConstraints | 4 | 267 | — | |
| `diagnostics_dashboard.py` | ✅ active | DiagnosticsDashboard | 7 | 149 | — | |
| `entry_context_guard.py` | ✅ active | EntryContextGuard | 7 | 207 | — | |
| `event_bus.py` | ✅ active | EventBus | 7 | 65 | — | |
| `event_schema.py` | ✅ active | EventSeverity, BaseTelemetryEvent, FailedSource, DataHealthPayload | 1 | 147 | — | |
| `health_checks.py` | 🧪 stub | HealthCheckMethods | 48 | 3529 | — | |
| `invariant_engine.py` | ✅ active | InvariantDef, InvariantViolation, InvariantEngine | 23 | 552 | — | |
| `live_alert_hub.py` | ✅ active | StormState, AlertStormDetector, BackgroundDeliveryWorker, LiveAlertHub, _QueueChannel, _AlertAuditLog | 36 | 927 | — | |
| `localization.py` | ✅ active | RuleRegistry | 6 | 190 | — | |
| `message_broker.py` | ✅ active | Message, MessageBroker, InProcessBroker, RedisStreamsBroker | 23 | 275 | — | |
| `meta_wire_events.py` | ✅ active | — | 4 | 128 | — | |
| `metric_names.py` | ✅ active | — | 2 | 56 | — | |
| `metrics_collector.py` | ✅ active | MetricsCollector | 10 | 96 | — | |
| `mlflow_bridge.py` | ✅ active | — | 4 | 141 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `slo_service.py` | ✅ active | SloService | 9 | 191 | — | |
| `tracing.py` | ✅ active | Span, TracingContext | 18 | 128 | — | |

## core/parliament

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contract_groups.py` | ✅ active | ContractGroupConsensus, ABGroupRouter | 15 | 867 | — | |
| `group_consensus.py` | ✅ active | — | 1 | 195 | — | |
| `parliament_service.py` | ✅ active | ParliamentService | 11 | 315 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/protocol

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `event_bar_sync.py` | ✅ active | BarSyncState, BarSyncPoller | 15 | 762 | — | |
| `live_execution_contract.py` | ✅ active | — | 5 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 8 | — | |

## core/protocol/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `communication_adapter.py` | ✅ active | CommunicationAdapter | 1 | 7 | — | |
| `communication_adapter_registry.py` | ✅ active | CommunicationAdapterRegistry | 3 | 61 | — | |
| `communication_dispatcher.py` | ✅ active | CommunicationDispatcher | 4 | 363 | — | |
| `decision_compiler.py` | ✅ active | DecisionCompiler | 5 | 118 | — | |
| `file_queue_communication_adapter.py` | ✅ active | FileQueueCommunicationAdapter | 2 | 55 | — | |
| `file_queue_receipt_reader.py` | ✅ active | FileQueueReceiptReader | 5 | 28 | — | |
| `fix_communication_adapter.py` | ✅ active | FixCommunicationAdapter | 6 | 116 | — | |
| `idempotency.py` | ✅ active | IdempotencyStore, DuplicateDetector | 10 | 111 | — | |
| `intent_message_builder.py` | ✅ active | IntentMessageBuilder | 3 | 60 | — | |
| `mt5_communication_adapter.py` | ✅ active | MT5CommunicationAdapter | 2 | 81 | — | |
| `override_resolver.py` | ✅ active | OverrideResolver | 1 | 22 | — | |
| `resilience.py` | ✅ active | CircuitState, CircuitBreaker, RateLimiter | 13 | 153 | — | |
| `stub_communication_adapter.py` | ✅ active | StubCommunicationAdapter | 2 | 23 | — | |
| `venue_router.py` | 🧪 stub | VenueAdapter, StubVenueAdapter, VenueRouter | 14 | 116 | — | |
| `zmq_communication_adapter.py` | ✅ active | CircuitBreakerOpenError, ZMQCommunicationAdapter | 6 | 237 | — | |
| `zmq_receipt_listener.py` | ✅ active | ZMQReceiptListener | 9 | 232 | — | |

## core/risk

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `regime_detector.py` | ✅ active | RegimeDetector | 14 | 318 | — | |
| `risk_evaluation_service.py` | ✅ active | RiskEvaluationService | 5 | 165 | — | |
| `risk_policies.py` | ✅ active | RiskPolicy, PositionLimitPolicy, DrawdownPolicy, ExposurePolicy, ConcentrationPolicy, ModePolicy | 12 | 136 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/runtime

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `alpha_budget_contracts.py` | ✅ active | AlphaBudgetContractError, AlphaRiskBudgetContractValidator, AlphaBudgetUsageContractValidator | 6 | 91 | — | |
| `alpha_budget_usage_reporter.py` | ✅ active | AlphaBudgetUsageReporter | 3 | 93 | — | |
| `alpha_budget_usage_store.py` | ✅ active | AlphaBudgetUsageStore | 9 | 57 | — | |
| `alpha_risk_budget_gate.py` | ✅ active | AlphaRiskBudgetGate | 9 | 111 | — | |
| `approval_contracts.py` | ✅ active | ExecutionApproval | 4 | 76 | — | |
| `btc_feature_persist.py` | ✅ active | — | 1 | 117 | — | |
| `circuit_breaker_reset.py` | ✅ active | — | 2 | 103 | — | |
| `close_label.py` | ✅ active | — | 6 | 158 | — | |
| `cooldown.py` | ✅ active | — | 1 | 21 | — | |
| `cycle_replay.py` | ✅ active | RuntimeReplayReport, RuntimeCycleReplay | 4 | 124 | — | |
| `daily_ops_scheduler.py` | ✅ active | — | 1 | 71 | — | |
| `daily_ops_state.py` | ✅ active | — | 8 | 81 | — | |
| `data_health_monitor.py` | ✅ active | — | 1 | 68 | — | |
| `deal_selection.py` | ✅ active | ExitResolution | 8 | 195 | — | |
| `dispatch_post.py` | ✅ active | — | 2 | 92 | — | |
| `evidence_contracts.py` | ✅ active | RuntimeEvidenceRecord | 2 | 61 | — | |
| `evidence_reader.py` | ✅ active | RuntimeEvidenceReader | 5 | 42 | — | |
| `evidence_writer.py` | ✅ active | RuntimeEvidenceWriter | 2 | 30 | — | |
| `execution_gates.py` | ✅ active | RuntimeRiskGate, RuntimeGovernanceGate, RuntimeExecutionApprovalChain | 6 | 107 | — | |
| `execution_gateway_router.py` | ✅ active | ExecutionGatewayRouter | 5 | 30 | — | |
| `execution_pipeline.py` | ✅ active | RuntimeExecutionPipeline | 4 | 106 | — | |
| `execution_state.py` | ✅ active | — | 3 | 339 | — | |
| `fault_handler.py` | ✅ active | FaultLevel, FaultTolerantContext | 11 | 466 | — | |
| `feature_freshness.py` | ✅ active | — | 2 | 81 | — | |
| `gate_audit_recorder.py` | ✅ active | — | 1 | 61 | — | |
| `gods_eye_bridge.py` | ✅ active | — | 2 | 151 | — | |
| `golden_master.py` | ✅ active | — | 8 | 309 | — | |
| `h1_features.py` | ✅ active | — | 1 | 112 | — | |
| `integration_contracts.py` | ✅ active | OrderSizingPolicy, RuntimePipelineResult | 2 | 52 | — | |
| `legacy_dispatch_reference.py` | ⬜ empty | — | 0 | 84 | — | |
| `live_bootstrap.py` | ✅ active | — | 1 | 177 | — | |
| `live_cycle.py` | ✅ active | LiveCycleConfig, LiveCycleState | 26 | 5215 | — | |
| `live_startup.py` | ✅ active | — | 10 | 369 | — | |
| `management_phase.py` | ✅ active | — | 18 | 2674 | — | |
| `market_ingress.py` | ✅ active | — | 8 | 383 | — | |
| `mia_close.py` | ✅ active | — | 2 | 204 | — | |
| `micro_persist.py` | ✅ active | — | 1 | 73 | — | |
| `modify_trail_dispatch.py` | ✅ active | — | 1 | 168 | — | |
| `order_dispatch.py` | ✅ active | _MinimalControlSnapshot | 10 | 308 | — | |
| `ou_hurst.py` | ✅ active | — | 1 | 72 | — | |
| `pnl_recording.py` | ✅ active | — | 1 | 83 | — | |
| `position_close_adapter.py` | ✅ active | PositionCloseAdapter | 12 | 653 | — | |
| `position_ownership.py` | ✅ active | — | 1 | 69 | — | |
| `position_registration.py` | ✅ active | — | 1 | 348 | — | |
| `pre_close_check.py` | ✅ active | — | 1 | 36 | — | |
| `pre_close_context.py` | ✅ active | PreCloseContext | 8 | 145 | — | |
| `reconciliation.py` | ✅ active | — | 2 | 408 | — | |
| `reentry_alert.py` | ✅ active | — | 1 | 79 | — | |
| `reentry_recording.py` | ✅ active | — | 2 | 85 | — | |
| `restart_state.py` | ✅ active | — | 2 | 490 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 16 | — | |
| `session_guards.py` | ✅ active | — | 3 | 188 | — | |
| `settlement_queue.py` | ✅ active | SettlementEntry, SettlementQueue | 14 | 706 | — | |
| `shadow_recorder.py` | ✅ active | — | 8 | 313 | — | |
| `signal_health.py` | ✅ active | GateResult, FeatureGate, _RollingStats, SignalHealthMonitor | 23 | 505 | — | |
| `signal_order_builder.py` | ✅ active | SignalOrderRequestBuilder | 3 | 52 | — | |
| `signal_pipeline.py` | ✅ active | — | 2 | 108 | — | |
| `signal_settlement.py` | ✅ active | — | 1 | 97 | — | |
| `strategy_builder.py` | ✅ active | — | 6 | 1313 | — | |
| `strategy_config_validator.py` | ✅ active | — | 1 | 48 | — | |
| `strategy_evaluator.py` | ✅ active | — | 6 | 1450 | — | |
| `summary_service.py` | ✅ active | RuntimeSummaryService | 11 | 143 | — | |
| `supervised_scheduler.py` | ✅ active | TaskStatus, TaskKind, SchedulerConfig, ThreadTask, ProcessTask, SupervisedScheduler | 17 | 458 | — | |
| `time_utils.py` | ✅ active | — | 1 | 18 | — | |
| `timeframe_scaling.py` | ✅ active | — | 1 | 60 | — | |
| `trade_notify.py` | ✅ active | — | 2 | 86 | — | |
| `trail_dispatch.py` | ✅ active | — | 1 | 510 | — | |
| `typed_clock.py` | ✅ active | MonotonicInstant, WallInstant, Duration, Clock | 19 | 205 | — | |

## core/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `trading_contracts.py` | ✅ active | BrainSignal, ConsensusResult, StrategyDecision, DegradedResult | 0 | 142 | — | |

## core/simulation

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `spread_model.py` | ✅ active | SpreadModel | 8 | 160 | — | |

## core/state

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `catalog.py` | ✅ active | DataIntegrityError, CrossSymbolContaminationError, StateArtifact | 22 | 593 | — | |
| `freshness_guard.py` | ✅ active | FreshnessEntry | 8 | 458 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `writer.py` | ✅ active | StateWriter | 13 | 326 | — | |

## core/state/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `control_snapshot.py` | ✅ active | ControlSnapshot | 0 | 12 | — | |
| `control_snapshot_service.py` | ✅ active | ControlSnapshotService | 2 | 29 | — | |

## core/state/stores

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `override_store.py` | ✅ active | OverrideStore | 4 | 57 | — | |
| `system_mode_store.py` | ✅ active | SystemModeStore | 6 | 150 | — | |

## core/strategies

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | StrategyMetadata, RequiredFeature, Signal, StrategyHealth, AlphaAgent | 9 | 102 | — | |
| `examples.py` | ✅ active | ThresholdAlphaAgent | 8 | 86 | — | |
| `registry.py` | ✅ active | StrategyPluginRegistry, StrategyPluginRunner | 11 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `structural_swing_v1.py` | ✅ active | SwingSignal, StructuralSwingV1 | 7 | 237 | — | |

## core/trading

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | LifecycleStage, RefinementResult, ExitVerdict, StageInfo | 5 | 141 | — | |
| `position_lifecycle.py` | ✅ active | StageGate, ExitPriorityQueue | 25 | 773 | — | |
| `ratchet_risk.py` | ✅ active | RatchetConfig, RatchetVerdict, RatchetRisk | 4 | 235 | — | |
| `signal_refinement.py` | ✅ active | RefinementConfig, SignalRefinementGate | 9 | 449 | — | |

## core/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_config.py` | ✅ active | — | 4 | 242 | — | |
| `breakeven.py` | ✅ active | BreakevenResult | 6 | 190 | — | |
| `checkpoint.py` | ✅ active | CheckpointInfo, CheckpointManager | 13 | 199 | — | |
| `cpcv.py` | ✅ active | CPCVFold, CPCVResult | 9 | 252 | — | |
| `custom_objectives.py` | ✅ active | — | 11 | 338 | — | |
| `dataset.py` | ✅ active | TrainingDataset | 17 | 429 | — | |
| `evaluation_report.py` | ✅ active | SHAPReport, TrainingEvalReport | 11 | 454 | — | |
| `experiment_tracker.py` | ✅ active | RunInfo, ExperimentTracker | 13 | 265 | — | |
| `feature_replay.py` | ✅ active | ReplayComponents | 19 | 468 | — | |
| `model_card.py` | ✅ active | ModelCard, ModelCardGenerator | 6 | 225 | — | |
| `model_hashing.py` | ✅ active | — | 4 | 59 | — | |
| `profitability_calibrator.py` | ✅ active | BarrierConfig, ProfitabilityPoint, ProfitabilitySurface | 8 | 413 | — | |
| `registries.py` | ✅ active | — | 32 | 287 | — | |
| `trainer_protocol.py` | ✅ active | TrainResult, TrainerProtocol | 5 | 110 | — | |
| `training_registry.py` | ✅ active | Base, TrainingRunRecord, TrainingRegistry | 16 | 303 | — | |
| `transfer_adapter.py` | ✅ active | TransferDataError, FrozenBaseModel, ResidualTransferLearner | 12 | 255 | — | |
| `utils.py` | ✅ active | — | 4 | 90 | — | |

## scripts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_analyze_h1_swing_now.py` | ✅ active | — | 3 | 379 | — | |
| `_analyze_h4_swing_now.py` | ✅ active | — | 3 | 379 | — | |
| `_analyze_m15_swing_now.py` | ✅ active | — | 3 | 323 | — | |
| `_audit_20260806_07_losses.py` | ✅ active | — | 8 | 359 | — | |
| `_audit_asof_join_miss_20260821.py` | ✅ active | — | 8 | 425 | — | |
| `_audit_btc_legacy_lineage_pnl_20260805.py` | ✅ active | — | 2 | 91 | — | |
| `_audit_btc_modify_misroute_exposure_20260819.py` | ✅ active | — | 3 | 222 | — | |
| `_audit_close_label_divergence_20260821.py` | ✅ active | — | 4 | 176 | — | |
| `_audit_dupe_category_20260805.py` | ✅ active | — | 2 | 144 | — | |
| `_audit_entry_regime_20260807.py` | 📄 config | — | 0 | 72 | — | |
| `_audit_entry_timing_20260807.py` | ✅ active | — | 5 | 301 | — | |
| `_audit_export_m5_20260817.py` | ✅ active | — | 3 | 153 | — | |
| `_audit_idempotent_key_20260805.py` | ✅ active | — | 1 | 94 | — | |
| `_audit_journal_universe_20260821.py` | ✅ active | — | 2 | 197 | — | |
| `_audit_live_health_20260814.py` | ✅ active | — | 16 | 475 | — | |
| `_audit_live_shadow_inventory_20260805.py` | ✅ active | — | 3 | 181 | — | |
| `_audit_magic_alignment_safety.py` | ✅ active | — | 3 | 115 | — | |
| `_audit_shadow_pnl_august_20260812.py` | ✅ active | — | 3 | 139 | — | |
| `_audit_shadow_pnl_replay_20260812.py` | ✅ active | — | 5 | 248 | — | |
| `_audit_storm_domain_wal_20260819.py` | ✅ active | — | 1 | 97 | — | |
| `_audit_storm_forensics_20260819.py` | ✅ active | — | 1 | 108 | — | |
| `_audit_storm_sender_20260806.py` | ✅ active | — | 1 | 130 | — | |
| `_audit_trail_mislabel_20260806.py` | ✅ active | — | 3 | 281 | — | |
| `_audit_two_longs_20260805.py` | ✅ active | — | 2 | 57 | — | |
| `_audit_watchdog_bg_kill_20260820.py` | ✅ active | — | 6 | 261 | — | |
| `_audit_xau_band_extremes_20260805.py` | ✅ active | — | 5 | 236 | — | |
| `_audit_xau_entry_quality_20260810.py` | ✅ active | — | 7 | 285 | — | |
| `_audit_xau_entry_structure_20260807.py` | ✅ active | — | 2 | 127 | — | |
| `_audit_xau_first_open_20260812.py` | ✅ active | — | 4 | 158 | — | |
| `_audit_xau_journal_pnl_null_20260806.py` | ✅ active | — | 1 | 92 | — | |
| `_audit_xau_longs_20260807.py` | ✅ active | — | 2 | 106 | — | |
| `_audit_xau_mt5_reconciliation_20260813.py` | ✅ active | — | 5 | 228 | — | |
| `_audit_xau_period_exit_match_20260813.py` | ✅ active | — | 7 | 361 | — | |
| `_audit_xau_pnl_after_volume_increase_20260813.py` | ✅ active | — | 5 | 224 | — | |
| `_audit_xau_pnl_detail_20260813.py` | ✅ active | — | 3 | 217 | — | |
| `_audit_xau_spatial_gate_20260810.py` | ✅ active | — | 4 | 153 | — | |
| `_audit_xau_tp_shrink_20260817.py` | ✅ active | — | 2 | 162 | — | |
| `_audit_xau_two_longs_20260805.py` | ✅ active | — | 1 | 38 | — | |
| `_audit_xau_votes_pnl_20260805.py` | ✅ active | — | 4 | 173 | — | |
| `_evaluate_probation_m30_h1v2.py` | ✅ active | — | 4 | 172 | — | |
| `_merge_aligned_multitf_data.py` | ✅ active | — | 5 | 351 | — | |
| `_monitor_direction_concentration.py` | ✅ active | — | 7 | 366 | — | |
| `_mypy_scope.py` | ✅ active | — | 2 | 51 | — | |
| `_reconcile_zombie_4454299643_20260807.py` | ✅ active | — | 3 | 200 | — | |
| `_train_h1_binary_final.py` | ✅ active | — | 4 | 301 | — | |
| `_train_h4_binary_final.py` | ✅ active | — | 4 | 303 | — | |
| `_train_m15_binary_final.py` | ✅ active | — | 4 | 284 | — | |
| `_verify_governance_evaluator.py` | ✅ active | — | 3 | 155 | — | |
| `alert_dispatcher.py` | ✅ active | AlertCard | 7 | 221 | — | |
| `analyze_90501_institutional.py` | 📄 config | — | 0 | 262 | — | |
| `analyze_deps.py` | ✅ active | — | 6 | 262 | — | |
| `analyze_dual_symbol_trades.py` | ✅ active | — | 4 | 275 | — | |
| `analyze_exit_optimization_effect.py` | ✅ active | — | 6 | 661 | — | |
| `analyze_feature_shift.py` | ✅ active | — | 5 | 323 | — | |
| `analyze_gate_activity.py` | ✅ active | — | 3 | 194 | — | |
| `analyze_live_brain_performance.py` | ✅ active | — | 6 | 754 | — | |
| `analyze_live_journal.py` | ✅ active | — | 7 | 827 | — | |
| `analyze_ou_pnl.py` | ✅ active | — | 10 | 401 | — | |
| `analyze_recent_losses.py` | ✅ active | — | 1 | 315 | — | |
| `analyze_shadow_exit.py` | ✅ active | — | 7 | 414 | — | |
| `analyze_shadow_predictions.py` | ✅ active | — | 11 | 569 | — | |
| `analyze_swing_pnl.py` | ✅ active | — | 16 | 527 | — | |
| `analyze_trail_impact.py` | ✅ active | — | 8 | 423 | — | |
| `analyze_xau_recent_entries.py` | ✅ active | — | 9 | 460 | — | |
| `assess_system_health.py` | 📄 config | — | 0 | 277 | — | |
| `audit_2day.py` | ✅ active | — | 1 | 236 | — | |
| `audit_behavior_compliance.py` | ✅ active | — | 5 | 437 | — | |
| `audit_brain_fleet.py` | ✅ active | — | 1 | 307 | — | |
| `audit_btc_cross_validate.py` | ✅ active | — | 4 | 285 | — | |
| `audit_btc_live_direction.py` | ✅ active | — | 7 | 304 | — | |
| `audit_btc_v11_ledger.py` | 📄 config | — | 0 | 107 | — | |
| `audit_cross_symbol_consistency.py` | ✅ active | — | 5 | 241 | — | |
| `audit_data_chain_integrity.py` | ✅ active | — | 22 | 1134 | — | |
| `audit_data_exhaustive.py` | ✅ active | — | 8 | 899 | — | |
| `audit_data_final.py` | ✅ active | — | 5 | 246 | — | |
| `audit_data_health_journal.py` | ✅ active | — | 2 | 174 | — | |
| `audit_data_integrity.py` | ✅ active | — | 12 | 1002 | — | |
| `audit_data_module.py` | ✅ active | — | 3 | 358 | — | |
| `audit_deferred_tasks.py` | ✅ active | TriggerCondition, TaskAssessment | 11 | 577 | — | |
| `audit_entry_spread.py` | ✅ active | — | 2 | 108 | — | |
| `audit_entry_spread_coverage.py` | ✅ active | — | 2 | 181 | — | |
| `audit_full_pipeline.py` | ✅ active | — | 12 | 648 | — | |
| `audit_institutional_performance.py` | ✅ active | — | 10 | 604 | — | |
| `audit_live_brains.py` | ✅ active | — | 1 | 231 | — | |
| `audit_live_health.py` | ✅ active | — | 3 | 201 | — | |
| `audit_memory.py` | ✅ active | — | 8 | 399 | — | |
| `audit_physics_thresholds.py` | ✅ active | — | 2 | 157 | — | |
| `audit_pnl_ledger_integrity.py` | ✅ active | — | 3 | 364 | — | |
| `audit_profitability.py` | ✅ active | — | 7 | 373 | — | |
| `audit_state_of_system.py` | ✅ active | — | 5 | 347 | — | |
| `audit_trade_quality.py` | ✅ active | — | 5 | 275 | — | |
| `audit_xau_directional_bias.py` | 📄 config | — | 0 | 128 | — | |
| `audit_xau_exits.py` | ✅ active | — | 2 | 152 | — | |
| `augment_journal_strategy.py` | ✅ active | — | 4 | 182 | — | |
| `backfill_fabricated_breakeven.py` | ✅ active | — | 9 | 310 | ✅ | |
| `backfill_journal_orphans.py` | ✅ active | — | 2 | 224 | — | |
| `backfill_journal_pnl.py` | ✅ active | — | 7 | 353 | — | |
| `backtest_rule_strategies.py` | ✅ active | — | 7 | 572 | — | |
| `backtest_runner.py` | ✅ active | — | 3 | 271 | — | |
| `backtest_structural_swing.py` | ✅ active | — | 5 | 340 | — | |
| `benchmark_zmq_latency.py` | ✅ active | — | 5 | 189 | — | |
| `ble001_phase3b_migrate_hotpath.py` | ✅ active | — | 5 | 162 | — | |
| `ble001_phase3c_fog_wrap.py` | ✅ active | — | 4 | 159 | — | |
| `ble001_phase3d_coldpath_fog_wrap.py` | ✅ active | — | 5 | 227 | — | |
| `ble001_phase3e_deferred_fog_wrap.py` | ✅ active | — | 4 | 231 | — | |
| `brain.py` | ✅ active | — | 13 | 861 | — | |
| `bridge_supervisor.py` | ✅ active | — | 4 | 121 | — | |
| `build_btc_metafilter_v2_dataset.py` | ✅ active | — | 11 | 710 | — | |
| `build_metafilter_dataset.py` | ✅ active | — | 2 | 222 | — | |
| `build_regime_snapshots.py` | ✅ active | — | 2 | 131 | — | |
| `calibrate_binary_threshold.py` | ✅ active | — | 4 | 394 | — | |
| `check_blueprint_compliance.py` | ✅ active | — | 10 | 763 | — | |
| `check_data_health_contract.py` | ✅ active | Severity | 13 | 732 | — | |
| `check_import_boundaries.py` | ✅ active | Violation, _ExceptionEntry | 3 | 239 | — | |
| `check_omega_compliance.py` | ✅ active | Violation | 7 | 234 | — | |
| `check_omega_pre_push.py` | ✅ active | — | 2 | 86 | — | |
| `check_preconditions.py` | ✅ active | — | 12 | 429 | — | |
| `check_symbol_liveness.py` | ✅ active | LivenessVerdict | 8 | 364 | — | |
| `check_training_readiness.py` | ✅ active | StageVerdict | 17 | 1323 | — | |
| `check_training_triggers.py` | ✅ active | — | 9 | 313 | — | |
| `ci_prepare_v9_shadow_fixtures.py` | ✅ active | — | 2 | 188 | — | |
| `classify_ble001.py` | ✅ active | — | 1 | 63 | — | |
| `clean_ledger_bloat.py` | ✅ active | — | 2 | 192 | — | |
| `cleanup_claude_transcripts.py` | ✅ active | — | 2 | 100 | — | |
| `commander_g2_metafilter_path.py` | ✅ active | — | 3 | 258 | — | |
| `commander_g3_alpha_vacuum.py` | ✅ active | — | 4 | 159 | — | |
| `commander_g4_g6_g7_coverage_xau.py` | ✅ active | — | 5 | 226 | — | |
| `commander_guardrails_arch.py` | ✅ active | — | 7 | 323 | — | |
| `coverage_baseline.py` | ✅ active | — | 9 | 388 | — | |
| `daily_cost_report.py` | ✅ active | — | 4 | 175 | — | |
| `daily_flow46_precheck.py` | ✅ active | — | 17 | 539 | — | |
| `daily_ops.py` | ✅ active | — | 42 | 3613 | — | |
| `data_integrity_check.py` | ✅ active | — | 9 | 553 | — | |
| `data_pipeline_audit.py` | 📄 config | — | 0 | 326 | — | |
| `dedup_journal.py` | ✅ active | — | 1 | 102 | — | |
| `dedup_journal_by_ticket.py` | ✅ active | — | 3 | 169 | — | |
| `deep_audit_live_data.py` | ✅ active | — | 15 | 875 | — | |
| `deep_audit_probes.py` | ✅ active | — | 7 | 363 | — | |
| `deploy_blue_green.py` | ✅ active | — | 7 | 126 | — | |
| `diagnose_data_health_failures.py` | ✅ active | — | 4 | 270 | — | |
| `diagnose_feature_drift.py` | ✅ active | — | 4 | 229 | — | |
| `diagnose_journal_mt5_sev2.py` | ✅ active | — | 3 | 407 | — | |
| `diagnose_mypy_baseline.py` | ✅ active | — | 6 | 210 | — | |
| `diagnose_process_health.py` | ✅ active | — | 10 | 555 | — | |
| `diagnose_sl_performance.py` | ✅ active | — | 3 | 156 | — | |
| `dqaf053_phase1_sanitize.py` | ✅ active | — | 8 | 368 | — | |
| `dqaf_collect.py` | ✅ active | — | 12 | 564 | — | |
| `export_ood_params.py` | ✅ active | — | 1 | 250 | — | |
| `extract_health_checks.py` | 📄 config | — | 0 | 179 | — | |
| `feature_store_maintenance.py` | ✅ active | — | 8 | 276 | — | |
| `feedback_loop.py` | ✅ active | — | 12 | 426 | — | |
| `forensic_feature_analysis.py` | ✅ active | — | 2 | 294 | — | |
| `gate2_sentinel.py` | ✅ active | — | 8 | 301 | — | |
| `generate_btc_empirical_scaler.py` | ✅ active | — | 3 | 221 | — | |
| `generate_micro_scaler.py` | ✅ active | — | 5 | 320 | — | |
| `governance_promote_m15.py` | 📄 config | — | 0 | 51 | — | |
| `guard_git_stash.py` | ✅ active | — | 3 | 111 | — | |
| `health_check.py` | ✅ active | — | 16 | 759 | — | |
| `hook_architecture_gate.py` | ✅ active | — | 6 | 397 | — | |
| `hook_blueprint_precheck.py` | ✅ active | — | 1 | 75 | — | |
| `hook_mypy_check.py` | ✅ active | — | 1 | 98 | — | |
| `hook_pre_push.py` | ✅ active | — | 6 | 284 | — | |
| `ingest_live_journal_to_alpha.py` | ✅ active | — | 4 | 96 | — | |
| `inject_regime_to_labels.py` | ✅ active | — | 3 | 211 | — | |
| `inspect_ofi_history.py` | ✅ active | — | 5 | 224 | ✅ | |
| `journal_freeze_gate.py` | ✅ active | — | 4 | 145 | ✅ | |
| `launcher_supervisor.py` | ✅ active | — | 19 | 365 | ✅ | |
| `live_audit_realtime.py` | ✅ active | — | 4 | 385 | — | |
| `live_auto_healthcheck.py` | ✅ active | — | 11 | 232 | — | |
| `live_daily_recap.py` | ✅ active | — | 25 | 941 | — | |
| `live_dashboard.py` | ✅ active | — | 16 | 538 | — | |
| `live_data_quality_report.py` | ✅ active | — | 13 | 368 | — | |
| `live_dispatch_policy.py` | ✅ active | — | 10 | 316 | — | |
| `live_feature_quality_report.py` | ✅ active | — | 6 | 212 | — | |
| `live_intent_loop.py` | ✅ active | — | 6 | 2843 | — | |
| `live_launcher.py` | ✅ active | — | 19 | 1409 | — | |
| `live_micro_rollout_gate.py` | ✅ active | — | 5 | 138 | — | |
| `live_monitor.py` | ✅ active | — | 12 | 482 | — | |
| `live_read_only_preflight.py` | ✅ active | — | 5 | 145 | — | |
| `live_shadow_ensemble.py` | ✅ active | — | 12 | 553 | — | |
| `live_shadow_intent_producer.py` | ✅ active | — | 7 | 260 | — | |
| `live_stack_diagnostic.py` | ✅ active | — | 5 | 204 | — | |
| `market_calendar.py` | ⬜ empty | — | 0 | 13 | — | |
| `migrate_fog_live_cycle.py` | ✅ active | — | 3 | 112 | — | |
| `monitor_feature_drift.py` | ✅ active | — | 11 | 773 | — | |
| `monitor_pwin_fix.py` | ✅ active | — | 3 | 171 | — | |
| `mt5_bridge_healthcheck.py` | ✅ active | — | 6 | 153 | — | |
| `mt5_bridge_worker.py` | ✅ active | — | 40 | 2365 | — | |
| `mt5_positions_snapshot.py` | ✅ active | — | 4 | 99 | — | |
| `mt5_spread_probe.py` | ✅ active | — | 1 | 67 | — | |
| `normalize_journal_pnl.py` | ✅ active | — | 1 | 202 | — | |
| `omega_constants.py` | ✅ active | — | 1 | 112 | — | |
| `omega_crash_snapshot.py` | ✅ active | — | 4 | 158 | — | |
| `omega_gate.py` | ✅ active | — | 4 | 558 | — | |
| `online_feedback_hook.py` | ✅ active | — | 2 | 126 | — | |
| `optimize_sl_tp.py` | ✅ active | — | 5 | 272 | — | |
| `optimize_sltp_params.py` | ✅ active | — | 4 | 310 | — | |
| `paper_trade_simulator.py` | ✅ active | — | 13 | 783 | — | |
| `phase4_final_audit.py` | ✅ active | — | 7 | 594 | — | |
| `phase4_shadow_review.py` | ✅ active | — | 11 | 419 | — | |
| `position_query.py` | ✅ active | — | 8 | 437 | — | |
| `position_snapshot.py` | ✅ active | — | 3 | 176 | — | |
| `pre_commit_blueprint.py` | ✅ active | — | 5 | 204 | — | |
| `pre_commit_mypy.py` | ✅ active | — | 5 | 168 | — | |
| `probe_xau_signal_generation.py` | ✅ active | — | 7 | 362 | — | |
| `purge_backtest_from_governance.py` | ✅ active | — | 5 | 249 | — | |
| `reconcile_fix_registry.py` | ✅ active | — | 6 | 381 | — | |
| `register_fix.py` | ✅ active | — | 10 | 385 | — | |
| `repair_brain_configs.py` | ✅ active | — | 4 | 141 | — | |
| `restore_btc_schema_41.py` | ✅ active | — | 4 | 177 | — | |
| `run_data_health.py` | ✅ active | — | 3 | 172 | — | |
| `runtime_protection_guard.py` | ✅ active | — | 1 | 22 | — | |
| `scan_barrier_params.py` | ✅ active | — | 5 | 286 | — | |
| `scan_ofi_wasserstein.py` | ✅ active | — | 7 | 297 | — | |
| `send_data_health_alert.py` | ✅ active | — | 6 | 230 | — | |
| `send_live_order.py` | ✅ active | — | 4 | 148 | — | |
| `shadow_decision_recorder.py` | ✅ active | — | 7 | 200 | — | |
| `shadow_live_compare_report.py` | ✅ active | — | 9 | 218 | — | |
| `shadow_pnl_loop.py` | ✅ active | — | 9 | 812 | — | |
| `shadow_rca.py` | ✅ active | — | 12 | 525 | — | |
| `smoke_test_e2e.py` | ✅ active | — | 15 | 381 | — | |
| `system_health.py` | ✅ active | — | 5 | 293 | — | |
| `system_trust_report.py` | ✅ active | — | 20 | 992 | — | |
| `task_a_directional_closure.py` | ✅ active | — | 8 | 437 | — | |
| `task_b_regime_baseline.py` | ✅ active | — | 8 | 419 | — | |
| `test_io_pipeline.py` | ✅ active | — | 3 | 198 | — | |
| `test_meta_pipeline.py` | ✅ active | — | 6 | 295 | — | |
| `tombstone_orphans.py` | ✅ active | — | 2 | 161 | — | |
| `trade_quality_report.py` | ✅ active | — | 6 | 113 | — | |
| `train_btc_metafilter_v2.py` | ✅ active | — | 7 | 343 | — | |
| `train_metafilter_path_b.py` | ✅ active | — | 3 | 223 | — | |
| `train_regime_aware_btc.py` | ✅ active | — | 3 | 256 | — | |
| `train_xau_metafilter.py` | ✅ active | — | 10 | 531 | — | |
| `training_strategy_report.py` | ⬜ empty | — | 0 | 172 | — | |
| `validate_artifacts.py` | ✅ active | — | 4 | 196 | — | |
| `validate_blueprints.py` | ✅ active | — | 8 | 361 | — | |
| `validate_brain_before_deploy.py` | ✅ active | — | 12 | 393 | — | |
| `validate_commit_msg.py` | ✅ active | CheckResult, ValidationReport | 6 | 409 | — | |
| `validate_journal_health_fix.py` | ✅ active | — | 1 | 93 | — | |
| `validate_magic_sync.py` | ✅ active | — | 4 | 178 | — | |
| `verify.py` | ✅ active | — | 18 | 1082 | — | |
| `verify_all_brains.py` | ✅ active | — | 1 | 90 | — | |
| `verify_capresult_ast.py` | ✅ active | Violation, ScanReport, DynamicCallDetector, CapResultOkPlacementDetector, RawAccessDetector, FailOpenGuardDetector, ProofLeakDetector | 31 | 650 | ✅ | |
| `verify_dqaf044_fix_effect.py` | ✅ active | — | 9 | 322 | — | |
| `verify_dqaf_002_fix.py` | ✅ active | — | 3 | 180 | — | |
| `verify_event_stream.py` | ✅ active | — | 3 | 200 | — | |
| `verify_health_check_coverage.py` | ✅ active | — | 2 | 143 | — | |
| `verify_phantom_contracts.py` | ✅ active | Violation, VerificationReport | 7 | 437 | — | |
| `verify_pnl_data_integrity.py` | ✅ active | — | 5 | 302 | — | |
| `verify_training_serving_parity.py` | ✅ active | — | 7 | 525 | — | |
| `watchdog_daily_ops.py` | ✅ active | — | 3 | 171 | — | |

## scripts/archive

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_analyze_btc_brains_20260730.py` | ✅ active | — | 1 | 372 | — | |
| `_analyze_btc_current_positions.py` | ✅ active | — | 2 | 143 | — | |
| `_analyze_btc_current_v2.py` | ✅ active | — | 2 | 75 | — | |
| `_analyze_btc_entry_quality.py` | ✅ active | — | 5 | 423 | — | |
| `_analyze_btc_exits.py` | ✅ active | — | 2 | 358 | — | |
| `_analyze_btc_improvement.py` | ✅ active | — | 1 | 270 | — | |
| `_analyze_console_log.py` | ✅ active | — | 4 | 422 | — | |
| `_analyze_dual_assassin_journal.py` | ✅ active | — | 3 | 360 | — | |
| `_analyze_signal_close_audit.py` | ✅ active | — | 6 | 462 | — | |
| `_analyze_v4_timeline.py` | 📄 config | — | 0 | 281 | — | |
| `_analyze_xau_loss_exits.py` | ✅ active | — | 6 | 419 | — | |
| `_assess_position_sizing.py` | ✅ active | — | 6 | 339 | — | |
| `_audit_btc_brains_full.py` | ✅ active | — | 9 | 502 | — | |
| `_audit_pwin_routing_20260802.py` | ✅ active | — | 1 | 131 | — | |
| `_audit_v4_attribution_20260802.py` | ✅ active | — | 2 | 197 | — | |
| `_audit_xau_brains_20260730.py` | ✅ active | — | 2 | 236 | — | |
| `_btc_audit_20260730_v2.py` | ✅ active | — | 2 | 352 | — | |
| `_calibrate_thresholds.py` | 📄 config | — | 0 | 133 | — | |
| `_check_live_entries_now.py` | ✅ active | — | 6 | 202 | — | |
| `_check_post_restart.py` | ✅ active | — | 3 | 224 | — | |
| `_commit_dqaf_011.py` | ✅ active | — | 2 | 55 | — | |
| `_cross_validate_mt5.py` | ✅ active | — | 4 | 361 | — | |
| `_diagnose_h1_v4_degradation.py` | ✅ active | — | 6 | 254 | — | |
| `_diagnose_pnl_mismatch.py` | 📄 config | — | 0 | 216 | — | |
| `_diagnose_pnl_provenance.py` | 📄 config | — | 0 | 203 | — | |
| `_diagnose_pnl_status.py` | 📄 config | — | 0 | 138 | — | |
| `_list_btc_brains.py` | 📄 config | — | 0 | 27 | — | |
| `_parse_mt5_text.py` | ✅ active | — | 2 | 87 | — | |
| `_validate_btc_july.py` | 📄 config | — | 0 | 197 | — | |
| `_validate_mt5_complete.py` | ✅ active | — | 7 | 536 | — | |
| `_validate_mt5_report.py` | 📄 config | — | 0 | 266 | — | |
| `_verify_expected_r_e2e.py` | ✅ active | — | 1 | 111 | — | |
| `_verify_expected_r_routing.py` | ✅ active | — | 1 | 177 | — | |
| `_verify_post_restart_state.py` | 📄 config | — | 0 | 248 | — | |
| `_verify_xau_final.py` | 📄 config | — | 0 | 452 | — | |
| `_verify_xau_governance_anomalies.py` | ✅ active | — | 2 | 435 | — | |
| `_xau_account_check.py` | ✅ active | — | 3 | 171 | — | |
| `_xau_full_audit.py` | ✅ active | — | 5 | 429 | — | |

## scripts/audit

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `model_inventory.py` | ✅ active | — | 1 | 75 | — | |
| `reference_integrity.py` | ✅ active | — | 2 | 168 | — | |

## scripts/audits

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_audit_xau_votes_today.py` | ✅ active | — | 3 | 148 | — | |

## scripts/backtest

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_dynamic_exit.py` | ✅ active | — | 10 | 586 | — | |
| `backtest_high_recall_precision.py` | ✅ active | — | 5 | 326 | — | |
| `backtest_meta_filter.py` | ✅ active | — | 5 | 318 | — | |
| `backtest_regime_2d.py` | ✅ active | — | 9 | 515 | — | |
| `backtest_v3_combined.py` | ✅ active | — | 21 | 857 | — | |

## scripts/dev

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `fix_project.py` | ✅ active | — | 13 | 466 | — | |

## scripts/features

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `feature_store_warmer.py` | ✅ active | — | 14 | 383 | — | |
| `reconcile_store_schemas.py` | ✅ active | — | 2 | 160 | — | |

## scripts/guards

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `journal_quality.py` | ✅ active | — | 2 | 40 | — | |

## scripts/migration

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `migrate_to_event_stream.py` | ✅ active | — | 2 | 172 | — | |

## scripts/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `augment_h1_directional_features.py` | ✅ active | — | 3 | 470 | — | |
| `batch_train_skeleton.py` | ✅ active | — | 2 | 105 | — | |
| `brain_leaderboard.py` | ✅ active | — | 8 | 304 | — | |
| `brain_promotion_runner.py` | ✅ active | — | 7 | 258 | — | |
| `build_btc_dataset_from_ssot.py` | ✅ active | — | 1 | 221 | — | |
| `build_btc_expected_r_dataset.py` | ✅ active | — | 12 | 799 | — | |
| `build_btc_flow46_dataset.py` | ✅ active | — | 6 | 301 | — | |
| `build_calibrated_dataset.py` | ✅ active | — | 19 | 771 | — | |
| `build_live_labeled_dataset.py` | ✅ active | — | 4 | 251 | — | |
| `build_meta_features.py` | ✅ active | — | 7 | 801 | — | |
| `build_meta_labeling_dataset.py` | ✅ active | — | 8 | 819 | — | |
| `build_meta_labels.py` | ✅ active | — | 6 | 379 | — | |
| `build_meta_learner.py` | ✅ active | — | 7 | 481 | — | |
| `build_micro_barrier_dataset.py` | ✅ active | — | 6 | 344 | — | |
| `build_micro_flat_features.py` | ✅ active | — | 3 | 151 | — | |
| `build_profitable_labels.py` | ✅ active | — | 5 | 611 | — | |
| `build_s1_regression_dataset.py` | ✅ active | — | 3 | 181 | — | |
| `build_swing_enhanced_dataset.py` | ✅ active | — | 8 | 927 | — | |
| `build_v9_micro_dataset.py` | ✅ active | — | 2 | 257 | — | |
| `calibrate_labels.py` | ✅ active | — | 4 | 298 | — | |
| `calibrate_meta_filter.py` | ✅ active | — | 4 | 218 | — | |
| `calibrate_sl_tp.py` | ✅ active | — | 7 | 461 | — | |
| `calibrate_v4_isotonic.py` | ✅ active | — | 4 | 391 | — | |
| `champion_challenger.py` | ✅ active | — | 7 | 307 | — | |
| `crt_manifest.py` | ✅ active | CRTManifestV1 | 9 | 159 | — | |
| `dataset_builder.py` | ✅ active | — | 14 | 569 | — | |
| `dataset_builder_d1.py` | ✅ active | — | 6 | 452 | — | |
| `download_mt5_ohlc.py` | ✅ active | — | 2 | 116 | — | |
| `e2e_pipeline_validation.py` | ✅ active | — | 9 | 538 | — | |
| `eval_alignment.py` | ✅ active | — | 9 | 318 | — | |
| `eval_ensemble_baselines.py` | ✅ active | — | 2 | 147 | — | |
| `eval_regime.py` | ✅ active | — | 8 | 361 | — | |
| `eval_tf_comparison.py` | ✅ active | — | 11 | 253 | — | |
| `export_mt5_data.py` | ✅ active | — | 2 | 143 | — | |
| `generate_batch_plan.py` | ✅ active | — | 5 | 355 | — | |
| `generate_brain_config.py` | ✅ active | — | 8 | 320 | — | |
| `governance_scheduler.py` | ✅ active | — | 9 | 937 | — | |
| `label_builder.py` | ✅ active | — | 19 | 1047 | — | |
| `label_builder_d1.py` | ✅ active | D1BarrierContract | 9 | 591 | — | |
| `monitor_training.py` | ✅ active | — | 18 | 421 | — | |
| `oos_blind_test.py` | ✅ active | OOSBlindError | 4 | 288 | — | |
| `optimize_ensemble_weights.py` | ✅ active | — | 4 | 164 | — | |
| `optimize_meta_threshold.py` | ✅ active | — | 4 | 242 | — | |
| `quality_gate.py` | ✅ active | — | 9 | 318 | — | |
| `reactivate_brains.py` | ✅ active | — | 3 | 232 | — | |
| `recipe_diff.py` | ✅ active | — | 5 | 195 | — | |
| `recipe_search.py` | ✅ active | — | 9 | 518 | — | |
| `register_brain.py` | ✅ active | — | 6 | 171 | — | |
| `retraining_trigger.py` | ✅ active | — | 9 | 515 | — | |
| `run_promotion.py` | ✅ active | — | 6 | 285 | — | |
| `run_train_batch.py` | ✅ active | — | 6 | 273 | — | |
| `scan_profitability_surface.py` | ✅ active | — | 3 | 180 | — | |
| `train.py` | ✅ active | ModelQualityException, PipelineResult | 23 | 2239 | — | |
| `train_btc_binary_directional.py` | ✅ active | — | 5 | 385 | — | |
| `train_btc_directional_v1.py` | ✅ active | — | 7 | 450 | — | |
| `train_btc_directional_v10.py` | ✅ active | — | 6 | 576 | — | |
| `train_btc_expected_r.py` | ✅ active | — | 8 | 634 | — | |
| `train_btc_expected_r_institutional.py` | ✅ active | — | 7 | 545 | — | |
| `train_btc_flow_46_transfer.py` | ✅ active | — | 6 | 527 | — | |
| `train_btc_swing_v9.py` | ✅ active | — | 16 | 1898 | — | |
| `train_daily_swing.py` | ✅ active | — | 10 | 641 | — | |
| `train_exit_metamodel.py` | ✅ active | — | 10 | 567 | — | |
| `train_from_csv.py` | ✅ active | MLP | 10 | 714 | — | |
| `train_meta_filter.py` | ✅ active | — | 5 | 332 | — | |
| `train_meta_model.py` | ✅ active | — | 6 | 384 | — | |
| `train_online_init.py` | ✅ active | — | 9 | 411 | — | |
| `train_stage2_lgb_pit.py` | ✅ active | — | 4 | 187 | — | |
| `train_stage2_mlp_pit.py` | ✅ active | — | 4 | 234 | — | |
| `train_swing_binary_directional.py` | ✅ active | — | 5 | 481 | — | |
| `train_swing_v9.py` | ✅ active | — | 4 | 491 | — | |
| `train_xau_directional_v1.py` | ✅ active | — | 6 | 293 | — | |
| `train_xau_directional_v2.py` | ✅ active | — | 18 | 923 | — | |
| `validate_label_vs_live.py` | ✅ active | LabelLiveMismatchError | 2 | 130 | — | |
| `verify_lineage.py` | ✅ active | — | 10 | 422 | — | |
| `write_manifest_stub.py` | ✅ active | — | 2 | 60 | — | |
| `your_trainer.py` | ✅ active | — | 7 | 223 | — | |

## scripts/training/builders

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `arb.py` | ✅ active | ArbDatasetBuilder | 3 | 59 | — | |
| `base.py` | 🧪 stub | BaseDatasetBuilder | 10 | 164 | — | |
| `microstructure.py` | ✅ active | MicrostructureDatasetBuilder | 4 | 165 | — | |

## scripts/training/trainers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `arb_trainer.py` | ✅ active | — | 5 | 273 | — | |
| `deep_res_mlp_trainer.py` | ✅ active | ResBlock, DeepResMLP, _Block, _Model | 14 | 565 | — | |
| `lgb_trainer.py` | ✅ active | — | 9 | 505 | — | |
| `mtx_trainer.py` | ✅ active | — | 7 | 392 | — | |
| `online_mlp_trainer.py` | ✅ active | — | 6 | 285 | — | |
| `sur_trainer.py` | ✅ active | — | 5 | 312 | — | |
| `transformer_trainer.py` | ✅ active | UpgradedQuantTransformer, _Model | 13 | 784 | — | |
| `xgb_trainer.py` | ✅ active | — | 9 | 624 | — | |

## scripts/tuning

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `tune_btc_kalman_h4.py` | ✅ active | SimpleKalman | 7 | 266 | — | |

## scripts/validators

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `feature_quality_validator.py` | ✅ active | — | 5 | 204 | — | |
| `journal_validator.py` | ✅ active | — | 4 | 166 | — | |

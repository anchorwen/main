# MODULE INVENTORY — 模块清单与完成度

> **自动生成**: 2026-06-11T10:25:25Z
> **扫描模块数**: 517
> **图例**: ✅ active | 🧪 stub | 📄 config | ⬜ empty

## apps/engine

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_runner.py` | ✅ active | BacktestRunner, BacktestResult | 6 | 153 | — | |
| `batch_processor.py` | ✅ active | BatchProcessor | 3 | 77 | — | |
| `bootstrap_v9.py` | ✅ active | — | 6 | 216 | — | |
| `cli.py` | ✅ active | — | 42 | 1542 | — | |
| `communication_ops_cli.py` | ✅ active | — | 7 | 137 | — | |
| `communication_summary_contract.py` | ✅ active | — | 1 | 71 | — | |
| `diagnostics_cli.py` | ✅ active | DiagnosticsCLI | 10 | 129 | — | |
| `main_v9_shadow.py` | ✅ active | FeatureInputError, OutputPlan, StreamEnvelopePlan, SessionStreamPlan, BaselineSuiteSpec, FormalBaselineManifest, ShadowSessionManager | 91 | 2180 | — | |
| `orchestrator.py` | ✅ active | CycleOutcome, DecisionCycleOrchestrator | 6 | 297 | — | |
| `runtime_loop.py` | ✅ active | SimpleFeatureSnapshot, DecisionCycleResult, RuntimeLoop | 3 | 316 | — | |
| `system_facade.py` | ✅ active | SystemFacade, SystemSelfTest | 27 | 236 | — | |
| `v9_shadow_sse.py` | ✅ active | SessionStreamQueryError, SessionStreamResponseStartError, SessionSSEClientBuffer, ShadowSessionSSEHandler | 22 | 315 | — | |
| `v9_shadow_support.py` | ✅ active | StubFeatureService, V9ParliamentAdapter | 2 | 66 | — | |

## apps/monitor

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `live_trading_dashboard.py` | ✅ active | LiveDashboardHandler | 34 | 2476 | — | |

## core

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `constants.py` | 📄 config | — | 0 | 195 | — | |

## core/alpha

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | AlphaLifecycleState, AlphaRecord, AlphaTransitionRecord | 4 | 78 | — | |
| `lifecycle_service.py` | ✅ active | AlphaLifecycleService | 10 | 106 | — | |
| `ou_optimizer.py` | ✅ active | KalmanHalfLifeFilter | 12 | 535 | — | |
| `performance_store.py` | ✅ active | AlphaPerformanceSnapshot, AlphaPerformanceStore | 14 | 212 | — | |
| `portfolio_allocator.py` | ✅ active | AlphaAllocationPolicy, AlphaAllocationRecommendation, AlphaPortfolioAllocator | 8 | 167 | — | |
| `promotion_gate.py` | ✅ active | AlphaPromotionPolicy, AlphaPromotionDecision, AlphaPromotionGate | 16 | 261 | — | |
| `registry.py` | ✅ active | AlphaRegistry | 10 | 85 | — | |
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
| `strategy_adapter.py` | ✅ active | StrategyLineAdapter | 9 | 264 | — | |

## core/brains

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_registry.py` | ✅ active | BrainEntry, BrainRegistry | 16 | 174 | — | |
| `online_mlp_model.py` | ✅ active | OnlineMLP, _TorchOnlineMLP, _Module | 15 | 269 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/brains/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `base_adapter.py` | ✅ active | BaseBrainAdapter | 13 | 240 | — | |
| `lightgbm_brain_adapter.py` | ✅ active | LightGBMBrainAdapter | 6 | 209 | — | |
| `meta_filter_adapter.py` | ✅ active | FeatureParityError, MetaFilterAdapter | 8 | 207 | — | |
| `online_learner_adapter.py` | ✅ active | OnlineLearnerAdapter | 18 | 597 | — | |
| `params_brain_adapter.py` | ✅ active | ParamsBrainAdapter | 9 | 269 | — | |
| `transformer_brain_adapter.py` | ✅ active | TransformerBrainAdapter | 9 | 266 | — | |
| `v9_onnx_brain_adapter.py` | ✅ active | V9OnnxBrainAdapter | 9 | 313 | — | |
| `xgboost_brain_adapter.py` | ✅ active | XGBoostBrainAdapter | 6 | 247 | — | |

## core/brains/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `ab_test.py` | ✅ active | ExperimentConfig, TrafficSplitter, ExperimentResult, ExperimentTracker | 11 | 305 | — | |
| `brain_attribution_service.py` | ✅ active | BrainAttribution, AttributionReport, BrainAttributionService | 11 | 328 | — | |
| `brain_factory.py` | ✅ active | BrainFactory | 1 | 170 | — | |
| `brain_leaderboard.py` | ✅ active | BrainRanking, BrainLeaderboard | 8 | 288 | — | |
| `brain_promotion.py` | ✅ active | BrainPromotionDecision, BrainPromotionThresholds, BrainPromotionEvaluator | 8 | 475 | — | |
| `brain_registry_loader.py` | ✅ active | BrainRegistryLoader | 1 | 7 | — | |
| `brain_registry_service.py` | ✅ active | BrainRegistryService | 7 | 117 | — | |
| `brain_run_service.py` | ✅ active | BrainRunService | 15 | 267 | — | |
| `dynamic_brain_weighter.py` | ✅ active | DynamicBrainWeighter | 13 | 421 | — | |
| `inference_guard.py` | ✅ active | InferenceGuard | 11 | 222 | — | |
| `onnx_worker.py` | ✅ active | — | 1 | 80 | — | |
| `stability_monitor.py` | ✅ active | StabilityReport | 4 | 195 | — | |

## core/config

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `asset_registry.py` | ✅ active | AssetConfig | 2 | 68 | — | |

## core/contracts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `domain_keys.py` | 📄 config | — | 0 | 979 | — | |
| `enums.py` | ✅ active | BrainRole, BrainStatus, DecisionAction, DecisionSide, RiskDecisionStatus, SystemMode, OverrideStatus, CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision, ExecutionEventType, ReconciliationStatus | 0 | 109 | — | |
| `exceptions.py` | ✅ active | DomainError, RiskError, RiskPolicyViolation, GovernanceError, InvalidTransitionError, BrainNotFoundError, ExecutionError, OrderNotFoundError, DuplicateOrderError, ProtocolError, DispatchError, IdempotencyError, ConfigurationError, ContractViolationError | 9 | 136 | — | |
| `ids.py` | ✅ active | — | 14 | 57 | — | |
| `position_events.py` | ✅ active | PositionClosed, PositionOpened | 2 | 140 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `strategy_magic.py` | 📄 config | — | 0 | 32 | — | |
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
| `label_contract.py` | ✅ active | BarrierResult, LabelContract | 11 | 549 | — | |
| `training_contract.py` | ✅ active | DatasetSpec, LabelSpec, ArchitectureSpec, ValidationSpec, QualityGateSpec, OutputSpec, TrainingContract | 12 | 474 | — | |
| `training_recipe.py` | ✅ active | ModelIdentity, LabelContractRef, DataAugmentation, DataConfig, TrainingConfig, EvaluationConfig, TrainingRecipe | 6 | 386 | — | |

## core/deployment

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `atomic_file_writer.py` | ✅ active | AtomicFileError, AtomicFileWriter | 12 | 130 | — | |
| `blue_green.py` | ✅ active | SlotState, SlotColor, DeploymentSlot, DeploymentTopology, CutoverResult, HealthProbe, BlueGreenManager | 26 | 526 | — | |
| `brain_alert.py` | ✅ active | — | 2 | 42 | — | |
| `brain_config_validator.py` | ✅ active | BrainConfigError, ValidationResult, BrainConfigValidator | 13 | 226 | — | |
| `brain_lifecycle_manager.py` | ✅ active | RetirementReport, RegistrationReport, IntegrityReport, ReferenceAuditReport, BrainLifecycleManager | 19 | 1284 | — | |
| `brain_registration_gate.py` | ✅ active | GateResult, BrainRegistrationGate | 18 | 370 | — | |
| `capability_registry.py` | ✅ active | CapabilitySpec, CapabilityRegistry | 5 | 112 | — | |
| `compliance_audit.py` | ✅ active | ComplianceAuditService | 9 | 567 | — | |
| `compliance_control_matrix.py` | ✅ active | ComplianceControlMatrixService | 11 | 424 | — | |
| `compliance_export.py` | ✅ active | TradeRecord, ComplianceReport | 9 | 368 | — | |
| `config_hot_reload.py` | ✅ active | ConfigHotReload | 7 | 129 | — | |
| `deployment_executor.py` | ✅ active | DeploymentExecutor | 8 | 329 | — | |
| `deployment_plan.py` | ✅ active | DeploymentPlanService | 7 | 300 | — | |
| `domain_keys.py` | ⬜ empty | — | 0 | 7 | — | |
| `environment_config.py` | ✅ active | Environment, EnvironmentConfig | 7 | 126 | — | |
| `evidence_bundle.py` | ✅ active | EvidenceBundleService | 10 | 287 | — | |
| `feature_update_producer.py` | ✅ active | — | 2 | 57 | — | |
| `final_audit.py` | ✅ active | FinalAuditService | 5 | 211 | — | |
| `governance_summary.py` | ✅ active | — | 4 | 56 | — | |
| `health_check.py` | ✅ active | HealthCheckService | 9 | 117 | — | |
| `lifecycle_manager.py` | ✅ active | LifecycleManager | 7 | 167 | — | |
| `operational_support.py` | ✅ active | RetryPolicy, ConfigValidator | 7 | 130 | — | |
| `operations_timeline.py` | ✅ active | OperationsTimelineService | 16 | 262 | — | |
| `ops_maturity.py` | ✅ active | OpsMaturityService | 4 | 164 | — | |
| `path_defaults.py` | ✅ active | — | 2 | 85 | — | |
| `permission_audit.py` | ✅ active | AuditEntry, PermissionMatrix, AuditTrail | 20 | 334 | — | |
| `postmortem_report.py` | ✅ active | PostmortemReportService | 11 | 468 | — | |
| `release_certification.py` | ✅ active | ReleaseCertificationService | 12 | 293 | — | |
| `release_gate.py` | ✅ active | ReleaseGateService | 16 | 320 | — | |
| `release_pipeline.py` | ✅ active | ReleasePipelineService | 7 | 378 | — | |
| `release_readiness.py` | ✅ active | ReleaseReadinessService | 13 | 437 | — | |
| `release_registry.py` | ✅ active | ReleaseRegistryService | 17 | 351 | — | |
| `replay_isolation.py` | ✅ active | ReplayDispatchAdapter, NullDispatchAdapter, ReplayEnvironment | 11 | 138 | — | |
| `rollback_drill.py` | ✅ active | RollbackDrillService | 8 | 304 | — | |
| `runbook_engine.py` | ✅ active | RunbookEngine | 16 | 664 | — | |
| `scheduled_task_registry.py` | ✅ active | — | 4 | 36 | — | |
| `scheduler_service.py` | ✅ active | ScheduledTask, SchedulerService | 19 | 450 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 29 | — | |
| `service_container.py` | ✅ active | ServiceContainer | 41 | 588 | — | |
| `startup_validator.py` | ✅ active | — | 1 | 114 | — | |
| `state_persistence.py` | ✅ active | StatePersistence | 6 | 100 | — | |
| `validation_mode.py` | ✅ active | — | 1 | 10 | — | |

## core/execution

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `barrier_strategy.py` | ✅ active | BarrierStrategy | 2 | 135 | — | |
| `broker_adapter.py` | ✅ active | BrokerAdapter | 9 | 68 | — | |
| `capital_allocator.py` | ✅ active | AllocationDecision, GroupCorrelationTracker, CapitalAllocator | 9 | 468 | — | |
| `conformal_calibrator.py` | ✅ active | ConformalCalibrator | 10 | 399 | — | |
| `conformal_ou_gate.py` | ✅ active | ConformalOUGate | 16 | 635 | — | |
| `correlation_sizer.py` | ✅ active | ClusterResult | 1 | 109 | — | |
| `dynamic_sl_tp.py` | ✅ active | StrategyFamily, DynamicSLTP | 3 | 244 | — | |
| `execution_manager.py` | ✅ active | ExecutionManager | 7 | 180 | — | |
| `execution_queue.py` | ✅ active | ExecutionQueueFatalError, QueuedDecision, DispatchResult, ExecutionQueue | 5 | 445 | — | |
| `exit_watchdog.py` | ✅ active | ExitAttempt, ExitWatchdogResult, ExitWatchdog | 8 | 485 | — | |
| `fill_simulator.py` | ✅ active | FillSimulationConfig, FillSimulator | 8 | 125 | — | |
| `fix_contracts.py` | ✅ active | FixSessionConfig, FixMessage, FixExecutionReport | 4 | 69 | — | |
| `fix_execution_mapper.py` | ✅ active | FixExecutionReportMapper | 5 | 75 | — | |
| `fix_gateway_adapter.py` | ✅ active | FixGatewayAdapter | 12 | 136 | — | |
| `fix_message_builder.py` | ✅ active | FixMessageBuilder | 5 | 57 | — | |
| `gateway_contracts.py` | ✅ active | OrderRequest, Fill, OrderState, ExecutionGateway | 9 | 103 | — | |
| `kelly_sizer.py` | ✅ active | KellyResult | 2 | 113 | — | |
| `limit_order_monitor.py` | ✅ active | LimitOrderIntent, LimitFillResult, LimitOrderMonitor | 9 | 329 | — | |
| `live_order_sender.py` | ✅ active | — | 6 | 325 | — | |
| `managed_close.py` | ✅ active | — | 2 | 345 | — | |
| `market_efficiency.py` | ✅ active | — | 2 | 67 | — | |
| `market_impact.py` | ✅ active | MarketImpactEstimate | 3 | 168 | — | |
| `meta_exit_engine.py` | ✅ active | ExitFeatureSnapshot, ExitEvaluation, MetaExitEngine | 13 | 509 | — | |
| `meta_filter_gate.py` | ✅ active | MetaFilterGate | 7 | 220 | — | |
| `meta_filter_routing.py` | ✅ active | — | 1 | 230 | — | |
| `meta_pipeline.py` | ✅ active | MetaProbeSpec, MetaProbeResult, MetaPipeline | 8 | 490 | — | |
| `meta_signal_filter.py` | ✅ active | FilterResult, MetaSignalFilter | 19 | 944 | — | |
| `micro_strategy.py` | ✅ active | MicroStrategy | 1 | 86 | — | |
| `mt5_broker_adapter.py` | ✅ active | MT5BrokerAdapter | 12 | 159 | — | |
| `mt5_worker.py` | ✅ active | MT5Worker | 20 | 405 | — | |
| `net_out_close_handler.py` | ✅ active | — | 1 | 181 | — | |
| `order_state_machine.py` | ✅ active | OrderStateMachine | 9 | 101 | — | |
| `paper_gateway.py` | ✅ active | PaperExecutionGateway | 10 | 153 | — | |
| `portfolio_risk.py` | ✅ active | RiskVerdict, RiskResult, PortfolioState, PortfolioRiskController | 14 | 529 | — | |
| `position_manager.py` | ✅ active | ActivePosition, ActivePositionManager | 53 | 1905 | — | |
| `pre_trade_guards.py` | ✅ active | IntradayDrawdownKill, CooldownRegistry, FamilyEntryTracker | 27 | 882 | — | |
| `pwin_chain.py` | ✅ active | — | 3 | 183 | — | |
| `quality_analyzer.py` | ✅ active | SlippageTracker, ExecutionQualityAnalyzer | 15 | 351 | — | |
| `quality_contracts.py` | ✅ active | ExecutionBenchmark, ExecutionQualityMetric, ImplementationShortfall, ExecutionQualityReport | 3 | 138 | — | |
| `reentry_guard.py` | ✅ active | ExitRecord, ReentryState | 7 | 550 | — | |
| `regime_gate.py` | ✅ active | RegimeModulation, OURegime2D, RegimeGate | 42 | 841 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `statarb_strategy.py` | ✅ active | StatArbStrategy | 1 | 76 | — | |
| `strategy_budget.py` | ✅ active | StrategyBudget | 11 | 318 | — | |
| `strategy_line.py` | 🧪 stub | StrategyDecision, StrategyLineConfig, StrategyLine | 18 | 2054 | — | |
| `strategy_type.py` | ✅ active | StrategyType | 0 | 30 | — | |
| `swing_strategy.py` | ✅ active | SwingStrategy | 1 | 130 | — | |
| `trail_stop_engine.py` | ✅ active | TrailPolicy, TrailStopEngine | 8 | 313 | — | |
| `trend_detector.py` | ✅ active | KalmanTrendFilter, TrendDetector | 33 | 699 | — | |
| `trend_isolation_gates.py` | ✅ active | — | 1 | 196 | — | |

## core/features

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `data_augmentation.py` | ✅ active | — | 4 | 141 | — | |
| `feature_assembler.py` | ✅ active | — | 3 | 233 | — | |
| `feature_service.py` | ✅ active | FeatureService, FeatureBrainRegistry, IntentExplainer | 13 | 365 | — | |
| `feature_snapshot.py` | ✅ active | StoredFeatureSnapshot | 2 | 33 | — | |
| `local_feature_store.py` | ✅ active | LocalFeatureStore | 18 | 267 | — | |
| `meta_feature_builder.py` | ✅ active | — | 1 | 136 | — | |
| `rolling_normalizer.py` | ✅ active | RollingNormalizer | 15 | 234 | — | |
| `store_contracts.py` | ✅ active | FeatureSchema, FeatureRecord, FeatureQuery, FeatureStore | 8 | 87 | — | |
| `update_job.py` | ✅ active | FeatureUpdateResult, IncrementalFeatureUpdateJob | 3 | 63 | — | |

## core/features/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `microstructure_feature_adapter.py` | ✅ active | MicrostructureFeatureAdapter | 9 | 107 | — | |
| `v9_feature_adapter.py` | ✅ active | V9FeatureAdapter | 6 | 103 | — | |

## core/features/computers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `btc_feature_augmenter.py` | ✅ active | BTCFeatureAugmenter | 5 | 359 | — | |
| `daily_computer.py` | ✅ active | DailyFeatureComputer | 22 | 726 | — | |
| `live_daily_provider.py` | ✅ active | LiveDailyFeatureProvider | 8 | 225 | — | |
| `microstructure_computer.py` | ✅ active | MicrostructureFeatureComputer | 20 | 552 | — | |
| `v9_live_computer.py` | ✅ active | V9LiveFeatureComputer | 15 | 340 | — | |
| `v9_micro_computer.py` | ✅ active | V9MicroComputer | 3 | 98 | — | |

## core/features/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `btc_macro_enhanced_schema.py` | 📄 config | — | 0 | 89 | — | |
| `daily_swing_schema.py` | 📄 config | — | 0 | 44 | — | |
| `microstructure_schema.py` | ✅ active | — | 1 | 32 | — | |
| `registry.py` | ✅ active | — | 4 | 209 | — | |
| `swing_enhanced_schema.py` | 📄 config | — | 0 | 44 | — | |
| `v9_institutional_schema.py` | 📄 config | — | 0 | 42 | — | |
| `v9_micro_schema.py` | ✅ active | — | 1 | 27 | — | |

## core/feedback

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_performance_tracker.py` | ✅ active | BrainPerformanceTracker | 9 | 137 | — | |
| `brain_pnl_ledger.py` | ✅ active | BrainPnLMetrics, BrainPnLStore | 24 | 816 | — | |
| `brain_quality_engine.py` | ✅ active | BrainQualityVerdict, BrainQualityEngine | 13 | 432 | — | |
| `decision_scorer.py` | ✅ active | DecisionScorer | 5 | 120 | — | |
| `experience_replay.py` | ✅ active | ExperienceReplayBuffer | 10 | 246 | — | |
| `feedback_loop.py` | ✅ active | FeedbackLoop | 4 | 110 | — | |
| `online_feedback_hook.py` | ✅ active | OnlineFeedbackHook | 10 | 438 | — | |
| `outcome_collector.py` | ✅ active | OutcomeCollector | 4 | 111 | — | |
| `param_optimizer.py` | ✅ active | — | 5 | 283 | — | |
| `performance_analytics.py` | ✅ active | PerformanceAnalytics | 12 | 194 | — | |

## core/governance

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_rule_engine.py` | ✅ active | GovernanceRule, GovernanceRuleEngine | 15 | 349 | — | |
| `governance_service.py` | ✅ active | GovernanceService | 17 | 266 | — | |
| `shadow_tracker.py` | ✅ active | ShadowBrainMetrics, ShadowTracker | 9 | 132 | — | |

## core/infrastructure

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `distributed_lock.py` | 🧪 stub | LockAcquireResult, BaseLock, FileLock, DirectoryLock | 23 | 400 | — | |

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
| `journal_cleanup.py` | ✅ active | — | 9 | 638 | — | |
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
| `calendar.py` | ✅ active | — | 4 | 153 | — | |
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
| `alert_channels.py` | ✅ active | SlackAlertChannel, DingTalkAlertChannel, CompositeAlertChannel | 10 | 300 | — | |
| `alert_runbook_bridge.py` | ✅ active | RunbookAction, RunbookSOP, AlertRunbookBridge | 10 | 502 | — | |
| `alert_service.py` | 🧪 stub | AlertRule, AlertChannel, LogAlertChannel, InMemoryAlertChannel, BatchingAlertChannel, SeverityRouter, AlertService | 26 | 467 | — | |
| `audit_log.py` | ✅ active | StructuredAuditLog | 11 | 180 | — | |
| `data_health_schema.py` | ✅ active | Tier, SourceStatus, SourceCheckResult, CrossCheckResult, OrphanFinding, BehavioralMetrics, HealthReport, HealthCheckMeta, SourceHealthRecord | 5 | 262 | — | |
| `data_health_service.py` | ✅ active | DataHealthService | 50 | 2690 | — | |
| `diagnostics_dashboard.py` | ✅ active | DiagnosticsDashboard | 7 | 149 | — | |
| `event_bus.py` | ✅ active | EventBus | 7 | 63 | — | |
| `live_alert_hub.py` | ✅ active | BackgroundDeliveryWorker, LiveAlertHub, _QueueChannel, _AlertAuditLog | 23 | 539 | — | |
| `message_broker.py` | ✅ active | Message, MessageBroker, InProcessBroker, RedisStreamsBroker | 23 | 277 | — | |
| `metric_names.py` | ✅ active | — | 2 | 56 | — | |
| `metrics_collector.py` | ✅ active | MetricsCollector | 10 | 96 | — | |
| `mlflow_bridge.py` | ✅ active | — | 4 | 144 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `slo_service.py` | ✅ active | SloService | 9 | 191 | — | |
| `tracing.py` | ✅ active | Span, TracingContext | 18 | 128 | — | |

## core/parliament

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contract_groups.py` | ✅ active | ContractGroupConsensus, ABGroupRouter | 15 | 773 | — | |
| `group_consensus.py` | ✅ active | — | 1 | 165 | — | |
| `parliament_service.py` | ✅ active | ParliamentService | 11 | 312 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/protocol

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `event_bar_sync.py` | ✅ active | BarSyncState, BarSyncPoller | 13 | 704 | — | |
| `live_execution_contract.py` | ✅ active | — | 5 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 8 | — | |

## core/protocol/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `communication_adapter.py` | ✅ active | CommunicationAdapter | 1 | 7 | — | |
| `communication_adapter_registry.py` | ✅ active | CommunicationAdapterRegistry | 3 | 61 | — | |
| `communication_dispatcher.py` | ✅ active | CommunicationDispatcher | 4 | 309 | — | |
| `decision_compiler.py` | ✅ active | DecisionCompiler | 5 | 118 | — | |
| `file_queue_communication_adapter.py` | ✅ active | FileQueueCommunicationAdapter | 2 | 55 | — | |
| `file_queue_receipt_reader.py` | ✅ active | FileQueueReceiptReader | 5 | 28 | — | |
| `fix_communication_adapter.py` | ✅ active | FixCommunicationAdapter | 6 | 117 | — | |
| `idempotency.py` | ✅ active | IdempotencyStore, DuplicateDetector | 10 | 111 | — | |
| `intent_message_builder.py` | ✅ active | IntentMessageBuilder | 3 | 60 | — | |
| `mt5_communication_adapter.py` | ✅ active | MT5CommunicationAdapter | 2 | 81 | — | |
| `override_resolver.py` | ✅ active | OverrideResolver | 1 | 22 | — | |
| `resilience.py` | ✅ active | CircuitState, CircuitBreaker, RateLimiter | 13 | 153 | — | |
| `stub_communication_adapter.py` | ✅ active | StubCommunicationAdapter | 2 | 23 | — | |
| `venue_router.py` | 🧪 stub | VenueAdapter, StubVenueAdapter, VenueRouter | 14 | 116 | — | |

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
| `cycle_replay.py` | ✅ active | RuntimeReplayReport, RuntimeCycleReplay | 4 | 124 | — | |
| `daily_ops_scheduler.py` | ✅ active | — | 3 | 224 | — | |
| `data_health_monitor.py` | ✅ active | — | 1 | 69 | — | |
| `evidence_contracts.py` | ✅ active | RuntimeEvidenceRecord | 2 | 61 | — | |
| `evidence_reader.py` | ✅ active | RuntimeEvidenceReader | 5 | 42 | — | |
| `evidence_writer.py` | ✅ active | RuntimeEvidenceWriter | 2 | 30 | — | |
| `execution_gates.py` | ✅ active | RuntimeRiskGate, RuntimeGovernanceGate, RuntimeExecutionApprovalChain | 6 | 107 | — | |
| `execution_gateway_router.py` | ✅ active | ExecutionGatewayRouter | 5 | 30 | — | |
| `execution_pipeline.py` | ✅ active | RuntimeExecutionPipeline | 4 | 106 | — | |
| `execution_state.py` | ✅ active | — | 4 | 219 | — | |
| `fault_handler.py` | ✅ active | FaultLevel, FaultTolerantContext | 11 | 409 | — | |
| `gate_audit_recorder.py` | ✅ active | — | 1 | 61 | — | |
| `golden_master.py` | ✅ active | — | 8 | 249 | — | |
| `integration_contracts.py` | ✅ active | OrderSizingPolicy, RuntimePipelineResult | 2 | 52 | — | |
| `legacy_dispatch_reference.py` | ⬜ empty | — | 0 | 84 | — | |
| `live_cycle.py` | ✅ active | LiveCycleConfig, LiveCycleState | 26 | 5688 | — | |
| `live_startup.py` | ✅ active | — | 10 | 343 | — | |
| `market_ingress.py` | ✅ active | — | 5 | 224 | — | |
| `order_dispatch.py` | ✅ active | _MinimalControlSnapshot | 10 | 298 | — | |
| `position_close_adapter.py` | ✅ active | PositionCloseAdapter | 12 | 517 | — | |
| `position_registration.py` | ✅ active | — | 1 | 270 | — | |
| `reconciliation.py` | ✅ active | — | 2 | 243 | — | |
| `restart_state.py` | ✅ active | — | 1 | 349 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 16 | — | |
| `shadow_recorder.py` | ✅ active | — | 8 | 313 | — | |
| `signal_health.py` | ✅ active | GateResult, FeatureGate, _RollingStats, SignalHealthMonitor | 23 | 507 | — | |
| `signal_order_builder.py` | ✅ active | SignalOrderRequestBuilder | 3 | 52 | — | |
| `signal_pipeline.py` | ✅ active | — | 2 | 108 | — | |
| `strategy_builder.py` | ✅ active | — | 6 | 798 | — | |
| `strategy_evaluator.py` | ✅ active | — | 2 | 508 | — | |
| `summary_service.py` | ✅ active | RuntimeSummaryService | 11 | 143 | — | |
| `trail_dispatch.py` | ✅ active | — | 1 | 245 | — | |

## core/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `trading_contracts.py` | ✅ active | BrainSignal, ConsensusResult, StrategyDecision, DegradedResult | 0 | 135 | — | |

## core/simulation

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `spread_model.py` | ✅ active | SpreadModel | 8 | 160 | — | |

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
| `system_mode_store.py` | ✅ active | SystemModeStore | 6 | 150 | — | |

## core/strategies

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contracts.py` | ✅ active | StrategyMetadata, RequiredFeature, Signal, StrategyHealth, AlphaAgent | 9 | 102 | — | |
| `examples.py` | ✅ active | ThresholdAlphaAgent | 8 | 86 | — | |
| `registry.py` | ✅ active | StrategyPluginRegistry, StrategyPluginRunner | 11 | 74 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |

## core/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `checkpoint.py` | ✅ active | CheckpointInfo, CheckpointManager | 13 | 199 | — | |
| `cpcv.py` | ✅ active | CPCVFold, CPCVResult | 9 | 252 | — | |
| `custom_objectives.py` | ✅ active | — | 11 | 338 | — | |
| `dataset.py` | ✅ active | TrainingDataset | 17 | 429 | — | |
| `evaluation_report.py` | ✅ active | SHAPReport, TrainingEvalReport | 11 | 456 | — | |
| `experiment_tracker.py` | ✅ active | RunInfo, ExperimentTracker | 13 | 265 | — | |
| `model_card.py` | ✅ active | ModelCard, ModelCardGenerator | 6 | 225 | — | |
| `model_hashing.py` | ✅ active | — | 3 | 45 | — | |
| `profitability_calibrator.py` | ✅ active | BarrierConfig, ProfitabilityPoint, ProfitabilitySurface | 8 | 385 | — | |
| `registries.py` | ✅ active | — | 32 | 287 | — | |
| `trainer_protocol.py` | ✅ active | TrainResult, TrainerProtocol | 5 | 110 | — | |
| `training_registry.py` | ✅ active | Base, TrainingRunRecord, TrainingRegistry | 16 | 295 | — | |

## scripts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_diag_cycle_stall.py` | ✅ active | — | 4 | 119 | — | |
| `_fix_unused_ignores_v2.py` | 📄 config | — | 0 | 55 | — | |
| `_test_xgb_names.py` | 📄 config | — | 0 | 81 | — | |
| `analyze_deps.py` | ✅ active | — | 6 | 262 | — | |
| `analyze_dual_symbol_trades.py` | ✅ active | — | 4 | 275 | — | |
| `analyze_live_journal.py` | ✅ active | — | 6 | 517 | — | |
| `analyze_recent_losses.py` | ✅ active | — | 1 | 315 | — | |
| `analyze_trail_impact.py` | ✅ active | — | 8 | 423 | — | |
| `analyze_xau_recent_entries.py` | ✅ active | — | 9 | 456 | — | |
| `audit_2day.py` | ✅ active | — | 1 | 204 | — | |
| `audit_behavior_compliance.py` | ✅ active | — | 5 | 415 | — | |
| `audit_btc_cross_validate.py` | ✅ active | — | 4 | 285 | — | |
| `audit_data_exhaustive.py` | ✅ active | — | 7 | 455 | — | |
| `audit_data_final.py` | ✅ active | — | 5 | 199 | — | |
| `audit_data_health.py` | ✅ active | — | 2 | 128 | — | |
| `audit_data_module.py` | ✅ active | — | 3 | 356 | — | |
| `audit_deep_fullstack.py` | ✅ active | — | 4 | 432 | — | |
| `audit_live_health.py` | ✅ active | — | 3 | 198 | — | |
| `audit_phase_c_fix5.py` | ✅ active | — | 7 | 327 | — | |
| `audit_pnl_ledger_integrity.py` | ✅ active | — | 3 | 361 | — | |
| `audit_trade_quality.py` | ✅ active | — | 5 | 271 | — | |
| `audit_xau_exits.py` | ✅ active | — | 2 | 148 | — | |
| `backtest_runner.py` | ✅ active | — | 3 | 274 | — | |
| `brain.py` | ✅ active | — | 12 | 734 | — | |
| `bridge_supervisor.py` | ✅ active | — | 4 | 122 | — | |
| `build_btc_metafilter_v2_dataset.py` | ✅ active | — | 7 | 309 | — | |
| `check_blueprint_compliance.py` | ✅ active | — | 10 | 587 | — | |
| `check_preconditions.py` | ✅ active | — | 12 | 429 | — | |
| `ci_prepare_v9_shadow_fixtures.py` | ✅ active | — | 2 | 180 | — | |
| `classify_ble001.py` | ✅ active | — | 1 | 63 | — | |
| `daily_cost_report.py` | ✅ active | — | 4 | 175 | — | |
| `daily_ops.py` | ✅ active | — | 29 | 1499 | — | |
| `deploy_blue_green.py` | ✅ active | — | 7 | 126 | — | |
| `diagnose_feature_drift.py` | ✅ active | — | 4 | 229 | — | |
| `dqaf_collect.py` | ✅ active | — | 12 | 565 | — | |
| `feature_store_maintenance.py` | ✅ active | — | 8 | 276 | — | |
| `feedback_loop.py` | ✅ active | — | 12 | 426 | — | |
| `hook_blueprint_precheck.py` | ✅ active | — | 1 | 65 | — | |
| `hook_mypy_check.py` | ✅ active | — | 1 | 94 | — | |
| `ingest_live_journal_to_alpha.py` | ✅ active | — | 4 | 96 | — | |
| `journal_freeze_gate.py` | ✅ active | — | 4 | 152 | — | |
| `live_auto_healthcheck.py` | ✅ active | — | 11 | 233 | — | |
| `live_daily_recap.py` | ✅ active | — | 25 | 942 | — | |
| `live_dashboard.py` | ✅ active | — | 16 | 542 | — | |
| `live_data_quality_report.py` | ✅ active | — | 13 | 369 | — | |
| `live_dispatch_policy.py` | ✅ active | — | 10 | 316 | — | |
| `live_feature_quality_report.py` | ✅ active | — | 6 | 212 | — | |
| `live_intent_loop.py` | ✅ active | — | 6 | 2475 | — | |
| `live_launcher.py` | ✅ active | — | 13 | 831 | — | |
| `live_micro_rollout_gate.py` | ✅ active | — | 5 | 138 | — | |
| `live_monitor.py` | ✅ active | — | 12 | 484 | — | |
| `live_read_only_preflight.py` | ✅ active | — | 5 | 145 | — | |
| `live_shadow_ensemble.py` | ✅ active | — | 10 | 393 | — | |
| `live_shadow_intent_producer.py` | ✅ active | — | 7 | 262 | — | |
| `live_stack_diagnostic.py` | ✅ active | — | 5 | 204 | — | |
| `market_calendar.py` | ⬜ empty | — | 0 | 13 | — | |
| `mt5_bridge_healthcheck.py` | ✅ active | — | 6 | 153 | — | |
| `mt5_bridge_worker.py` | ✅ active | — | 23 | 895 | — | |
| `mt5_positions_snapshot.py` | ✅ active | — | 4 | 97 | — | |
| `mt5_spread_probe.py` | ✅ active | — | 1 | 65 | — | |
| `online_feedback_hook.py` | ✅ active | — | 2 | 126 | — | |
| `optimize_sl_tp.py` | ✅ active | — | 5 | 272 | — | |
| `paper_trade_simulator.py` | ✅ active | — | 13 | 783 | — | |
| `position_query.py` | ✅ active | — | 6 | 194 | — | |
| `position_snapshot.py` | ✅ active | — | 3 | 176 | — | |
| `pre_commit_mypy.py` | ✅ active | — | 5 | 162 | — | |
| `register_fix.py` | ✅ active | — | 8 | 329 | — | |
| `repair_brain_configs.py` | ✅ active | — | 4 | 141 | — | |
| `run_data_health.py` | ✅ active | — | 3 | 172 | — | |
| `runtime_protection_guard.py` | ✅ active | — | 1 | 22 | — | |
| `send_data_health_alert.py` | ✅ active | — | 6 | 218 | — | |
| `send_live_order.py` | ✅ active | — | 4 | 149 | — | |
| `shadow_decision_recorder.py` | ✅ active | — | 7 | 199 | — | |
| `shadow_live_compare_report.py` | ✅ active | — | 9 | 218 | — | |
| `shadow_pnl_loop.py` | ✅ active | — | 9 | 762 | — | |
| `smoke_test_e2e.py` | ✅ active | — | 15 | 381 | — | |
| `test_io_pipeline.py` | ✅ active | — | 3 | 198 | — | |
| `test_meta_pipeline.py` | ✅ active | — | 6 | 295 | — | |
| `trade_quality_report.py` | ✅ active | — | 6 | 113 | — | |
| `train_btc_metafilter_v2.py` | ✅ active | — | 6 | 247 | — | |
| `validate_artifacts.py` | ✅ active | — | 4 | 196 | — | |
| `validate_blueprints.py` | ✅ active | — | 7 | 292 | — | |
| `validate_brain_before_deploy.py` | ✅ active | — | 12 | 394 | — | |
| `verify.py` | ✅ active | — | 13 | 711 | — | |
| `verify_all_brains.py` | ✅ active | — | 1 | 91 | — | |
| `verify_dqaf_002_fix.py` | ✅ active | — | 3 | 180 | — | |

## scripts/audit

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `model_inventory.py` | ✅ active | — | 1 | 76 | — | |
| `reference_integrity.py` | ✅ active | — | 2 | 168 | — | |

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
| `feature_store_warmer.py` | ✅ active | — | 14 | 385 | — | |

## scripts/guards

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `journal_quality.py` | ✅ active | — | 2 | 40 | — | |

## scripts/training

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `batch_train_skeleton.py` | ✅ active | — | 2 | 105 | — | |
| `brain_leaderboard.py` | ✅ active | — | 8 | 260 | — | |
| `brain_promotion_runner.py` | ✅ active | — | 6 | 202 | — | |
| `build_calibrated_dataset.py` | ✅ active | — | 19 | 772 | — | |
| `build_live_labeled_dataset.py` | ✅ active | — | 4 | 236 | — | |
| `build_meta_features.py` | ✅ active | — | 7 | 801 | — | |
| `build_meta_labeling_dataset.py` | ✅ active | — | 8 | 819 | — | |
| `build_meta_labels.py` | ✅ active | — | 6 | 379 | — | |
| `build_meta_learner.py` | ✅ active | — | 7 | 481 | — | |
| `build_micro_barrier_dataset.py` | ✅ active | — | 6 | 344 | — | |
| `build_micro_flat_features.py` | ✅ active | — | 3 | 151 | — | |
| `build_profitable_labels.py` | ✅ active | — | 5 | 452 | — | |
| `build_s1_regression_dataset.py` | ✅ active | — | 3 | 181 | — | |
| `build_swing_enhanced_dataset.py` | ✅ active | — | 8 | 928 | — | |
| `build_v9_micro_dataset.py` | ✅ active | — | 2 | 257 | — | |
| `calibrate_labels.py` | ✅ active | — | 4 | 298 | — | |
| `calibrate_meta_filter.py` | ✅ active | — | 4 | 218 | — | |
| `calibrate_sl_tp.py` | ✅ active | — | 7 | 461 | — | |
| `champion_challenger.py` | ✅ active | — | 7 | 307 | — | |
| `crt_manifest.py` | ✅ active | CRTManifestV1 | 9 | 159 | — | |
| `dataset_builder.py` | ✅ active | — | 14 | 569 | — | |
| `dataset_builder_d1.py` | ✅ active | — | 6 | 452 | — | |
| `download_mt5_ohlc.py` | ✅ active | — | 2 | 114 | — | |
| `e2e_pipeline_validation.py` | ✅ active | — | 9 | 539 | — | |
| `eval_alignment.py` | ✅ active | — | 9 | 318 | — | |
| `eval_ensemble_baselines.py` | ✅ active | — | 2 | 147 | — | |
| `eval_regime.py` | ✅ active | — | 8 | 361 | — | |
| `eval_tf_comparison.py` | ✅ active | — | 11 | 253 | — | |
| `export_mt5_data.py` | ✅ active | — | 2 | 143 | — | |
| `generate_batch_plan.py` | ✅ active | — | 5 | 355 | — | |
| `generate_brain_config.py` | ✅ active | — | 8 | 320 | — | |
| `governance_scheduler.py` | ✅ active | — | 5 | 381 | — | |
| `institutional_train.py` | ✅ active | TrainResult | 21 | 1200 | — | |
| `label_builder.py` | ✅ active | — | 14 | 698 | — | |
| `label_builder_d1.py` | ✅ active | D1BarrierContract | 9 | 591 | — | |
| `monitor_training.py` | ✅ active | — | 18 | 421 | — | |
| `optimize_ensemble_weights.py` | ✅ active | — | 4 | 164 | — | |
| `optimize_meta_threshold.py` | ✅ active | — | 4 | 242 | — | |
| `quality_gate.py` | ✅ active | — | 9 | 318 | — | |
| `reactivate_brains.py` | ✅ active | — | 4 | 234 | — | |
| `recipe_diff.py` | ✅ active | — | 5 | 195 | — | |
| `recipe_search.py` | ✅ active | — | 9 | 518 | — | |
| `register_brain.py` | ✅ active | — | 6 | 171 | — | |
| `retraining_trigger.py` | ✅ active | — | 9 | 474 | — | |
| `run_promotion.py` | ✅ active | — | 6 | 284 | — | |
| `run_train_batch.py` | ✅ active | — | 6 | 274 | — | |
| `scan_profitability_surface.py` | ✅ active | — | 3 | 180 | — | |
| `train.py` | ✅ active | ModelQualityException, PipelineResult | 24 | 2009 | — | |
| `train_btc_directional_v1.py` | ✅ active | — | 7 | 434 | — | |
| `train_btc_directional_v10.py` | ✅ active | — | 6 | 569 | — | |
| `train_btc_swing_v9.py` | ✅ active | — | 18 | 963 | — | |
| `train_daily_swing.py` | ✅ active | — | 10 | 640 | — | |
| `train_exit_metamodel.py` | ✅ active | — | 7 | 343 | — | |
| `train_from_csv.py` | ✅ active | MLP | 10 | 725 | — | |
| `train_meta_filter.py` | ✅ active | — | 5 | 332 | — | |
| `train_meta_model.py` | ✅ active | — | 6 | 384 | — | |
| `train_online_init.py` | ✅ active | — | 9 | 411 | — | |
| `train_stage2_lgb_pit.py` | ✅ active | — | 4 | 187 | — | |
| `train_stage2_mlp_pit.py` | ✅ active | — | 4 | 234 | — | |
| `train_swing_v9.py` | ✅ active | — | 4 | 451 | — | |
| `train_v6_m15_baseline.py` | 📄 config | — | 0 | 168 | — | |
| `train_v6_multitf_v2.py` | 📄 config | — | 0 | 215 | — | |
| `train_xau_directional_v1.py` | ✅ active | — | 6 | 292 | — | |
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
| `arb_trainer.py` | ✅ active | — | 5 | 274 | — | |
| `deep_res_mlp_trainer.py` | ✅ active | ResBlock, DeepResMLP, _Block, _Model | 15 | 574 | — | |
| `lgb_trainer.py` | ✅ active | — | 10 | 514 | — | |
| `mtx_trainer.py` | ✅ active | — | 7 | 392 | — | |
| `online_mlp_trainer.py` | ✅ active | — | 7 | 294 | — | |
| `sur_trainer.py` | ✅ active | — | 5 | 313 | — | |
| `transformer_trainer.py` | ✅ active | UpgradedQuantTransformer, _Model | 14 | 793 | — | |
| `xgb_trainer.py` | ✅ active | — | 10 | 634 | — | |

## scripts/tuning

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `tune_btc_kalman_h4.py` | ✅ active | SimpleKalman | 7 | 265 | — | |

## scripts/validators

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `feature_quality_validator.py` | ✅ active | — | 5 | 204 | — | |
| `journal_validator.py` | ✅ active | — | 4 | 166 | — | |

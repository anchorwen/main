# MODULE INVENTORY — 模块清单与完成度

> **自动生成**: 2026-05-23T14:25:50Z
> **扫描模块数**: 439
> **图例**: ✅ active | 🧪 stub | 📄 config | ⬜ empty

## apps/engine

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_runner.py` | ✅ active | BacktestRunner, BacktestResult | 6 | 153 | — | |
| `batch_processor.py` | ✅ active | BatchProcessor | 3 | 77 | — | |
| `bootstrap_v9.py` | ✅ active | — | 6 | 227 | — | |
| `cli.py` | ✅ active | — | 42 | 1519 | — | |
| `communication_ops_cli.py` | ✅ active | — | 7 | 133 | — | |
| `communication_summary_contract.py` | ✅ active | — | 1 | 71 | — | |
| `diagnostics_cli.py` | ✅ active | DiagnosticsCLI | 10 | 129 | — | |
| `main_v9_shadow.py` | ✅ active | FeatureInputError, OutputPlan, StreamEnvelopePlan, SessionStreamPlan, BaselineSuiteSpec, FormalBaselineManifest, ShadowSessionManager | 91 | 2180 | — | |
| `orchestrator.py` | ✅ active | CycleOutcome, DecisionCycleOrchestrator | 6 | 297 | — | |
| `runtime_loop.py` | ✅ active | SimpleFeatureSnapshot, DecisionCycleResult, RuntimeLoop | 3 | 317 | — | |
| `system_facade.py` | ✅ active | SystemFacade, SystemSelfTest | 27 | 236 | — | |
| `v9_shadow_sse.py` | ✅ active | SessionStreamQueryError, SessionStreamResponseStartError, SessionSSEClientBuffer, ShadowSessionSSEHandler | 22 | 314 | — | |
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
| `strategy_adapter.py` | ✅ active | StrategyLineAdapter | 9 | 274 | — | |

## core/brains

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_registry.py` | ✅ active | BrainEntry, BrainRegistry | 15 | 165 | — | |
| `online_mlp_model.py` | ✅ active | OnlineMLP, _TorchOnlineMLP, _Module | 15 | 269 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/brains/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `base_adapter.py` | ✅ active | BaseBrainAdapter | 12 | 191 | — | |
| `lightgbm_brain_adapter.py` | ✅ active | LightGBMBrainAdapter | 7 | 230 | — | |
| `meta_filter_adapter.py` | ✅ active | FeatureParityError, MetaFilterAdapter | 8 | 207 | — | |
| `online_learner_adapter.py` | ✅ active | OnlineLearnerAdapter | 18 | 597 | — | |
| `params_brain_adapter.py` | ✅ active | ParamsBrainAdapter | 9 | 269 | — | |
| `transformer_brain_adapter.py` | ✅ active | TransformerBrainAdapter | 10 | 286 | — | |
| `v9_onnx_brain_adapter.py` | ✅ active | V9OnnxBrainAdapter | 10 | 327 | — | |
| `xgboost_brain_adapter.py` | ✅ active | XGBoostBrainAdapter | 7 | 228 | — | |

## core/brains/services

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `ab_test.py` | ✅ active | ExperimentConfig, TrafficSplitter, ExperimentResult, ExperimentTracker | 11 | 305 | — | |
| `brain_attribution_service.py` | ✅ active | BrainAttribution, AttributionReport, BrainAttributionService | 11 | 323 | — | |
| `brain_factory.py` | ✅ active | BrainFactory | 1 | 106 | — | |
| `brain_leaderboard.py` | ✅ active | BrainRanking, BrainLeaderboard | 8 | 275 | — | |
| `brain_promotion.py` | ✅ active | BrainPromotionDecision, BrainPromotionThresholds, BrainPromotionEvaluator | 8 | 432 | — | |
| `brain_registry_loader.py` | ✅ active | BrainRegistryLoader | 1 | 7 | — | |
| `brain_registry_service.py` | ✅ active | BrainRegistryService | 3 | 59 | — | |
| `brain_run_service.py` | ✅ active | BrainRunService | 15 | 267 | — | |
| `dynamic_brain_weighter.py` | ✅ active | DynamicBrainWeighter | 13 | 412 | — | |
| `inference_guard.py` | ✅ active | InferenceGuard | 11 | 222 | — | |
| `onnx_worker.py` | ✅ active | — | 1 | 80 | — | |
| `stability_monitor.py` | ✅ active | StabilityReport | 4 | 195 | — | |

## core/contracts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `domain_keys.py` | 📄 config | — | 0 | 984 | — | |
| `enums.py` | ✅ active | BrainRole, BrainStatus, DecisionAction, DecisionSide, RiskDecisionStatus, SystemMode, OverrideStatus, CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision, ExecutionEventType, ReconciliationStatus | 0 | 109 | — | |
| `exceptions.py` | ✅ active | DomainError, RiskError, RiskPolicyViolation, GovernanceError, InvalidTransitionError, BrainNotFoundError, ExecutionError, OrderNotFoundError, DuplicateOrderError, ProtocolError, DispatchError, IdempotencyError, ConfigurationError, ContractViolationError | 9 | 136 | — | |
| `ids.py` | ✅ active | — | 14 | 57 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 4 | — | |
| `strategy_magic.py` | 📄 config | — | 0 | 28 | — | |
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
| `label_contract.py` | ✅ active | BarrierResult, LabelContract | 11 | 524 | — | |
| `training_contract.py` | ✅ active | DatasetSpec, LabelSpec, ArchitectureSpec, ValidationSpec, QualityGateSpec, OutputSpec, TrainingContract | 12 | 458 | — | |
| `training_recipe.py` | ✅ active | ModelIdentity, LabelContractRef, DataAugmentation, DataConfig, TrainingConfig, EvaluationConfig, TrainingRecipe | 6 | 386 | — | |

## core/deployment

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `atomic_file_writer.py` | ✅ active | AtomicFileError, AtomicFileWriter | 12 | 130 | — | |
| `blue_green.py` | ✅ active | SlotState, SlotColor, DeploymentSlot, DeploymentTopology, CutoverResult, HealthProbe, BlueGreenManager | 26 | 526 | — | |
| `brain_alert.py` | ✅ active | — | 2 | 42 | — | |
| `brain_config_validator.py` | ✅ active | BrainConfigError, ValidationResult, BrainConfigValidator | 12 | 305 | — | |
| `brain_lifecycle_manager.py` | ✅ active | RetirementReport, RegistrationReport, IntegrityReport, ReferenceAuditReport, BrainLifecycleManager | 18 | 942 | — | |
| `brain_registration_gate.py` | ✅ active | GateResult, BrainRegistrationGate | 18 | 356 | — | |
| `capability_registry.py` | ✅ active | CapabilitySpec, CapabilityRegistry | 5 | 112 | — | |
| `compliance_audit.py` | ✅ active | ComplianceAuditService | 9 | 567 | — | |
| `compliance_control_matrix.py` | ✅ active | ComplianceControlMatrixService | 11 | 424 | — | |
| `compliance_export.py` | ✅ active | TradeRecord, ComplianceReport | 9 | 368 | — | |
| `config_hot_reload.py` | ✅ active | ConfigHotReload | 7 | 123 | — | |
| `deployment_executor.py` | ✅ active | DeploymentExecutor | 8 | 329 | — | |
| `deployment_plan.py` | ✅ active | DeploymentPlanService | 7 | 300 | — | |
| `domain_keys.py` | ⬜ empty | — | 0 | 7 | — | |
| `environment_config.py` | ✅ active | Environment, EnvironmentConfig | 7 | 126 | — | |
| `evidence_bundle.py` | ✅ active | EvidenceBundleService | 10 | 289 | — | |
| `feature_update_producer.py` | ✅ active | — | 2 | 53 | — | |
| `final_audit.py` | ✅ active | FinalAuditService | 5 | 211 | — | |
| `governance_summary.py` | ✅ active | — | 4 | 56 | — | |
| `health_check.py` | ✅ active | HealthCheckService | 8 | 90 | — | |
| `lifecycle_manager.py` | ✅ active | LifecycleManager | 7 | 167 | — | |
| `operational_support.py` | ✅ active | RetryPolicy, ConfigValidator | 7 | 130 | — | |
| `operations_timeline.py` | ✅ active | OperationsTimelineService | 16 | 262 | — | |
| `ops_maturity.py` | ✅ active | OpsMaturityService | 4 | 164 | — | |
| `path_defaults.py` | ✅ active | — | 2 | 80 | — | |
| `permission_audit.py` | ✅ active | AuditEntry, PermissionMatrix, AuditTrail | 20 | 334 | — | |
| `postmortem_report.py` | ✅ active | PostmortemReportService | 11 | 467 | — | |
| `release_certification.py` | ✅ active | ReleaseCertificationService | 12 | 293 | — | |
| `release_gate.py` | ✅ active | ReleaseGateService | 16 | 320 | — | |
| `release_pipeline.py` | ✅ active | ReleasePipelineService | 7 | 377 | — | |
| `release_readiness.py` | ✅ active | ReleaseReadinessService | 13 | 444 | — | |
| `release_registry.py` | ✅ active | ReleaseRegistryService | 17 | 351 | — | |
| `replay_isolation.py` | ✅ active | ReplayDispatchAdapter, NullDispatchAdapter, ReplayEnvironment | 11 | 138 | — | |
| `rollback_drill.py` | ✅ active | RollbackDrillService | 8 | 304 | — | |
| `runbook_engine.py` | ✅ active | RunbookEngine | 16 | 673 | — | |
| `scheduled_task_registry.py` | ✅ active | — | 4 | 36 | — | |
| `scheduler_service.py` | ✅ active | ScheduledTask, SchedulerService | 19 | 359 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 29 | — | |
| `service_container.py` | ✅ active | ServiceContainer | 41 | 588 | — | |
| `startup_validator.py` | ✅ active | — | 1 | 114 | — | |
| `state_persistence.py` | ✅ active | StatePersistence | 6 | 100 | — | |
| `validation_mode.py` | ✅ active | — | 1 | 10 | — | |

## core/execution

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `barrier_strategy.py` | ✅ active | BarrierStrategy | 1 | 60 | — | |
| `broker_adapter.py` | ✅ active | BrokerAdapter | 9 | 68 | — | |
| `capital_allocator.py` | ✅ active | AllocationDecision, GroupCorrelationTracker, CapitalAllocator | 9 | 464 | — | |
| `correlation_sizer.py` | ✅ active | ClusterResult | 1 | 109 | — | |
| `dynamic_sl_tp.py` | ✅ active | DynamicSLTP | 2 | 162 | — | |
| `execution_manager.py` | ✅ active | ExecutionManager | 7 | 161 | — | |
| `execution_queue.py` | ✅ active | QueuedDecision, DispatchResult, ExecutionQueue | 4 | 343 | — | |
| `exit_watchdog.py` | ✅ active | ExitAttempt, ExitWatchdogResult, ExitWatchdog | 7 | 412 | — | |
| `fill_simulator.py` | ✅ active | FillSimulationConfig, FillSimulator | 7 | 98 | — | |
| `fix_contracts.py` | ✅ active | FixSessionConfig, FixMessage, FixExecutionReport | 4 | 69 | — | |
| `fix_execution_mapper.py` | ✅ active | FixExecutionReportMapper | 5 | 75 | — | |
| `fix_gateway_adapter.py` | ✅ active | FixGatewayAdapter | 12 | 136 | — | |
| `fix_message_builder.py` | ✅ active | FixMessageBuilder | 5 | 57 | — | |
| `gateway_contracts.py` | ✅ active | OrderRequest, Fill, OrderState, ExecutionGateway | 9 | 103 | — | |
| `kelly_sizer.py` | ✅ active | KellyResult | 2 | 130 | — | |
| `limit_order_monitor.py` | ✅ active | LimitOrderIntent, LimitFillResult, LimitOrderMonitor | 9 | 329 | — | |
| `live_order_sender.py` | ✅ active | — | 6 | 316 | — | |
| `market_efficiency.py` | ✅ active | — | 2 | 67 | — | |
| `market_impact.py` | ✅ active | MarketImpactEstimate | 3 | 168 | — | |
| `meta_exit_engine.py` | ✅ active | ExitFeatureSnapshot, ExitEvaluation, MetaExitEngine | 13 | 509 | — | |
| `meta_filter_gate.py` | ✅ active | MetaFilterGate | 7 | 187 | — | |
| `meta_pipeline.py` | ✅ active | MetaProbeSpec, MetaProbeResult, MetaPipeline | 8 | 479 | — | |
| `meta_signal_filter.py` | ✅ active | FilterResult, MetaSignalFilter | 19 | 878 | — | |
| `micro_strategy.py` | ✅ active | MicroStrategy | 1 | 85 | — | |
| `mt5_broker_adapter.py` | ✅ active | MT5BrokerAdapter | 15 | 210 | — | |
| `order_state_machine.py` | ✅ active | OrderStateMachine | 9 | 101 | — | |
| `paper_gateway.py` | ✅ active | PaperExecutionGateway | 10 | 141 | — | |
| `portfolio_risk.py` | ✅ active | RiskVerdict, RiskResult, PortfolioState, PortfolioRiskController | 11 | 431 | — | |
| `position_manager.py` | ✅ active | ActivePosition, ActivePositionManager | 48 | 1693 | — | |
| `pre_trade_guards.py` | ✅ active | IntradayDrawdownKill, CooldownRegistry, FamilyEntryTracker | 27 | 837 | — | |
| `quality_analyzer.py` | ✅ active | SlippageTracker, ExecutionQualityAnalyzer | 15 | 351 | — | |
| `quality_contracts.py` | ✅ active | ExecutionBenchmark, ExecutionQualityMetric, ImplementationShortfall, ExecutionQualityReport | 3 | 138 | — | |
| `reentry_guard.py` | ✅ active | ExitRecord, ReentryState | 7 | 330 | — | |
| `regime_gate.py` | ✅ active | RegimeModulation, OURegime2D, RegimeGate | 37 | 701 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `statarb_strategy.py` | ✅ active | StatArbStrategy | 1 | 64 | — | |
| `strategy_budget.py` | ✅ active | StrategyBudget | 9 | 248 | — | |
| `strategy_line.py` | 🧪 stub | StrategyDecision, StrategyLineConfig, StrategyLine | 13 | 1421 | — | |
| `strategy_type.py` | ✅ active | StrategyType | 0 | 30 | — | |
| `swing_strategy.py` | ✅ active | SwingStrategy | 1 | 66 | — | |
| `trend_detector.py` | ✅ active | KalmanTrendFilter, TrendDetector | 31 | 643 | — | |

## core/features

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `data_augmentation.py` | ✅ active | — | 4 | 141 | — | |
| `feature_service.py` | ✅ active | FeatureService, FeatureBrainRegistry, IntentExplainer | 15 | 376 | — | |
| `feature_snapshot.py` | ✅ active | StoredFeatureSnapshot | 2 | 33 | — | |
| `local_feature_store.py` | ✅ active | LocalFeatureStore | 18 | 267 | — | |
| `rolling_normalizer.py` | ✅ active | RollingNormalizer | 15 | 234 | — | |
| `store_contracts.py` | ✅ active | FeatureSchema, FeatureRecord, FeatureQuery, FeatureStore | 8 | 87 | — | |
| `update_job.py` | ✅ active | FeatureUpdateResult, IncrementalFeatureUpdateJob | 3 | 63 | — | |

## core/features/adapters

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `microstructure_feature_adapter.py` | ✅ active | MicrostructureFeatureAdapter | 9 | 93 | — | |
| `v9_feature_adapter.py` | ✅ active | V9FeatureAdapter | 6 | 85 | — | |

## core/features/computers

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `daily_computer.py` | ✅ active | DailyFeatureComputer | 22 | 718 | — | |
| `live_daily_provider.py` | ✅ active | LiveDailyFeatureProvider | 8 | 211 | — | |
| `microstructure_computer.py` | ✅ active | MicrostructureFeatureComputer | 19 | 479 | — | |
| `v9_live_computer.py` | ✅ active | V9LiveFeatureComputer | 15 | 320 | — | |
| `v9_micro_computer.py` | ✅ active | V9MicroComputer | 3 | 92 | — | |

## core/features/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `daily_swing_schema.py` | 📄 config | — | 0 | 44 | — | |
| `microstructure_schema.py` | ✅ active | — | 1 | 32 | — | |
| `v9_institutional_schema.py` | 📄 config | — | 0 | 42 | — | |
| `v9_micro_schema.py` | ✅ active | — | 1 | 27 | — | |

## core/feedback

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `brain_performance_tracker.py` | ✅ active | BrainPerformanceTracker | 9 | 137 | — | |
| `brain_pnl_ledger.py` | ✅ active | BrainPnLMetrics, BrainPnLStore | 20 | 721 | — | |
| `brain_quality_engine.py` | ✅ active | BrainQualityVerdict, BrainQualityEngine | 13 | 431 | — | |
| `decision_scorer.py` | ✅ active | DecisionScorer | 5 | 120 | — | |
| `feedback_loop.py` | ✅ active | FeedbackLoop | 4 | 99 | — | |
| `online_feedback_hook.py` | ✅ active | OnlineFeedbackHook | 7 | 271 | — | |
| `outcome_collector.py` | ✅ active | OutcomeCollector | 4 | 111 | — | |
| `param_optimizer.py` | ✅ active | — | 5 | 283 | — | |
| `performance_analytics.py` | ✅ active | PerformanceAnalytics | 11 | 162 | — | |

## core/governance

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `governance_rule_engine.py` | ✅ active | GovernanceRule, GovernanceRuleEngine | 14 | 313 | — | |
| `governance_service.py` | ✅ active | GovernanceService | 16 | 194 | — | |
| `shadow_tracker.py` | ✅ active | ShadowBrainMetrics, ShadowTracker | 9 | 133 | — | |

## core/infrastructure

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `distributed_lock.py` | 🧪 stub | LockAcquireResult, BaseLock, FileLock, DirectoryLock | 23 | 395 | — | |

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
| `communication_operations_service.py` | ✅ active | CommunicationOperationsService | 13 | 445 | — | |
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
| `journal_cleanup.py` | ✅ active | — | 8 | 356 | — | |
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
| `alert_channels.py` | ✅ active | SlackAlertChannel, CompositeAlertChannel | 6 | 124 | — | |
| `alert_runbook_bridge.py` | ✅ active | RunbookAction, RunbookSOP, AlertRunbookBridge | 10 | 359 | — | |
| `alert_service.py` | 🧪 stub | AlertRule, AlertChannel, LogAlertChannel, InMemoryAlertChannel, BatchingAlertChannel, SeverityRouter, AlertService | 22 | 268 | — | |
| `audit_log.py` | ✅ active | StructuredAuditLog | 11 | 180 | — | |
| `diagnostics_dashboard.py` | ✅ active | DiagnosticsDashboard | 6 | 135 | — | |
| `event_bus.py` | ✅ active | EventBus | 7 | 63 | — | |
| `message_broker.py` | ✅ active | Message, MessageBroker, InProcessBroker, RedisStreamsBroker | 23 | 277 | — | |
| `metric_names.py` | ✅ active | — | 2 | 56 | — | |
| `metrics_collector.py` | ✅ active | MetricsCollector | 10 | 96 | — | |
| `mlflow_bridge.py` | ✅ active | — | 4 | 144 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |
| `slo_service.py` | ✅ active | SloService | 9 | 189 | — | |
| `tracing.py` | ✅ active | Span, TracingContext | 18 | 128 | — | |

## core/parliament

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `contract_groups.py` | ✅ active | ContractGroupConsensus, ABGroupRouter | 15 | 703 | — | |
| `parliament_service.py` | ✅ active | ParliamentService | 11 | 299 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 3 | — | |

## core/protocol

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `event_bar_sync.py` | ✅ active | BarSyncState, BarSyncPoller | 12 | 557 | — | |
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
| `resilience.py` | ✅ active | CircuitState, CircuitBreaker, RateLimiter | 12 | 137 | — | |
| `stub_communication_adapter.py` | ✅ active | StubCommunicationAdapter | 2 | 23 | — | |
| `venue_router.py` | 🧪 stub | VenueAdapter, StubVenueAdapter, VenueRouter | 14 | 116 | — | |

## core/risk

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `regime_detector.py` | ✅ active | RegimeDetector | 14 | 312 | — | |
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
| `live_cycle.py` | ✅ active | LiveCycleConfig, LiveCycleState | 25 | 6348 | — | |
| `market_ingress.py` | ✅ active | — | 8 | 141 | — | |
| `order_dispatch.py` | ✅ active | _MinimalControlSnapshot | 12 | 314 | — | |
| `schema_versions.py` | 📄 config | — | 0 | 16 | — | |
| `shadow_recorder.py` | ✅ active | — | 8 | 313 | — | |
| `signal_health.py` | ✅ active | GateResult, FeatureGate, _RollingStats, SignalHealthMonitor | 23 | 508 | — | |
| `signal_order_builder.py` | ✅ active | SignalOrderRequestBuilder | 3 | 52 | — | |
| `signal_pipeline.py` | ✅ active | — | 2 | 108 | — | |
| `summary_service.py` | ✅ active | RuntimeSummaryService | 11 | 143 | — | |

## core/schemas

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `trading_contracts.py` | ✅ active | BrainSignal, ConsensusResult, StrategyDecision, DegradedResult | 0 | 129 | — | |

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
| `system_mode_store.py` | ✅ active | SystemModeStore | 6 | 146 | — | |

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
| `cpcv.py` | ✅ active | CPCVFold, CPCVResult | 9 | 244 | — | |
| `custom_objectives.py` | ✅ active | — | 11 | 313 | — | |
| `dataset.py` | ✅ active | TrainingDataset | 17 | 378 | — | |
| `evaluation_report.py` | ✅ active | SHAPReport, TrainingEvalReport | 11 | 431 | — | |
| `experiment_tracker.py` | ✅ active | RunInfo, ExperimentTracker | 11 | 249 | — | |
| `model_card.py` | ✅ active | ModelCard, ModelCardGenerator | 6 | 225 | — | |
| `model_hashing.py` | ✅ active | — | 3 | 45 | — | |
| `profitability_calibrator.py` | ✅ active | BarrierConfig, ProfitabilityPoint, ProfitabilitySurface | 8 | 374 | — | |
| `registries.py` | ✅ active | — | 32 | 287 | — | |
| `trainer_protocol.py` | ✅ active | TrainResult, TrainerProtocol | 5 | 110 | — | |
| `training_registry.py` | ✅ active | Base, TrainingRunRecord, TrainingRegistry | 16 | 295 | — | |

## scripts

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `_diag_cycle_stall.py` | ✅ active | — | 4 | 119 | — | |
| `_fix_unused_ignores_v2.py` | 📄 config | — | 0 | 55 | — | |
| `analyze_deps.py` | ✅ active | — | 6 | 262 | — | |
| `backtest_runner.py` | ✅ active | — | 3 | 274 | — | |
| `bridge_supervisor.py` | ✅ active | — | 4 | 122 | — | |
| `check_blueprint_compliance.py` | ✅ active | — | 10 | 525 | — | |
| `ci_prepare_v9_shadow_fixtures.py` | ✅ active | — | 2 | 180 | — | |
| `daily_cost_report.py` | ✅ active | — | 4 | 175 | — | |
| `daily_ops.py` | ✅ active | — | 24 | 1034 | — | |
| `deploy_blue_green.py` | ✅ active | — | 7 | 126 | — | |
| `feature_store_maintenance.py` | ✅ active | — | 8 | 275 | — | |
| `feedback_loop.py` | ✅ active | — | 12 | 431 | — | |
| `hook_blueprint_precheck.py` | ✅ active | — | 1 | 65 | — | |
| `hook_mypy_check.py` | ✅ active | — | 1 | 94 | — | |
| `ingest_live_journal_to_alpha.py` | ✅ active | — | 4 | 96 | — | |
| `live_auto_healthcheck.py` | ✅ active | — | 11 | 233 | — | |
| `live_daily_recap.py` | ✅ active | — | 25 | 924 | — | |
| `live_dashboard.py` | ✅ active | — | 16 | 542 | — | |
| `live_data_quality_report.py` | ✅ active | — | 13 | 369 | — | |
| `live_dispatch_policy.py` | ✅ active | — | 10 | 316 | — | |
| `live_feature_quality_report.py` | ✅ active | — | 6 | 212 | — | |
| `live_intent_loop.py` | ✅ active | — | 13 | 2094 | — | |
| `live_launcher.py` | ✅ active | — | 13 | 749 | — | |
| `live_micro_rollout_gate.py` | ✅ active | — | 5 | 136 | — | |
| `live_monitor.py` | ✅ active | — | 12 | 484 | — | |
| `live_read_only_preflight.py` | ✅ active | — | 5 | 139 | — | |
| `live_shadow_ensemble.py` | ✅ active | — | 10 | 393 | — | |
| `live_shadow_intent_producer.py` | ✅ active | — | 7 | 262 | — | |
| `live_stack_diagnostic.py` | ✅ active | — | 5 | 204 | — | |
| `market_calendar.py` | ⬜ empty | — | 0 | 13 | — | |
| `mt5_bridge_healthcheck.py` | ✅ active | — | 6 | 153 | — | |
| `mt5_bridge_worker.py` | ✅ active | — | 21 | 759 | — | |
| `mt5_positions_snapshot.py` | ✅ active | — | 4 | 97 | — | |
| `mt5_spread_probe.py` | ✅ active | — | 1 | 65 | — | |
| `online_feedback_hook.py` | ✅ active | — | 2 | 119 | — | |
| `optimize_sl_tp.py` | ✅ active | — | 5 | 272 | — | |
| `paper_trade_simulator.py` | ✅ active | — | 13 | 783 | — | |
| `position_query.py` | ✅ active | — | 6 | 194 | — | |
| `position_snapshot.py` | ✅ active | — | 3 | 176 | — | |
| `pre_commit_mypy.py` | ✅ active | — | 5 | 162 | — | |
| `register_fix.py` | ✅ active | — | 8 | 329 | — | |
| `repair_brain_configs.py` | ✅ active | — | 5 | 174 | — | |
| `runtime_protection_guard.py` | ✅ active | — | 1 | 22 | — | |
| `send_live_order.py` | ✅ active | — | 4 | 149 | — | |
| `shadow_decision_recorder.py` | ✅ active | — | 7 | 199 | — | |
| `shadow_live_compare_report.py` | ✅ active | — | 9 | 218 | — | |
| `shadow_pnl_loop.py` | ✅ active | — | 9 | 759 | — | |
| `smoke_test_e2e.py` | ✅ active | — | 15 | 381 | — | |
| `test_meta_pipeline.py` | ✅ active | — | 6 | 295 | — | |
| `trade_quality_report.py` | ✅ active | — | 6 | 113 | — | |
| `validate_blueprints.py` | ✅ active | — | 7 | 292 | — | |
| `validate_brain_before_deploy.py` | ✅ active | — | 12 | 394 | — | |
| `verify.py` | ✅ active | — | 9 | 372 | — | |
| `verify_all_brains.py` | ✅ active | — | 1 | 91 | — | |

## scripts/audit

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `model_inventory.py` | ✅ active | — | 1 | 76 | — | |
| `reference_integrity.py` | ✅ active | — | 2 | 168 | — | |

## scripts/backtest

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `backtest_dynamic_exit.py` | ✅ active | — | 10 | 571 | — | |
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
| `brain_promotion_runner.py` | ✅ active | — | 6 | 196 | — | |
| `build_calibrated_dataset.py` | ✅ active | — | 19 | 743 | — | |
| `build_live_labeled_dataset.py` | ✅ active | — | 4 | 236 | — | |
| `build_meta_features.py` | ✅ active | — | 7 | 702 | — | |
| `build_meta_labeling_dataset.py` | ✅ active | — | 7 | 627 | — | |
| `build_meta_labels.py` | ✅ active | — | 6 | 379 | — | |
| `build_meta_learner.py` | ✅ active | — | 7 | 481 | — | |
| `build_micro_barrier_dataset.py` | ✅ active | — | 6 | 344 | — | |
| `build_micro_flat_features.py` | ✅ active | — | 3 | 151 | — | |
| `build_profitable_labels.py` | ✅ active | — | 5 | 452 | — | |
| `build_s1_regression_dataset.py` | ✅ active | — | 3 | 181 | — | |
| `build_v9_micro_dataset.py` | ✅ active | — | 2 | 257 | — | |
| `calibrate_labels.py` | ✅ active | — | 4 | 283 | — | |
| `calibrate_meta_filter.py` | ✅ active | — | 4 | 218 | — | |
| `calibrate_sl_tp.py` | ✅ active | — | 7 | 461 | — | |
| `champion_challenger.py` | ✅ active | — | 7 | 307 | — | |
| `crt_manifest.py` | ✅ active | CRTManifestV1 | 9 | 159 | — | |
| `dataset_builder.py` | ✅ active | — | 14 | 569 | — | |
| `dataset_builder_d1.py` | ✅ active | — | 6 | 452 | — | |
| `download_mt5_ohlc.py` | ✅ active | — | 2 | 115 | — | |
| `e2e_pipeline_validation.py` | ✅ active | — | 9 | 539 | — | |
| `eval_alignment.py` | ✅ active | — | 9 | 318 | — | |
| `eval_ensemble_baselines.py` | ✅ active | — | 2 | 147 | — | |
| `eval_regime.py` | ✅ active | — | 8 | 357 | — | |
| `eval_tf_comparison.py` | ✅ active | — | 11 | 253 | — | |
| `export_mt5_data.py` | ✅ active | — | 2 | 143 | — | |
| `generate_batch_plan.py` | ✅ active | — | 5 | 355 | — | |
| `generate_brain_config.py` | ✅ active | — | 7 | 329 | — | |
| `governance_scheduler.py` | ✅ active | — | 5 | 367 | — | |
| `institutional_train.py` | ✅ active | TrainResult | 20 | 1205 | — | |
| `label_builder.py` | ✅ active | — | 14 | 688 | — | |
| `label_builder_d1.py` | ✅ active | D1BarrierContract | 9 | 591 | — | |
| `monitor_training.py` | ✅ active | — | 18 | 421 | — | |
| `optimize_ensemble_weights.py` | ✅ active | — | 4 | 164 | — | |
| `optimize_meta_threshold.py` | ✅ active | — | 4 | 242 | — | |
| `quality_gate.py` | ✅ active | — | 9 | 318 | — | |
| `reactivate_brains.py` | ✅ active | — | 4 | 230 | — | |
| `recipe_diff.py` | ✅ active | — | 5 | 195 | — | |
| `recipe_search.py` | ✅ active | — | 9 | 518 | — | |
| `register_brain.py` | ✅ active | — | 5 | 157 | — | |
| `retraining_trigger.py` | ✅ active | — | 9 | 474 | — | |
| `run_promotion.py` | ✅ active | — | 6 | 256 | — | |
| `run_train_batch.py` | ✅ active | — | 6 | 274 | — | |
| `scan_profitability_surface.py` | ✅ active | — | 3 | 179 | — | |
| `train.py` | ✅ active | PipelineResult | 23 | 1935 | — | |
| `train_daily_swing.py` | ✅ active | — | 10 | 640 | — | |
| `train_exit_metamodel.py` | ✅ active | — | 7 | 343 | — | |
| `train_from_csv.py` | ✅ active | MLP | 10 | 725 | — | |
| `train_meta_filter.py` | ✅ active | — | 5 | 332 | — | |
| `train_meta_model.py` | ✅ active | — | 6 | 384 | — | |
| `train_online_init.py` | ✅ active | — | 9 | 411 | — | |
| `train_stage2_lgb_pit.py` | ✅ active | — | 4 | 187 | — | |
| `train_stage2_mlp_pit.py` | ✅ active | — | 4 | 234 | — | |
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

## scripts/validators

| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |
|------|------|----|------|------|------|------|
| `feature_quality_validator.py` | ✅ active | — | 5 | 204 | — | |
| `journal_validator.py` | ✅ active | — | 4 | 166 | — | |

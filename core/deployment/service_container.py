from core.deployment.environment_config import EnvironmentConfig
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_reconciliation_service import ExecutionReconciliationService
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.decision_record_writer import DecisionRecordWriter
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.idempotency import IdempotencyStore
from core.risk.risk_evaluation_service import RiskEvaluationService
from core.risk.risk_policies import (
    PositionLimitPolicy,
    DrawdownPolicy,
    ExposurePolicy,
    ConcentrationPolicy,
    ModePolicy,
)
from core.feedback.outcome_collector import OutcomeCollector
from core.feedback.decision_scorer import DecisionScorer
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.feedback_loop import FeedbackLoop
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL
from core.observability.metrics_collector import MetricsCollector
from core.observability.audit_log import StructuredAuditLog
from core.observability.diagnostics_dashboard import DiagnosticsDashboard
from core.governance.governance_service import GovernanceService
from core.governance.governance_rule_engine import GovernanceRuleEngine
from core.parliament.parliament_service import ParliamentService
from core.market.position_tracker import PositionTracker, MarketContextProvider
from core.execution.execution_manager import ExecutionManager
from core.deployment.health_check import HealthCheckService
from core.features.feature_service import FeatureService, BrainRegistryService, IntentExplainer
from core.brains.services.brain_run_service import BrainRunService
from core.brains.services.brain_factory import BrainFactory
from core.protocol.services.decision_compiler import DecisionCompiler
from core.protocol.services.override_resolver import OverrideResolver
from core.protocol.services.venue_router import VenueRouter, StubVenueAdapter
from core.state.services.control_snapshot_service import ControlSnapshotService
from core.state.stores.system_mode_store import SystemModeStore
from core.state.stores.override_store import OverrideStore
from core.state.schema_versions import SCHEMA_SYSTEM_MODE_STATE
from core.contracts.domain.system_mode_state import SystemModeState
from core.contracts.enums import SystemMode
from core.observability.alert_service import AlertService, LogAlertChannel
from core.deployment.config_hot_reload import ConfigHotReload
from core.deployment.domain_keys import TIMELINE_ACTOR_HOT_RELOAD, TIMELINE_EVENT_ENGINE_CONFIG
from core.deployment.release_readiness import ReleaseReadinessService
from core.deployment.runbook_engine import RunbookEngine
from core.observability.slo_service import SloService
from core.deployment.release_gate import ReleaseGateService
from core.deployment.evidence_bundle import EvidenceBundleService
from core.deployment.deployment_plan import DeploymentPlanService
from core.deployment.deployment_executor import DeploymentExecutor
from core.deployment.rollback_drill import RollbackDrillService
from core.deployment.operations_timeline import OperationsTimelineService
from core.deployment.postmortem_report import PostmortemReportService
from core.deployment.release_pipeline import ReleasePipelineService
from core.deployment.release_certification import ReleaseCertificationService
from core.deployment.release_registry import ReleaseRegistryService
from core.deployment.compliance_audit import ComplianceAuditService
from core.deployment.compliance_control_matrix import ComplianceControlMatrixService
from core.deployment.final_audit import FinalAuditService
from core.deployment.ops_maturity import OpsMaturityService
from core.deployment.schema_versions import SCHEMA_ENGINE_CONFIG_RELOAD_EVENT


class ServiceContainer:
    """Dependency injection container that wires all services based on
    the environment configuration.

    Call ``build()`` once to construct all services, then access them
    as attributes.
    """

    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self.validation_mode = getattr(config, "validation_mode", None)
        self._built = False

        self.ledger_store = None
        self.communication_writer = None
        self.communication_reader = None
        self.execution_event_writer = None
        self.execution_event_reader = None
        self.reconciliation_service = None
        self.inspection_service = None
        self.replay_service = None
        self.replay_gate = None
        self.operations_service = None
        self.dispatcher = None
        self.message_builder = None
        self.idempotency_store = None
        self.risk_service = None
        self.feedback_loop = None
        self.metrics = None
        self.audit_log = None
        self.diagnostics = None
        self.brain_tracker = None
        self.governance_service = None
        self.parliament_service = None
        self.position_tracker = None
        self.market_context = None
        self.execution_manager = None
        self.governance_rule_engine = None
        self.orchestrator = None
        self.health_check = None
        self.feature_service = None
        self.brain_registry = None
        self.brain_run_service = None
        self.override_resolver = None
        self.decision_compiler = None
        self.decision_record_writer = None
        self.control_snapshot_service = None
        self.runtime_loop = None
        self.venue_router = None
        self.alert_service = None
        self.config_hot_reload = None
        self.release_readiness = None
        self.runbook_engine = None
        self.slo_service = None
        self.release_gate = None
        self.evidence_bundle = None
        self.deployment_plan = None
        self.deployment_executor = None
        self.rollback_drill = None
        self.operations_timeline = None
        self.postmortem_report = None
        self.release_pipeline = None
        self.release_certification = None
        self.release_registry = None
        self.compliance_audit = None
        self.compliance_control_matrix = None
        self.final_audit = None
        self.ops_maturity = None

    def build(self) -> "ServiceContainer":
        if self._built:
            return self
        self._built = True

        self.ledger_store = JsonlLedgerStore(self.config.base_dir)
        self.communication_writer = CommunicationRecordWriter(ledger_store=self.ledger_store)
        self.communication_reader = CommunicationRecordReader(self.config.base_dir)
        self.execution_event_writer = ExecutionEventWriter(self.ledger_store)
        self.execution_event_reader = ExecutionEventReader(self.config.base_dir)
        self.reconciliation_service = ExecutionReconciliationService(
            self.communication_reader, self.execution_event_reader,
        )

        self._build_inspection()
        self._build_replay()
        self._build_operations()
        self._build_dispatcher()
        self._build_risk()
        self._build_observability()
        self._build_governance()
        self._build_parliament()
        self._build_market()
        self._build_execution()
        self._build_decision_pipeline()
        self._build_feedback()
        self._build_diagnostics()
        self._build_governance_rules()
        self._build_health_check()
        self._build_venue_router()
        self._build_alert_service()
        self._build_config_hot_reload()
        self._build_release_readiness()
        self._build_runbook_engine()
        self._build_slo_service()
        self._build_release_gate()
        self._build_evidence_bundle()
        self._build_deployment_plan()
        self._build_deployment_executor()
        self._build_rollback_drill()
        self._build_operations_timeline()
        self._build_postmortem_report()
        self._build_release_pipeline()
        self._build_release_certification()
        self._build_release_registry()
        self._build_compliance_audit()
        self._build_compliance_control_matrix()
        self._build_final_audit()
        self._build_ops_maturity()

        return self

    def _build_inspection(self) -> None:
        self.inspection_service = CommunicationInspectionService(
            record_reader=self.communication_reader,
            execution_event_reader=self.execution_event_reader,
        )

    def _build_replay(self) -> None:
        self.replay_service = CommunicationReplayService(
            inspection_service=self.inspection_service,
        )
        self.replay_gate = CommunicationReplayGate()

    def _build_operations(self) -> None:
        from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
        receipt_reader = None
        if self.config.receipt_dir:
            receipt_reader = FileQueueReceiptReader(self.config.receipt_dir)
        self.operations_service = CommunicationOperationsService(
            communication_reader=self.communication_reader,
            inspection_service=self.inspection_service,
            replay_service=self.replay_service,
            replay_gate=self.replay_gate,
            receipt_reader=receipt_reader,
            reconciliation_service=self.reconciliation_service,
        )

    def _build_dispatcher(self) -> None:
        self.message_builder = IntentMessageBuilder(
            producer=self.config.producer_name,
            target=self.config.target_name,
        )

        self.idempotency_store = None
        if self.config.enable_idempotency:
            self.idempotency_store = IdempotencyStore(
                self.config.base_dir, ttl_hours=self.config.idempotency_ttl_hours,
            )

        adapter = StubCommunicationAdapter()
        self.dispatcher = CommunicationDispatcher(
            adapter=adapter,
            idempotency_store=self.idempotency_store,
        )

    def _build_risk(self) -> None:
        policies = [
            ModePolicy(),
            PositionLimitPolicy(max_open_positions=self.config.max_open_positions),
            DrawdownPolicy(max_drawdown_pct=self.config.max_drawdown_pct),
            ExposurePolicy(max_notional=self.config.max_notional_exposure),
            ConcentrationPolicy(max_per_symbol=self.config.max_per_symbol),
        ]
        self.risk_service = RiskEvaluationService(policies)

    def _build_observability(self) -> None:
        self.metrics = MetricsCollector() if self.config.enable_metrics else None
        audit_dir = self.config.audit_dir or self.config.base_dir
        self.audit_log = StructuredAuditLog(audit_dir) if self.config.enable_audit_log else None

    def _build_governance(self) -> None:
        self.governance_service = GovernanceService(audit_log=self.audit_log)

    def _build_parliament(self) -> None:
        self.parliament_service = ParliamentService(
            governance_service=self.governance_service,
        )

    def _build_market(self) -> None:
        self.position_tracker = PositionTracker()
        self.market_context = MarketContextProvider()

    def _build_execution(self) -> None:
        self.execution_manager = ExecutionManager(
            execution_event_writer=self.execution_event_writer,
            position_tracker=self.position_tracker,
            metrics=self.metrics,
        )

    def _build_decision_pipeline(self) -> None:
        from datetime import datetime
        initial_mode_state = SystemModeState(
            schema_version=SCHEMA_SYSTEM_MODE_STATE,
            mode_state_id="mode_state_default",
            current_mode=SystemMode(self.config.system_mode) if self.config.system_mode in {m.value for m in SystemMode} else SystemMode.NORMAL,
            entered_at=datetime.utcnow(),
            previous_mode=None,
            reason="container_bootstrap",
        )
        mode_store = SystemModeStore(initial_state=initial_mode_state)
        override_store = OverrideStore()
        self.brain_registry = BrainRegistryService()
        self.control_snapshot_service = ControlSnapshotService(
            mode_store=mode_store,
            override_store=override_store,
            brain_registry_service=self.brain_registry,
        )
        self.feature_service = FeatureService()
        brain_factory = BrainFactory()
        self.brain_run_service = BrainRunService(
            brain_factory=brain_factory,
            brain_registry_service=self.brain_registry,
        )
        self.override_resolver = OverrideResolver()
        self.decision_compiler = DecisionCompiler(
            base_policy={
                "probability_shift": 0.0,
                "probability_scale": 1.0,
                "entry_long_threshold": 0.70,
                "entry_short_threshold": 0.70,
            },
            intent_explainer=IntentExplainer(),
        )
        decision_ledger = JsonlLedgerStore(self.config.base_dir)
        self.decision_record_writer = DecisionRecordWriter(ledger_store=decision_ledger)

    def _build_feedback(self) -> None:
        if not self.config.enable_feedback_loop:
            self.feedback_loop = None
            self.brain_tracker = None
            return
        self.brain_tracker = BrainPerformanceTracker(window_size=self.config.feedback_window_size)
        self.feedback_loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(
                self.execution_event_reader, self.reconciliation_service,
            ),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=self.brain_tracker,
        )

    def _build_diagnostics(self) -> None:
        self.diagnostics = DiagnosticsDashboard(
            metrics_collector=self.metrics,
            audit_log=self.audit_log,
            brain_performance_tracker=self.brain_tracker,
        )

    def _build_governance_rules(self) -> None:
        self.governance_rule_engine = GovernanceRuleEngine.with_default_rules(
            self.governance_service, audit_log=self.audit_log,
        )

    def _build_health_check(self) -> None:
        self.health_check = HealthCheckService(self)

    def build_runtime_loop(self):
        """Create a fully wired RuntimeLoop from container services."""
        from apps.engine.runtime_loop import RuntimeLoop
        self.runtime_loop = RuntimeLoop(
            control_snapshot_service=self.control_snapshot_service,
            feature_service=self.feature_service,
            brain_run_service=self.brain_run_service,
            parliament_adapter=self.parliament_service,
            override_resolver=self.override_resolver,
            decision_compiler=self.decision_compiler,
            decision_record_writer=self.decision_record_writer,
            intent_message_builder=self.message_builder,
            communication_dispatcher=self.dispatcher,
            communication_record_writer=self.communication_writer,
            communication_operations_service=self.operations_service,
            risk_evaluation_service=self.risk_service,
        )
        return self.runtime_loop

    def build_orchestrator(self, runtime_loop=None):
        """Create an orchestrator wrapping the given or auto-built RuntimeLoop."""
        if runtime_loop is None:
            runtime_loop = self.runtime_loop or self.build_runtime_loop()
        from apps.engine.orchestrator import DecisionCycleOrchestrator
        self.orchestrator = DecisionCycleOrchestrator(
            runtime_loop,
            execution_manager=self.execution_manager,
            position_tracker=self.position_tracker,
            market_context=self.market_context,
            feedback_loop=self.feedback_loop,
            governance_service=self.governance_service,
            audit_log=self.audit_log,
            metrics=self.metrics,
        )
        return self.orchestrator

    def _build_venue_router(self) -> None:
        self.venue_router = VenueRouter(
            default_adapter=StubVenueAdapter("default"),
        )

    def _build_alert_service(self) -> None:
        channels = []
        if self.audit_log:
            channels.append(LogAlertChannel(self.audit_log))
        self.alert_service = AlertService.with_default_rules(channels=channels)

    def _build_config_hot_reload(self) -> None:
        from pathlib import Path
        explicit = getattr(self.config, "hot_reload_path", None)
        if explicit:
            config_path = explicit
        else:
            config_path = str(Path(self.config.base_dir) / "engine_config.json")
        self.config_hot_reload = ConfigHotReload(config_path)
        p = Path(config_path)
        if p.is_file():
            data = self.config_hot_reload.load()
            if data:
                self.config_hot_reload.apply_overrides(self, data)
        self.config_hot_reload.register_listener(self._on_engine_config_changed)

    def _on_engine_config_changed(self, _changes: dict, new: dict) -> None:
        self.config_hot_reload.apply_overrides(self, new)
        if self.metrics is not None:
            self.metrics.inc(ENGINE_CONFIG_RELOAD_TOTAL)
        try:
            if getattr(self, "operations_timeline", None) is not None:
                self.operations_timeline.record(
                    TIMELINE_EVENT_ENGINE_CONFIG,
                    {
                        "schema_version": SCHEMA_ENGINE_CONFIG_RELOAD_EVENT,
                        "reloaded": True,
                        "changes": _changes,
                        "ops_maturity_min_score": self.config.ops_maturity_min_score,
                    },
                    actor=TIMELINE_ACTOR_HOT_RELOAD,
                )
        except Exception:
            pass

    def _build_release_readiness(self) -> None:
        self.release_readiness = ReleaseReadinessService(self)

    def _build_runbook_engine(self) -> None:
        self.runbook_engine = RunbookEngine(self)

    def _build_slo_service(self) -> None:
        self.slo_service = SloService(self.metrics)

    def _build_release_gate(self) -> None:
        self.release_gate = ReleaseGateService(self)

    def _build_evidence_bundle(self) -> None:
        self.evidence_bundle = EvidenceBundleService(self)

    def _build_deployment_plan(self) -> None:
        self.deployment_plan = DeploymentPlanService(self)

    def _build_deployment_executor(self) -> None:
        self.deployment_executor = DeploymentExecutor(self)

    def _build_rollback_drill(self) -> None:
        self.rollback_drill = RollbackDrillService(self)

    def _build_operations_timeline(self) -> None:
        self.operations_timeline = OperationsTimelineService(str(self.config.base_dir))

    def _build_postmortem_report(self) -> None:
        self.postmortem_report = PostmortemReportService(self)

    def _build_release_pipeline(self) -> None:
        self.release_pipeline = ReleasePipelineService(self)

    def _build_release_certification(self) -> None:
        self.release_certification = ReleaseCertificationService(self)

    def _build_release_registry(self) -> None:
        self.release_registry = ReleaseRegistryService(str(self.config.base_dir))

    def _build_compliance_audit(self) -> None:
        self.compliance_audit = ComplianceAuditService(self)

    def _build_compliance_control_matrix(self) -> None:
        self.compliance_control_matrix = ComplianceControlMatrixService(self)

    def _build_final_audit(self) -> None:
        self.final_audit = FinalAuditService(self)

    def _build_ops_maturity(self) -> None:
        self.ops_maturity = OpsMaturityService(self)

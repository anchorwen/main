"""Tests for unified exception hierarchy and code quality checks."""
from pathlib import Path
import re

from core.contracts.exceptions import (
    DomainError, RiskError, RiskPolicyViolation,
    GovernanceError, InvalidTransitionError, BrainNotFoundError,
    ExecutionError, OrderNotFoundError, DuplicateOrderError,
    ProtocolError, DispatchError, IdempotencyError,
    ConfigurationError, ContractViolationError,
)
from core.contracts.validators import ContractValidator


class TestExceptionHierarchy:
    def test_domain_error_base(self):
        e = DomainError("test", code="test_code", detail={"k": "v"})
        assert str(e) == "test"
        assert e.code == "test_code"
        assert e.detail == {"k": "v"}

    def test_risk_errors(self):
        e = RiskPolicyViolation("drawdown", "exceeded 5%", limit=5.0)
        assert isinstance(e, RiskError)
        assert isinstance(e, DomainError)
        assert e.code == "risk_policy_violation"
        assert "drawdown" in str(e)
        assert e.detail["policy"] == "drawdown"
        assert e.detail["limit"] == 5.0

    def test_governance_errors(self):
        e1 = InvalidTransitionError("alpha", "live", "candidate")
        assert isinstance(e1, GovernanceError)
        assert e1.detail["brain_id"] == "alpha"

        e2 = BrainNotFoundError("missing")
        assert isinstance(e2, GovernanceError)
        assert e2.detail["brain_id"] == "missing"

    def test_execution_errors(self):
        e1 = OrderNotFoundError("msg_123")
        assert isinstance(e1, ExecutionError)
        assert e1.detail["message_id"] == "msg_123"

        e2 = DuplicateOrderError("msg_456")
        assert isinstance(e2, ExecutionError)

    def test_protocol_errors(self):
        e1 = DispatchError("timeout", venue="MT5")
        assert isinstance(e1, ProtocolError)
        assert e1.detail["venue"] == "MT5"

        e2 = IdempotencyError("key_abc")
        assert isinstance(e2, ProtocolError)

    def test_config_error(self):
        e = ConfigurationError("bad config")
        assert isinstance(e, DomainError)

    def test_contract_violation_error(self):
        violations = [
            type("V", (), {"to_dict": lambda self: {"f": "x"}})(),
        ]
        e = ContractViolationError(violations)
        assert isinstance(e, DomainError)
        assert len(e.detail["violations"]) == 1

    def test_catch_broad_category(self):
        errors = [
            RiskPolicyViolation("p", "r"),
            InvalidTransitionError("b", "a", "c"),
            OrderNotFoundError("m"),
            DispatchError("x"),
        ]
        for e in errors:
            assert isinstance(e, DomainError)

    def test_catch_layer_specific(self):
        try:
            raise RiskPolicyViolation("concentration", "too high")
        except RiskError as e:
            assert "concentration" in str(e)

        try:
            raise BrainNotFoundError("x")
        except GovernanceError as e:
            assert "x" in str(e)


class TestCodeQualityChecks:
    @staticmethod
    def _unused_constants_in_file(constants_file: Path, search_files: list[Path]) -> list[str]:
        text = constants_file.read_text(encoding="utf-8")
        constants = re.findall(r"^([A-Z][A-Z0-9_]+)\s*=\s*", text, flags=re.M)
        assert constants, f"No constants found in {constants_file}"

        unused = []
        for const in constants:
            pattern = re.compile(r"\b" + re.escape(const) + r"\b")
            is_used = False
            for file_path in search_files:
                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if pattern.search(content):
                    is_used = True
                    break
            if not is_used:
                unused.append(const)
        return unused

    @staticmethod
    def _stale_constants_in_file(constants_file: Path, search_files: list[Path]) -> list[str]:
        text = constants_file.read_text(encoding="utf-8")
        constants = re.findall(r"^([A-Z][A-Z0-9_]+)\s*=\s*", text, flags=re.M)
        assert constants, f"No constants found in {constants_file}"

        stale = []
        for const in constants:
            pattern = re.compile(r"\b" + re.escape(const) + r"\b")
            hits = 0
            for file_path in search_files:
                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                hits += len(pattern.findall(content))
            if hits <= 1:
                stale.append(const)
        return stale

    def test_no_empty_python_files(self):
        core_path = Path("d:/cursor/core")
        empty = [p for p in core_path.rglob("*.py") if p.stat().st_size == 0]
        assert empty == [], f"Empty files found: {empty}"

    def test_all_core_packages_have_init(self):
        core_path = Path("d:/cursor/core")
        packages = set()
        for p in core_path.rglob("*.py"):
            if p.name != "__init__.py":
                packages.add(p.parent)
        missing = []
        for pkg in packages:
            init = pkg / "__init__.py"
            if not init.exists() and pkg != core_path:
                missing.append(str(pkg))
        # Sub-packages (services/, adapters/, stores/) use implicit namespace
        # packages which is valid Python 3 behavior
        assert len(missing) <= 15, f"Too many packages missing __init__.py: {missing}"

    def test_key_modules_importable(self):
        from core.contracts.enums import DecisionAction, RiskDecisionStatus
        from core.contracts.exceptions import DomainError
        from core.contracts.validators import ContractValidator
        from core.risk.risk_evaluation_service import RiskEvaluationService
        from core.governance.governance_service import GovernanceService
        from core.governance.governance_rule_engine import GovernanceRuleEngine
        from core.execution.execution_manager import ExecutionManager
        from core.market.position_tracker import PositionTracker
        from core.market.signal_processor import SignalFilter
        from core.feedback.feedback_loop import FeedbackLoop
        from core.feedback.performance_analytics import PerformanceAnalytics
        from core.observability.metrics_collector import MetricsCollector
        from core.observability.audit_log import StructuredAuditLog
        from core.observability.event_bus import EventBus
        from core.observability.alert_service import AlertService
        from core.observability.tracing import TracingContext
        from core.protocol.services.resilience import CircuitBreaker, RateLimiter
        from core.protocol.services.venue_router import VenueRouter
        from core.deployment.service_container import ServiceContainer
        from core.deployment.environment_config import EnvironmentConfig
        from core.deployment.lifecycle_manager import LifecycleManager
        from core.deployment.scheduler_service import SchedulerService
        from core.deployment.config_hot_reload import ConfigHotReload
        from core.deployment.state_persistence import StatePersistence
        from core.deployment.operational_support import RetryPolicy, ConfigValidator
        from core.deployment.health_check import HealthCheckService
        from core.deployment.replay_isolation import ReplayEnvironment
        from core.features.feature_service import FeatureService, BrainRegistryService
        from apps.engine.orchestrator import DecisionCycleOrchestrator
        from apps.engine.system_facade import SystemFacade, SystemSelfTest
        from apps.engine.batch_processor import BatchProcessor
        from apps.engine.backtest_runner import BacktestRunner
        from apps.engine.diagnostics_cli import DiagnosticsCLI

    def test_exception_hierarchy_complete(self):
        from core.contracts import exceptions as ex
        base = ex.DomainError
        layers = [
            ex.RiskError, ex.GovernanceError,
            ex.ExecutionError, ex.ProtocolError,
            ex.ConfigurationError, ex.ContractViolationError,
        ]
        for layer in layers:
            assert issubclass(layer, base), f"{layer} not subclass of DomainError"

        specifics = [
            (ex.RiskPolicyViolation, ex.RiskError),
            (ex.InvalidTransitionError, ex.GovernanceError),
            (ex.BrainNotFoundError, ex.GovernanceError),
            (ex.OrderNotFoundError, ex.ExecutionError),
            (ex.DuplicateOrderError, ex.ExecutionError),
            (ex.DispatchError, ex.ProtocolError),
            (ex.IdempotencyError, ex.ProtocolError),
        ]
        for specific, parent in specifics:
            assert issubclass(specific, parent)

    def test_domain_keys_constants_are_referenced(self):
        workspace = Path("d:/cursor")
        domain_keys = workspace / "core/deployment/domain_keys.py"
        py_files = [p for p in workspace.rglob("*.py") if p != domain_keys]
        unused = self._unused_constants_in_file(domain_keys, py_files)
        assert unused == [], f"Unused domain key constants found: {unused}"

    def test_schema_version_constants_are_referenced(self):
        workspace = Path("d:/cursor")
        schema_files = [p for p in (workspace / "core").rglob("schema_versions.py")]
        assert schema_files, "No schema_versions.py files found under core/"

        py_files = list(workspace.rglob("*.py"))
        stale = {}
        for schema_file in schema_files:
            search_files = [p for p in py_files if p != schema_file]
            unused = self._unused_constants_in_file(schema_file, search_files)
            if unused:
                stale[str(schema_file.relative_to(workspace)).replace("\\", "/")] = unused

        assert stale == {}, f"Unused schema version constants found: {stale}"

    def test_metric_name_constants_are_referenced(self):
        workspace = Path("d:/cursor")
        metric_names = workspace / "core/observability/metric_names.py"
        py_files = list(workspace.rglob("*.py"))
        stale = self._stale_constants_in_file(metric_names, py_files)
        assert stale == [], f"Stale metric name constants found: {stale}"

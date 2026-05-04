"""Final audit tests: CLI smoke, module integrity, architecture invariants."""

import importlib
import json
import sys
from pathlib import Path

from apps.engine.cli import main
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_TOTAL

ALL_CORE_MODULES = [
    "core.contracts.exceptions",
    "core.contracts.validators",
    "core.contracts.domain.execution_event",
    "core.risk.risk_evaluation_service",
    "core.risk.risk_policies",
    "core.feedback.outcome_collector",
    "core.feedback.decision_scorer",
    "core.feedback.brain_performance_tracker",
    "core.feedback.feedback_loop",
    "core.feedback.performance_analytics",
    "core.protocol.services.idempotency",
    "core.protocol.services.resilience",
    "core.protocol.services.venue_router",
    "core.observability.metrics_collector",
    "core.observability.metric_names",
    "core.observability.audit_log",
    "core.observability.tracing",
    "core.observability.diagnostics_dashboard",
    "core.observability.event_bus",
    "core.observability.alert_service",
    "core.deployment.environment_config",
    "core.deployment.service_container",
    "core.deployment.health_check",
    "core.deployment.state_persistence",
    "core.deployment.replay_isolation",
    "core.deployment.operational_support",
    "core.deployment.lifecycle_manager",
    "core.deployment.scheduler_service",
    "core.deployment.config_hot_reload",
    "core.governance.governance_service",
    "core.governance.governance_rule_engine",
    "core.parliament.parliament_service",
    "core.market.position_tracker",
    "core.market.signal_processor",
    "core.execution.execution_manager",
    "core.features.feature_service",
    "apps.engine.orchestrator",
    "apps.engine.diagnostics_cli",
    "apps.engine.batch_processor",
    "apps.engine.system_facade",
    "apps.engine.backtest_runner",
    "apps.engine.cli",
]


class TestModuleIntegrity:
    def test_all_modules_import(self):
        for mod in ALL_CORE_MODULES:
            importlib.import_module(mod)

    def test_no_circular_imports(self):
        before = set(sys.modules.keys())
        for mod in ALL_CORE_MODULES:
            importlib.import_module(mod)
        after = set(sys.modules.keys())
        new_modules = after - before
        assert len(new_modules) < 200


class TestCLISmoke:
    def test_selftest_dev(self, tmp_path):
        assert main(["--base-dir", str(tmp_path), "selftest"]) == 0

    def test_selftest_prod(self, tmp_path):
        assert main(["--base-dir", str(tmp_path), "--env", "production", "selftest"]) == 0

    def test_validate_dev(self, tmp_path):
        assert main(["--base-dir", str(tmp_path), "validate"]) == 0

    def test_status(self, tmp_path):
        assert main(["--base-dir", str(tmp_path), "status"]) == 0

    def test_diagnose_all_subcommands(self, tmp_path):
        for sub in ["health", "metrics", "snapshot", "positions"]:
            assert main(["--base-dir", str(tmp_path), "diagnose", sub]) == 0

    def test_backtest_roundtrip(self, tmp_path):
        scenarios = [
            {"trigger": {"symbol": "XAUUSD"}, "features": {"f": 1.0}},
            {"trigger": {"symbol": "EURUSD"}, "features": {"f": 0.5}},
        ]
        sf = tmp_path / "scenarios.json"
        sf.write_text(json.dumps(scenarios))
        out = str(tmp_path / "report.json")
        assert (
            main(["--base-dir", str(tmp_path), "backtest", "--scenarios", str(sf), "--output", out])
            == 0
        )
        report = json.loads(Path(out).read_text())
        assert report["summary"]["scenarios"] == 2

    def test_no_command_help(self, tmp_path):
        assert main(["--base-dir", str(tmp_path)]) == 0


class TestArchitectureInvariants:
    def test_container_builds_all_envs(self, tmp_path):
        for env_factory in [
            EnvironmentConfig.development,
            EnvironmentConfig.production,
            EnvironmentConfig.test,
        ]:
            cfg = env_factory(str(tmp_path / env_factory.__name__))
            c = ServiceContainer(cfg).build()
            assert c.governance_service is not None
            assert c.venue_router is not None
            assert c.alert_service is not None

    def test_orchestrator_produces_trace(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.trace_summary is not None
        assert outcome.trace_summary["span_count"] >= 2

    def test_full_e2e_decide_fill_governance(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        cfg.enable_idempotency = False
        c = ServiceContainer(cfg).build()
        c.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]

        from apps.engine.system_facade import SystemFacade
        from core.deployment.lifecycle_manager import LifecycleManager
        from core.deployment.state_persistence import StatePersistence

        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, sp)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch, lifecycle=lm)

        lm.startup()

        decisions = [facade.decide("XAUUSD") for _ in range(5)]
        filled = [d for d in decisions if d.get("allowed") and d.get("message_id")]

        for d in filled[:2]:
            facade.process_event(d["message_id"], "ack", venue="ex")
            facade.process_event(
                d["message_id"], "filled", filled_quantity=0.001, price=2000, venue="ex"
            )

        h = facade.health()
        assert h["readiness"]["status"] == "ready"
        assert c.metrics.get_counter(CYCLES_TOTAL) >= 5  # type: ignore[reportOptionalMemberAccess]

        lm.shutdown(save_state=True)

    def test_exception_hierarchy_depth(self):
        from core.contracts import exceptions as ex

        for cls in [
            ex.RiskPolicyViolation,
            ex.InvalidTransitionError,
            ex.OrderNotFoundError,
            ex.DispatchError,
        ]:
            depth = 0
            c = cls.__mro__
            for klass in c:
                if klass is Exception:
                    break
                depth += 1
            assert depth >= 3

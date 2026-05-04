from datetime import UTC, datetime

from core.deployment.domain_keys import (
    ENGINE_CONFIG_KEY_HOT_RELOAD,
    ENGINE_CONFIG_KEY_RUNTIME_METRICS,
    EVIDENCE_SECTION_ENGINE_CONFIG,
)
from core.deployment.schema_versions import SCHEMA_ENGINE_CONFIG_EVIDENCE
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL


class SystemFacade:
    """Unified API surface for the entire decision system.

    Provides high-level operations that compose multiple service
    calls, designed for external callers (CLI, HTTP, scheduler).
    """

    def __init__(
        self, container, orchestrator=None, lifecycle=None, scheduler=None, alert_service=None
    ):
        self._c = container
        self._orch = orchestrator or container.orchestrator
        self._lifecycle = lifecycle
        self._scheduler = scheduler
        self._alert_svc = alert_service

    # --- Decision operations ---

    def decide(self, symbol: str, features: dict | None = None) -> dict:
        if self._orch is None:
            return {"error": "orchestrator not built"}
        outcome = self._orch.run_cycle({"symbol": symbol}, features or {})
        return {
            "cycle_id": outcome.cycle_id,
            "verdict": outcome.decision_result.verdict.status.value
            if outcome.decision_result
            else None,
            "allowed": outcome.decision_result.verdict.is_allowed()
            if outcome.decision_result
            else False,
            "message_id": (
                outcome.decision_result.communication_record.message_id
                if outcome.decision_result and outcome.decision_result.communication_record
                else None
            ),
            "execution": outcome.execution_result,
        }

    def process_event(self, message_id: str, event_type: str, **kwargs) -> dict:
        if self._orch is None:
            return {"error": "orchestrator not built"}
        return self._orch.process_execution_event(
            message_id=message_id, event_type=event_type, **kwargs
        )

    # --- Observability ---

    def health(self) -> dict:
        return {
            "liveness": self._c.health_check.liveness(),
            "readiness": self._c.health_check.readiness(),
            "lifecycle": self._lifecycle.get_status() if self._lifecycle else None,
            "scheduler": self._scheduler.get_status() if self._scheduler else None,
        }

    def metrics(self) -> dict:
        if self._c.metrics:
            return self._c.metrics.snapshot()
        return {"error": "metrics not enabled"}

    def snapshot(self) -> dict:
        snap = self._c.diagnostics.build_snapshot()
        if not isinstance(snap, dict):
            snap = {}
        else:
            snap = dict(snap)
        snap[EVIDENCE_SECTION_ENGINE_CONFIG] = self._c.evidence_bundle.engine_config_snapshot()
        return snap

    # --- Brain management ---

    def list_brains(self) -> list[dict]:
        states = self._c.governance_service.get_all_states()
        result = []
        for bid, state in states.items():
            perf = self._c.brain_tracker.get_brain_summary(bid) if self._c.brain_tracker else {}
            result.append(
                {
                    "brain_id": bid,
                    "status": state["status"],
                    "health": perf.get("health_signal", "unknown"),
                    "composite_mean": perf.get("composite_mean", 0),
                    "sample_count": perf.get("sample_count", 0),
                }
            )
        return result

    def freeze_brain(self, brain_id: str, reason: str = "manual") -> dict:
        self._c.governance_service.transition(brain_id, "frozen", reason=reason)
        return self._c.governance_service.get_brain_state(brain_id)

    def unfreeze_brain(self, brain_id: str, reason: str = "manual") -> dict:
        self._c.governance_service.transition(brain_id, "probation", reason=reason)
        return self._c.governance_service.get_brain_state(brain_id)

    # --- Position & Risk ---

    def positions(self) -> dict:
        if self._c.position_tracker is None:
            return {"open": [], "risk_context": {}}
        return {
            "open": self._c.position_tracker.list_open(),
            "risk_context": self._c.position_tracker.get_risk_context(),
        }

    def orders(self) -> dict:
        if self._c.execution_manager is None:
            return {"orders": [], "count": 0}
        return {
            "orders": self._c.execution_manager.list_orders(),
            "count": len(self._c.execution_manager.list_orders()),
        }

    # --- Audit ---

    def audit_recent(self, limit: int = 50) -> list[dict]:
        if self._c.audit_log:
            return self._c.audit_log.read_entries()[-limit:]
        return []

    def alerts_recent(self, limit: int = 50) -> list[dict]:
        if self._alert_svc:
            return self._alert_svc.get_fired_history(limit)
        return []


class SystemSelfTest:
    """Runs a comprehensive self-test against the live system.

    Validates that all critical paths are operational without
    producing real side effects.
    """

    def __init__(self, container):
        self._c = container

    def run(self) -> dict:
        results = []
        results.append(self._check("health_check", self._test_health))
        results.append(self._check("metrics", self._test_metrics))
        results.append(self._check("audit_log", self._test_audit))
        results.append(self._check("governance", self._test_governance))
        results.append(self._check("risk_service", self._test_risk))
        results.append(self._check("position_tracker", self._test_positions))
        results.append(self._check("execution_manager", self._test_execution))
        results.append(self._check("dispatcher", self._test_dispatcher))
        results.append(self._check("diagnostics", self._test_diagnostics))
        results.append(self._check(EVIDENCE_SECTION_ENGINE_CONFIG, self._test_engine_config))
        results.append(self._check("feature_service", self._test_features))

        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] == "fail")

        return {
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "all_passed": failed == 0,
            "results": results,
        }

    def _check(self, name: str, fn) -> dict:
        try:
            fn()
            return {"name": name, "status": "pass"}
        except Exception as exc:
            return {"name": name, "status": "fail", "error": str(exc)}

    def _test_health(self):
        r = self._c.health_check.liveness()
        assert r["status"] == "alive", f"liveness: {r}"

    def _test_metrics(self):
        if self._c.metrics is None:
            return
        self._c.metrics.snapshot()

    def _test_audit(self):
        if self._c.audit_log is None:
            return
        self._c.audit_log.read_entries()

    def _test_governance(self):
        self._c.governance_service.get_all_states()

    def _test_risk(self):
        assert self._c.risk_service is not None

    def _test_positions(self):
        self._c.position_tracker.list_open()
        self._c.position_tracker.get_risk_context()

    def _test_execution(self):
        self._c.execution_manager.list_orders()

    def _test_dispatcher(self):
        assert self._c.dispatcher is not None

    def _test_diagnostics(self):
        self._c.diagnostics.build_snapshot()

    def _test_engine_config(self):
        snap = self._c.evidence_bundle.engine_config_snapshot()
        assert snap.get("schema_version") == SCHEMA_ENGINE_CONFIG_EVIDENCE
        if not snap.get("available"):
            return
        assert "effective" in snap
        eff = snap["effective"]
        assert "ops_maturity_min_score" in eff
        assert "engine_config_poll_interval_seconds" in eff
        hr = snap.get(ENGINE_CONFIG_KEY_HOT_RELOAD)
        assert isinstance(hr, dict)
        assert "reload_count" in hr
        m = self._c.metrics
        if m is not None:
            n = m.get_counter(ENGINE_CONFIG_RELOAD_TOTAL)
            assert n >= 0.0
            rm = snap.get(ENGINE_CONFIG_KEY_RUNTIME_METRICS, {})
            assert rm.get(ENGINE_CONFIG_RELOAD_TOTAL) == n

    def _test_features(self):
        assert self._c.feature_service is not None
        snap = self._c.feature_service.build_snapshot({"symbol": "SELFTEST"})
        assert snap.symbol == "SELFTEST"

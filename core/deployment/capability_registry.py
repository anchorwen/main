"""Capability registry for release readiness.

Centralizes capability definitions so new capabilities are added through
registry entries rather than scattered conditionals.
"""

from dataclasses import dataclass

from core.contracts.domain_keys import (
    READINESS_CAP_ALERTS,
    READINESS_CAP_AUDIT_LOG,
    READINESS_CAP_BACKTESTING,
    READINESS_CAP_CLI_OPERATIONS,
    READINESS_CAP_CONFIG_HOT_RELOAD,
    READINESS_CAP_DECISION_CYCLE,
    READINESS_CAP_DIAGNOSTICS,
    READINESS_CAP_EXECUTION_LIFECYCLE,
    READINESS_CAP_FEEDBACK_LOOP,
    READINESS_CAP_GOVERNANCE_RULES,
    READINESS_CAP_LEDGER_PERSISTENCE,
    READINESS_CAP_METRICS,
    READINESS_CAP_RECONCILIATION,
    READINESS_CAP_REPLAY_OPERATIONS,
    READINESS_CAP_RISK_EVALUATION,
    READINESS_CAP_TRACING,
    READINESS_CAP_VENUE_ROUTING,
    READINESS_SVC_ALERT_SERVICE,
    READINESS_SVC_AUDIT_LOG,
    READINESS_SVC_BRAIN_TRACKER,
    READINESS_SVC_CONFIG_HOT_RELOAD,
    READINESS_SVC_DECISION_COMPILER,
    READINESS_SVC_DECISION_RECORD_WRITER,
    READINESS_SVC_DIAGNOSTICS,
    READINESS_SVC_EXECUTION_MANAGER,
    READINESS_SVC_FEEDBACK_LOOP,
    READINESS_SVC_GOVERNANCE_RULE_ENGINE,
    READINESS_SVC_GOVERNANCE_SERVICE,
    READINESS_SVC_HEALTH_CHECK,
    READINESS_SVC_INSPECTION_SERVICE,
    READINESS_SVC_LEDGER_STORE,
    READINESS_SVC_METRICS,
    READINESS_SVC_OPERATIONS_SERVICE,
    READINESS_SVC_RECONCILIATION_SERVICE,
    READINESS_SVC_REPLAY_GATE,
    READINESS_SVC_REPLAY_SERVICE,
    READINESS_SVC_RISK_SERVICE,
    READINESS_SVC_RUNTIME_LOOP,
    READINESS_SVC_VENUE_ROUTER,
)


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    dependencies: tuple[str, ...]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}

    def register(self, name: str, dependencies: list[str] | tuple[str, ...]) -> None:
        self._specs[name] = CapabilitySpec(name=name, dependencies=tuple(dependencies))

    def list_names(self) -> list[str]:
        return list(self._specs.keys())

    def evaluate(self, container, capability_name: str) -> bool:
        spec = self._specs.get(capability_name)
        if spec is None:
            return False
        return all(getattr(container, dep, None) is not None for dep in spec.dependencies)


def build_default_release_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        READINESS_CAP_DECISION_CYCLE, [READINESS_SVC_RUNTIME_LOOP, READINESS_SVC_DECISION_COMPILER]
    )
    registry.register(READINESS_CAP_RISK_EVALUATION, [READINESS_SVC_RISK_SERVICE])
    registry.register(
        READINESS_CAP_LEDGER_PERSISTENCE,
        [READINESS_SVC_LEDGER_STORE, READINESS_SVC_DECISION_RECORD_WRITER],
    )
    registry.register(READINESS_CAP_EXECUTION_LIFECYCLE, [READINESS_SVC_EXECUTION_MANAGER])
    registry.register(READINESS_CAP_RECONCILIATION, [READINESS_SVC_RECONCILIATION_SERVICE])
    registry.register(
        READINESS_CAP_REPLAY_OPERATIONS,
        [
            READINESS_SVC_INSPECTION_SERVICE,
            READINESS_SVC_REPLAY_SERVICE,
            READINESS_SVC_REPLAY_GATE,
            READINESS_SVC_OPERATIONS_SERVICE,
        ],
    )
    registry.register(
        READINESS_CAP_FEEDBACK_LOOP, [READINESS_SVC_FEEDBACK_LOOP, READINESS_SVC_BRAIN_TRACKER]
    )
    registry.register(
        READINESS_CAP_GOVERNANCE_RULES,
        [READINESS_SVC_GOVERNANCE_SERVICE, READINESS_SVC_GOVERNANCE_RULE_ENGINE],
    )
    registry.register(READINESS_CAP_METRICS, [READINESS_SVC_METRICS])
    registry.register(READINESS_CAP_AUDIT_LOG, [READINESS_SVC_AUDIT_LOG])
    registry.register(READINESS_CAP_DIAGNOSTICS, [READINESS_SVC_DIAGNOSTICS])
    registry.register(READINESS_CAP_TRACING, [READINESS_SVC_RUNTIME_LOOP])
    registry.register(READINESS_CAP_ALERTS, [READINESS_SVC_ALERT_SERVICE])
    registry.register(READINESS_CAP_CONFIG_HOT_RELOAD, [READINESS_SVC_CONFIG_HOT_RELOAD])
    registry.register(READINESS_CAP_VENUE_ROUTING, [READINESS_SVC_VENUE_ROUTER])
    registry.register(READINESS_CAP_BACKTESTING, [READINESS_SVC_RUNTIME_LOOP])
    registry.register(READINESS_CAP_CLI_OPERATIONS, [READINESS_SVC_HEALTH_CHECK])
    return registry

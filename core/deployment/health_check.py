from datetime import datetime

from core.deployment.domain_keys import (
    HEALTH_CHECK_NAME_DISPATCHER,
    HEALTH_CHECK_NAME_FEEDBACK_LOOP,
    HEALTH_CHECK_NAME_LEDGER_STORE,
    HEALTH_CHECK_NAME_METRICS,
    HEALTH_CHECK_NAME_RISK_SERVICE,
    HEALTH_CHECK_STATUS_FAILED,
    HEALTH_CHECK_STATUS_OK,
    HEALTH_STATUS_ALIVE,
    HEALTH_STATUS_NOT_READY,
    HEALTH_STATUS_READY,
    PAYLOAD_KEY_CHECKS,
    PAYLOAD_KEY_ENVIRONMENT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_TIMESTAMP,
)


class HealthCheckService:
    """Provides liveness and readiness probes for the system.

    Checks critical dependencies and surfaces aggregate health status.
    """

    def __init__(self, service_container):
        self._container = service_container

    def liveness(self) -> dict:
        return {
            PAYLOAD_KEY_STATUS: HEALTH_STATUS_ALIVE,
            PAYLOAD_KEY_TIMESTAMP: datetime.utcnow().isoformat(),
            PAYLOAD_KEY_ENVIRONMENT: self._container.config.environment.value,
        }

    def readiness(self) -> dict:
        checks = []
        checks.append(self._check_ledger_store())
        checks.append(self._check_risk_service())
        checks.append(self._check_dispatcher())

        if self._container.config.enable_metrics:
            checks.append(self._check_metrics())
        if self._container.config.enable_feedback_loop:
            checks.append(self._check_feedback())

        all_ok = all(c[PAYLOAD_KEY_STATUS] == HEALTH_CHECK_STATUS_OK for c in checks)
        return {
            PAYLOAD_KEY_STATUS: HEALTH_STATUS_READY if all_ok else HEALTH_STATUS_NOT_READY,
            PAYLOAD_KEY_TIMESTAMP: datetime.utcnow().isoformat(),
            PAYLOAD_KEY_ENVIRONMENT: self._container.config.environment.value,
            PAYLOAD_KEY_CHECKS: checks,
        }

    def _check_ledger_store(self) -> dict:
        ok = self._container.ledger_store is not None
        return {PAYLOAD_KEY_NAME: HEALTH_CHECK_NAME_LEDGER_STORE, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK if ok else HEALTH_CHECK_STATUS_FAILED}

    def _check_risk_service(self) -> dict:
        ok = self._container.risk_service is not None
        return {PAYLOAD_KEY_NAME: HEALTH_CHECK_NAME_RISK_SERVICE, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK if ok else HEALTH_CHECK_STATUS_FAILED}

    def _check_dispatcher(self) -> dict:
        ok = self._container.dispatcher is not None
        return {PAYLOAD_KEY_NAME: HEALTH_CHECK_NAME_DISPATCHER, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK if ok else HEALTH_CHECK_STATUS_FAILED}

    def _check_metrics(self) -> dict:
        ok = self._container.metrics is not None
        return {PAYLOAD_KEY_NAME: HEALTH_CHECK_NAME_METRICS, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK if ok else HEALTH_CHECK_STATUS_FAILED}

    def _check_feedback(self) -> dict:
        ok = self._container.feedback_loop is not None
        return {PAYLOAD_KEY_NAME: HEALTH_CHECK_NAME_FEEDBACK_LOOP, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK if ok else HEALTH_CHECK_STATUS_FAILED}

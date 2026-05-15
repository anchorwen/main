from datetime import UTC, datetime

from core.contracts.domain_keys import (
    HEALTH_CHECK_STATUS_OK,
    LIFECYCLE_AUDIT_ACTION_SHUTDOWN,
    LIFECYCLE_AUDIT_ACTION_STARTUP,
    LIFECYCLE_PHASE_HEALTH_CHECK,
    LIFECYCLE_PHASE_HOOK,
    LIFECYCLE_PHASE_STATE_RESTORE,
    LIFECYCLE_PHASE_STATE_SAVE,
    LIFECYCLE_PHASE_STATUS_ERROR,
    LIFECYCLE_STATUS_ALREADY_STARTED,
    LIFECYCLE_STATUS_NOT_STARTED,
    LIFECYCLE_STATUS_STARTED,
    LIFECYCLE_STATUS_STOPPED,
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_LABEL,
    PAYLOAD_KEY_PATHS,
    PAYLOAD_KEY_PHASE,
    PAYLOAD_KEY_PHASES,
    PAYLOAD_KEY_RESTORE_STATE,
    PAYLOAD_KEY_RESTORED,
    PAYLOAD_KEY_RESULT,
    PAYLOAD_KEY_RUNNING,
    PAYLOAD_KEY_SHUTDOWN_HOOKS,
    PAYLOAD_KEY_STARTED_AT,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_UPTIME_SECONDS,
)
from core.deployment.state_persistence import StatePersistence
from core.observability.metric_names import LIFECYCLE_STARTUPS


class LifecycleManager:
    """Manages graceful startup and shutdown of the system.

    Coordinates state persistence, health checks, and ordered
    service initialization/teardown.
    """

    def __init__(self, service_container, state_persistence: StatePersistence | None = None):
        self._container = service_container
        self._persistence = state_persistence
        self._started = False
        self._start_time: datetime | None = None
        self._shutdown_hooks: list = []

    def startup(self, *, restore_state: bool = False, state_label: str = "latest") -> dict:
        if self._started:
            return {PAYLOAD_KEY_STATUS: LIFECYCLE_STATUS_ALREADY_STARTED}

        result: dict = {PAYLOAD_KEY_PHASES: []}

        health = self._container.health_check.readiness()
        result[PAYLOAD_KEY_PHASES].append(
            {PAYLOAD_KEY_PHASE: LIFECYCLE_PHASE_HEALTH_CHECK, PAYLOAD_KEY_RESULT: health}
        )

        if restore_state and self._persistence:
            restored = self._persistence.restore_governance_state(
                self._container.governance_service,
                label=state_label,
            )
            result[PAYLOAD_KEY_PHASES].append(
                {
                    PAYLOAD_KEY_PHASE: LIFECYCLE_PHASE_STATE_RESTORE,
                    PAYLOAD_KEY_RESTORED: restored is not None,
                }
            )

        self._started = True
        self._start_time = datetime.now(UTC).replace(tzinfo=None)
        result[PAYLOAD_KEY_STATUS] = LIFECYCLE_STATUS_STARTED
        result[PAYLOAD_KEY_STARTED_AT] = self._start_time.isoformat()

        if self._container.metrics:
            self._container.metrics.inc(LIFECYCLE_STARTUPS)

        if self._container.audit_log:
            self._container.audit_log.log(
                event_type="lifecycle",
                severity="info",
                actor="lifecycle_manager",
                detail={
                    PAYLOAD_KEY_ACTION: LIFECYCLE_AUDIT_ACTION_STARTUP,
                    PAYLOAD_KEY_RESTORE_STATE: restore_state,
                },
            )

        return result

    def shutdown(self, *, save_state: bool = True, state_label: str | None = None) -> dict:
        if not self._started:
            return {PAYLOAD_KEY_STATUS: LIFECYCLE_STATUS_NOT_STARTED}

        result: dict = {PAYLOAD_KEY_PHASES: []}

        for hook in self._shutdown_hooks:
            try:
                hook()
                result[PAYLOAD_KEY_PHASES].append(
                    {
                        PAYLOAD_KEY_PHASE: LIFECYCLE_PHASE_HOOK,
                        PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK,
                    }
                )
            except Exception as exc:
                result[PAYLOAD_KEY_PHASES].append(
                    {
                        PAYLOAD_KEY_PHASE: LIFECYCLE_PHASE_HOOK,
                        PAYLOAD_KEY_STATUS: LIFECYCLE_PHASE_STATUS_ERROR,
                        PAYLOAD_KEY_ERROR: str(exc),
                    }
                )

        if save_state and self._persistence:
            label = state_label or datetime.now(UTC).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
            save_result = self._persistence.save_all(self._container, label=label)
            result[PAYLOAD_KEY_PHASES].append(
                {
                    PAYLOAD_KEY_PHASE: LIFECYCLE_PHASE_STATE_SAVE,
                    PAYLOAD_KEY_LABEL: label,
                    PAYLOAD_KEY_PATHS: save_result[PAYLOAD_KEY_PATHS],
                }
            )

        uptime = (
            (datetime.now(UTC).replace(tzinfo=None) - self._start_time).total_seconds()
            if self._start_time
            else 0
        )
        result[PAYLOAD_KEY_UPTIME_SECONDS] = round(uptime, 2)

        if self._container.audit_log:
            self._container.audit_log.log(
                event_type="lifecycle",
                severity="info",
                actor="lifecycle_manager",
                detail={
                    PAYLOAD_KEY_ACTION: LIFECYCLE_AUDIT_ACTION_SHUTDOWN,
                    PAYLOAD_KEY_UPTIME_SECONDS: result[PAYLOAD_KEY_UPTIME_SECONDS],
                },
            )

        self._started = False
        result[PAYLOAD_KEY_STATUS] = LIFECYCLE_STATUS_STOPPED
        return result

    def register_shutdown_hook(self, hook) -> None:
        self._shutdown_hooks.append(hook)

    def is_running(self) -> bool:
        return self._started

    def get_uptime(self) -> float:
        if not self._started or not self._start_time:
            return 0.0
        return (datetime.now(UTC).replace(tzinfo=None) - self._start_time).total_seconds()

    def get_status(self) -> dict:
        return {
            PAYLOAD_KEY_RUNNING: self._started,
            PAYLOAD_KEY_STARTED_AT: self._start_time.isoformat() if self._start_time else None,
            PAYLOAD_KEY_UPTIME_SECONDS: round(self.get_uptime(), 2),
            PAYLOAD_KEY_SHUTDOWN_HOOKS: len(self._shutdown_hooks),
        }

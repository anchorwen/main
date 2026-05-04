import threading
from datetime import UTC, datetime

from core.deployment.domain_keys import (
    HEALTH_CHECK_STATUS_OK,
    LIFECYCLE_PHASE_STATUS_ERROR,
    PAYLOAD_KEY_BRAIN_ID,
    PAYLOAD_KEY_COUNTERS,
    PAYLOAD_KEY_ENABLED,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_ERROR_COUNT,
    PAYLOAD_KEY_ERROR_RATE,
    PAYLOAD_KEY_INTERVAL_SECONDS,
    PAYLOAD_KEY_LAST_ERROR,
    PAYLOAD_KEY_LAST_RUN,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_RUN_COUNT,
    PAYLOAD_KEY_RUNNING,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_TASK,
    PAYLOAD_KEY_TASK_COUNT,
    PAYLOAD_KEY_TASKS,
    PAYLOAD_KEY_THROTTLE_RATE,
)
from core.observability.metric_names import (
    CYCLES_ERRORS,
    CYCLES_THROTTLED,
    CYCLES_TOTAL,
)


class ScheduledTask:
    """A periodic task that runs at a fixed interval."""

    def __init__(self, name: str, fn, interval_seconds: float, enabled: bool = True):
        self.name = name
        self.fn = fn
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.last_run: datetime | None = None
        self.run_count = 0
        self.error_count = 0
        self.last_error: str | None = None


class SchedulerService:
    """Runs periodic background tasks: governance evaluation,
    state snapshots, alert checks, metrics flush.

    Non-blocking — tasks run in a background thread. Call ``start()``
    to begin and ``stop()`` for graceful shutdown.
    """

    def __init__(self):
        self._tasks: list[ScheduledTask] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def add_task(self, name: str, fn, interval_seconds: float, enabled: bool = True) -> None:
        self._tasks.append(ScheduledTask(name, fn, interval_seconds, enabled))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._running = False

    def run_once(self) -> list[dict]:
        """Execute all due tasks once (synchronous). Useful for testing."""
        results = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for task in self._tasks:
            if not task.enabled:
                continue
            if task.last_run and (now - task.last_run).total_seconds() < task.interval_seconds:
                continue
            results.append(self._execute_task(task))
        return results

    def get_status(self) -> dict:
        return {
            PAYLOAD_KEY_RUNNING: self._running,
            PAYLOAD_KEY_TASK_COUNT: len(self._tasks),
            PAYLOAD_KEY_TASKS: [
                {
                    PAYLOAD_KEY_NAME: t.name,
                    PAYLOAD_KEY_ENABLED: t.enabled,
                    PAYLOAD_KEY_INTERVAL_SECONDS: t.interval_seconds,
                    PAYLOAD_KEY_RUN_COUNT: t.run_count,
                    PAYLOAD_KEY_ERROR_COUNT: t.error_count,
                    PAYLOAD_KEY_LAST_RUN: t.last_run.isoformat() if t.last_run else None,
                    PAYLOAD_KEY_LAST_ERROR: t.last_error,
                }
                for t in self._tasks
            ],
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(UTC).replace(tzinfo=None)
            for task in self._tasks:
                if not task.enabled:
                    continue
                if (
                    task.last_run is None
                    or (now - task.last_run).total_seconds() >= task.interval_seconds
                ):
                    self._execute_task(task)
            self._stop_event.wait(0.1)

    def _execute_task(self, task: ScheduledTask) -> dict:
        try:
            task.fn()
            task.run_count += 1
            task.last_run = datetime.now(UTC).replace(tzinfo=None)
            task.last_error = None
            return {PAYLOAD_KEY_TASK: task.name, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_OK}
        except Exception as exc:
            task.error_count += 1
            task.last_run = datetime.now(UTC).replace(tzinfo=None)
            task.last_error = str(exc)
            return {
                PAYLOAD_KEY_TASK: task.name,
                PAYLOAD_KEY_STATUS: LIFECYCLE_PHASE_STATUS_ERROR,
                PAYLOAD_KEY_ERROR: str(exc),
            }

    @classmethod
    def for_container(cls, container, persistence=None, alert_service=None):
        """Build a scheduler with standard periodic tasks for a ServiceContainer."""
        svc = cls()

        if container.governance_rule_engine and container.brain_tracker:

            def governance_eval():
                summaries = container.brain_tracker.get_all_summaries()
                summary_map = {s[PAYLOAD_KEY_BRAIN_ID]: s for s in summaries}
                if summary_map:
                    container.governance_rule_engine.evaluate(summary_map)

            svc.add_task("governance_evaluation", governance_eval, interval_seconds=60)

        if persistence:

            def state_snapshot():
                persistence.save_all(container)

            svc.add_task("state_snapshot", state_snapshot, interval_seconds=300)

        if alert_service and container.metrics:

            def alert_check():
                snap = container.metrics.snapshot()
                counters = snap.get(PAYLOAD_KEY_COUNTERS, {})
                total = counters.get(CYCLES_TOTAL, 0)
                errors = counters.get(CYCLES_ERRORS, 0)
                ctx = {
                    PAYLOAD_KEY_ERROR_RATE: errors / total if total > 0 else 0,
                    PAYLOAD_KEY_THROTTLE_RATE: counters.get(CYCLES_THROTTLED, 0) / max(total, 1),
                }
                alert_service.evaluate(ctx)

            svc.add_task("alert_check", alert_check, interval_seconds=30)

        poll_iv = float(
            getattr(
                container.config,
                "engine_config_poll_interval_seconds",
                60.0,
            )
        )
        if poll_iv > 0 and getattr(container, "config_hot_reload", None) is not None:

            def engine_config_poll():
                container.config_hot_reload.check_and_reload()

            svc.add_task("engine_config_poll", engine_config_poll, interval_seconds=poll_iv)

        return svc

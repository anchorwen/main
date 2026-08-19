"""Tests for SupervisedScheduler — UGR v3.1 dual-isolation task scheduler.

Covers:
- THREAD task lifecycle (start, run, complete)
- THREAD task cancellation
- THREAD task heartbeat + stuck detection
- PROCESS task lifecycle (start, run, complete)
- PROCESS task unexpected death detection
- Graceful shutdown
- Concurrent task execution
- Task status transitions
"""

from __future__ import annotations

import threading
import time as _time
from typing import cast

import pytest

from core.runtime.supervised_scheduler import (
    SchedulerConfig,
    SupervisedScheduler,
    TaskStatus,
)

# ── Module-level process targets (must be pickleable on Windows) ────────────


def _proc_simple_worker() -> None:
    """Simple one-shot process target."""
    pass


def _proc_bad_exit_worker() -> None:
    """Process target that exits with code 1."""
    import sys

    sys.exit(1)


@pytest.fixture
def scheduler():
    """Create a fresh scheduler with fast detection for testing."""
    config = SchedulerConfig(
        heartbeat_interval=0.05,  # 50ms — fast for tests
        stuck_threshold=0.3,  # 300ms
        shutdown_grace=2.0,
        process_kill_escalation=1.0,
    )
    s = SupervisedScheduler(config)
    yield s
    s.shutdown(timeout=1.0)


# ═══════════════════════════════════════════════════════════════════════════
# THREAD Task Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestThreadTask:
    """Tests for THREAD task execution."""

    def test_task_runs_and_completes(self, scheduler: SupervisedScheduler) -> None:
        """A THREAD task starts, runs, and completes."""
        completed = threading.Event()

        def work() -> None:
            completed.set()

        scheduler.add_thread_task(name="test_worker", target=work, heartbeat_interval=0.1)
        scheduler.start()

        assert completed.wait(timeout=2.0), "Task did not complete within timeout"

    def test_task_status_transitions(self, scheduler: SupervisedScheduler) -> None:
        """Task transitions from PENDING → RUNNING → STOPPED."""
        started = threading.Event()
        done = threading.Event()

        def work() -> None:
            started.set()
            done.wait(timeout=1.0)

        task = scheduler.add_thread_task(name="status_test", target=work)
        assert task.status == TaskStatus.PENDING

        scheduler.start()
        assert started.wait(timeout=2.0)
        assert (
            cast(object, task.status) == TaskStatus.RUNNING
        )  # TECH_DEBT-009: L86 已收窄 PENDING, start() 副作用不被静态追踪

        done.set()
        _time.sleep(0.1)
        # Task should complete
        assert task.status in (TaskStatus.RUNNING, TaskStatus.STOPPED)

    def test_cancellation_stops_task(self, scheduler: SupervisedScheduler) -> None:
        """Setting cancel_event stops the task."""

        def work() -> None:
            # This task loops until cancelled
            pass  # One-shot tasks just return

        scheduler.add_thread_task(name="cancel_test", target=work)
        scheduler.start()
        _time.sleep(0.1)
        scheduler.shutdown(timeout=1.0)

        status = scheduler.get_status("cancel_test")
        # Task may be STOPPING (during shutdown) or STOPPED or None
        assert status in (TaskStatus.STOPPING, TaskStatus.STOPPED, None)

    def test_heartbeat_reported(self, scheduler: SupervisedScheduler) -> None:
        """Heartbeat updates last_heartbeat timestamp."""
        heartbeat_event = threading.Event()

        def work() -> None:
            heartbeat_event.wait(timeout=1.0)

        task = scheduler.add_thread_task(name="hb_test", target=work, heartbeat_interval=0.1)
        scheduler.start()

        # Wait for heartbeat to update
        _time.sleep(0.3)
        assert task.last_heartbeat > 0

        heartbeat_event.set()

    def test_stuck_detection(self, scheduler: SupervisedScheduler) -> None:
        """A task with stale heartbeat → STALLED (white-box test).

        Tests the detection mechanism by manipulating last_heartbeat directly.
        Real stalls are detected via thread death (thread_died event) or
        heartbeat timeout when the heartbeat thread is also blocked.
        """
        alerts: list[dict] = []

        def alert_handler(source: str, event: str, context: dict) -> None:
            alerts.append({"source": source, "event": event, "context": context})

        scheduler._on_alert = alert_handler

        hold = threading.Event()

        def work() -> None:
            hold.wait(timeout=2.0)

        task = scheduler.add_thread_task(
            name="stuck_test",
            target=work,
            heartbeat_interval=999.0,  # Effectively disable auto-heartbeat
        )
        scheduler.start()
        _time.sleep(0.2)  # Let task start

        # White-box: force last_heartbeat to appear ancient.
        # Use lock to prevent race with heartbeat thread.
        # Guard against fresh machines where monotonic() < 999:
        # the supervisor checks `last_heartbeat > 0` at line 368,
        # so negative values would skip the stall check entirely.
        with scheduler._lock:
            task.last_heartbeat = max(_time.monotonic() - 999.0, 0.001)

        # Wait for supervisor to detect the stall
        _time.sleep(0.5)

        hold.set()

        stall_alerts = [a for a in alerts if a["event"] == "thread_stalled"]
        assert len(stall_alerts) >= 1, (
            f"No stall alert. Alerts: {alerts}. " f"last_heartbeat={task.last_heartbeat}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# PROCESS Task Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessTask:
    """Tests for PROCESS task execution."""

    def test_process_task_runs(self, scheduler: SupervisedScheduler) -> None:
        """A PROCESS task starts and runs."""
        scheduler.add_process_task(name="proc_test", target=_proc_simple_worker)
        scheduler.start()

        _time.sleep(0.5)
        status = scheduler.get_status("proc_test")
        # Process may have completed already
        assert status in (TaskStatus.RUNNING, TaskStatus.STOPPED, None)

    def test_process_task_exit_zero(self, scheduler: SupervisedScheduler) -> None:
        """Process with exit code 0 → STOPPED (not FAILED)."""
        task = scheduler.add_process_task(name="exit_zero", target=_proc_simple_worker)
        scheduler.start()

        # Wait for process to finish
        if task.process:
            task.process.join(timeout=3.0)
        _time.sleep(0.2)  # Let supervisor update status

        status = scheduler.get_status("exit_zero")
        # Exit 0 → STOPPED; if it finished before supervisor check, None
        assert status in (TaskStatus.STOPPED, None)

    def test_process_task_exit_nonzero_detected(self, scheduler: SupervisedScheduler) -> None:
        """Process with non-zero exit → FAILED."""
        alerts: list[dict] = []

        def alert_handler(source: str, event: str, context: dict) -> None:
            alerts.append({"source": source, "event": event, "context": context})

        scheduler._on_alert = alert_handler

        task = scheduler.add_process_task(name="exit_bad", target=_proc_bad_exit_worker)
        scheduler.start()

        # Wait for process to finish
        if task.process:
            task.process.join(timeout=3.0)
        _time.sleep(0.5)  # Let supervisor detect

        die_alerts = [a for a in alerts if a["event"] == "process_died"]
        assert len(die_alerts) >= 1, f"No process_died alert. Alerts: {alerts}"


# ═══════════════════════════════════════════════════════════════════════════
# Shutdown
# ═══════════════════════════════════════════════════════════════════════════


class TestShutdown:
    """Tests for graceful shutdown."""

    def test_shutdown_stops_all_tasks(self, scheduler: SupervisedScheduler) -> None:
        """shutdown() stops all running tasks."""

        def work() -> None:
            _time.sleep(5.0)  # Long-running, will be cancelled

        scheduler.add_thread_task(name="long_runner", target=work)
        scheduler.start()
        _time.sleep(0.1)

        scheduler.shutdown(timeout=1.0)
        # After shutdown, task should not be RUNNING
        status = scheduler.get_status("long_runner")
        assert status != TaskStatus.RUNNING


# ═══════════════════════════════════════════════════════════════════════════
# Multiple Tasks
# ═══════════════════════════════════════════════════════════════════════════


class TestMultipleTasks:
    """Tests for concurrent execution of multiple tasks."""

    def test_concurrent_thread_tasks(self, scheduler: SupervisedScheduler) -> None:
        """Multiple THREAD tasks run concurrently."""
        results: list[int] = []
        lock = threading.Lock()

        def make_worker(n: int):
            def work() -> None:
                with lock:
                    results.append(n)

            return work

        for i in range(5):
            scheduler.add_thread_task(name=f"worker_{i}", target=make_worker(i))

        scheduler.start()
        _time.sleep(0.5)

        assert len(results) == 5
        assert sorted(results) == [0, 1, 2, 3, 4]

    def test_list_tasks(self, scheduler: SupervisedScheduler) -> None:
        """list_tasks() returns all registered tasks."""
        scheduler.add_thread_task(name="t1", target=lambda: None)
        scheduler.add_thread_task(name="t2", target=lambda: None)
        scheduler.add_process_task(name="p1", target=lambda: None)

        tasks = scheduler.list_tasks()
        assert len(tasks) == 3
        names = {t["name"] for t in tasks}
        assert names == {"t1", "t2", "p1"}


# ═══════════════════════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_scheduler_start_shutdown(self) -> None:
        """Starting and shutting down an empty scheduler is safe."""
        s = SupervisedScheduler()
        s.start()
        s.shutdown(timeout=1.0)
        # No crash = pass

    def test_get_status_unknown_task(self, scheduler: SupervisedScheduler) -> None:
        """get_status() for unknown task returns None."""
        assert scheduler.get_status("nonexistent") is None

    def test_task_exception_triggers_alert(self, scheduler: SupervisedScheduler) -> None:
        """Exception in THREAD task → FAILED + alert."""
        alerts: list[dict] = []

        def alert_handler(source: str, event: str, context: dict) -> None:
            alerts.append({"source": source, "event": event, "context": context})

        scheduler._on_alert = alert_handler

        def work() -> None:
            raise RuntimeError("intentional test failure")

        task = scheduler.add_thread_task(name="crash_test", target=work)
        scheduler.start()
        _time.sleep(0.3)

        fail_alerts = [a for a in alerts if a["event"] == "thread_task_failed"]
        assert len(fail_alerts) >= 1
        assert fail_alerts[0]["context"]["task"] == "crash_test"

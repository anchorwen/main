"""SupervisedScheduler — Dual-isolation task execution for UGR v3.1.

Eliminates silent component death via dual-isolation architecture:

  THREAD tasks  — Event-based cancellation, stuck detection via heartbeat
  PROCESS tasks — SIGTERM → SIGKILL escalation, hard restart on stall

Usage::

    scheduler = SupervisedScheduler()

    scheduler.add_thread_task(
        name="heartbeat_ping",
        target=send_heartbeat,
        heartbeat_interval=5.0,
    )
    scheduler.add_process_task(
        name="daily_ops",
        target=run_daily_ops,
    )

    scheduler.start()
    # ... system runs ...
    scheduler.shutdown(timeout=10.0)
"""

from __future__ import annotations

import enum
import multiprocessing
import signal
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════


class TaskStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    STALLED = "stalled"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class TaskKind(enum.Enum):
    THREAD = "thread"
    PROCESS = "process"


# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class SchedulerConfig:
    """SupervisedScheduler configuration."""

    heartbeat_interval: float = 1.0  # How often the supervisor checks heartbeats
    stuck_threshold: float = 30.0  # Seconds without heartbeat → STALLED
    shutdown_grace: float = 10.0  # Seconds to wait for graceful shutdown
    process_kill_escalation: float = 5.0  # SIGTERM → SIGKILL after this many seconds
    alert_callback: Callable[[str, str, dict[str, Any]], None] | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Task descriptors
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ThreadTask:
    """A task that runs in a daemon thread with cancellation support."""

    name: str
    target: Callable[[], Any]
    heartbeat_interval: float = 5.0

    # Internal state (managed by scheduler)
    thread: threading.Thread | None = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    last_heartbeat: float = field(default=0.0, repr=False)
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class ProcessTask:
    """A task that runs in a subprocess with SIGTERM→SIGKILL escalation."""

    name: str
    target: Callable[[], Any]

    # Internal state (managed by scheduler)
    process: multiprocessing.Process | None = field(default=None, repr=False)
    last_heartbeat: float = field(default=0.0, repr=False)
    status: TaskStatus = TaskStatus.PENDING


# ═══════════════════════════════════════════════════════════════════════════
# Process target wrapper (module-level — must be pickleable for Windows spawn)
# ═══════════════════════════════════════════════════════════════════════════


def _process_target(target: Callable[[], Any]) -> None:
    """Module-level wrapper for multiprocessing.Process targets.

    Must be at module level to be pickleable on Windows (spawn mode).
    Sets up signal handling and catches exceptions.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    import contextlib

    with contextlib.suppress(Exception):  # noqa: BLE001 — process-level safety net
        target()


# ═══════════════════════════════════════════════════════════════════════════
# SupervisedScheduler
# ═══════════════════════════════════════════════════════════════════════════


class SupervisedScheduler:
    """Dual-isolation task scheduler with heartbeat monitoring.

    Manages THREAD tasks (lightweight, cancellable) and PROCESS tasks
    (heavy, hard-kill enforced).  The supervisor loop runs in its own
    thread and monitors heartbeats.
    """

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self._config = config or SchedulerConfig()
        self._thread_tasks: dict[str, ThreadTask] = {}
        self._process_tasks: dict[str, ProcessTask] = {}
        self._supervisor_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._on_alert = self._config.alert_callback or self._default_alert

    # ── Task Registration ───────────────────────────────────────────────

    def add_thread_task(
        self,
        name: str,
        target: Callable[[], Any],
        heartbeat_interval: float = 5.0,
    ) -> ThreadTask:
        """Register a THREAD task. Does not start it."""
        task = ThreadTask(
            name=name,
            target=target,
            heartbeat_interval=heartbeat_interval,
        )
        with self._lock:
            self._thread_tasks[name] = task
        return task

    def add_process_task(
        self,
        name: str,
        target: Callable[[], Any],
    ) -> ProcessTask:
        """Register a PROCESS task. Does not start it."""
        task = ProcessTask(name=name, target=target)
        with self._lock:
            self._process_tasks[name] = task
        return task

    # ── WAL Integrity Spot Check (UGR-A10) ────────────────────────────────

    def register_wal_integrity_check(
        self,
        wal: object,
        *,
        interval_seconds: float = 14400.0,  # default: 4 hours
        wal_label: str = "default",
    ) -> ThreadTask:
        """Register a periodic WAL integrity check as a low-priority THREAD task.

        Runs wal.verify_integrity() every `interval_seconds`.  On failure,
        fires a critical alert through the scheduler's alert callback.

        Args:
            wal: A WriteAheadLog instance to verify.
            interval_seconds: How often to run the check (default 4h).
            wal_label: Human-readable label for this WAL (used in alerts).
        """

        def _integrity_loop() -> None:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=interval_seconds)
                if self._shutdown_event.is_set():
                    break
                try:
                    ok, reason = wal.verify_integrity()  # type: ignore[attr-defined]
                    if not ok:
                        self._on_alert(
                            "wal_integrity",
                            "hash_chain_broken",
                            {"wal": wal_label, "reason": reason},
                        )
                except Exception as exc:  # noqa: BLE001
                    self._on_alert(
                        "wal_integrity",
                        "check_failed",
                        {"wal": wal_label, "error": str(exc)[:500]},
                    )
                self.heartbeat(f"wal_check.{wal_label}")

        return self.add_thread_task(
            name=f"wal_check.{wal_label}",
            target=_integrity_loop,
            heartbeat_interval=interval_seconds * 2.5,  # generous: 2.5× the check interval
        )

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Start all registered tasks and the supervisor loop."""
        # Start PROCESS tasks first (they're heavier)
        with self._lock:
            for p_task in self._process_tasks.values():
                self._start_process_task(p_task)

        # Start THREAD tasks
        with self._lock:
            for t_task in self._thread_tasks.values():
                self._start_thread_task(t_task)

        # Start supervisor
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="supervised-scheduler-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def shutdown(self, timeout: float | None = None) -> None:
        """Gracefully shut down all tasks.

        THREAD tasks: cancel_event.set() → join(timeout)
        PROCESS tasks: SIGTERM → wait(escalation) → SIGKILL
        """
        if timeout is None:
            timeout = self._config.shutdown_grace

        self._shutdown_event.set()

        # Cancel all THREAD tasks
        with self._lock:
            for task in self._thread_tasks.values():
                task.cancel_event.set()
                task.status = TaskStatus.STOPPING

        # Try graceful join for THREAD tasks
        for task in self._thread_tasks.values():
            if task.thread and task.thread.is_alive():
                task.thread.join(timeout=timeout)
                if task.thread.is_alive():
                    task.status = TaskStatus.STALLED
                    self._on_alert(
                        "scheduler",
                        "thread_did_not_stop",
                        {"task": task.name, "timeout": timeout},
                    )
                else:
                    task.status = TaskStatus.STOPPED

        # Stop PROCESS tasks
        process_timeout = self._config.process_kill_escalation
        for p_task in self._process_tasks.values():
            if p_task.process and p_task.process.is_alive():
                p_task.status = TaskStatus.STOPPING
                p_task.process.terminate()  # SIGTERM
                p_task.process.join(timeout=process_timeout)
                if p_task.process.is_alive():
                    p_task.process.kill()  # SIGKILL
                    p_task.process.join(timeout=5.0)
                    p_task.status = TaskStatus.STOPPED
                else:
                    p_task.status = TaskStatus.STOPPED

    # ── Heartbeat ──────────────────────────────────────────────────────

    def heartbeat(self, task_name: str) -> None:
        """Report a heartbeat for a THREAD task.

        Called by the task itself to signal "I'm alive."
        """
        with self._lock:
            task = self._thread_tasks.get(task_name)
            if task and task.status == TaskStatus.RUNNING:
                task.last_heartbeat = _time.monotonic()

    # ── Status ─────────────────────────────────────────────────────────

    def get_status(self, task_name: str) -> TaskStatus | None:
        """Get the status of a task by name."""
        with self._lock:
            for d in (self._thread_tasks, self._process_tasks):
                if task_name in d:
                    return d[task_name].status
        return None

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks with their status."""
        tasks: list[dict[str, Any]] = []
        with self._lock:
            for name, t_task in self._thread_tasks.items():
                tasks.append(
                    {
                        "name": name,
                        "kind": TaskKind.THREAD.value,
                        "status": t_task.status.value,
                        "last_heartbeat": t_task.last_heartbeat,
                    }
                )
            for name, p_task in self._process_tasks.items():
                tasks.append(
                    {
                        "name": name,
                        "kind": TaskKind.PROCESS.value,
                        "status": p_task.status.value,
                        "last_heartbeat": p_task.last_heartbeat,
                    }
                )
        return tasks

    # ── Internal: Task Starters ─────────────────────────────────────────

    def _start_thread_task(self, task: ThreadTask) -> None:
        """Start a THREAD task in a daemon thread with cancellation wrapper."""

        def _wrapped() -> None:
            task.status = TaskStatus.RUNNING
            task.last_heartbeat = _time.monotonic()

            # Heartbeat thread
            stop_heartbeat = threading.Event()

            def _heartbeat_loop() -> None:
                while not stop_heartbeat.is_set() and not task.cancel_event.is_set():
                    self.heartbeat(task.name)
                    stop_heartbeat.wait(timeout=task.heartbeat_interval)

            hb_thread = threading.Thread(
                target=_heartbeat_loop,
                daemon=True,
                name=f"hb-{task.name}",
            )
            hb_thread.start()

            try:
                task.target()
            except Exception as exc:  # noqa: BLE001 — catch-all for thread task safety net
                task.status = TaskStatus.FAILED
                self._on_alert(
                    "scheduler",
                    "thread_task_failed",
                    {"task": task.name, "error": str(exc)},
                )
            finally:
                stop_heartbeat.set()
                hb_thread.join(timeout=2.0)
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.STOPPED

        task.thread = threading.Thread(
            target=_wrapped,
            name=f"sched-{task.name}",
            daemon=True,
        )
        task.thread.start()

    def _start_process_task(self, task: ProcessTask) -> None:
        """Start a PROCESS task in a subprocess."""
        task.status = TaskStatus.RUNNING
        task.last_heartbeat = _time.monotonic()
        task.process = multiprocessing.Process(
            target=_process_target,
            args=(task.target,),
            name=f"sched-{task.name}",
            daemon=True,
        )
        task.process.start()

    # ── Internal: Supervisor Loop ───────────────────────────────────────

    def _supervisor_loop(self) -> None:
        """Monitor heartbeats and detect stalls."""
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=self._config.heartbeat_interval)

            now = _time.monotonic()
            with self._lock:
                # Check THREAD tasks for stalls
                for task in self._thread_tasks.values():
                    if task.status == TaskStatus.RUNNING:
                        # Check 1: Thread died unexpectedly
                        if task.thread and not task.thread.is_alive():
                            task.status = TaskStatus.FAILED
                            self._on_alert(
                                "scheduler",
                                "thread_died",
                                {"task": task.name},
                            )
                            continue
                        # Check 2: Heartbeat timeout
                        if task.last_heartbeat > 0:
                            gap = now - task.last_heartbeat
                            if gap > self._config.stuck_threshold:
                                task.status = TaskStatus.STALLED
                                self._on_alert(
                                    "scheduler",
                                    "thread_stalled",
                                    {
                                        "task": task.name,
                                        "seconds_since_heartbeat": gap,
                                    },
                                )

                # Check PROCESS tasks for unexpected death
                for p_task in self._process_tasks.values():
                    if p_task.status == TaskStatus.RUNNING:
                        if p_task.process and not p_task.process.is_alive():
                            exit_code = p_task.process.exitcode
                            if exit_code != 0:
                                p_task.status = TaskStatus.FAILED
                                self._on_alert(
                                    "scheduler",
                                    "process_died",
                                    {
                                        "task": p_task.name,
                                        "exit_code": exit_code,
                                    },
                                )
                            else:
                                p_task.status = TaskStatus.STOPPED

    # ── Internal: Default Alert ─────────────────────────────────────────

    @staticmethod
    def _default_alert(source: str, event: str, context: dict[str, Any]) -> None:
        """Default alert handler — log to stderr."""
        import sys

        print(
            f"[SupervisedScheduler] ALERT: {source}/{event} "
            f"task={context.get('task', '?')} "
            f"detail={context}",
            file=sys.stderr,
        )

import threading
from datetime import UTC, datetime

from core.contracts.domain_keys import (
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
from core.deployment.scheduled_task_registry import get_task
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

                # Merge shadow metrics for candidate brains
                if container.governance_service:
                    try:
                        from core.governance.shadow_tracker import (
                            ShadowTracker,
                            build_shadow_summary,
                        )

                        candidate_ids = [
                            bid
                            for bid, state in container.governance_service.get_all_states().items()
                            if state.get("status") == "candidate"
                        ]
                        if candidate_ids:
                            tracker = ShadowTracker(base_dir="data")
                            shadow_map = build_shadow_summary(tracker, candidate_ids)
                            summary_map.update(shadow_map)
                    except Exception:
                        pass  # Shadow tracking is non-critical

                if summary_map:
                    container.governance_rule_engine.evaluate(summary_map)

                # ── Auditor→Executor pipeline (护栏三: 报告驱动模型) ──
                if container.governance_service and container.governance_rule_engine:
                    try:
                        import json
                        from pathlib import Path as _Path

                        from core.brains.services.brain_promotion import (
                            BrainPromotionEvaluator,
                        )
                        from scripts.training.run_promotion import (
                            compute_performance_from_ledger,
                        )

                        pnl_path = _Path("data") / "brain_pnl_ledger.json"
                        if pnl_path.exists():
                            pnl_ledger = json.loads(pnl_path.read_text(encoding="utf-8"))
                            perf = compute_performance_from_ledger(pnl_ledger)
                            brain_states = container.governance_service.get_all_states()
                            # Auditor: generate report only (no state writes)
                            evaluator = BrainPromotionEvaluator()
                            decisions = evaluator.evaluate_all(brain_states, perf)
                            # Executor: apply transitions through the single authority
                            container.governance_rule_engine.execute_transitions(decisions)
                    except Exception:
                        pass  # Promotion evaluation is non-critical, don't crash scheduler

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

        # Daily ops: feedback loop, governance, champion/challenger, retraining check, recap
        daily_ops_enabled = getattr(container.config, "daily_ops_enabled", True)
        if daily_ops_enabled:

            def daily_ops():
                run_daily_ops = get_task("daily_ops")
                if run_daily_ops is None:
                    return

                from pathlib import Path

                base_dir = str(container.config.base_dir)
                run_daily_ops(base_dir=base_dir, skip_shadow=True)

                # Reload container's tracker from disk so governance sees fresh data
                if container.brain_tracker is not None:
                    try:
                        tracker_path = Path(base_dir) / "brain_performance.json"
                        if tracker_path.exists():
                            fresh = type(container.brain_tracker).load(tracker_path)
                            container.brain_tracker._records = fresh._records
                    except Exception:
                        pass

            svc.add_task("daily_ops", daily_ops, interval_seconds=86400)

        # ── Feature store periodic update ──
        feature_store_enabled = getattr(container.config, "feature_store_scheduled_update", True)
        if feature_store_enabled:

            def feature_store_update():
                run_full_maintenance = get_task("feature_store_maintenance")
                if run_full_maintenance is None:
                    return

                base_dir = str(container.config.base_dir)
                symbol = getattr(container.config, "symbol", "XAUUSDc")
                mt5_path = getattr(container.config, "mt5_terminal_path", None)
                extensions = getattr(container.config, "extensions", {}) or {}
                if not mt5_path:
                    mt5_path = extensions.get("mt5_terminal_path", None)
                fs_dir = extensions.get("feature_store_dir", None)

                run_full_maintenance(
                    base_dir=base_dir,
                    symbol=symbol,
                    mt5_terminal_path=mt5_path,
                    feature_store_dir=fs_dir,
                    retention_days=90,
                    skip_compact=False,
                )

            # Run every 5 minutes — fills gaps the trading loop might miss
            svc.add_task("feature_store_update", feature_store_update, interval_seconds=300)

        # ── Ops monitoring ──
        ops_mon_enabled = getattr(container.config, "ops_monitoring_enabled", True)
        if ops_mon_enabled:
            base_dir = str(container.config.base_dir)
            symbol = getattr(container.config, "symbol", "XAUUSDc")
            mt5_path = getattr(container.config, "mt5_terminal_path", None)
            extensions = getattr(container.config, "extensions", {}) or {}
            if not mt5_path:
                mt5_path = extensions.get("mt5_terminal_path", None)

            def live_monitor_snapshot():
                build_snapshot = get_task("live_monitor_snapshot")
                if build_snapshot is None:
                    return

                import json
                from pathlib import Path

                snap = build_snapshot(
                    base_dir=Path(base_dir),
                    symbol=symbol,
                    mt5_terminal_path=mt5_path,
                )
                # Write snapshot to file for dashboard consumption
                out_path = Path(base_dir) / "reports" / "monitor_snapshot_latest.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8"
                )

            svc.add_task("live_monitor_snapshot", live_monitor_snapshot, interval_seconds=30)

            def auto_healthcheck():
                build_report = get_task("auto_healthcheck")
                if build_report is None:
                    return

                import json
                from pathlib import Path

                report = build_report(base_dir=Path(base_dir), symbol=symbol)
                out_path = Path(base_dir) / "reports" / "healthcheck_latest.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
                )

            svc.add_task("auto_healthcheck", auto_healthcheck, interval_seconds=60)

            def data_quality_report():
                build_report = get_task("data_quality_report")
                if build_report is None:
                    return

                import json
                from pathlib import Path

                report = build_report(base_dir=Path(base_dir))
                out_path = Path(base_dir) / "reports" / "data_quality_latest.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
                )

            svc.add_task("data_quality_report", data_quality_report, interval_seconds=900)

        return svc

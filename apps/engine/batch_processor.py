from core.observability.metric_names import BATCH_ERRORS, BATCH_TOTAL_TRIGGERS


class BatchProcessor:
    """Runs multiple decision cycles in batch and collects results.

    Useful for backtesting, end-of-day processing, and bulk
    replay workflows.
    """

    def __init__(self, orchestrator, *, metrics=None, event_bus=None):
        self._orchestrator = orchestrator
        self._metrics = metrics
        self._event_bus = event_bus

    def run_batch(self, triggers: list[dict], feature_source: dict) -> dict:
        results = []
        errors = []
        for i, trigger in enumerate(triggers):
            try:
                outcome = self._orchestrator.run_cycle(trigger, feature_source)
                results.append(
                    {
                        "index": i,
                        "trigger": trigger,
                        "cycle_id": outcome.cycle_id,
                        "verdict_allowed": outcome.decision_result.verdict.is_allowed()
                        if outcome.decision_result
                        else None,
                        "status": "completed",
                    }
                )
            except Exception as exc:  # BLE001:REVIEWED
                errors.append({"index": i, "trigger": trigger, "error": str(exc)})
                results.append(
                    {"index": i, "trigger": trigger, "status": "error", "error": str(exc)}
                )

        if self._metrics:
            self._metrics.inc(BATCH_TOTAL_TRIGGERS, len(triggers))
            self._metrics.inc(BATCH_ERRORS, len(errors))

        if self._event_bus:
            self._event_bus.publish(
                "batch.completed",
                {
                    "total": len(triggers),
                    "completed": len(triggers) - len(errors),
                    "errors": len(errors),
                },
            )

        return {
            "total": len(triggers),
            "completed": len(triggers) - len(errors),
            "errors": len(errors),
            "results": results,
            "error_details": errors,
        }

    def process_venue_events_batch(self, events: list[dict]) -> dict:
        results = []
        errors = []
        for i, event in enumerate(events):
            try:
                r = self._orchestrator.process_execution_event(**event)
                results.append({"index": i, "status": "processed", **r})
            except Exception as exc:  # BLE001:REVIEWED
                errors.append({"index": i, "error": str(exc), "event": event})

        return {
            "total": len(events),
            "processed": len(events) - len(errors),
            "errors": len(errors),
            "results": results,
            "error_details": errors,
        }

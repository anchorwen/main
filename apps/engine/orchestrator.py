from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.observability.metric_names import (
    CYCLES_ALLOWED,
    CYCLES_BLOCKED,
    CYCLES_CIRCUIT_OPEN,
    CYCLES_ERRORS,
    CYCLES_THROTTLED,
    CYCLES_TOTAL,
    venue_events_metric,
)
from core.observability.tracing import TracingContext, set_current_context, clear_current_context


@dataclass
class CycleOutcome:
    cycle_id: str
    trigger: dict
    decision_result: Any
    execution_result: dict | None = None
    feedback_result: dict | None = None
    governance_actions: list = field(default_factory=list)
    audit_entries: list = field(default_factory=list)
    trace_summary: dict | None = None


class DecisionCycleOrchestrator:
    """Top-level orchestrator that drives the full closed loop:

    1. RuntimeLoop decision cycle (features → brains → parliament → risk → dispatch)
    2. Order registration in ExecutionManager
    3. Audit + metrics recording
    4. Post-execution: feedback processing + governance actions

    Step 4 runs asynchronously when venue events arrive via
    ``process_execution_event()``.
    """

    def __init__(
        self,
        runtime_loop,
        *,
        execution_manager=None,
        position_tracker=None,
        market_context=None,
        feedback_loop=None,
        governance_service=None,
        audit_log=None,
        metrics=None,
        circuit_breaker=None,
        rate_limiter=None,
    ):
        self._loop = runtime_loop
        self._execution_manager = execution_manager
        self._position_tracker = position_tracker
        self._market_context = market_context
        self._feedback_loop = feedback_loop
        self._governance = governance_service
        self._audit = audit_log
        self._metrics = metrics
        self._circuit_breaker = circuit_breaker
        self._rate_limiter = rate_limiter
        self._pending_cycles: dict[str, dict] = {}

    def run_cycle(self, trigger: dict, feature_source: dict) -> CycleOutcome:
        from core.contracts.ids import new_record_id
        cycle_id = new_record_id()
        trace = TracingContext()
        set_current_context(trace)
        root = trace.start_span("decision_cycle")
        root.set_attribute("trigger.symbol", trigger.get("symbol", "unknown"))

        def _finish(outcome: CycleOutcome) -> CycleOutcome:
            trace.end_span(root)
            clear_current_context()
            outcome.trace_summary = trace.get_trace_summary()
            return outcome

        if self._rate_limiter and not self._rate_limiter.allow():
            if self._metrics:
                self._metrics.inc(CYCLES_THROTTLED)
            root.add_event("throttled")
            return _finish(CycleOutcome(
                cycle_id=cycle_id, trigger=trigger, decision_result=None,
                audit_entries=[{"event_type": "throttled", "reason": "rate_limit_exceeded"}],
            ))

        if self._circuit_breaker and not self._circuit_breaker.allow_request():
            if self._metrics:
                self._metrics.inc(CYCLES_CIRCUIT_OPEN)
            root.add_event("circuit_open")
            return _finish(CycleOutcome(
                cycle_id=cycle_id, trigger=trigger, decision_result=None,
                audit_entries=[{"event_type": "circuit_open", "reason": "dispatch_circuit_open"}],
            ))

        try:
            decision_span = trace.start_span("runtime_loop")
            result = self._loop.run_decision_cycle(trigger, feature_source)
            trace.end_span(decision_span)
        except Exception as exc:
            if self._metrics:
                self._metrics.inc(CYCLES_ERRORS)
            if self._audit:
                self._audit.log(
                    event_type="cycle_error", severity="error",
                    actor="orchestrator", detail={"error": str(exc)},
                )
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
            root.set_error(str(exc))
            return _finish(CycleOutcome(
                cycle_id=cycle_id, trigger=trigger, decision_result=None,
                audit_entries=[{"event_type": "cycle_error", "error": str(exc)}],
            ))

        if self._metrics:
            self._metrics.inc(CYCLES_TOTAL)
            if result.verdict.is_allowed():
                self._metrics.inc(CYCLES_ALLOWED)
            else:
                self._metrics.inc(CYCLES_BLOCKED)

        audit_entries = []
        if self._audit:
            entry = self._audit.log_decision(
                intent_id=result.intent.intent_id,
                verdict_status=result.verdict.status.value if hasattr(result.verdict.status, "value") else str(result.verdict.status),
                symbol=result.intent.symbol,
                action=result.intent.action.value if hasattr(result.intent.action, "value") else str(result.intent.action),
                risk_tier=result.verdict.risk_tier,
            )
            audit_entries.append(entry)

            if result.verdict.blocking_reasons:
                audit_entries.append(self._audit.log_risk_verdict(
                    intent_id=result.intent.intent_id,
                    status=result.verdict.status.value if hasattr(result.verdict.status, "value") else str(result.verdict.status),
                    risk_tier=result.verdict.risk_tier,
                    blocking_reasons=result.verdict.blocking_reasons,
                ))

        execution_result = None
        if result.verdict.is_allowed() and result.communication_record and self._execution_manager:
            if self._circuit_breaker:
                dr = result.dispatch_result
                failed = hasattr(dr, "status") and str(getattr(dr.status, "value", dr.status)) == "failed"
                if failed:
                    self._circuit_breaker.record_failure()
                else:
                    self._circuit_breaker.record_success()
            msg_id = result.communication_record.message_id
            record_id = result.record.record_id
            execution_result = self._execution_manager.register_order(
                message_id=msg_id,
                correlation_id=record_id,
                symbol=result.intent.symbol,
                side=result.intent.side.value if hasattr(result.intent.side, "value") else str(result.intent.side),
                quantity=result.intent.suggested_risk_fraction or 1.0,
            )
            self._pending_cycles[msg_id] = {
                "cycle_id": cycle_id,
                "record_id": record_id,
                "intent": result.intent,
                "candidate": result.candidate,
                "symbol": result.intent.symbol,
            }

        return _finish(CycleOutcome(
            cycle_id=cycle_id,
            trigger=trigger,
            decision_result=result,
            execution_result=execution_result,
            audit_entries=audit_entries,
        ))

    def process_execution_event(
        self,
        *,
        message_id: str,
        event_type: str,
        venue: str = "unknown",
        venue_order_id: str | None = None,
        filled_quantity: float = 0,
        price: float = 0,
    ) -> dict:
        results = {"message_id": message_id, "event_type": event_type}

        if self._execution_manager:
            exec_result = self._execution_manager.process_venue_event(
                message_id=message_id,
                event_type=event_type,
                venue_order_id=venue_order_id,
                filled_quantity=filled_quantity,
                price=price,
                venue=venue,
            )
            results["execution"] = exec_result

        if self._metrics:
            self._metrics.inc(venue_events_metric(event_type))

        if self._audit and event_type in {"filled", "rejected", "cancelled"}:
            self._audit.log(
                event_type="execution_terminal",
                severity="info" if event_type == "filled" else "warning",
                actor="venue",
                subject=message_id,
                detail={"venue_event": event_type, "filled_quantity": filled_quantity, "price": price},
            )

        terminal_types = {"filled", "rejected", "cancelled", "expired"}
        if event_type in terminal_types:
            feedback_result = self._run_feedback(message_id)
            results["feedback"] = feedback_result
            if feedback_result:
                governance_actions = self._run_governance(feedback_result)
                results["governance_actions"] = governance_actions

        return results

    def _run_feedback(self, message_id: str) -> dict | None:
        if self._feedback_loop is None:
            return None
        cycle_info = self._pending_cycles.get(message_id)
        if cycle_info is None:
            return None

        intent = cycle_info["intent"]
        candidate = cycle_info["candidate"]
        record_id = cycle_info["record_id"]
        symbol = cycle_info["symbol"]

        market_ctx = {}
        if self._market_context:
            market_ctx = self._market_context.get_context(symbol)

        return self._feedback_loop.process_decision_outcome(
            date_key=datetime.utcnow().strftime("%Y-%m-%d"),
            target="exec_bridge",
            message_id=message_id,
            correlation_id=record_id,
            intended_action=intent.action.value if hasattr(intent.action, "value") else str(intent.action),
            intended_side=intent.side.value if hasattr(intent.side, "value") else str(intent.side),
            intended_quantity=intent.suggested_risk_fraction or 1.0,
            supporting_brain_ids=list(candidate.supporting_brains),
            opposing_brain_ids=list(candidate.opposing_brains),
            market_context=market_ctx,
        )

    def _run_governance(self, feedback_result: dict) -> list[dict]:
        if self._governance is None:
            return []
        signals = feedback_result.get("governance_signals", [])
        if not signals:
            return []
        return self._governance.process_feedback_signals(signals)

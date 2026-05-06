from core.deployment.domain_keys import (
    DISPATCH_STATUS_TRANSPORT_DELIVERED,
    MISMATCH_TYPE_QUANTITY,
    MISMATCH_TYPE_STATE,
    PAYLOAD_KEY_ACTUAL,
    PAYLOAD_KEY_BREACHED_MESSAGE_IDS,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_DELTA,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_DISPATCH,
    PAYLOAD_KEY_DISPATCH_STATUS,
    PAYLOAD_KEY_ENVELOPE,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVENT_TYPES,
    PAYLOAD_KEY_EXECUTION_TERMINAL,
    PAYLOAD_KEY_FILLED_QUANTITY,
    PAYLOAD_KEY_INTENDED,
    PAYLOAD_KEY_INTENDED_QUANTITY,
    PAYLOAD_KEY_IS_TERMINAL,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MESSAGE_RESULTS,
    PAYLOAD_KEY_MISMATCHES,
    PAYLOAD_KEY_PAYLOAD,
    PAYLOAD_KEY_QUANTITY,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_TERMINAL_EVENT_TYPE,
    PAYLOAD_KEY_TOTAL_FILLED_QUANTITY,
    PAYLOAD_KEY_TOTAL_INTENDED_QUANTITY,
    PAYLOAD_KEY_TYPE,
    PAYLOAD_KEY_UNMATCHED_MESSAGE_IDS,
    RECONCILIATION_STATUS_BREACHED,
    RECONCILIATION_STATUS_MATCHED,
    RECONCILIATION_STATUS_PARTIAL,
    RECONCILIATION_STATUS_STALE,
    RECONCILIATION_STATUS_UNMATCHED,
    REPLAY_TRACE_SCOPE_CORRELATION,
    REPLAY_TRACE_SCOPE_MESSAGE,
    TERMINAL_EVENT_CANCELLED,
    TERMINAL_EVENT_EXPIRED,
    TERMINAL_EVENT_FILLED,
    TERMINAL_EVENT_REJECTED,
)
from core.observability.metric_names import (
    RECONCILIATION_BREACHED,
    RECONCILIATION_MATCHED,
    RECONCILIATION_PARTIAL,
    RECONCILIATION_UNMATCHED,
)


class ExecutionReconciliationService:
    """Reconciles communication dispatch records against downstream execution events.

    Produces a per-message reconciliation verdict that tells the caller
    whether the dispatched intent and the downstream execution are in a
    consistent state, and surfaces any quantity/price/state mismatches.
    """

    STATUS_MATCHED = RECONCILIATION_STATUS_MATCHED
    STATUS_UNMATCHED = RECONCILIATION_STATUS_UNMATCHED
    STATUS_PARTIAL = RECONCILIATION_STATUS_PARTIAL
    STATUS_BREACHED = RECONCILIATION_STATUS_BREACHED
    STATUS_STALE = RECONCILIATION_STATUS_STALE

    def __init__(self, communication_reader, execution_event_reader, metrics=None):
        self._communication_reader = communication_reader
        self._execution_event_reader = execution_event_reader
        self._metrics = metrics

    def reconcile_message(
        self, *, date_key: str, target: str, message_id: str, correlation_id: str
    ) -> dict:
        record = self._communication_reader.find_by_message_id(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        timeline = self._execution_event_reader.build_execution_timeline(
            date_key=date_key,
            correlation_id=correlation_id,
            message_id=message_id,
        )
        return self._build_reconciliation_result(record, timeline, message_id=message_id)

    def reconcile_correlation(
        self, *, date_key: str, target: str, correlation_id: str, message_ids: list[str]
    ) -> dict:
        message_results = []
        for message_id in message_ids:
            msg_result = self.reconcile_message(
                date_key=date_key,
                target=target,
                message_id=message_id,
                correlation_id=correlation_id,
            )
            message_results.append(msg_result)

        statuses = [r[PAYLOAD_KEY_STATUS] for r in message_results]
        self._inc_reconciliation_metrics(statuses)
        if all(s == self.STATUS_MATCHED for s in statuses):
            overall = self.STATUS_MATCHED
        elif any(s == self.STATUS_BREACHED for s in statuses):
            overall = self.STATUS_BREACHED
        elif any(s == self.STATUS_UNMATCHED for s in statuses):
            overall = self.STATUS_UNMATCHED
        elif any(s == self.STATUS_PARTIAL for s in statuses):
            overall = self.STATUS_PARTIAL
        else:
            overall = self.STATUS_STALE

        return {
            PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_CORRELATION,
            PAYLOAD_KEY_CORRELATION_ID: correlation_id,
            PAYLOAD_KEY_STATUS: overall,
            PAYLOAD_KEY_MESSAGE_COUNT: len(message_ids),
            PAYLOAD_KEY_MESSAGE_RESULTS: message_results,
            PAYLOAD_KEY_STATUS_COUNTS: self._count_statuses(statuses),
            PAYLOAD_KEY_TOTAL_INTENDED_QUANTITY: sum(
                r.get(PAYLOAD_KEY_INTENDED_QUANTITY, 0) for r in message_results
            ),
            PAYLOAD_KEY_TOTAL_FILLED_QUANTITY: sum(
                r.get(PAYLOAD_KEY_FILLED_QUANTITY, 0) for r in message_results
            ),
            PAYLOAD_KEY_BREACHED_MESSAGE_IDS: [
                r[PAYLOAD_KEY_MESSAGE_ID]
                for r in message_results
                if r[PAYLOAD_KEY_STATUS] == self.STATUS_BREACHED
            ],
            PAYLOAD_KEY_UNMATCHED_MESSAGE_IDS: [
                r[PAYLOAD_KEY_MESSAGE_ID]
                for r in message_results
                if r[PAYLOAD_KEY_STATUS] == self.STATUS_UNMATCHED
            ],
        }

    def _build_reconciliation_result(
        self, record: dict | None, timeline: dict, *, message_id: str
    ) -> dict:
        intended_qty = self._extract_intended_quantity(record)
        filled_qty = timeline.get(PAYLOAD_KEY_TOTAL_FILLED_QUANTITY, 0)
        terminal_type = timeline.get(PAYLOAD_KEY_TERMINAL_EVENT_TYPE)
        is_terminal = timeline.get(PAYLOAD_KEY_IS_TERMINAL, False)
        event_count = timeline.get(PAYLOAD_KEY_EVENT_COUNT, 0)

        status = self._determine_status(
            record=record,
            terminal_type=terminal_type,
            is_terminal=is_terminal,
            event_count=event_count,
            intended_qty=intended_qty,
            filled_qty=filled_qty,
        )

        mismatches = self._detect_mismatches(
            record=record,
            timeline=timeline,
            intended_qty=intended_qty,
            filled_qty=filled_qty,
        )

        return {
            PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_MESSAGE,
            PAYLOAD_KEY_MESSAGE_ID: message_id,
            PAYLOAD_KEY_STATUS: status,
            PAYLOAD_KEY_INTENDED_QUANTITY: intended_qty,
            PAYLOAD_KEY_FILLED_QUANTITY: filled_qty,
            PAYLOAD_KEY_TERMINAL_EVENT_TYPE: terminal_type,
            PAYLOAD_KEY_IS_TERMINAL: is_terminal,
            PAYLOAD_KEY_EVENT_COUNT: event_count,
            PAYLOAD_KEY_EVENT_TYPES: timeline.get(PAYLOAD_KEY_EVENT_TYPES, []),
            PAYLOAD_KEY_MISMATCHES: mismatches,
        }

    def _determine_status(
        self,
        *,
        record,
        terminal_type,
        is_terminal,
        event_count,
        intended_qty,
        filled_qty,
    ) -> str:
        if record is None:
            return self.STATUS_UNMATCHED

        if event_count == 0:
            return self.STATUS_UNMATCHED

        if terminal_type == TERMINAL_EVENT_REJECTED:
            return self.STATUS_BREACHED

        if terminal_type in {TERMINAL_EVENT_CANCELLED, TERMINAL_EVENT_EXPIRED}:
            if filled_qty > 0:
                return self.STATUS_PARTIAL
            return self.STATUS_BREACHED

        if terminal_type == TERMINAL_EVENT_FILLED:
            if intended_qty > 0 and filled_qty != intended_qty:
                return self.STATUS_BREACHED
            return self.STATUS_MATCHED

        if not is_terminal:
            if filled_qty > 0:
                return self.STATUS_PARTIAL
            return self.STATUS_STALE

        return self.STATUS_MATCHED

    def _detect_mismatches(self, *, record, timeline, intended_qty, filled_qty) -> list[dict]:
        mismatches = []
        if intended_qty > 0 and filled_qty > 0 and filled_qty != intended_qty:
            mismatches.append(
                {
                    PAYLOAD_KEY_TYPE: MISMATCH_TYPE_QUANTITY,
                    PAYLOAD_KEY_INTENDED: intended_qty,
                    PAYLOAD_KEY_ACTUAL: filled_qty,
                    PAYLOAD_KEY_DELTA: filled_qty - intended_qty,
                }
            )
        if record is not None:
            dispatch_status = record.get(PAYLOAD_KEY_DISPATCH, {}).get(PAYLOAD_KEY_STATUS)
            terminal_type = timeline.get(PAYLOAD_KEY_TERMINAL_EVENT_TYPE)
            if (
                dispatch_status == DISPATCH_STATUS_TRANSPORT_DELIVERED
                and terminal_type == TERMINAL_EVENT_REJECTED
            ):
                mismatches.append(
                    {
                        PAYLOAD_KEY_TYPE: MISMATCH_TYPE_STATE,
                        PAYLOAD_KEY_DISPATCH_STATUS: dispatch_status,
                        PAYLOAD_KEY_EXECUTION_TERMINAL: terminal_type,
                        PAYLOAD_KEY_DETAIL: "dispatch delivered but execution rejected",
                    }
                )
        return mismatches

    def _extract_intended_quantity(self, record: dict | None) -> float:
        if record is None:
            return 0
        payload = record.get(PAYLOAD_KEY_ENVELOPE, {}).get(PAYLOAD_KEY_PAYLOAD, {})
        return payload.get(PAYLOAD_KEY_QUANTITY, 0)

    def _count_statuses(self, statuses: list[str]) -> dict:
        counts = {}
        for s in statuses:
            counts[s] = counts.get(s, 0) + 1
        return counts

    def _inc_reconciliation_metrics(self, statuses: list[str]) -> None:
        if self._metrics is None:
            return
        for s in statuses:
            if s == self.STATUS_MATCHED:
                self._metrics.inc(RECONCILIATION_MATCHED)
            elif s == self.STATUS_BREACHED:
                self._metrics.inc(RECONCILIATION_BREACHED)
            elif s == self.STATUS_UNMATCHED:
                self._metrics.inc(RECONCILIATION_UNMATCHED)
            elif s == self.STATUS_PARTIAL:
                self._metrics.inc(RECONCILIATION_PARTIAL)

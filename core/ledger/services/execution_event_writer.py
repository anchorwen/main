from core.contracts.domain.execution_event import ExecutionEvent
from core.ledger.schema_versions import SCHEMA_EXECUTION_EVENT
from core.contracts.ids import new_execution_event_id
from core.ledger.stream_names import LEDGER_STREAM_EXECUTION_EVENTS


class ExecutionEventWriter:
    def __init__(self, ledger_store):
        self._ledger_store = ledger_store

    def write_event(self, execution_event: ExecutionEvent) -> tuple[ExecutionEvent, object]:
        ledger_path = self._ledger_store.append_record(
            date_key=execution_event.event_time.strftime("%Y-%m-%d"),
            symbol=execution_event.correlation_id,
            record=execution_event,
            stream_name=LEDGER_STREAM_EXECUTION_EVENTS,
        )
        return execution_event, ledger_path

    def write_from_venue_payload(
        self,
        *,
        message_id: str,
        correlation_id: str,
        event_type: str,
        venue: str,
        event_time,
        venue_order_id: str | None = None,
        quantity: dict | None = None,
        price: dict | None = None,
        details: dict | None = None,
    ) -> tuple[ExecutionEvent, object]:
        event = ExecutionEvent(
            schema_version=SCHEMA_EXECUTION_EVENT,
            event_id=new_execution_event_id(),
            message_id=message_id,
            correlation_id=correlation_id,
            event_type=event_type,
            event_time=event_time,
            recorded_at=event_time,
            venue=venue,
            venue_order_id=venue_order_id,
            quantity=quantity or {},
            price=price or {},
            details=details or {},
        )
        return self.write_event(event)

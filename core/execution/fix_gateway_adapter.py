"""Dry-run FIX gateway adapter skeleton."""

from datetime import UTC, datetime
from typing import Any

from core.execution.fix_contracts import FixExecutionReport, FixSessionConfig
from core.execution.fix_execution_mapper import FixExecutionReportMapper
from core.execution.fix_message_builder import FixMessageBuilder
from core.execution.gateway_contracts import OrderRequest, OrderState
from core.execution.order_state_machine import OrderStateMachine


class FixGatewayAdapter:
    """FIX adapter skeleton with no network I/O.

    This adapter provides deterministic dry-run behavior and canonical state
    mapping. Real QuickFIX/broker transports should plug into this boundary.
    """

    def __init__(
        self,
        session_config: FixSessionConfig,
        execution_event_writer=None,
        state_machine: OrderStateMachine | None = None,
    ):
        self._config = session_config
        self._writer = execution_event_writer
        self._state_machine = state_machine or OrderStateMachine()
        self._builder = FixMessageBuilder(session_config)
        self._mapper = FixExecutionReportMapper(self._state_machine)
        self._connected = False
        self._orders: dict[str, OrderState] = {}
        self._outbox: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []

    def connect(self) -> dict[str, Any]:
        self._connected = True
        return {"status": "connected", "venue": self._config.venue, "dry_run": True}

    def disconnect(self) -> dict[str, Any]:
        self._connected = False
        return {"status": "disconnected", "venue": self._config.venue, "dry_run": True}

    def is_connected(self) -> bool:
        return self._connected

    def submit_order(
        self, request: OrderRequest, market: dict[str, Any] | None = None
    ) -> OrderState:
        if not self._connected:
            raise RuntimeError("FIX adapter is not connected")
        if request.order_id in self._orders:
            raise ValueError(f"duplicate order_id: {request.order_id}")
        state = self._state_machine.create(request, self._config.venue)
        self._orders[request.order_id] = state
        message = self._builder.build_new_order_single(request)
        self._outbox.append(message.to_tag_dict())
        self._state_machine.acknowledge(state)
        self._emit(request.order_id, request.correlation_id, "ack", state)
        return state

    def cancel_order(self, order_id: str) -> OrderState:
        if not self._connected:
            raise RuntimeError("FIX adapter is not connected")
        state = self._orders.get(order_id)
        if state is None:
            raise ValueError(f"unknown order_id: {order_id}")
        message = self._builder.build_cancel_request(order_id, state.symbol, state.side)
        self._outbox.append(message.to_tag_dict())
        return state

    def receive_execution_report(self, report: FixExecutionReport | dict[str, Any]) -> OrderState:
        parsed = self._mapper.from_tag_dict(report) if isinstance(report, dict) else report
        state = self._orders.get(parsed.order_id)
        if state is None:
            raise ValueError(f"unknown order_id: {parsed.order_id}")
        self._mapper.apply(state, parsed)
        self._emit(
            state.order_id,
            state.correlation_id,
            self._mapper.execution_event_type(parsed),
            state,
            quantity=parsed.last_qty,
            price=parsed.last_px,
            text=parsed.text,
        )
        return state

    def get_order(self, order_id: str) -> OrderState | None:
        return self._orders.get(order_id)

    def list_orders(self, status: str | None = None) -> list[OrderState]:
        orders = list(self._orders.values())
        if status is not None:
            return [o for o in orders if o.status == status]
        return orders

    def outbox(self) -> list[dict[str, Any]]:
        return list(self._outbox)

    def list_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def _emit(
        self,
        order_id: str,
        correlation_id: str,
        event_type: str,
        state: OrderState,
        quantity: float = 0.0,
        price: float = 0.0,
        text: str | None = None,
    ) -> None:
        payload = {
            "event_type": event_type,
            "order_id": order_id,
            "correlation_id": correlation_id,
            "venue": self._config.venue,
            "quantity": quantity,
            "price": price,
            "text": text,
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        self._events.append(payload)
        if self._writer:
            self._writer.write_from_venue_payload(
                message_id=order_id,
                correlation_id=correlation_id,
                event_type=event_type,
                venue=self._config.venue,
                event_time=datetime.now(UTC).replace(tzinfo=None),
                venue_order_id=order_id,
                quantity={"filled": quantity} if quantity else {},
                price={"average": price} if price else {},
                details={"fix_dry_run": True, "status": state.status, "text": text},
            )

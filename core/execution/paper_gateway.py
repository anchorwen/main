"""Paper execution gateway.

A safe simulated execution adapter that emits the same execution event
lifecycle expected by downstream reconciliation and analytics.
"""

from datetime import UTC, datetime
from typing import Any

from core.execution.fill_simulator import FillSimulator
from core.execution.gateway_contracts import OrderRequest, OrderState
from core.execution.order_state_machine import OrderStateMachine
from core.observability.metric_names import PAPER_EXECUTION_FILL_QUANTITY, PAPER_EXECUTION_FILLED


class PaperExecutionGateway:
    """Paper gateway backed by a canonical state machine and fill simulator."""

    def __init__(
        self,
        execution_event_writer=None,
        metrics=None,
        venue: str = "PAPER",
        fill_simulator: FillSimulator | None = None,
        state_machine: OrderStateMachine | None = None,
    ):
        self._writer = execution_event_writer
        self._metrics = metrics
        self._venue = venue
        self._simulator = fill_simulator or FillSimulator()
        self._state_machine = state_machine or OrderStateMachine()
        self._orders: dict[str, OrderState] = {}
        self._events: list[dict[str, Any]] = []

    def submit_order(self, request: OrderRequest, market: dict[str, Any]) -> OrderState:
        if request.order_id in self._orders:
            raise ValueError(f"duplicate order_id: {request.order_id}")
        state = self._state_machine.create(request, self._venue)
        self._orders[request.order_id] = state
        self._state_machine.acknowledge(state)
        self._emit(request, "ack", state)
        self._state_machine.accept(state)
        self._emit(request, "accepted", state)
        fill = self._simulator.simulate(request, state, market)
        if fill is None:
            self._state_machine.rest(state)
            return state
        return self._apply_fill(request, state, fill)

    def cancel_order(self, order_id: str) -> OrderState:
        state = self._orders.get(order_id)
        if state is None:
            raise ValueError(f"unknown order_id: {order_id}")
        if state.is_terminal:
            return state
        self._state_machine.cancel(state)
        request = self._request_from_state(state)
        self._emit(request, "cancelled", state)
        return state

    def get_order(self, order_id: str) -> OrderState | None:
        return self._orders.get(order_id)

    def list_orders(self, status: str | None = None) -> list[OrderState]:
        orders = list(self._orders.values())
        if status is not None:
            orders = [o for o in orders if o.status == status]
        return orders

    def list_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def mark_to_market(self, order_id: str, market: dict[str, Any]) -> OrderState:
        state = self._orders.get(order_id)
        if state is None:
            raise ValueError(f"unknown order_id: {order_id}")
        if state.is_terminal:
            return state
        request = self._request_from_state(state)
        fill = self._simulator.simulate(request, state, market)
        if fill is None:
            return state
        return self._apply_fill(request, state, fill)

    def _apply_fill(self, request: OrderRequest, state: OrderState, fill) -> OrderState:
        self._state_machine.apply_fill(state, fill)
        event_type = "filled" if state.status == "filled" else "partially_filled"
        self._emit(request, event_type, state, quantity=fill.quantity, price=fill.price)
        if self._metrics:
            self._metrics.inc(PAPER_EXECUTION_FILLED)
            self._metrics.observe(PAPER_EXECUTION_FILL_QUANTITY, fill.quantity)
        return state

    def _emit(
        self,
        request: OrderRequest,
        event_type: str,
        state: OrderState,
        quantity: float = 0.0,
        price: float = 0.0,
    ) -> None:
        payload = {
            "event_type": event_type,
            "order_id": request.order_id,
            "correlation_id": request.correlation_id,
            "venue": self._venue,
            "quantity": quantity,
            "price": price,
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        self._events.append(payload)
        if self._writer:
            self._writer.write_from_venue_payload(
                message_id=request.order_id,
                correlation_id=request.correlation_id,
                event_type=event_type,
                venue=self._venue,
                event_time=datetime.now(UTC).replace(tzinfo=None),
                venue_order_id=request.order_id,
                quantity={"filled": quantity} if quantity else {},
                price={"average": price} if price else {},
                details={
                    "paper": True,
                    "symbol": request.symbol,
                    "side": request.side,
                    "status": state.status,
                },
            )

    def _request_from_state(self, state: OrderState) -> OrderRequest:
        return OrderRequest(
            order_id=state.order_id,
            correlation_id=state.correlation_id,
            symbol=state.symbol,
            side=state.side,
            quantity=state.remaining_quantity or state.quantity,
            order_type=state.order_type,
            limit_price=state.limit_price,
            venue=state.venue,
            created_at=state.created_at,
        )

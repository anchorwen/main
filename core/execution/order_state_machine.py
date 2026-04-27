"""Order state machine for execution gateways."""
from datetime import datetime

from core.execution.gateway_contracts import Fill, OrderRequest, OrderState


class OrderStateMachine:
    """Canonical lifecycle transitions for paper and live execution adapters."""

    TERMINAL_STATUSES = {"filled", "cancelled", "rejected", "expired"}
    VALID_TRANSITIONS = {
        "created": {"acknowledged", "rejected"},
        "acknowledged": {"accepted", "rejected"},
        "accepted": {"working", "partial", "filled", "cancelled", "rejected", "expired"},
        "working": {"partial", "filled", "cancelled", "rejected", "expired"},
        "partial": {"partial", "filled", "cancelled", "rejected", "expired"},
        "filled": set(),
        "cancelled": set(),
        "rejected": set(),
        "expired": set(),
    }

    def create(self, request: OrderRequest, venue: str) -> OrderState:
        return OrderState(
            order_id=request.order_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            status="created",
            order_type=request.order_type,
            venue=venue,
            created_at=request.created_at,
            updated_at=datetime.utcnow(),
            limit_price=request.limit_price,
        )

    def transition(self, state: OrderState, new_status: str, *, reason: str | None = None) -> OrderState:
        if new_status not in self.VALID_TRANSITIONS:
            raise ValueError(f"unknown order status: {new_status}")
        allowed = self.VALID_TRANSITIONS.get(state.status, set())
        if new_status not in allowed:
            raise ValueError(f"invalid order transition: {state.status} -> {new_status}")
        state.status = new_status
        state.updated_at = datetime.utcnow()
        if new_status == "rejected":
            state.rejection_reason = reason or "rejected"
        return state

    def acknowledge(self, state: OrderState) -> OrderState:
        return self.transition(state, "acknowledged")

    def accept(self, state: OrderState) -> OrderState:
        return self.transition(state, "accepted")

    def rest(self, state: OrderState) -> OrderState:
        return self.transition(state, "working")

    def cancel(self, state: OrderState) -> OrderState:
        if state.is_terminal:
            return state
        return self.transition(state, "cancelled")

    def reject(self, state: OrderState, reason: str) -> OrderState:
        if state.is_terminal:
            return state
        return self.transition(state, "rejected", reason=reason)

    def apply_fill(self, state: OrderState, fill: Fill) -> OrderState:
        if state.is_terminal:
            raise ValueError(f"cannot fill terminal order: {state.status}")
        if fill.quantity > state.remaining_quantity:
            raise ValueError("fill quantity exceeds remaining quantity")
        prev_filled = state.filled_quantity
        new_total = prev_filled + fill.quantity
        state.average_price = round(
            (state.average_price * prev_filled + fill.price * fill.quantity) / new_total,
            6,
        )
        state.filled_quantity = new_total
        state.fills.append(fill)
        target_status = "filled" if state.remaining_quantity <= 0 else "partial"
        return self.transition(state, target_status)

    def event_type_for_status(self, status: str) -> str:
        mapping = {
            "acknowledged": "ack",
            "accepted": "accepted",
            "working": "accepted",
            "partial": "partially_filled",
            "filled": "filled",
            "cancelled": "cancelled",
            "rejected": "rejected",
            "expired": "expired",
        }
        if status not in mapping:
            raise ValueError(f"no event type for status: {status}")
        return mapping[status]

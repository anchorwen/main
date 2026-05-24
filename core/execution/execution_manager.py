import logging
from datetime import UTC, datetime

from core.contracts.exceptions import OrderNotFoundError
from core.observability.metric_names import EXECUTION_FILL_QUANTITY, execution_event_metric

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Order lifecycle management and execution gateway.

    Coordinates between the dispatch layer and downstream venue
    execution.  Manages order state transitions and provides the
    bridge between communication dispatch and execution events.
    """

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_ACKNOWLEDGED = "acknowledged"
    STATUS_WORKING = "working"
    STATUS_FILLED = "filled"
    STATUS_PARTIAL = "partial"
    STATUS_CANCELLED = "cancelled"
    STATUS_REJECTED = "rejected"

    def __init__(self, execution_event_writer=None, position_tracker=None, metrics=None):
        self._event_writer = execution_event_writer
        self._position_tracker = position_tracker
        self._metrics = metrics
        self._orders: dict[str, dict] = {}

    def register_order(
        self,
        *,
        message_id: str,
        correlation_id: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
    ) -> dict:
        order = {
            "message_id": message_id,
            "correlation_id": correlation_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "status": self.STATUS_PENDING,
            "filled_quantity": 0.0,
            "average_price": 0.0,
            "venue_order_id": None,
            "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "events": [],
        }
        self._orders[message_id] = order
        return order

    def process_venue_event(
        self,
        *,
        message_id: str,
        event_type: str,
        venue_order_id: str | None = None,
        filled_quantity: float = 0,
        price: float = 0,
        venue: str = "unknown",
    ) -> dict:
        order = self._orders.get(message_id)
        if order is None:
            return {"status": "unknown_order", "message_id": message_id}

        order["venue_order_id"] = venue_order_id or order.get("venue_order_id")

        new_status = self._map_event_to_status(event_type, order, filled_quantity)
        old_status = order["status"]
        order["status"] = new_status

        if filled_quantity > 0:
            prev_filled = order["filled_quantity"]
            prev_avg = order["average_price"]
            new_total = prev_filled + filled_quantity
            if new_total > 0:
                order["average_price"] = round(
                    (prev_avg * prev_filled + price * filled_quantity) / new_total, 6
                )
                order["filled_quantity"] = new_total
            else:
                logger.error(
                    "execution_manager: new_total=%s from prev_filled=%s + filled_qty=%s "
                    "for message_id=%s — filled_quantity not updated (upstream data corruption)",
                    new_total,
                    prev_filled,
                    filled_quantity,
                    message_id,
                )
        elif filled_quantity < 0:
            logger.error(
                "execution_manager: negative filled_quantity=%s for message_id=%s "
                "— skipping update (upstream data corruption)",
                filled_quantity,
                message_id,
            )

        event_record = {
            "event_type": event_type,
            "filled_quantity": filled_quantity,
            "price": price,
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        order["events"].append(event_record)

        if self._event_writer:
            self._event_writer.write_from_venue_payload(
                message_id=message_id,
                correlation_id=order["correlation_id"],
                event_type=event_type,
                venue=venue,
                event_time=datetime.now(UTC).replace(tzinfo=None),
                venue_order_id=venue_order_id,
                quantity={"filled": filled_quantity} if filled_quantity else {},
                price={"average": price} if price else {},
            )

        if self._metrics:
            self._metrics.inc(execution_event_metric(event_type))
            if filled_quantity > 0:
                self._metrics.observe(EXECUTION_FILL_QUANTITY, filled_quantity)

        if new_status == self.STATUS_FILLED and self._position_tracker:
            self._position_tracker.open_position(
                position_id=message_id,
                symbol=order["symbol"],
                side=order["side"],
                quantity=order["filled_quantity"],
                entry_price=order["average_price"],
            )

        return {
            "status": "processed",
            "message_id": message_id,
            "old_status": old_status,
            "new_status": new_status,
            "order": order,
        }

    def get_order(self, message_id: str) -> dict | None:
        return self._orders.get(message_id)

    def get_order_strict(self, message_id: str) -> dict:
        """Like get_order() but raises OrderNotFoundError if missing."""
        order = self._orders.get(message_id)
        if order is None:
            raise OrderNotFoundError(message_id)
        return order

    def list_orders(self, status: str | None = None) -> list[dict]:
        if status is None:
            return list(self._orders.values())
        return [o for o in self._orders.values() if o["status"] == status]

    def _map_event_to_status(self, event_type: str, order: dict, filled_qty: float) -> str:
        if event_type == "ack":
            return self.STATUS_SENT
        if event_type == "accepted":
            return self.STATUS_ACKNOWLEDGED
        if event_type == "rejected":
            return self.STATUS_REJECTED
        if event_type == "cancelled":
            return self.STATUS_CANCELLED
        if event_type == "expired":
            return self.STATUS_CANCELLED
        if event_type == "filled":
            return self.STATUS_FILLED
        if event_type == "partially_filled":
            return self.STATUS_PARTIAL
        if event_type == "amended":
            return self.STATUS_WORKING
        return order["status"]

"""Execution gateway router."""
from core.execution.gateway_contracts import OrderRequest, OrderState


class ExecutionGatewayRouter:
    """Routes order requests to registered execution gateways by venue."""

    def __init__(self):
        self._gateways = {}

    def register(self, venue: str, gateway) -> None:
        if not venue:
            raise ValueError("venue is required")
        self._gateways[venue] = gateway

    def get(self, venue: str):
        return self._gateways.get(venue)

    def submit_order(self, request: OrderRequest, market: dict | None = None) -> OrderState:
        gateway = self._gateways.get(request.venue)
        if gateway is None:
            raise ValueError(f"No execution gateway registered for venue: {request.venue}")
        return gateway.submit_order(request, market or {})

    def list_orders(self) -> list[OrderState]:
        orders = []
        for gateway in self._gateways.values():
            orders.extend(gateway.list_orders())
        return orders

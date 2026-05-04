"""Builds execution order requests from strategy signals."""

from core.contracts.ids import new_intent_id
from core.execution.gateway_contracts import OrderRequest
from core.runtime.integration_contracts import OrderSizingPolicy
from core.strategies.contracts import Signal


class SignalOrderRequestBuilder:
    """Converts actionable strategy signals into execution order requests."""

    def __init__(
        self, sizing_policy: OrderSizingPolicy | None = None, default_venue: str = "PAPER"
    ):
        self._sizing = sizing_policy or OrderSizingPolicy()
        self._default_venue = default_venue

    def build(self, signal: Signal, market: dict | None = None) -> OrderRequest | None:
        if signal.side in {"hold", "flat"}:
            return None
        if signal.confidence < self._sizing.min_confidence:
            return None
        if signal.strength < self._sizing.min_strength:
            return None
        quantity = self._quantity(signal)
        order_type = signal.extensions.get("order_type", "market")
        limit_price = signal.extensions.get("limit_price")
        venue = signal.extensions.get("venue", self._default_venue)
        if order_type == "limit" and limit_price is None and market:
            limit_price = market.get("price") or market.get("last")
        return OrderRequest(
            order_id=new_intent_id().replace("intent_", "order_", 1),
            correlation_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            venue=venue,
            created_at=signal.generated_at,
            metadata={
                "strategy_id": signal.strategy_id,
                "signal_id": signal.signal_id,
                "confidence": signal.confidence,
                "strength": signal.strength,
                "reason": signal.reason,
            },
        )

    def _quantity(self, signal: Signal) -> float:
        raw = self._sizing.base_quantity * signal.strength * signal.confidence
        return round(min(self._sizing.max_quantity, max(0.0, raw)), 10)

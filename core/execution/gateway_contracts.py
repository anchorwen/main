"""Execution gateway contracts for paper/FIX adapters."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class OrderRequest:
    order_id: str
    correlation_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None
    venue: str = "PAPER"
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    quantity: float
    price: float
    filled_at: datetime = field(default_factory=datetime.utcnow)
    liquidity: str = "paper"

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.price <= 0:
            raise ValueError("fill price must be positive")


@dataclass
class OrderState:
    order_id: str
    correlation_id: str
    symbol: str
    side: str
    quantity: float
    status: str
    order_type: str
    venue: str
    created_at: datetime
    updated_at: datetime
    limit_price: float | None = None
    filled_quantity: float = 0.0
    average_price: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    rejection_reason: str | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "cancelled", "rejected", "expired"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "status": self.status,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "venue": self.venue,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "remaining_quantity": self.remaining_quantity,
            "fills": [f.__dict__ | {"filled_at": f.filled_at.isoformat()} for f in self.fills],
            "rejection_reason": self.rejection_reason,
        }


@runtime_checkable
class ExecutionGateway(Protocol):
    def submit_order(self, request: OrderRequest, market: dict[str, Any]) -> OrderState:
        ...

    def cancel_order(self, order_id: str) -> OrderState:
        ...

    def get_order(self, order_id: str) -> OrderState | None:
        ...

    def list_orders(self, status: str | None = None) -> list[OrderState]:
        ...

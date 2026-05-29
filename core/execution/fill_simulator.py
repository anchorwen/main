"""Fill simulation for paper execution."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.contracts.ids import new_execution_event_id
from core.execution.gateway_contracts import Fill, OrderRequest, OrderState


@dataclass(frozen=True)
class FillSimulationConfig:
    max_fill_ratio: float = 1.0
    slippage_bps: float = 0.0
    min_liquidity_quantity: float | None = None

    def __post_init__(self) -> None:
        if not 0 < self.max_fill_ratio <= 1:
            raise ValueError("max_fill_ratio must be in (0, 1]")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if self.min_liquidity_quantity is not None and self.min_liquidity_quantity <= 0:
            raise ValueError("min_liquidity_quantity must be positive")

    @classmethod
    def from_slippage_points(
        cls,
        slippage_points: float = 0.0,
        approximate_price: float = 2000.0,
        max_fill_ratio: float = 1.0,
        min_liquidity_quantity: float | None = None,
    ) -> "FillSimulationConfig":
        """Create config from MT5 slippage_points (FIX-20260529-031).

        10 points × 0.01 tick = 0.10 price units on XAUUSDc.
        0.10 / 2000 * 10000 ≈ 0.5 bps.
        """
        if slippage_points <= 0 or approximate_price <= 0:
            return cls(
                max_fill_ratio=max_fill_ratio,
                slippage_bps=0.0,
                min_liquidity_quantity=min_liquidity_quantity,
            )
        slippage_price = slippage_points * 0.01  # XAUUSDc tick_size
        slippage_bps = round((slippage_price / approximate_price) * 10000, 6)
        return cls(
            max_fill_ratio=max_fill_ratio,
            slippage_bps=slippage_bps,
            min_liquidity_quantity=min_liquidity_quantity,
        )


class FillSimulator:
    """Deterministic fill simulator for market and limit orders."""

    def __init__(self, config: FillSimulationConfig | None = None):
        self._config = config or FillSimulationConfig()

    def simulate(
        self, request: OrderRequest, state: OrderState, market: dict[str, Any]
    ) -> Fill | None:
        if state.is_terminal or state.remaining_quantity <= 0:
            return None
        price = self._executable_price(request, market)
        if price is None:
            return None
        quantity = self._fill_quantity(state, market)
        if quantity <= 0:
            return None
        return Fill(
            fill_id=new_execution_event_id().replace("exec_event_", "fill_", 1),
            order_id=state.order_id,
            quantity=quantity,
            price=self._apply_slippage(price, request.side),
            filled_at=datetime.now(UTC).replace(tzinfo=None),
            liquidity="paper",
        )

    def _executable_price(self, request: OrderRequest, market: dict[str, Any]) -> float | None:
        bid = market.get("bid")
        ask = market.get("ask")
        last = market.get("last") or market.get("price")
        if request.order_type == "market":
            if request.side == "buy":
                return self._first_price(ask, last, bid)
            return self._first_price(bid, last, ask)
        if (
            request.side == "buy"
            and ask is not None
            and request.limit_price is not None
            and float(ask) <= float(request.limit_price)
        ):
            return float(ask)
        if (
            request.side == "sell"
            and bid is not None
            and request.limit_price is not None
            and float(bid) >= float(request.limit_price)
        ):
            return float(bid)
        return None

    def _fill_quantity(self, state: OrderState, market: dict[str, Any]) -> float:
        requested = state.remaining_quantity * self._config.max_fill_ratio
        market_liquidity = market.get("available_quantity")
        if market_liquidity is not None:
            requested = min(requested, float(market_liquidity))
        if (
            self._config.min_liquidity_quantity is not None
            and requested < self._config.min_liquidity_quantity
        ):
            return 0.0
        return round(min(state.remaining_quantity, requested), 10)

    def _apply_slippage(self, price: float, side: str) -> float:
        multiplier = self._config.slippage_bps / 10000
        if side == "buy":
            return round(price * (1 + multiplier), 6)
        return round(price * (1 - multiplier), 6)

    def _first_price(self, *values) -> float:
        for value in values:
            if value is not None:
                return float(value)
        raise ValueError("market price is required")

"""Backtest execution simulator — fills orders with realistic slippage/spread."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SimulatedFill:
    """A simulated fill in backtest."""

    timestamp: datetime
    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    price: float
    spread_cost: float = 0.0
    slippage_bps: float = 0.0
    commission: float = 0.0

    @property
    def notional(self) -> float:
        return self.quantity * self.price * 100.0  # XAUUSD contract size

    @property
    def direction_sign(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass
class ExecutionSimulator:
    """Simulate market order execution with configurable slippage and spread.

    Models:
    - Half-spread cost (crossing the bid-ask)
    - Proportional slippage (worse fill as position size increases)
    - Fixed commission per lot
    - Minimum fill latency

    All parameters are configurable to match real broker conditions.
    """

    spread_bps: float = 3.0  # typical XAUUSD spread in bps (0.3 pips)
    slippage_per_lot_bps: float = 0.5  # additional slippage per lot traded
    commission_per_lot: float = 7.0  # round-turn commission
    min_latency_ms: float = 50.0  # minimum fill delay
    contract_size: float = 100.0  # XAUUSD ounces per lot

    filled_orders: list[SimulatedFill] = field(default_factory=list)

    def execute_market(
        self, *, timestamp: datetime, symbol: str, side: str, quantity: float, mid_price: float
    ) -> SimulatedFill:
        """Execute a market order at the given mid price.

        Buy: fill = mid + half_spread + slippage (pays ask)
        Sell: fill = mid - half_spread - slippage (receives bid)
        """
        half_spread = mid_price * (self.spread_bps / 2.0) / 10000.0
        volume_slippage = mid_price * (self.slippage_per_lot_bps * quantity) / 10000.0

        if side == "buy":
            fill_price = mid_price + half_spread + volume_slippage
            slippage_bps = (fill_price - mid_price) / mid_price * 10000.0
        else:
            fill_price = mid_price - half_spread - volume_slippage
            slippage_bps = (mid_price - fill_price) / mid_price * 10000.0

        commission = quantity * self.commission_per_lot

        fill = SimulatedFill(
            timestamp=timestamp,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=round(fill_price, 5),
            spread_cost=round(half_spread * 2 * quantity * self.contract_size, 4),
            slippage_bps=round(slippage_bps, 4),
            commission=round(commission, 4),
        )
        self.filled_orders.append(fill)
        return fill

    @property
    def total_fills(self) -> int:
        return len(self.filled_orders)

    @property
    def total_spread_cost(self) -> float:
        return sum(f.spread_cost for f in self.filled_orders)

    @property
    def total_commission(self) -> float:
        return sum(f.commission for f in self.filled_orders)

    @property
    def total_cost(self) -> float:
        return self.total_spread_cost + self.total_commission

    @property
    def average_slippage_bps(self) -> float:
        if not self.filled_orders:
            return 0.0
        return sum(f.slippage_bps for f in self.filled_orders) / len(self.filled_orders)

"""Virtual portfolio for backtest — tracks positions, cash, equity, and P&L."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class VirtualPosition:
    """A single open position in the backtest portfolio."""

    symbol: str
    side: str
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: float = 0.0
    take_profit: float = 0.0
    magic: int = 0

    @property
    def direction_sign(self) -> int:
        return 1 if self.side == "buy" else -1

    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized P&L in account currency (USD)."""
        if self.side == "buy":
            return (current_price - self.entry_price) * self.quantity * 100.0
        return (self.entry_price - current_price) * self.quantity * 100.0

    def is_stopped(self, bar_low: float, bar_high: float) -> bool:
        """Check if stop-loss or take-profit was hit during this bar."""
        if self.stop_loss > 0:
            if self.side == "buy" and bar_low <= self.stop_loss:
                return True
            if self.side == "sell" and bar_high >= self.stop_loss:
                return True
        if self.take_profit > 0:
            if self.side == "buy" and bar_high >= self.take_profit:
                return True
            if self.side == "sell" and bar_low <= self.take_profit:
                return True
        return False

    def exit_price_from_bar(self, bar_low: float, bar_high: float) -> float:
        """Estimate exit price when SL/TP hit within a bar."""
        if self.stop_loss > 0:
            if self.side == "buy" and bar_low <= self.stop_loss:
                return self.stop_loss
            if self.side == "sell" and bar_high >= self.stop_loss:
                return self.stop_loss
        if self.take_profit > 0:
            if self.side == "buy" and bar_high >= self.take_profit:
                return self.take_profit
            if self.side == "sell" and bar_low <= self.take_profit:
                return self.take_profit
        return 0.0


@dataclass
class VirtualPortfolio:
    """Backtest portfolio tracking positions, cash, and equity curve."""

    initial_cash: float = 10000.0
    cash: float = 10000.0
    positions: list[VirtualPosition] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)

    contract_size: float = 100.0

    def open_position(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        timestamp: datetime,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        magic: int = 0,
        commission: float = 0.0,
    ) -> VirtualPosition:
        """Open a new position, deducting commission from cash."""
        pos = VirtualPosition(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=price,
            entry_time=timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            magic=magic,
        )
        self.positions.append(pos)
        self.cash -= commission
        return pos

    def close_position(
        self,
        position: VirtualPosition,
        exit_price: float,
        exit_time: datetime,
        reason: str = "manual",
        commission: float = 0.0,
    ) -> dict[str, Any]:
        """Close a position and record the trade."""
        pnl = position.unrealized_pnl(exit_price)
        self.cash += pnl - commission
        self.positions.remove(position)

        trade = {
            "symbol": position.symbol,
            "side": position.side,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "entry_time": position.entry_time.isoformat(),
            "exit_price": exit_price,
            "exit_time": exit_time.isoformat(),
            "pnl": round(pnl, 4),
            "pnl_after_cost": round(pnl - commission, 4),
            "commission": commission,
            "reason": reason,
            "magic": position.magic,
            "holding_bars": 0,  # filled by caller
        }
        self.closed_trades.append(trade)
        return trade

    def record_equity(self, timestamp: datetime, mark_price: float) -> None:
        """Record equity at current mark price."""
        unrealized = sum(p.unrealized_pnl(mark_price) for p in self.positions)
        self.equity_curve.append(
            {
                "timestamp": timestamp.isoformat(),
                "cash": round(self.cash, 2),
                "unrealized_pnl": round(unrealized, 2),
                "equity": round(self.cash + unrealized, 2),
                "position_count": len(self.positions),
            }
        )

    @property
    def equity(self) -> float:
        if not self.equity_curve:
            return self.cash
        return self.equity_curve[-1]["equity"]

    @property
    def gross_exposure(self) -> float:
        """Total notional exposure / equity."""
        if self.equity <= 0:
            return 0.0
        total_notional = sum(
            p.quantity * p.entry_price * self.contract_size for p in self.positions
        )
        return total_notional / self.equity

    @property
    def net_exposure(self) -> float:
        """Net directional exposure / equity."""
        if self.equity <= 0:
            return 0.0
        net = sum(
            p.direction_sign * p.quantity * p.entry_price * self.contract_size
            for p in self.positions
        )
        return net / self.equity

    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def total_pnl(self) -> float:
        return sum(t["pnl"] for t in self.closed_trades)

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t["pnl"] > 0)
        return wins / len(self.closed_trades)

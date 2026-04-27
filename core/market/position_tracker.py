from datetime import datetime


class PositionTracker:
    """Tracks open positions and provides the risk context that
    risk policies need for evaluation.

    In-memory for the runtime loop cycle; can be snapshotted to disk.
    """

    def __init__(self):
        self._positions: dict[str, dict] = {}
        self._closed: list[dict] = []

    def open_position(
        self,
        *,
        position_id: str,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        opened_at: datetime | None = None,
    ) -> dict:
        pos = {
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "opened_at": (opened_at or datetime.utcnow()).isoformat(),
            "status": "open",
            "notional": quantity * entry_price,
            "realized_pnl": 0.0,
        }
        self._positions[position_id] = pos
        return pos

    def close_position(self, position_id: str, exit_price: float, closed_at: datetime | None = None) -> dict | None:
        pos = self._positions.pop(position_id, None)
        if pos is None:
            return None
        pnl = self._compute_pnl(pos, exit_price)
        pos["status"] = "closed"
        pos["exit_price"] = exit_price
        pos["closed_at"] = (closed_at or datetime.utcnow()).isoformat()
        pos["realized_pnl"] = round(pnl, 4)
        self._closed.append(pos)
        return pos

    def get_position(self, position_id: str) -> dict | None:
        return self._positions.get(position_id)

    def list_open(self) -> list[dict]:
        return list(self._positions.values())

    def list_closed(self) -> list[dict]:
        return list(self._closed)

    def get_risk_context(self) -> dict:
        open_positions = list(self._positions.values())
        per_symbol: dict[str, int] = {}
        total_notional = 0.0
        for p in open_positions:
            sym = p["symbol"]
            per_symbol[sym] = per_symbol.get(sym, 0) + 1
            total_notional += p.get("notional", 0)

        total_realized = sum(c.get("realized_pnl", 0) for c in self._closed)
        peak_equity = max(total_realized, 1.0)
        current_dd = max(0.0, (peak_equity - total_realized) / peak_equity * 100) if self._closed else 0.0

        return {
            "open_position_count": len(open_positions),
            "positions_per_symbol": per_symbol,
            "current_notional_exposure": round(total_notional, 2),
            "current_drawdown_pct": round(current_dd, 4),
            "total_realized_pnl": round(total_realized, 4),
            "closed_position_count": len(self._closed),
        }

    def _compute_pnl(self, pos: dict, exit_price: float) -> float:
        qty = pos["quantity"]
        entry = pos["entry_price"]
        if pos["side"] == "long":
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty


class MarketContextProvider:
    """Provides market state context for decision scoring and risk."""

    def __init__(self):
        self._snapshots: dict[str, dict] = {}

    def update(self, symbol: str, *, bid: float, ask: float, timestamp: datetime | None = None) -> None:
        prev = self._snapshots.get(symbol)
        mid = (bid + ask) / 2
        prev_mid = prev.get("mid") if prev else mid
        pct_move = ((mid - prev_mid) / prev_mid * 100) if prev_mid else 0.0

        self._snapshots[symbol] = {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": round(mid, 6),
            "spread": round(ask - bid, 6),
            "price_move_pct": round(pct_move, 6),
            "timestamp": (timestamp or datetime.utcnow()).isoformat(),
        }

    def get_context(self, symbol: str) -> dict:
        snap = self._snapshots.get(symbol)
        if snap is None:
            return {"symbol": symbol, "available": False}
        return {**snap, "available": True}

    def get_all(self) -> dict[str, dict]:
        return dict(self._snapshots)

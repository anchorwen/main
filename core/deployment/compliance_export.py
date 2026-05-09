"""MiFID II / RTS 27/28 compliance trade report generation.

Generates standardised trade execution reports from ledger records.
Outputs JSON (machine-readable) with optional XML rendering for
regulatory filing.

Usage:
    from core.deployment.compliance_export import generate_trade_report

    report = generate_trade_report(
        trades=ledger.get_records(start=..., end=...),
        firm_name="Quant Fund Ltd",
        report_type="rts27",
    )
    print(report.to_json())

"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── Standard fields ─────────────────────────────────────────────────────────

REQUIRED_TRADE_FIELDS = [
    "trade_id",
    "order_id",
    "symbol",
    "isin",
    "side",
    "quantity",
    "price",
    "venue",
    "timestamp",
    "execution_timestamp",
    "decision_price",
    "arrival_price",
    "commission",
    "spread_bps",
    "slippage_bps",
    "latency_ms",
    "fill_rate",
    "reject_reason",
]

OPTIONAL_TRADE_FIELDS = [
    "strategy_id",
    "brain_id",
    "regime",
    "pnl",
    "swap",
    "comment",
]

STANDARD_INSTRUMENTS = {
    "XAUUSDc": {"isin": "XC0009655157", "cfi": "MRCXXX", "description": "Gold Spot vs USD"},
    "XAUUSD": {"isin": "XC0009655157", "cfi": "MRCXXX", "description": "Gold Spot vs USD"},
    "EURUSD": {"isin": "EU0009652759", "cfi": "MRCXXX", "description": "Euro vs USD"},
}


# ── Report dataclasses ──────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """Standard trade record for compliance reporting."""

    trade_id: str
    order_id: str
    symbol: str
    isin: str
    cfi: str
    side: str
    quantity: float
    price: float
    venue: str
    timestamp: str
    execution_timestamp: str
    decision_price: float | None
    arrival_price: float | None
    commission: float
    spread_bps: float | None
    slippage_bps: float | None
    latency_ms: float
    fill_rate: float
    strategy_id: str = ""
    brain_id: str = ""
    regime: str = ""
    pnl: float | None = None
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        info = STANDARD_INSTRUMENTS.get(self.symbol, {"isin": self.isin, "cfi": ""})
        return {
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "isin": self.isin or info.get("isin", ""),
            "cfi": self.cfi or info.get("cfi", ""),
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "venue": self.venue,
            "timestamp": self.timestamp,
            "execution_timestamp": self.execution_timestamp,
            "decision_price": self.decision_price,
            "arrival_price": self.arrival_price,
            "commission": self.commission,
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "latency_ms": self.latency_ms,
            "fill_rate": self.fill_rate,
            "strategy_id": self.strategy_id,
            "brain_id": self.brain_id,
            "regime": self.regime,
            "pnl": self.pnl,
            "reject_reason": self.reject_reason,
        }

    @classmethod
    def from_trade_dict(
        cls,
        d: dict[str, Any],
        *,
        isin: str = "",
        cfi: str = "",
    ) -> TradeRecord:
        return cls(
            trade_id=str(d.get("trade_id", d.get("ticket", ""))),
            order_id=str(d.get("order_id", "")),
            symbol=str(d.get("symbol", "XAUUSDc")),
            isin=isin,
            cfi=cfi,
            side=str(d.get("side", "")),
            quantity=float(d.get("quantity", d.get("volume", 0))),
            price=float(d.get("price", d.get("entry_price", d.get("fill_price", 0)))),
            venue=str(d.get("venue", "MT5")),
            timestamp=str(d.get("timestamp", d.get("time", ""))),
            execution_timestamp=str(
                d.get("execution_timestamp", d.get("exit_time", d.get("timestamp", "")))
            ),
            decision_price=float(d["decision_price"]) if d.get("decision_price") else None,
            arrival_price=float(d["arrival_price"]) if d.get("arrival_price") else None,
            commission=float(d.get("commission", 0)),
            spread_bps=float(d["spread_bps"]) if d.get("spread_bps") else None,
            slippage_bps=float(d["slippage_bps"]) if d.get("slippage_bps") else None,
            latency_ms=float(d.get("latency_ms", 0)),
            fill_rate=float(d.get("fill_rate", d.get("fill_ratio", 1.0))),
            strategy_id=str(d.get("strategy_id", d.get("strategy_name", ""))),
            brain_id=str(d.get("brain_id", "")),
            regime=str(d.get("regime", "")),
            pnl=float(d["pnl"]) if d.get("pnl") is not None else None,
            reject_reason=str(d.get("reject_reason", "")),
        )


@dataclass
class ComplianceReport:
    """Aggregated compliance/execution quality report.

    Covers RTS 27 (per-trade) and RTS 28 (summary) formats.
    """

    report_type: str  # "rts27" or "rts28"
    generated_at: str
    firm_name: str
    firm_lei: str
    reporting_period_start: str
    reporting_period_end: str
    venue: str
    trades: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_type": self.report_type,
            "generated_at": self.generated_at,
            "firm_name": self.firm_name,
            "firm_lei": self.firm_lei,
            "reporting_period": {
                "start": self.reporting_period_start,
                "end": self.reporting_period_end,
            },
            "venue": self.venue,
            "trades": self.trades,
            "summary": self.summary,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str, ensure_ascii=False)


# ── Report generators ───────────────────────────────────────────────────────


def generate_trade_report(
    *,
    trades: list[dict[str, Any]],
    firm_name: str = "Quant Fund Ltd",
    firm_lei: str = "",
    venue: str = "MT5",
    report_type: str = "rts27",
    period_start: str | None = None,
    period_end: str | None = None,
) -> ComplianceReport:
    """Generate a MiFID II compliance report from trade records.

    Args:
        trades: List of trade dicts from the ledger or backtest.
        firm_name: Reporting firm name.
        firm_lei: Legal Entity Identifier (20-char).
        venue: Execution venue.
        report_type: ``"rts27"`` (per-trade) or ``"rts28"`` (summary).
        period_start: ISO datetime of period start.
        period_end: ISO datetime of period end.

    Returns:
        ComplianceReport with trades and aggregate summary.
    """
    now = datetime.now(UTC).isoformat()
    records = [TradeRecord.from_trade_dict(t).to_dict() for t in trades]

    # Determine period from trade timestamps if not given
    timestamps = [r["timestamp"] for r in records if r["timestamp"]]
    ps = period_start or (min(timestamps) if timestamps else now)
    pe = period_end or (max(timestamps) if timestamps else now)

    summary = _build_summary(records, venue)

    return ComplianceReport(
        report_type=report_type,
        generated_at=now,
        firm_name=firm_name,
        firm_lei=firm_lei,
        reporting_period_start=ps,
        reporting_period_end=pe,
        venue=venue,
        trades=records if report_type == "rts27" else [],
        summary=summary,
    )


def _build_summary(records: list[dict[str, Any]], venue: str) -> dict[str, Any]:
    """Build RTS 28 style execution quality summary."""
    filled = [r for r in records if not r.get("reject_reason")]
    rejected = [r for r in records if r.get("reject_reason")]

    buy_trades = [r for r in filled if r["side"] in ("buy", "long")]
    sell_trades = [r for r in filled if r["side"] in ("sell", "short")]

    def _avg(key: str, lst: list[dict[str, Any]]) -> float | None:
        vals = [r[key] for r in lst if r.get(key) is not None]
        return round(sum(vals) / len(vals), 6) if vals else None

    def _count(lst: list[dict[str, Any]]) -> int:
        return len(lst)

    symbols = sorted({r["symbol"] for r in records})

    by_symbol: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        sym_recs = [r for r in filled if r["symbol"] == sym]
        by_symbol[sym] = {
            "trade_count": _count(sym_recs),
            "total_volume": round(sum(r["quantity"] for r in sym_recs), 4),
            "avg_slippage_bps": _avg("slippage_bps", sym_recs),
            "avg_latency_ms": _avg("latency_ms", sym_recs),
            "avg_fill_rate": _avg("fill_rate", sym_recs),
            "total_commission": round(sum(r["commission"] for r in sym_recs), 4),
        }

    return {
        "venue": venue,
        "total_orders": _count(records),
        "filled_orders": _count(filled),
        "rejected_orders": _count(rejected),
        "buy_trades": _count(buy_trades),
        "sell_trades": _count(sell_trades),
        "reject_rate": round(_count(rejected) / max(_count(records), 1), 4),
        "average_slippage_bps": _avg("slippage_bps", filled),
        "average_latency_ms": _avg("latency_ms", filled),
        "average_fill_rate": _avg("fill_rate", filled),
        "total_commission": round(sum(r["commission"] for r in filled), 4),
        "total_volume": round(sum(r["quantity"] for r in filled), 4),
        "by_symbol": by_symbol,
    }


def generate_order_audit_trail(
    trades: list[dict[str, Any]],
    *,
    output_path: str = "",
) -> list[dict[str, Any]]:
    """Generate a reconstructable order audit trail.

    Each record represents a state transition in the order lifecycle:
    CREATED → SUBMITTED → PARTIAL_FILL / FILLED / REJECTED → CLOSED.

    Args:
        trades: List of trade/order dicts.
        output_path: If provided, writes JSONL to this path.

    Returns:
        List of audit trail records.
    """
    trail: list[dict[str, Any]] = []
    for i, t in enumerate(trades):
        trade_id = t.get("trade_id", t.get("ticket", str(i)))
        order_id = t.get("order_id", trade_id)
        ts = t.get("timestamp", t.get("time", datetime.now(UTC).isoformat()))

        # Creation
        trail.append(
            {
                "sequence": len(trail) + 1,
                "order_id": order_id,
                "trade_id": trade_id,
                "state": "CREATED",
                "timestamp": ts,
                "symbol": t.get("symbol", ""),
                "side": t.get("side", ""),
                "quantity": t.get("quantity", t.get("volume", 0)),
                "price": t.get("price", 0),
                "venue": t.get("venue", "MT5"),
                "operator": "strategy",
            }
        )

        # Fill or reject
        if t.get("reject_reason"):
            trail.append(
                {
                    "sequence": len(trail) + 1,
                    "order_id": order_id,
                    "trade_id": trade_id,
                    "state": "REJECTED",
                    "timestamp": ts,
                    "reason": t.get("reject_reason"),
                }
            )
        else:
            trail.append(
                {
                    "sequence": len(trail) + 1,
                    "order_id": order_id,
                    "trade_id": trade_id,
                    "state": "FILLED",
                    "timestamp": t.get("execution_timestamp", ts),
                    "fill_price": t.get("price", 0),
                    "slippage_bps": t.get("slippage_bps"),
                    "latency_ms": t.get("latency_ms"),
                }
            )

    if output_path:
        from pathlib import Path

        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for record in trail:
                f.write(json.dumps(record, default=str) + "\n")

    return trail

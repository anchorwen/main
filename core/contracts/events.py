"""Immutable event contracts for the append-only event stream.

The single source of truth for all data that flows through the system.
Every event written to ledger_events.jsonl MUST pass Pydantic validation.
ValidationError = physical rejection.  No dirty data enters the stream.

FIX-20260611-021: Event Sourcing Foundation — Step 1 Contract Forging.

Usage::

    from core.contracts.events import PnLEvent, GovernanceTransitionEvent

    event = PnLEvent(
        timestamp=datetime.now(UTC),
        source="live",
        event_type="SignalSettled",
        brain_id="Swing_V9_M15_V2",
        symbol="XAUUSDc",
        direction="long",
        pnl_r=5.2,
        generated_by="live_intent_loop.v2",
    )
    # model_dump_json() → '{"event_id":"evt_abc123",...}'
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── Data source constants ────────────────────────────────────────────────


class DataSource:
    """String constants for event provenance tagging.

    Every event MUST carry a ``source`` field.  This is the single most
    important field in the entire data pipeline — it is the ONLY thing that
    prevents backtest data from poisoning live governance decisions.

    Usage::

        event = PnLEvent(source=DataSource.LIVE, ...)
    """

    LIVE: str = "live"
    SHADOW: str = "shadow"
    BACKTEST: str = "backtest"
    MIGRATION: str = "migration"


# ── Event type constants ─────────────────────────────────────────────────


class EventType:
    """String constants for event_type discrimination in projections."""

    SIGNAL_RECORDED: str = "SignalRecorded"
    SIGNAL_SETTLED: str = "SignalSettled"
    POSITION_CLOSED: str = "PositionClosed"


# ── Core events ──────────────────────────────────────────────────────────


class PnLEvent(BaseModel):
    """Immutable P&L event — the atomic unit of the event stream.

    Written by exactly one writer (EventWriter) per process.
    Read by projection engines to rebuild governance state.
    """

    event_id: str = Field(
        default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}",
        description="Globally unique event identifier",
    )
    timestamp: datetime = Field(
        description="UTC timestamp of event generation",
    )
    source: Literal["live", "shadow", "backtest", "migration"] = Field(
        description="Data provenance — THE critical field for governance isolation",
    )
    event_type: Literal["SignalRecorded", "SignalSettled", "PositionClosed"] = Field(
        description="Event type for projection discrimination",
    )
    brain_id: str = Field(
        min_length=1,
        description="Brain identifier that generated the signal",
    )
    symbol: str = Field(
        min_length=1,
        description="Trading symbol (BTCUSDc, XAUUSDc)",
    )
    direction: Literal["long", "short"] | None = Field(
        default=None,
        description="Trade direction (None for neutral/unknown)",
    )

    # ── Price fields — float precision preserved for XAUUSDc 3-decimal ──
    entry_price: float | None = Field(
        default=None,
        description="Entry price (None if not yet known)",
    )
    exit_price: float | None = Field(
        default=None,
        description="Exit price at settlement (None if pending)",
    )
    pnl_r: float = Field(
        ...,
        allow_inf_nan=False,
        description="P&L in R-units (risk-normalised).  NaN/Inf REJECTED.",
    )

    # ── Signal metadata ──
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Signal confidence [0.0, 1.0]",
    )
    position_ticket: int | None = Field(
        default=None,
        description="MT5 position ticket for cross-reference",
    )
    generated_by: str = Field(
        min_length=1,
        description="Code path that generated this event (e.g. 'live_intent_loop.v2')",
    )

    model_config = {
        "extra": "forbid",  # Unknown fields → ValidationError (physical rejection)
        "frozen": True,  # Immutable after construction
    }


class GovernanceTransitionEvent(BaseModel):
    """Immutable record of a brain lifecycle state change.

    Generated ONLY by manual CLI or explicit human-approved actions
    while _GOVERNANCE_MANUAL_MODE is active.
    """

    event_id: str = Field(
        default_factory=lambda: f"gov_{uuid.uuid4().hex[:12]}",
        description="Globally unique governance event identifier",
    )
    timestamp: datetime = Field(
        description="UTC timestamp of the transition",
    )
    brain_id: str = Field(min_length=1)
    from_status: str = Field(min_length=1)
    to_status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    triggered_by: Literal["manual", "auto_evaluator", "circuit_breaker"] = Field(
        description="What triggered this transition",
    )
    metrics_snapshot: dict | None = Field(
        default=None,
        description="Snapshot of brain metrics at transition time",
    )

    model_config = {
        "extra": "forbid",
        "frozen": True,
    }

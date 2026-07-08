"""SSOT for MT5 deal selection — the single authority that turns a raw
``history_deals_get()`` list into the authoritative close resolution.

Root cause of DQAF-20260708-003 (L3): three divergent implementations of
"which deal is the close" coexisted —

  * ``position_close_adapter._build_event`` picked ``deals[0]`` (the earliest
    deal = the DEAL_ENTRY_IN **opening** deal on the first pass, because the
    adapter is re-instantiated every cycle so its deal cursor is always 0).
    The opening deal carries ``price = entry_fill`` and ``profit = 0`` →
    every full close was **fabricated as a break-even at the entry price**.
  * ``reconciliation.reconcile_closed_positions`` correctly filtered
    ``entry == 1`` (DEAL_ENTRY_OUT).
  * ``mia_close.enrich_mia_from_deals`` correctly filtered ``entry == 1``.

Three copies of the same MT5-deal knowledge, one of them wrong.  This module
is the ONE place that knows the MT5 deal model, so no fourth path can diverge.

MT5 deal model (``mt5.history_deals_get(position=ticket)``):
  * ``deal.entry``  — 0 = DEAL_ENTRY_IN (open), 1 = DEAL_ENTRY_OUT (close),
                       2 = DEAL_ENTRY_INOUT (reverse), 3 = DEAL_ENTRY_OUT_BY.
  * ``deal.reason`` — 0 client, 1 mobile, 2 web, 3 signal/expert,
                       4 SL, 5 TP, 6 stop-out, 7 risk-out.
  * The opening (entry==0) deal always carries ``profit = 0``; the realized
    P&L lives on the exit (entry==1) deal(s).

Invariant enforced here: a close price / P&L / reason MUST come from a
DEAL_ENTRY_OUT deal.  When no exit deal exists the resolver returns a
``no_exit_deal`` provenance and ``close_price = None`` — callers MUST treat
that as "close price unknown", never fabricate a break-even.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── MT5 DEAL entry types ──
DEAL_ENTRY_IN = 0  # opening a position
DEAL_ENTRY_OUT = 1  # closing (full or partial)
DEAL_ENTRY_INOUT = 2  # reversal
DEAL_ENTRY_OUT_BY = 3  # close-by

# ── MT5 DEAL reasons (subset used for labels) ──
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5

# ── close_price provenance tags ──
CLOSE_SRC_EXIT_DEAL = "mt5_exit_deal"
"""Authoritative — close resolved from a DEAL_ENTRY_OUT deal."""

CLOSE_SRC_NO_EXIT_DEAL = "no_exit_deal"
"""Anomaly — no exit deal in the list.  Caller MUST NOT fabricate a close."""


@dataclass(frozen=True)
class ExitResolution:
    """Authoritative close resolution extracted from an MT5 deal list."""

    close_price: float | None
    close_pnl: float | None  # summed realized profit across unprocessed exit deals
    close_reason: int | None
    close_time: int | float | None
    close_volume: float
    deal_id: int  # ticket of the representative exit deal (cursor advance)
    position_id: int  # MT5 immutable position identifier
    comment: str
    entry_fill_price: float | None  # actual entry fill (from the DEAL_ENTRY_IN deal)
    close_price_source: str
    n_exit_deals: int

    @property
    def has_exit(self) -> bool:
        """True iff an authoritative exit deal produced a usable close price."""
        return (
            self.close_price_source == CLOSE_SRC_EXIT_DEAL
            and self.close_price is not None
            and self.close_price > 0
        )


def resolve_exit_deal(deals: Any, cursor: int = 0) -> ExitResolution | None:
    """Return the authoritative close resolution from an MT5 deal list.

    Args:
        deals: iterable of MT5 Deal objects from ``history_deals_get(position=...)``.
        cursor: only exit deals with ``ticket > cursor`` count as "unprocessed"
            (supports sequential partial-close settlement).  Default 0 = all.

    Returns:
        ``ExitResolution`` describing the close, or ``None`` when *deals* is
        empty/None.  When no DEAL_ENTRY_OUT deal exists, the resolution carries
        ``close_price_source == CLOSE_SRC_NO_EXIT_DEAL`` and ``close_price=None``
        — the caller MUST treat this as "close price unknown", NEVER as
        break-even and NEVER fall back to the entry deal's price.
    """
    if not deals:
        return None

    # ── Entry deal → actual entry fill price (earliest DEAL_ENTRY_IN) ──
    entry_deals = [d for d in deals if _entry_of(d) == DEAL_ENTRY_IN]
    entry_deal = min(entry_deals, key=_ticket_of) if entry_deals else None
    entry_fill: float | None = None
    if entry_deal is not None:
        _ep = _f(getattr(entry_deal, "price", 0))
        entry_fill = _ep if _ep > 0 else None

    # ── Exit deals after cursor ──
    exit_deals = [d for d in deals if _entry_of(d) == DEAL_ENTRY_OUT and _ticket_of(d) > cursor]

    if not exit_deals:
        return ExitResolution(
            close_price=None,
            close_pnl=None,
            close_reason=None,
            close_time=None,
            close_volume=0.0,
            deal_id=0,
            position_id=_position_id_of(entry_deal) if entry_deal is not None else 0,
            comment="",
            entry_fill_price=entry_fill,
            close_price_source=CLOSE_SRC_NO_EXIT_DEAL,
            n_exit_deals=0,
        )

    # ── Representative exit deal: prefer an SL/TP-reason deal (definitive
    #    close), else the latest exit deal by time (aggregate final close). ──
    _sltp = [d for d in exit_deals if _reason_of(d) in (DEAL_REASON_SL, DEAL_REASON_TP)]
    primary = max(_sltp or exit_deals, key=_time_of)

    # ── Aggregate realized profit across all unprocessed exit deals (correct
    #    for multi-partial closes settled in one pass).  None only if no deal
    #    carries a profit attribute at all. ──
    _profits = [
        _f(getattr(d, "profit", None)) for d in exit_deals if getattr(d, "profit", None) is not None
    ]
    close_pnl = round(sum(_profits), 2) if _profits else None

    close_price_f = _f(getattr(primary, "price", 0))
    return ExitResolution(
        close_price=close_price_f if close_price_f > 0 else None,
        close_pnl=close_pnl,
        close_reason=_reason_of(primary),
        close_time=getattr(primary, "time", None),
        close_volume=round(sum(_f(getattr(d, "volume", 0)) for d in exit_deals), 8),
        deal_id=_ticket_of(primary),
        position_id=_position_id_of(primary) or (_position_id_of(entry_deal) if entry_deal else 0),
        comment=str(getattr(primary, "comment", "") or ""),
        entry_fill_price=entry_fill,
        close_price_source=CLOSE_SRC_EXIT_DEAL,
        n_exit_deals=len(exit_deals),
    )


# ── Field extractors — tolerant of missing attrs, careful with falsy 0 ──
def _entry_of(d: Any) -> int:
    try:
        return int(getattr(d, "entry", -1))
    except (TypeError, ValueError):
        return -1


def _reason_of(d: Any) -> int:
    try:
        return int(getattr(d, "reason", -1))
    except (TypeError, ValueError):
        return -1


def _ticket_of(d: Any) -> int:
    try:
        return int(getattr(d, "ticket", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _position_id_of(d: Any) -> int:
    try:
        return int(getattr(d, "position_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _time_of(d: Any) -> float:
    try:
        return float(getattr(d, "time", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

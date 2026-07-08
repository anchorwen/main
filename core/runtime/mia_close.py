"""MIA (Missing In Action) close journal entry builders.

Strangler Fig #20: extracted from live_cycle.py.
Pure data-transformation functions — no MT5 I/O, receive data via parameters.

Related FIXes: FIX-20260610-004 (close_volume=0 fix), FIX-20260610-006 (trail telemetry)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.runtime.time_utils import _utc_iso


def build_mia_close_entry(
    pos: Any, known_entry: dict[str, Any], *, symbol: str = "XAUUSDc"
) -> dict[str, Any]:
    """Build a close journal entry for a position detected MIA in MT5.

    Called when positions_get returns empty for a tracked ticket.
    Uses engine-side info since MT5 deal history may not be available yet.
    """
    side = str(getattr(pos, "side", known_entry.get("side", "")))
    entry_price = float(
        getattr(pos, "entry_price", None) or known_entry.get("entry_price", 0.0) or 0.0
    )
    close_volume = float(
        getattr(pos, "volume", None)
        or known_entry.get("volume", 0.0)
        or known_entry.get("effective_volume_hint", 0.0)
    )
    if close_volume <= 0.0:
        close_volume = float(getattr(pos, "volume", 0) or 0)
    initial_sl = float(getattr(pos, "initial_sl", None) or known_entry.get("sl", 0.0) or 0.0)
    initial_tp = float(getattr(pos, "initial_tp", None) or known_entry.get("tp", 0.0) or 0.0)
    current_sl = float(getattr(pos, "current_sl", initial_sl) or initial_sl)
    close_time_iso = _utc_iso()

    close_price = current_sl  # conservative estimate — _enrich_mia_from_deals overrides

    pnl = None
    if entry_price > 0 and close_price > 0 and close_volume > 0:
        if side == "long":
            pnl = round((close_price - entry_price) * close_volume, 2)
        elif side == "short":
            pnl = round((entry_price - close_price) * close_volume, 2)

    _resolved_strategy = known_entry.get("strategy", "")
    _resolved_magic = known_entry.get("magic") or known_entry.get("detail", {}).get(
        "request", {}
    ).get("magic", 0)
    if not _resolved_strategy and _resolved_magic:
        try:
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass

    return {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": close_time_iso,
        "message_id": f"mia_close_{known_entry.get('message_id', 'unknown')}",
        "target": "exec_bridge",
        "ack_status": "closed",
        "detail": {
            "reason": "mia_close",
            "close_price": close_price,
            "pnl": pnl,
            "mia_detected_at": close_time_iso,
            "entry_price": entry_price,
        },
        "symbol": known_entry.get("symbol") or symbol,
        "action": "close",
        "side": side,
        "volume": close_volume,
        "entry_price": entry_price,
        "pnl": pnl,
        "label": "loss"
        if (pnl is not None and pnl < 0)
        else ("win" if (pnl is not None and pnl > 0) else "breakeven"),
        "position_ticket": pos.ticket,
        "magic": _resolved_magic,
        "strategy": _resolved_strategy,
        "sl": initial_sl,
        "tp": initial_tp,
        "trail_contribution": {
            "initial_sl": initial_sl,
            "final_sl": current_sl,
            "trail_advances": getattr(pos, "trail_advances", 0),
        },
        "open_message_id": known_entry.get("message_id"),
        "brain_ids": known_entry.get("brain_ids"),
        # DQAF-20260708-003: SL-estimate provenance — overridden by
        # enrich_mia_from_deals() once broker deal history is available.
        "_close_price_source": "mia_sl_estimate",
        "_pnl_status": (
            "estimated_from_close_price" if pnl is not None else "pending_mt5_confirmation"
        ),
    }


def enrich_mia_from_deals(
    mia_entry: dict[str, Any],
    deals: list[Any],
) -> None:
    """Enrich an MIA close entry with actual MT5 deal history data.

    Overrides the conservative SL-hit estimate with actual close_price
    and close_reason from deal history.  Mutates mia_entry in place.
    """
    from core.runtime.deal_selection import resolve_exit_deal

    _res = resolve_exit_deal(deals) if deals else None
    # ``has_exit`` already guarantees ``close_price is not None``; the explicit
    # ``is None`` disjunct narrows the type for the arithmetic below and is
    # defensive in depth.
    if _res is None or not _res.has_exit or _res.close_price is None:
        # No authoritative exit deal — keep the conservative SL estimate that
        # build_mia_close_entry() already set, and record provenance so the
        # non-broker-verified close is not mistaken for a real exit price.
        mia_entry["_close_price_source"] = (
            _res.close_price_source if _res is not None else "no_deals"
        )
        return

    close_price = _res.close_price
    close_time = _res.close_time
    close_reason = _res.close_reason

    mia_entry["detail"]["close_price"] = close_price
    mia_entry["_close_price_source"] = _res.close_price_source
    close_reason_str = {4: "sl_hit", 5: "tp_hit"}.get(close_reason or 0, "unknown_close")
    mia_entry["detail"]["reason"] = close_reason_str

    side = mia_entry.get("side", "")
    entry_price = mia_entry.get("detail", {}).get("entry_price") or mia_entry.get("entry_price", 0)
    close_volume = mia_entry.get("volume", 0) or 0.0
    if close_volume <= 0.0 and _res.close_volume > 0:
        close_volume = _res.close_volume
        mia_entry["volume"] = close_volume

    # FIX-20260626-143 / DQAF-20260708-003: prefer MT5 deal.profit
    # (SSOT-aggregated across exit deals) — broker-authoritative, accounts for
    # slippage/commission/swap.  Fall back to price-based estimate only when no
    # deal carries a profit.
    _deal_profit: float | None = _res.close_pnl

    if _deal_profit is not None:
        mia_entry["pnl"] = _deal_profit
        mia_entry["_pnl_status"] = "verified_from_mt5_deal"
        if isinstance(mia_entry.get("detail"), dict):
            mia_entry["detail"]["pnl"] = _deal_profit
    elif isinstance(entry_price, int | float) and entry_price > 0 and close_price > 0:
        if side == "long":
            mia_entry["pnl"] = round((close_price - entry_price) * float(close_volume), 2)
        elif side == "short":
            mia_entry["pnl"] = round((entry_price - close_price) * float(close_volume), 2)
        mia_entry["_pnl_status"] = "estimated_from_close_price"
        if isinstance(mia_entry.get("detail"), dict):
            mia_entry["detail"]["pnl"] = mia_entry["pnl"]

    if mia_entry.get("pnl") is not None:
        # Use PnlGuard for safe label classification
        from core.ledger.services.pnl_guard import PnlGuard

        if mia_entry.get("_pnl_status") != "verified_from_mt5_deal":
            mia_entry["label"] = PnlGuard.classify_label(mia_entry)
        else:
            pnl = mia_entry["pnl"]
            if pnl < 0:
                mia_entry["label"] = "loss"
            elif pnl > 0:
                mia_entry["label"] = "win"
            else:
                mia_entry["label"] = "breakeven"

    if close_reason == 4:
        _tc = mia_entry.get("trail_contribution", {})
        if isinstance(_tc, dict) and _tc.get("trail_advances", 0) > 0:
            mia_entry["label"] = "sl_hit_trailed"
        else:
            mia_entry["label"] = "sl_hit_first"
    elif close_reason == 5:
        mia_entry["label"] = "tp_hit_first"

    if close_time is not None:
        mia_entry["recorded_at"] = (
            datetime.fromtimestamp(close_time, tz=UTC).isoformat().replace("+00:00", "Z")
        )

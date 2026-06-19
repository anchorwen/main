"""MIA (Missing In Action) close journal entry builders.

Strangler Fig #20: extracted from live_cycle.py.
Pure data-transformation functions — no MT5 I/O, receive data via parameters.

Related FIXes: FIX-20260610-004 (close_volume=0 fix), FIX-20260610-006 (trail telemetry)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.runtime.fault_handler import fail_open_guard


def _utc_iso() -> str:
    """Minimal UTC ISO timestamp — mirror of live_cycle._utc_iso()."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        with fail_open_guard("MIA_MagicResolution"):
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")

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
    }


def enrich_mia_from_deals(
    mia_entry: dict[str, Any],
    deals: list[Any],
) -> None:
    """Enrich an MIA close entry with actual MT5 deal history data.

    Overrides the conservative SL-hit estimate with actual close_price
    and close_reason from deal history.  Mutates mia_entry in place.
    """
    close_price = None
    close_time = None
    close_reason: int | None = None

    for deal in deals:
        deal_reason = getattr(deal, "reason", -1)
        if deal_reason in (4, 5):  # DEAL_REASON_SL=4, DEAL_REASON_TP=5
            close_price = getattr(deal, "price", None)
            close_time = getattr(deal, "time", None)
            close_reason = deal_reason

    if close_price is None and deals and len(deals) >= 2:
        exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
        if exit_deals:
            last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
            close_price = getattr(last_exit, "price", None)
            close_time = getattr(last_exit, "time", None)
        if close_price is None:
            last_deal = max(deals, key=lambda d: getattr(d, "time", 0))
            close_price = getattr(last_deal, "price", None)
            close_time = getattr(last_deal, "time", None)

    if close_price is not None:
        mia_entry["detail"]["close_price"] = close_price
        close_reason_str = {4: "sl_hit", 5: "tp_hit"}.get(close_reason or 0, "unknown_close")
        mia_entry["detail"]["reason"] = close_reason_str

        side = mia_entry.get("side", "")
        entry_price = mia_entry.get("detail", {}).get("entry_price") or mia_entry.get(
            "entry_price", 0
        )
        close_volume = mia_entry.get("volume", 0) or 0.0

        if close_volume <= 0.0 and deals:
            _deal_volume = 0.0
            for _d in deals:
                if getattr(_d, "entry", -1) == 1:
                    _dv = float(getattr(_d, "volume", 0) or 0)
                    _deal_volume += _dv
            if _deal_volume > 0:
                close_volume = _deal_volume
                mia_entry["volume"] = close_volume

        if isinstance(entry_price, int | float) and entry_price > 0 and close_price > 0:
            if side == "long":
                mia_entry["pnl"] = round((close_price - entry_price) * float(close_volume), 2)
            elif side == "short":
                mia_entry["pnl"] = round((entry_price - close_price) * float(close_volume), 2)
            if isinstance(mia_entry.get("detail"), dict):
                mia_entry["detail"]["pnl"] = mia_entry["pnl"]
        if mia_entry.get("pnl") is not None:
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

"""Signal settlement — Strangler Fig #32 from live_cycle.py.

Extracted from live_cycle.py (~52 lines).  Settles brain signals against
actual trade PnL at close time, writing PnLEvent records to the ledger.
This replaces the legacy TTL-based bar settlement with ground-truth
trade outcome measurement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard


def settle_closed_trade_signals(
    evt: Any,
    base_dir: str,
) -> None:
    """Settle brain signals against actual trade PnL.

    For each brain_id attached to a closed trade event, writes a
    SignalSettled PnLEvent to the ledger with the real entry/exit prices
    and computed PnL(R).  This is ground truth — if a brain voted LONG
    and the trade lost, it records a loss.

    Args:
        evt: Close event with ``brain_ids``, ``symbol``, ``side``,
            ``entry_price``, ``close_price``, ``position_ticket``.
        base_dir: Live config base directory for ledger path.
    """
    if not getattr(evt, "brain_ids", None):
        return

    with fail_open_guard("SignalSettledWrite"):
        from core.contracts.events import PnLEvent
        from core.data.event_writer import EventWriter

        _ledger_path = str(Path(base_dir) / "ledger_events.jsonl")
        _writer = EventWriter(_ledger_path)
        _settled_ts = datetime.now(UTC)

        _entry_price = float(getattr(evt, "entry_price", 0) or 0)
        _close_price = float(getattr(evt, "close_price", 0) or 0)
        _pnl_r = (
            (_close_price - _entry_price) / _entry_price
            if _entry_price > 0 and evt.side == "long"
            else (_entry_price - _close_price) / _entry_price
            if _entry_price > 0
            else 0.0
        )

        for _brain_id in evt.brain_ids:
            _event = PnLEvent(
                timestamp=_settled_ts,
                source="live",
                event_type="SignalSettled",
                brain_id=str(_brain_id),
                symbol=evt.symbol,
                direction=evt.side,
                entry_price=_entry_price,
                exit_price=_close_price,
                pnl_r=round(_pnl_r, 6),
                position_ticket=evt.position_ticket,
                generated_by="live_cycle.reconciliation",
            )
            _writer.write(_event)

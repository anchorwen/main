"""MIA close reentry recording — Strangler Fig #30 from live_cycle.py.

Extracted from live_cycle.py (~51 lines).  Processes MIA (Missing In Action)
close entries and records exit events for the reentry guard system.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime
from typing import Any

from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout."""
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def record_mia_exits_for_reentry(
    mia_closed: list[dict[str, Any]],
    state: Any,
) -> None:
    """Record MIA-close entries as exit events for reentry guard.

    For each MIA close entry, parses close price, timestamp, strategy,
    and confidence from the entry data, builds an ExitRecord, and records
    it via ``ensure_reentry_state()``.

    Args:
        mia_closed: List of MIA close entry dicts (from
            ``state._pending_mia_closes``).
        state: LiveCycleState, reads ``_reentry_states``.
    """
    for _entry in mia_closed:
        _exit_strategy = _entry.get("strategy", "")
        _exit_side = _entry.get("side", "")
        _exit_price = float(_entry.get("detail", {}).get("close_price", 0) or 0)
        _exit_ts_str = _entry.get("recorded_at", "")
        _exit_ts = _time.time()
        if _exit_ts_str:
            try:
                _parsed = datetime.fromisoformat(_exit_ts_str.replace("Z", "+00:00"))
                _exit_ts = _parsed.timestamp()
            except (ValueError, OSError):
                pass
        _exit_confidence = (
            _entry.get("entry_consensus", {}).get("consensus_score", 0.5)
            if isinstance(_entry.get("entry_consensus"), dict)
            else 0.5
        )
        _exit_reason = _entry.get("detail", {}).get("reason", "mia_close")
        if _exit_strategy and _exit_side in ("long", "short"):
            try:
                from core.execution.reentry_guard import (
                    ExitRecord,
                    ensure_reentry_state,
                )

                _mia_rec = ExitRecord(
                    timestamp=_exit_ts,
                    strategy_name=_exit_strategy,
                    direction=_exit_side,
                    reason=_exit_reason,
                    confidence=float(_exit_confidence),
                    price=_exit_price,
                    ticket=_entry.get("position_ticket", 0),
                )
                _rs = ensure_reentry_state(state._reentry_states, _exit_strategy)
                _rs.record_exit(_mia_rec)
                _emit(
                    "mia_close_reentry_recorded",
                    ticket=_entry.get("position_ticket"),
                    strategy=_exit_strategy,
                    direction=_exit_side,
                    reason=_exit_reason,
                    close_price=_exit_price,
                    pnl=_entry.get("pnl"),
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass

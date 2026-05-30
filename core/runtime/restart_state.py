"""Restart state bootstrap — replay journal closes to restore runtime guards.

Extracted from live_cycle.py per the Strangler Fig pattern (#8).
Called once on first cycle after restart to populate reentry states,
SL streak counters, and graduated cooldown from recent journal history.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any


def bootstrap_restart_state(state: Any, journal_path: str, config: Any) -> None:
    """Replay recent journal close entries to restore runtime guard state.

    Called once on the first cycle after restart.  Scans the last 30 min
    of journal closes and populates:
      - _reentry_states  (most recent exit per strategy)
      - _pending_sl_records  (all SL/loss events for graduated cooldown)
      - consecutive_sl_hits  (per-strategy SL streak counter)
    """
    from pathlib import Path as _Path

    _jp = _Path(journal_path)
    if not _jp.exists():
        return

    now = time.time()
    cutoff = now - 1800.0  # 30 min lookback
    _close_entries: list[dict[str, Any]] = []

    # Build set of message_ids for currently-open positions so we skip
    # their closes (those will be handled by normal reconciliation).
    _active_open_mids: set[str] = {
        _v.get("message_id", "")
        for _v in state.known_open_tickets.values()
        if _v.get("message_id", "")
    }

    try:
        for _line in _jp.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line:
                continue
            try:
                _entry = json.loads(_line)
            except json.JSONDecodeError:
                continue

            _action = _entry.get("action", "")
            if _action not in ("close",):
                continue

            # Skip closes whose open is still tracked (will be reconciled normally)
            _open_mid = _entry.get("open_message_id", "")
            if _open_mid and _open_mid in _active_open_mids:
                continue

            # Check timestamp
            _ts_str = _entry.get("recorded_at", "")
            try:
                if _ts_str:
                    _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00")).timestamp()
                else:
                    continue
            except Exception:
                continue

            if _ts < cutoff:
                continue

            _close_entries.append(_entry)
    except Exception:
        return

    if not _close_entries:
        return

    # Sort by timestamp ascending
    _close_entries.sort(key=lambda e: e.get("recorded_at", ""))

    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

    for _entry in _close_entries:
        _strategy = _entry.get("strategy", "")
        if not _strategy:
            _magic = _entry.get("magic", 0)
            if _magic:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M

                _strategy = _M.get(_magic, "")
        if not _strategy:
            continue

        _side = _entry.get("side", "")
        _label = _entry.get("label", "")
        _close_price = _entry.get("detail", {}).get("close_price") or 0.0
        _ticket = _entry.get("position_ticket", 0)
        _reason = _entry.get("detail", {}).get("reason", "unknown_close")

        # ── Record exit for re-entry guard ──
        if _side in ("long", "short"):
            try:
                _rec = ExitRecord(
                    timestamp=now,  # use now — we only care about "has recent exit"
                    strategy_name=_strategy,
                    direction=_side,
                    reason=_reason,
                    confidence=0.5,  # unknown, conservative
                    price=float(_close_price) if _close_price else 0.0,
                    ticket=int(_ticket) if _ticket else 0,
                )
                _rs = ensure_reentry_state(state._reentry_states, _strategy)
                _rs.record_exit(_rec)
            except Exception:
                pass

        # ── Count SL/loss for streak tracker ──
        if _label in ("sl_hit_first", "loss"):
            _curr = state.consecutive_sl_hits.get(_strategy, 0) + 1
            state.consecutive_sl_hits[_strategy] = _curr
            state._pending_sl_records.append(
                {
                    "strategy": _strategy,
                    "timestamp": now,
                }
            )
            if _curr >= 3:
                state.sl_streak_blocked_until[_strategy] = now + 1800
        elif _label in ("tp_hit_first", "win"):
            state.consecutive_sl_hits[_strategy] = 0

    if state._pending_sl_records:
        print(
            json.dumps(
                {
                    "event": "restart_state_bootstrapped",
                    "time": datetime.now(UTC).isoformat(),
                    "close_entries_replayed": len(_close_entries),
                    "sl_records": len(state._pending_sl_records),
                    "reentry_strategies": list(state._reentry_states.keys()),
                    "sl_streaks": dict(state.consecutive_sl_hits),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

"""Pre-close market check — pure function extracted from live_cycle.py.

Strangler Fig #23 (FIX-20260619-041): Extracted the calendar check and
decision logic.  Side effects (position flattening) remain in live_cycle.py
to avoid circular imports (_dispatch_managed_close).

Pure function contract: zero I/O beyond the calendar load, deterministic
given (now_utc, symbol, calendar_path).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def check_pre_close(now_utc: datetime | None, symbol: str, calendar_path: str) -> dict[str, Any]:
    """Check if we are approaching a market close.

    Args:
        now_utc: Current time in UTC (or None for datetime.now(UTC)).
        symbol: Trading symbol (e.g. "XAUUSDc").
        calendar_path: Path to market calendar JSON.

    Returns:
        Dict with keys: in_pre_close, minutes_to_close, no_new_positions,
        must_flatten, close_label.  Empty dict = no action needed.
    """
    from core.market.calendar import evaluate_pre_close, load_calendar

    _now = now_utc if now_utc is not None else datetime.now(UTC)
    cal = load_calendar(calendar_path)
    result = evaluate_pre_close(now_utc=_now, symbol=symbol, config=cal)
    if not result.get("in_pre_close"):
        return {}
    return result

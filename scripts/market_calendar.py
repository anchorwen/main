"""UTC market calendar evaluation for live dispatch blocking."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any


def load_calendar(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {
            "schema_version": "market_calendar.v1",
            "weekly_blackout": {"utc_weekend": False},
            "fixed_blackouts": [],
            "session_buffers": {"enabled": False},
        }
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_iso_z(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)


def evaluate_utc_blackout(
    *, now_utc: datetime, symbol: str | None, config: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Return (blocked, reasons)."""
    reasons: list[str] = []
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    wb = config.get("weekly_blackout") or {}
    if wb.get("utc_weekend"):
        wd = now_utc.weekday()
        if wd in (5, 6):
            reasons.append("utc_weekend_blackout")

    for window in config.get("fixed_blackouts") or []:
        try:
            start = _parse_iso_z(str(window["start"]))
            end = _parse_iso_z(str(window["end"]))
        except (KeyError, ValueError):
            continue
        if start <= now_utc < end:
            reasons.append(f"fixed_blackout:{window.get('label', 'unnamed')}")

    sb = config.get("session_buffers") or {}
    overrides = (config.get("symbol_overrides") or {}).get(symbol or "", {}) if symbol else {}
    if sb.get("enabled"):
        wd = int(sb.get("anchor_weekday_utc", 0))
        hour = int(sb.get("anchor_hour_utc", 0))
        minute = int(sb.get("anchor_minute_utc", 0))
        buf_min = int(overrides.get("buffer_minutes", sb.get("buffer_minutes", 45)))

        days_back = (now_utc.weekday() - wd) % 7
        anchor_day = now_utc.date() - timedelta(days=days_back)
        anchor = datetime.combine(anchor_day, time(hour, minute), tzinfo=UTC)
        end_buf = anchor + timedelta(minutes=buf_min)
        if anchor <= now_utc < end_buf:
            reasons.append(f"session_buffer_weekly_open({buf_min}m_after_anchor_utc)")

    return (len(reasons) > 0, reasons)

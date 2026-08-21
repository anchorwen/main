"""UTC market calendar evaluation for live dispatch blocking.

Extracted from scripts/market_calendar.py to eliminate reverse dependency
(core → scripts). Pure functions with zero core dependencies.
"""

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

    wcw = wb.get("utc_close_window")
    if wcw:
        start, end = _weekly_close_window_bounds(now_utc, wcw)
        if start <= now_utc < end:
            reasons.append("weekly_close_window")

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


def evaluate_pre_close(
    *, now_utc: datetime, symbol: str | None, config: dict[str, Any]
) -> dict[str, Any]:
    """Check if we are approaching a market close that requires action.

    Returns a dict with:
      - in_pre_close: bool — within any pre-close window (tighten + aggressive + flatten)
      - in_tighten: bool — Phase 1: T-tighten_start to T-no_new (exit tightening only)
      - minutes_to_close: float | None — minutes until market close
      - no_new_positions: bool — Phase 2: T-no_new to T-flatten (stop opening)
      - must_flatten: bool — Phase 3: T-flatten to T-0 (close all positions)
      - phase: str — "tighten" | "aggressive" | "flatten" | ""
      - close_label: str — human-readable label for logging
    """
    base: dict[str, Any] = {
        "in_pre_close": False,
        "in_tighten": False,
        "minutes_to_close": None,
        "no_new_positions": False,
        "must_flatten": False,
        "phase": "",
        "close_label": "",
    }

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    # Per-symbol overrides for any field (weekly_close, fixed_blackouts, etc.)
    overrides = (config.get("symbol_overrides") or {}).get(symbol or "", {}) if symbol else {}

    for window in config.get("fixed_blackouts") or []:
        try:
            start = _parse_iso_z(str(window["start"]))
        except (KeyError, ValueError):
            continue
        pre_close_min = int(window.get("pre_close_minutes", 30))
        flatten_min = int(window.get("flatten_minutes", 5))
        pre_close_start = start - timedelta(minutes=pre_close_min)
        flatten_start = start - timedelta(minutes=flatten_min)
        if pre_close_start <= now_utc < start:
            delta = start - now_utc
            minutes = delta.total_seconds() / 60.0
            base["in_pre_close"] = True
            base["minutes_to_close"] = str(round(minutes, 1))
            base["no_new_positions"] = True
            base["must_flatten"] = now_utc >= flatten_start
            base["phase"] = "flatten" if base["must_flatten"] else "aggressive"
            base["close_label"] = str(window.get("label", "holiday_close"))
            return base

    wc = config.get("weekly_close") or {}
    wc_override = overrides.get("weekly_close") or {}

    # Per-symbol override can explicitly disable weekly_close (e.g. BTC 24/7)
    if wc_override.get("enabled") is False:
        return base
    if not wc.get("enabled"):
        return base

    close_wd = int(wc_override.get("close_weekday_utc", wc.get("close_weekday_utc", 4)))
    close_h = int(wc_override.get("close_hour_utc", wc.get("close_hour_utc", 21)))
    close_m = int(wc_override.get("close_minute_utc", wc.get("close_minute_utc", 0)))
    tighten_m = int(wc_override.get("tighten_start_minutes", wc.get("tighten_start_minutes", 0)))
    no_new_m = int(
        wc_override.get("no_new_positions_minutes", wc.get("no_new_positions_minutes", 30))
    )
    flatten_m = int(wc_override.get("flatten_all_minutes", wc.get("flatten_all_minutes", 5)))

    days_ahead = (close_wd - now_utc.weekday()) % 7
    if days_ahead == 0 and (
        now_utc.hour > close_h or (now_utc.hour == close_h and now_utc.minute >= close_m)
    ):
        days_ahead = 7
    close_dt = datetime.combine(
        now_utc.date() + timedelta(days=days_ahead),
        time(close_h, close_m),
        tzinfo=UTC,
    )

    tighten_start = close_dt - timedelta(minutes=tighten_m) if tighten_m > 0 else None
    pre_close_start = close_dt - timedelta(minutes=no_new_m)
    flatten_start = close_dt - timedelta(minutes=flatten_m)

    if tighten_start and tighten_start <= now_utc < pre_close_start:
        # Phase 1: T-tighten → T-no_new — exit tightening only, new positions still allowed
        delta = close_dt - now_utc
        minutes = delta.total_seconds() / 60.0
        base["in_pre_close"] = True
        base["in_tighten"] = True
        base["minutes_to_close"] = str(round(minutes, 1))
        base["phase"] = "tighten"
        base["close_label"] = "weekly_close"
        return base

    if pre_close_start <= now_utc < close_dt:
        delta = close_dt - now_utc
        minutes = delta.total_seconds() / 60.0
        base["in_pre_close"] = True
        base["minutes_to_close"] = str(round(minutes, 1))
        base["no_new_positions"] = True
        base["must_flatten"] = now_utc >= flatten_start
        base["phase"] = "flatten" if base["must_flatten"] else "aggressive"
        base["close_label"] = "weekly_close"

    return base


# ---------------------------------------------------------------------------
# The Calendar Grid — market-type presets & the single staleness clock
# (FIX-20260821-001, DQAF-20260820-005: TECH_DEBT-011 / TECH_DEBT-012 batch)
# ---------------------------------------------------------------------------
# Single source of truth for "is the market open / when did it last close".
# Live core (health_checks) and offline audit (scripts/audit_data_chain_integrity)
# both consume these presets via staleness_anchor — exactly ONE clock (IC ruling).
# crypto_24_7 never closes → staleness never relaxes.

MARKET_TYPE_PRESETS: dict[str, dict[str, Any]] = {
    # XAUUSD spot: weekday trading, closed Fri 22:00 UTC → Sun 22:00 UTC.
    "forex_24_5": {
        "weekly_blackout": {
            "utc_close_window": {
                "start_weekday": 4,
                "start_hour": 22,
                "start_minute": 0,
                "end_weekday": 6,
                "end_hour": 22,
                "end_minute": 0,
            },
        },
        "weekly_close": {
            "enabled": True,
            "close_weekday_utc": 4,
            "close_hour_utc": 22,
            "close_minute_utc": 0,
        },
        "fixed_blackouts": [],
        "session_buffers": {"enabled": False},
    },
    # BTCUSDC: 24/7 — never blocked, staleness never relaxes.
    "crypto_24_7": {
        "weekly_blackout": {},
        "weekly_close": {"enabled": False},
        "fixed_blackouts": [],
        "session_buffers": {"enabled": False},
    },
}


def resolve_calendar(
    market_type: str | None, user_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve a market calendar from the preset grid + optional user overrides.

    user_config (holiday blackouts, per-symbol overrides) wins per-field over
    the preset.  Unknown market_type → empty config (never blocked)."""
    merged: dict[str, Any] = dict(MARKET_TYPE_PRESETS.get(market_type or "", {}))
    if user_config:
        for key, val in user_config.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **val}
            else:
                merged[key] = val
    return merged


def _weekly_close_window_bounds(
    now_utc: datetime, wcw: dict[str, Any]
) -> tuple[datetime, datetime]:
    """Weekly recurring close window containing now_utc → (start, end)."""
    start_wd = int(wcw.get("start_weekday", 4))
    start_h = int(wcw.get("start_hour", 22))
    start_m = int(wcw.get("start_minute", 0))
    end_wd = int(wcw.get("end_weekday", 6))
    end_h = int(wcw.get("end_hour", 22))
    end_m = int(wcw.get("end_minute", 0))
    days_back = (now_utc.weekday() - start_wd) % 7
    start = datetime.combine(
        now_utc.date() - timedelta(days=days_back), time(start_h, start_m), tzinfo=UTC
    )
    span_days = (end_wd - start_wd) % 7
    end = start + timedelta(days=span_days, hours=end_h - start_h, minutes=end_m - start_m)
    return start, end


def is_market_open(
    *,
    now_utc: datetime,
    market_type: str | None,
    user_config: dict[str, Any] | None = None,
) -> bool:
    """True when the market type is currently trading (not blacked out)."""
    cfg = resolve_calendar(market_type, user_config)
    blocked, _ = evaluate_utc_blackout(now_utc=now_utc, symbol=None, config=cfg)
    return not blocked


def last_market_close(
    *,
    now_utc: datetime,
    market_type: str | None,
    user_config: dict[str, Any] | None = None,
) -> datetime:
    """Most recent point at which the market stopped trading.

    crypto_24_7 (never closes) → returns now_utc: a closed-market anchor would
    otherwise freeze BTC staleness checks forever."""
    cfg = resolve_calendar(market_type, user_config)
    wcw = (cfg.get("weekly_blackout") or {}).get("utc_close_window")
    if wcw:
        start, end = _weekly_close_window_bounds(now_utc, wcw)
        if start <= now_utc < end:
            return start  # currently closed → this week's close
        return start if now_utc >= end else start - timedelta(days=7)
    return now_utc


def staleness_anchor(
    *,
    now_utc: datetime,
    market_type: str | None,
    base_threshold_min: float = 720.0,
    user_config: dict[str, Any] | None = None,
) -> datetime:
    """Data must have advanced to at least this point for a staleness PASS.

    - Market open     → now − base_threshold  (identical to a plain age check)
    - Market closed   → last close − base_threshold  (freeze at close is
      EXPECTED; the threshold also absorbs close-boundary skew)
    - crypto_24_7     → always the open branch → zero behavior change.
    """
    now = now_utc
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    if is_market_open(now_utc=now, market_type=market_type, user_config=user_config):
        return now - timedelta(minutes=base_threshold_min)
    close = last_market_close(now_utc=now, market_type=market_type, user_config=user_config)
    return close - timedelta(minutes=base_threshold_min)

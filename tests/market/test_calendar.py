"""Unit tests for market calendar — pure UTC blackout evaluation.

evaluate_utc_blackout and evaluate_pre_close are pure functions:
datetime + config → (blocked, reasons/dict). Zero I/O.
Part of Test 3: market dedicated test suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.market.calendar import (
    evaluate_pre_close,
    evaluate_utc_blackout,
    is_market_open,
    last_market_close,
    resolve_calendar,
    staleness_anchor,
)

# ── Test configs ──────────────────────────────────────────────────────────


@pytest.fixture
def empty_config():
    return {
        "schema_version": "v1",
        "weekly_blackout": {},
        "fixed_blackouts": [],
        "session_buffers": {},
    }


@pytest.fixture
def weekend_config():
    return {
        "weekly_blackout": {"utc_weekend": True},
        "fixed_blackouts": [],
        "session_buffers": {},
    }


# ── evaluate_utc_blackout ─────────────────────────────────────────────────


class TestUTCBlackout:
    def test_empty_config_not_blocked(self, empty_config):
        now = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
        blocked, reasons = evaluate_utc_blackout(now_utc=now, symbol=None, config=empty_config)
        assert blocked is False
        assert reasons == []

    def test_saturday_blocked(self, weekend_config):
        saturday = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)  # Saturday
        blocked, _ = evaluate_utc_blackout(now_utc=saturday, symbol=None, config=weekend_config)
        assert blocked is True

    def test_sunday_blocked(self, weekend_config):
        sunday = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)  # Sunday
        blocked, _ = evaluate_utc_blackout(now_utc=sunday, symbol=None, config=weekend_config)
        assert blocked is True

    def test_monday_not_blocked(self, weekend_config):
        monday = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
        blocked, _ = evaluate_utc_blackout(now_utc=monday, symbol=None, config=weekend_config)
        assert blocked is False

    def test_fixed_blackout_active(self):
        config = {
            "weekly_blackout": {},
            "fixed_blackouts": [
                {"start": "2026-06-17T10:00:00Z", "end": "2026-06-17T14:00:00Z", "label": "FOMC"}
            ],
            "session_buffers": {},
        }
        now = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
        blocked, reasons = evaluate_utc_blackout(now_utc=now, symbol=None, config=config)
        assert blocked is True
        assert "FOMC" in reasons[0]

    def test_fixed_blackout_expired(self):
        config = {
            "weekly_blackout": {},
            "fixed_blackouts": [{"start": "2026-06-17T10:00:00Z", "end": "2026-06-17T14:00:00Z"}],
            "session_buffers": {},
        }
        now = datetime(2026, 6, 17, 15, 0, tzinfo=UTC)  # after
        blocked, _ = evaluate_utc_blackout(now_utc=now, symbol=None, config=config)
        assert blocked is False

    def test_no_tzinfo_assumes_utc(self):
        """Naive datetime is treated as UTC."""
        now = datetime(2026, 6, 20, 12, 0)  # Saturday, no tzinfo
        blocked, _ = evaluate_utc_blackout(
            now_utc=now,
            symbol=None,
            config={
                "weekly_blackout": {"utc_weekend": True},
                "fixed_blackouts": [],
                "session_buffers": {},
            },
        )
        assert blocked is True


# ── evaluate_pre_close ────────────────────────────────────────────────────


class TestPreClose:
    def test_no_close_info_returns_false(self):
        now = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
        config = {
            "weekly_blackout": {},
            "fixed_blackouts": [],
            "session_buffers": {"enabled": False},
            "close_sessions": [],
        }
        result = evaluate_pre_close(now_utc=now, symbol=None, config=config)
        assert result["in_pre_close"] is False

    def test_returns_expected_keys(self):
        now = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
        result = evaluate_pre_close(
            now_utc=now,
            symbol=None,
            config={
                "weekly_blackout": {},
                "fixed_blackouts": [],
                "session_buffers": {"enabled": False},
                "close_sessions": [],
            },
        )
        assert "in_pre_close" in result
        assert "no_new_positions" in result
        assert "must_flatten" in result


# ── The Calendar Grid (FIX-20260821-001, TECH_DEBT-011/012 batch) ──────────
# Reference dates (all UTC): 2026-08-14=Friday, 2026-08-15=Saturday,
# 2026-08-16=Sunday, 2026-08-17=Monday.  forex_24_5 closes Fri 22:00 → Sun 22:00.


class TestCalendarGrid:
    # ── is_market_open / last_market_close ────────────────────────────────
    def test_forex_open_weekday(self):
        now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)  # Friday 12:00
        assert is_market_open(now_utc=now, market_type="forex_24_5") is True

    def test_forex_closed_weekend(self):
        now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)  # Saturday 10:00
        assert is_market_open(now_utc=now, market_type="forex_24_5") is False

    def test_forex_reopened_sunday_evening(self):
        now = datetime(2026, 8, 16, 23, 0, tzinfo=UTC)  # Sunday 23:00 (reopened)
        assert is_market_open(now_utc=now, market_type="forex_24_5") is True

    def test_crypto_always_open(self):
        now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)  # Saturday 10:00
        assert is_market_open(now_utc=now, market_type="crypto_24_7") is True

    def test_last_close_forex_weekend(self):
        now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)  # Saturday
        assert last_market_close(now_utc=now, market_type="forex_24_5") == datetime(
            2026, 8, 14, 22, 0, tzinfo=UTC
        )

    def test_last_close_forex_monday(self):
        now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)  # Monday (reopened)
        assert last_market_close(now_utc=now, market_type="forex_24_5") == datetime(
            2026, 8, 14, 22, 0, tzinfo=UTC
        )

    def test_last_close_forex_before_close(self):
        now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)  # Friday midday
        assert last_market_close(now_utc=now, market_type="forex_24_5") == datetime(
            2026,
            8,
            7,
            22,
            0,
            tzinfo=UTC,  # last Friday's close
        )

    # ── staleness_anchor ──────────────────────────────────────────────────
    def test_anchor_forex_open_equals_now_minus_base(self):
        now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
        assert staleness_anchor(now_utc=now, market_type="forex_24_5", base_threshold_min=720) == (
            now - timedelta(minutes=720)
        )

    def test_anchor_forex_closed_shifts_to_last_close(self):
        now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)  # Saturday
        anchor = staleness_anchor(now_utc=now, market_type="forex_24_5", base_threshold_min=720)
        assert anchor == datetime(2026, 8, 14, 22, 0, tzinfo=UTC) - timedelta(minutes=720)

    def test_anchor_crypto_never_relaxes(self):
        now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)  # Saturday
        assert staleness_anchor(now_utc=now, market_type="crypto_24_7", base_threshold_min=720) == (
            now - timedelta(minutes=720)
        )

    # ── utc_close_window within evaluate_utc_blackout ─────────────────────
    def test_close_window_blocks_saturday(self):
        cfg = resolve_calendar("forex_24_5")
        sat = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
        blocked, reasons = evaluate_utc_blackout(now_utc=sat, symbol=None, config=cfg)
        assert blocked is True
        assert "weekly_close_window" in reasons

    def test_close_window_reopens_sunday_evening(self):
        cfg = resolve_calendar("forex_24_5")
        sun = datetime(2026, 8, 16, 23, 0, tzinfo=UTC)
        blocked, _ = evaluate_utc_blackout(now_utc=sun, symbol=None, config=cfg)
        assert blocked is False

    def test_close_window_open_friday_midday(self):
        cfg = resolve_calendar("forex_24_5")
        fri = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
        blocked, _ = evaluate_utc_blackout(now_utc=fri, symbol=None, config=cfg)
        assert blocked is False

    # ── backward compatibility: utc_weekend configs unchanged ─────────────
    def test_utc_weekend_unchanged_sunday_evening_still_blocked(self, weekend_config):
        sun = datetime(2026, 8, 16, 23, 0, tzinfo=UTC)
        blocked, _ = evaluate_utc_blackout(now_utc=sun, symbol=None, config=weekend_config)
        assert blocked is True

    # ── user override merge ───────────────────────────────────────────────
    def test_resolve_calendar_merges_user_config(self):
        cfg = resolve_calendar(
            "forex_24_5",
            user_config={
                "fixed_blackouts": [
                    {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T12:00:00Z", "label": "X"}
                ]
            },
        )
        assert cfg["fixed_blackouts"][0]["label"] == "X"
        assert "utc_close_window" in cfg["weekly_blackout"]  # preset retained

    def test_holiday_blackout_via_user_config_blocks(self):
        user = {
            "fixed_blackouts": [
                {"start": "2026-08-13T00:00:00Z", "end": "2026-08-14T23:00:00Z", "label": "HOL"}
            ]
        }
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        assert is_market_open(now_utc=now, market_type="forex_24_5", user_config=user) is False

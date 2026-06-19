"""Unit tests for market calendar — pure UTC blackout evaluation.

evaluate_utc_blackout and evaluate_pre_close are pure functions:
datetime + config → (blocked, reasons/dict). Zero I/O.
Part of Test 3: market dedicated test suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.market.calendar import evaluate_pre_close, evaluate_utc_blackout


# ── Test configs ──────────────────────────────────────────────────────────


@pytest.fixture
def empty_config():
    return {"schema_version": "v1", "weekly_blackout": {}, "fixed_blackouts": [], "session_buffers": {}}


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
            "fixed_blackouts": [
                {"start": "2026-06-17T10:00:00Z", "end": "2026-06-17T14:00:00Z"}
            ],
            "session_buffers": {},
        }
        now = datetime(2026, 6, 17, 15, 0, tzinfo=UTC)  # after
        blocked, _ = evaluate_utc_blackout(now_utc=now, symbol=None, config=config)
        assert blocked is False

    def test_no_tzinfo_assumes_utc(self):
        """Naive datetime is treated as UTC."""
        now = datetime(2026, 6, 20, 12, 0)  # Saturday, no tzinfo
        blocked, _ = evaluate_utc_blackout(now_utc=now, symbol=None, config={"weekly_blackout": {"utc_weekend": True}, "fixed_blackouts": [], "session_buffers": {}})
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
        result = evaluate_pre_close(now_utc=now, symbol=None, config={"weekly_blackout": {}, "fixed_blackouts": [], "session_buffers": {"enabled": False}, "close_sessions": []})
        assert "in_pre_close" in result
        assert "no_new_positions" in result
        assert "must_flatten" in result

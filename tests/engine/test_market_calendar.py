"""Tests for UTC market calendar evaluation."""

from datetime import UTC, datetime

from scripts.market_calendar import evaluate_utc_blackout


def test_weekend_blackout_saturday_utc():
    cfg = {
        "weekly_blackout": {"utc_weekend": True},
        "fixed_blackouts": [],
        "session_buffers": {"enabled": False},
    }
    sat = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    blocked, reasons = evaluate_utc_blackout(now_utc=sat, symbol="XAUUSDc", config=cfg)
    assert blocked is True
    assert "utc_weekend_blackout" in reasons


def test_weekday_not_weekend():
    cfg = {
        "weekly_blackout": {"utc_weekend": True},
        "fixed_blackouts": [],
        "session_buffers": {"enabled": False},
    }
    wed = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
    blocked, reasons = evaluate_utc_blackout(now_utc=wed, symbol=None, config=cfg)
    assert blocked is False


def test_fixed_blackout_window():
    cfg = {
        "weekly_blackout": {"utc_weekend": False},
        "fixed_blackouts": [
            {"label": "x", "start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"}
        ],
        "session_buffers": {"enabled": False},
    }
    inside = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)
    blocked, reasons = evaluate_utc_blackout(now_utc=inside, symbol=None, config=cfg)
    assert blocked is True
    assert any("fixed_blackout" in r for r in reasons)


def test_session_buffer_after_anchor():
    cfg = {
        "weekly_blackout": {"utc_weekend": False},
        "fixed_blackouts": [],
        "session_buffers": {
            "enabled": True,
            "anchor_weekday_utc": 0,
            "anchor_hour_utc": 10,
            "anchor_minute_utc": 0,
            "buffer_minutes": 30,
        },
    }
    mon = datetime(2026, 6, 1, 10, 15, 0, tzinfo=UTC)
    blocked, reasons = evaluate_utc_blackout(now_utc=mon, symbol=None, config=cfg)
    assert blocked is True
    assert any("session_buffer" in r for r in reasons)

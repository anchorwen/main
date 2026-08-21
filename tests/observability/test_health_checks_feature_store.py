"""check_feature_store calendar-aware staleness (FIX-20260821-001).

The ad-hoc POST_OUTAGE 1440-min heuristic (FIX-20260629-172) is replaced by the
single calendar clock (core/market/calendar.staleness_anchor).  Because
check_feature_store evaluates against REAL datetime.now(UTC), the closed-market
(weekend) branch is tested with a mocked staleness_anchor; the branches that
are day-independent (unreadable / cold-start / crypto-24-7 / fresh-pass) run
against the real clock.

Previously there was ZERO test coverage for check_feature_store.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core.observability.data_health_schema import SourceStatus
from core.observability.health_checks import HealthCheckMethods


class _Host(HealthCheckMethods):
    """Minimal DataHealthService-shaped host exposing only what the check needs."""

    def __init__(
        self,
        base_dir: str,
        symbol: str,
        max_age: float,
        cold_grace: float,
        start_time: float,
    ) -> None:
        self._base_dir = base_dir
        self._symbol = symbol
        self._start_time = start_time
        self._thresholds = {
            "feature_store_max_age_minutes": max_age,
            "feature_store_cold_start_grace_minutes": cold_grace,
        }

    def _t(self, key: str) -> float:
        return self._thresholds.get(key, 0.0)


def _write_feature(base_dir: str, symbol: str, event_ts: str) -> None:
    path = (
        Path(base_dir)
        / "feature_store"
        / "records"
        / f"symbol={symbol}"
        / "timeframe=M5"
        / "features.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema_name": "technical_v1",
        "schema_version": "1.0",
        "symbol": symbol,
        "timeframe": "M5",
        "event_time": event_ts,
        "values": {"ema_bias": 1.0},
        "source": "test",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _run_check(
    tmp_path,
    symbol: str,
    *,
    max_age: float,
    cold_grace: float,
    uptime_min: float = 60.0,
):
    start_time = time.perf_counter() - uptime_min * 60.0
    host = _Host(str(tmp_path), symbol, max_age, cold_grace, start_time)
    return host.check_feature_store()


def _closed_anchors(close_dt: datetime, max_age: float):
    """staleness_anchor side_effect for a market closed at close_dt:
    warn_anchor = close − max_age, fail_anchor = close − 2×max_age."""
    warn = close_dt - timedelta(minutes=max_age)
    fail = close_dt - timedelta(minutes=max_age * 2)

    def _anchor(**kw: object) -> datetime:
        if kw["base_threshold_min"] == max_age:
            return warn
        return fail

    return _anchor


class TestFeatureStoreCalendarStaleness:
    def test_closed_market_freeze_passes(self, tmp_path):
        """XAU frozen at Fri 21:00, checked Sat (closed) → calendar anchor → PASS.

        Pre-fix: age 13h > 12h hardcode → S1-style false FAIL; POST_OUTAGE
        mask only covered >24h.  Now the anchor shifts to Fri 22:00 − max_age."""
        _write_feature(str(tmp_path), "XAUUSDc", "2026-08-14T21:00:00Z")  # Fri 21:00
        close = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)  # Fri 22:00 weekly close
        with (
            patch("core.observability.health_checks._age_minutes", return_value=780.0),
            patch(
                "core.observability.health_checks.staleness_anchor",
                side_effect=_closed_anchors(close, 720),
            ),
        ):
            res = _run_check(tmp_path, "XAUUSDc", max_age=720, cold_grace=30)
        assert res.status == SourceStatus.PASS
        assert res.primary_code == "FEATURE_STORE_OK"

    def test_closed_market_pre_close_break_fails(self, tmp_path):
        """XAU chain last wrote BEFORE Fri 22:00 − 2×max_age → genuine stall, FAIL
        even in a closed market (data broke before the close freeze)."""
        _write_feature(str(tmp_path), "XAUUSDc", "2026-08-13T20:00:00Z")  # Thu 20:00
        close = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
        with (
            patch("core.observability.health_checks._age_minutes", return_value=1500.0),
            patch(
                "core.observability.health_checks.staleness_anchor",
                side_effect=_closed_anchors(close, 720),
            ),
        ):
            res = _run_check(tmp_path, "XAUUSDc", max_age=720, cold_grace=30)
        assert res.status == SourceStatus.FAIL
        assert res.primary_code == "FEATURE_STORE_STALE"

    def test_crypto_never_relaxes_mid_age_warns(self, tmp_path):
        """BTC 24/7 → real clock, never relaxed: 18h-old store with 12h max_age → WARN."""
        _write_feature(
            str(tmp_path),
            "BTCUSDC",
            (datetime.now(UTC) - timedelta(hours=18)).isoformat(),
        )
        res = _run_check(tmp_path, "BTCUSDC", max_age=720, cold_grace=30)
        assert res.status == SourceStatus.WARN
        assert res.primary_code == "FEATURE_STORE_STALE"

    def test_timestamp_unreadable_warns(self, tmp_path):
        _write_feature(str(tmp_path), "XAUUSDc", "not-a-date")
        res = _run_check(tmp_path, "XAUUSDc", max_age=720, cold_grace=30)
        assert res.status == SourceStatus.WARN
        assert res.primary_code == "FEATURE_STORE_TIMESTAMP_UNREADABLE"

    def test_cold_start_downgrades_warn(self, tmp_path):
        _write_feature(
            str(tmp_path),
            "XAUUSDc",
            (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
        )
        res = _run_check(tmp_path, "XAUUSDc", max_age=720, cold_grace=60, uptime_min=0.1)
        assert res.status == SourceStatus.WARN
        assert res.primary_code == "FEATURE_STORE_COLD_START"

    def test_fresh_store_passes(self, tmp_path):
        _write_feature(
            str(tmp_path),
            "XAUUSDc",
            (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        res = _run_check(tmp_path, "XAUUSDc", max_age=720, cold_grace=30)
        assert res.status == SourceStatus.PASS
        assert res.primary_code == "FEATURE_STORE_OK"

"""P7 (TECH_DEBT-018): SSOT ``meta_pipeline_wired`` event file — module tests.

The wired-event SSOT (``core/observability/meta_wire_events.py``) decouples
the health check from the intent log file lifecycle: the producer appends
every successful wire here regardless of stdout routing. The check then reads
this file (tail, last line) instead of guessing where the event landed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from core.observability.meta_wire_events import (
    parse_intent_boot_ts,
    read_last_wired_event,
    record_wired_event,
    wired_events_path,
)


def _wired(time_iso: str, **extra) -> dict:
    evt = {"event": "meta_pipeline_wired", "time": time_iso}
    evt.update(extra)
    return evt


class TestWiredEventsPath:
    def test_path_scoped_to_asset_state_dir(self, tmp_path) -> None:
        assert wired_events_path(tmp_path) == tmp_path / "state" / "meta_pipeline_wired.jsonl"


class TestRecordWiredEvent:
    def test_appends_line_and_creates_dirs(self, tmp_path) -> None:
        ok = record_wired_event(tmp_path, _wired("2026-08-20T14:37:18.390115Z", features=40))
        assert ok is True
        path = wired_events_path(tmp_path)
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["time"] == "2026-08-20T14:37:18.390115Z"

    def test_defaults_time_when_absent(self, tmp_path) -> None:
        ok = record_wired_event(tmp_path, {"event": "meta_pipeline_wired"})
        assert ok is True
        evt = read_last_wired_event(tmp_path)
        assert evt is not None
        # the defaulted timestamp must be parseable (ageable)
        assert datetime.fromisoformat(str(evt["time"]).replace("Z", "+00:00"))

    def test_keeps_unicode_intact(self, tmp_path) -> None:
        record_wired_event(
            tmp_path, _wired("2026-08-20T14:37:18Z", stage2_filter="模型路径/含中文")
        )
        evt = read_last_wired_event(tmp_path)
        assert evt is not None
        assert evt["stage2_filter"] == "模型路径/含中文"

    def test_non_fatal_on_unwritable_path(self, tmp_path) -> None:
        # base_dir is a regular file → mkdir under it fails → False, no raise
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        assert record_wired_event(blocker, _wired("2026-08-20T14:37:18Z")) is False


class TestReadLastWiredEvent:
    def test_returns_last_record(self, tmp_path) -> None:
        record_wired_event(tmp_path, _wired("2026-08-19T00:00:00Z"))
        record_wired_event(tmp_path, _wired("2026-08-20T14:37:18Z"))
        evt = read_last_wired_event(tmp_path)
        assert evt is not None
        assert evt["time"] == "2026-08-20T14:37:18Z"

    def test_none_when_missing(self, tmp_path) -> None:
        assert read_last_wired_event(tmp_path) is None

    def test_none_when_empty(self, tmp_path) -> None:
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "meta_pipeline_wired.jsonl").write_text("", encoding="utf-8")
        assert read_last_wired_event(tmp_path) is None

    def test_skips_corrupt_trailing_line(self, tmp_path) -> None:
        record_wired_event(tmp_path, _wired("2026-08-20T14:37:18Z"))
        path = wired_events_path(tmp_path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{truncated-json\n")
        evt = read_last_wired_event(tmp_path)
        assert evt is not None
        assert evt["time"] == "2026-08-20T14:37:18Z"


class TestParseIntentBootTs:
    def test_parses_launcher_name(self) -> None:
        dt = parse_intent_boot_ts("intent_20260820T143718Z.log")
        assert dt == datetime(2026, 8, 20, 14, 37, 18, tzinfo=UTC)

    def test_none_for_wrong_prefix(self) -> None:
        assert parse_intent_boot_ts("live_launcher_20260820T143718Z.log") is None

    def test_none_for_garbage(self) -> None:
        assert parse_intent_boot_ts("intent_zzz.log") is None
        assert parse_intent_boot_ts("") is None
        # missing .log suffix
        assert parse_intent_boot_ts("intent_20260820T143718Z") is None

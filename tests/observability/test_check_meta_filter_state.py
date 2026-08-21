"""P7 (TECH_DEBT-018): ``check_meta_filter_state`` boot-anchored semantics.

Regression lock for the META_FILTER_WIRED_STALE false positive:

  D1 (semantic, this test's core): the old check flagged any wired event older
  than 360min as STALE — but a boot-scoped event does NOT age. A healthy
  long-running process (wired at boot 20h ago) is still filtered → must PASS.
  The new contract anchors on the CURRENT boot (newest intent log) instead.

  D2 (lifecycle): the wired event is now ALSO appended to a persistent SSOT
  file (``state/meta_pipeline_wired.jsonl``) by the producer, so the check no
  longer depends on where the launcher routed the intent process stdout
  (crash-loop era re-routed it into ``live_launcher_*.log`` and stopped
  rotating ``intent_*.log`` files entirely).

Decision table (current boot = newest intent log):
  * current boot wired (SSOT time >= boot ts, or wired in newest intent log
    head)                 → PASS (MICRO_SCALER_NOT_LOADED WARN if scaler off)
  * current boot unconfirmed but last wire within staleness window (mid-boot)
                          → PASS (transient, no wolf-crying)
  * current boot unconfirmed AND last wire older than staleness window
                          → WARN META_FILTER_WIRED_STALE (genuine)
  * no wired evidence anywhere → existing state-file secondary / MISSING
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from core.observability.data_health_schema import SourceCheckResult, SourceStatus
from core.observability.data_health_service import DataHealthService

_WIRED_FILENAME = "meta_pipeline_wired.jsonl"


def _wired(time_iso: str, *, scaler: bool = True) -> dict:
    return {
        "event": "meta_pipeline_wired",
        "time": time_iso,
        "lgb_loaded": True,
        "calibrator_loaded": False,
        "micro_scaler_loaded": scaler,
        "features": 40,
    }


def _write_log(tmp_path, name: str, *lines: str) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / name).write_text("".join(lines) + "\n", encoding="utf-8", newline="\n")


def _write_ssot(tmp_path, events: list[dict]) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / _WIRED_FILENAME).write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )


def _intent_name(dt: datetime) -> str:
    return "intent_" + dt.strftime("%Y%m%dT%H%M%SZ") + ".log"


def _run(tmp_path) -> SourceCheckResult:
    svc = DataHealthService(base_dir=str(tmp_path), symbol="XAUUSDc")
    return svc.check_meta_filter_state()


class TestHealthyLongRunning:
    """THE regression lock — the daily false WARN the IC's P7 targets."""

    def test_20h_uptime_healthy_process_passes_not_stale(self, tmp_path) -> None:
        # Booted 8/20 14:37, wired at boot, running continuously ever since.
        # Old check: wired_age ~20h > 360 → false META_FILTER_WIRED_STALE.
        wired_at = "2026-08-20T14:37:18.390115Z"
        _write_log(tmp_path, "intent_20260820T143718Z.log", json.dumps(_wired(wired_at)))
        _write_ssot(tmp_path, [_wired(wired_at)])
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "META_FILTER_OK"
        # age is now a diagnostic metric, NOT a failure predicate
        assert result.metrics["wired_age_minutes"] > 360
        assert result.metrics["current_boot_wired"] is True
        assert result.metrics["log_source"] == "meta_wired.jsonl"
        # downstream metric contract preserved
        assert result.metrics["micro_scaler_loaded"] is True
        assert result.metrics["feature_count"] == 40
        assert result.metrics["lgb_loaded"] is True

    def test_ssot_anchor_is_authoritative_when_stdout_rerouted(self, tmp_path) -> None:
        # Crash-loop era: intent stdout captured into live_launcher log → no
        # fresh intent_*.log. The SSOT still records the wire. No intent log
        # today; recent SSOT wire → the process wired this boot window → PASS.
        now = datetime.now(UTC).replace(microsecond=0)
        _write_ssot(tmp_path, [_wired(now.isoformat())])
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "META_FILTER_OK"


class TestGenuineStale:
    """Current boot did not wire AND last wire is old → STALE (real signal)."""

    def test_current_boot_failed_to_wire_is_stale(self, tmp_path) -> None:
        # New boot (8/21 10:00) produced no wired event (load failed); the last
        # confirmed wire belongs to the previous boot (8/20 14:37).
        _write_log(
            tmp_path,
            "intent_20260821T100000Z.log",
            json.dumps({"event": "meta_filter_load_failed", "time": "2026-08-21T10:00:05Z"}),
        )
        _write_ssot(tmp_path, [_wired("2026-08-20T14:37:18Z")])
        result = _run(tmp_path)

        assert result.status == SourceStatus.WARN
        assert result.primary_code == "META_FILTER_WIRED_STALE"
        assert result.metrics["current_boot_wired"] is False

    def test_stale_from_old_intent_log_without_ssot(self, tmp_path) -> None:
        # SSOT absent (rollout gap). Newest intent log (8/21) has no wired; the
        # previous log (8/20) carries the last wire → STALE via age fallback.
        _write_log(
            tmp_path,
            "intent_20260821T100000Z.log",
            json.dumps({"event": "meta_filter_load_failed", "time": "2026-08-21T10:00:05Z"}),
        )
        _write_log(
            tmp_path, "intent_20260820T143718Z.log", json.dumps(_wired("2026-08-20T14:37:18Z"))
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.WARN
        assert result.primary_code == "META_FILTER_WIRED_STALE"


class TestCrashLoopRecovery:
    def test_wire_recovered_from_last_real_log_passes(self, tmp_path) -> None:
        # 8/11 crash-loop era: no fresh intent logs; the last real one DID wire
        # ("每次重启均成功 wired"). Old check false-WARNed at 2808min age; the
        # new check passes — that wire IS the newest (only) intent log's.
        _write_log(
            tmp_path, "intent_20260811T003150Z.log", json.dumps(_wired("2026-08-11T00:31:59Z"))
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "META_FILTER_OK"
        assert result.metrics["log_source"] == "intent_20260811T003150Z.log"


class TestTransientWindow:
    def test_recent_wire_mid_boot_passes(self, tmp_path) -> None:
        # New boot just started (no wired yet); the SSOT holds a wire from 5min
        # ago (previous boot). Do not wolf-cry during the boot window.
        now = datetime.now(UTC).replace(microsecond=0)
        _write_log(tmp_path, _intent_name(now), "")
        _write_ssot(
            tmp_path, [_wired((now - timedelta(minutes=5)).replace(tzinfo=None).isoformat())]
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "META_FILTER_OK"
        assert result.metrics["current_boot_wired"] is False


class TestMicroScaler:
    def test_micro_scaler_not_loaded_warns(self, tmp_path) -> None:
        _write_log(
            tmp_path,
            "intent_20260821T100000Z.log",
            json.dumps(_wired("2026-08-21T10:00:05Z", scaler=False)),
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.WARN
        assert result.primary_code == "MICRO_SCALER_NOT_LOADED"


class TestNoEvidenceFallback:
    def test_missing_when_no_evidence_at_all(self, tmp_path) -> None:
        result = _run(tmp_path)
        assert result.status == SourceStatus.MISSING
        assert result.primary_code == "META_FILTER_STATE_MISSING"

    def test_state_file_fallback_preserved(self, tmp_path) -> None:
        (tmp_path / "meta_filter_state.json").write_text(
            json.dumps({"pred_buffer": [1, 2, 3], "atr_buffer": []}), encoding="utf-8"
        )
        result = _run(tmp_path)
        assert result.status == SourceStatus.PASS
        assert result.primary_code == "META_FILTER_OK"

    def test_atr_frozen_state_file_warns(self, tmp_path) -> None:
        (tmp_path / "meta_filter_state.json").write_text(
            json.dumps({"pred_buffer": [], "atr_buffer": [1.0] * 90}), encoding="utf-8"
        )
        result = _run(tmp_path)
        assert result.status == SourceStatus.WARN
        assert result.primary_code == "META_FILTER_ATR_FROZEN"

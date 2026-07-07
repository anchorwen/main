"""Tests for inspect_ofi_history.py — dual-count readiness gates.

FIX-20260707-005 (dual-count): the monitor must distinguish raw 30s settles
(Gate 1, Wasserstein screening) from distinct H1 windows (Gate 2, H1-cadence
retrain).  These tests pin the two counting helpers and the gate verdicts so a
future edit cannot silently re-conflate raw settles with training samples.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.inspect_ofi_history import (
    _distinct_h1_windows,
    _span_days,
    inspect,
)

_BASE = datetime(2026, 1, 1, 0, 0, 0)


def _rec_at(dt: datetime, **overrides: Any) -> dict[str, Any]:
    """Build one OFI history record stamped at *dt* (defaults: 3/5 live)."""
    rec: dict[str, Any] = {
        "time": dt.isoformat(),
        "OFI_M5": 1.0,
        "OFI_ZScore_20": 0.5,
        "OFI_Cumulative_Delta": -10.0,
        "OFI_Delta_Divergence": 0.0,
        "OFI_Volume_Real_Ratio": 0.0,
    }
    rec.update(overrides)
    return rec


def _write_history(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    with open(reports / "ofi_history.jsonl", "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return tmp_path


# ── _distinct_h1_windows ──────────────────────────────────────────────


def test_distinct_h1_windows_counts_unique_hours() -> None:
    # 4 records but only 2 distinct calendar hours → 2 H1 windows.
    recs = [
        _rec_at(_BASE),  # 00:00
        _rec_at(_BASE + timedelta(minutes=15)),  # 00:15 (same hour)
        _rec_at(_BASE + timedelta(minutes=59)),  # 00:59 (same hour)
        _rec_at(_BASE + timedelta(hours=1)),  # 01:00 (new hour)
    ]
    assert _distinct_h1_windows(recs) == 2


def test_distinct_h1_windows_skips_malformed() -> None:
    recs = [
        _rec_at(_BASE),
        {"OFI_M5": 1.0},  # no "time"
        {"time": 12345},  # non-str time
        {"time": "short"},  # too short to bucket
    ]
    assert _distinct_h1_windows(recs) == 1


# ── _span_days ────────────────────────────────────────────────────────


def test_span_days_computes_difference() -> None:
    recs = [_rec_at(_BASE), _rec_at(_BASE + timedelta(days=7))]
    assert _span_days(recs) == pytest.approx(7.0, abs=1e-6)


def test_span_days_insufficient_or_malformed() -> None:
    assert _span_days([_rec_at(_BASE)]) == 0.0
    assert _span_days([{"time": "bad"}, {"time": "also-bad"}]) == 0.0


# ── inspect() verdicts ────────────────────────────────────────────────


def test_inspect_no_data(tmp_path: Path) -> None:
    _write_history(tmp_path, [])
    out = inspect(tmp_path)
    assert out["n_records"] == 0
    assert out["verdict"].startswith("NO_DATA")


def test_inspect_accumulating_reports_both_counts(tmp_path: Path) -> None:
    recs = [_rec_at(_BASE + timedelta(minutes=30 * i)) for i in range(5)]
    _write_history(tmp_path, recs)
    out = inspect(tmp_path)
    assert out["n_records"] == 5
    assert out["distinct_h1_windows"] == 3  # 0:00,0:30,1:00,1:30,2:00 → hrs 0,1,2
    assert out["gate1_screening"]["ready"] is False
    assert out["gate2_retrain"]["ready"] is False
    assert out["verdict"].startswith("ACCUMULATING")


def test_inspect_gate1_requires_span_not_just_raw_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lower the raw threshold so count is met, but keep records within 1 hour →
    # span far below 7d → Gate 1 must stay NOT ready (regime-coverage guard).
    monkeypatch.setattr("scripts.inspect_ofi_history._EVAL_RAW_THRESHOLD", 3)
    recs = [_rec_at(_BASE + timedelta(seconds=30 * i)) for i in range(5)]
    _write_history(tmp_path, recs)
    out = inspect(tmp_path)
    assert out["n_records"] >= 3  # raw count satisfied
    assert out["gate1_screening"]["ready"] is False  # span guard blocks it


def test_inspect_gate2_retrain_ready_on_h1_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.inspect_ofi_history._RETRAIN_H1_THRESHOLD", 3)
    recs = [_rec_at(_BASE + timedelta(hours=i)) for i in range(4)]  # 4 H1 windows
    _write_history(tmp_path, recs)
    out = inspect(tmp_path)
    assert out["distinct_h1_windows"] == 4
    assert out["gate2_retrain"]["ready"] is True
    assert out["verdict"].startswith("RETRAIN_READY")


def test_inspect_effective_dim_excludes_dead_features(tmp_path: Path) -> None:
    # Default records: M5/ZScore/CumDelta live, Divergence+VolReal dead → 44-dim.
    recs = [_rec_at(_BASE + timedelta(hours=i)) for i in range(10)]
    _write_history(tmp_path, recs)
    out = inspect(tmp_path)
    assert out["effective_flow_dim"] == 3
    assert out["effective_schema_dim"] == 44
    assert out["volume_real_available"] is False

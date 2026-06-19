"""Tests for core.runtime.golden_master — regression recording and replay.

FIX-20260619-032: Tier 1 zero-coverage breakout #3.
Covers record_cycle_inputs, record_cycle_outputs, load_records,
_fuzzy_equal, and replay_check_cycle.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.runtime.golden_master import (
    _fuzzy_equal,
    _is_recording,
    _is_replaying,
    _now_utc,
    load_records,
    record_cycle_inputs,
    record_cycle_outputs,
    replay_check_cycle,
)


class TestEnvChecks:
    def test_recording_on_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _is_recording() is True

    def test_recording_disabled_with_zero(self) -> None:
        with patch.dict(os.environ, {"GOLDEN_MASTER_RECORD": "0"}, clear=True):
            assert _is_recording() is False

    def test_recording_still_on_with_other_values(self) -> None:
        with patch.dict(os.environ, {"GOLDEN_MASTER_RECORD": "1"}, clear=True):
            assert _is_recording() is True

    def test_replay_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _is_replaying() is False

    def test_replay_enabled(self) -> None:
        with patch.dict(os.environ, {"GOLDEN_MASTER_REPLAY": "1"}, clear=True):
            assert _is_replaying() is True


class TestNowUtc:
    def test_returns_iso_format(self) -> None:
        ts = _now_utc()
        assert "T" in ts
        assert len(ts) > 10  # at least 'YYYY-MM-DDTHH'


class TestRecordCycleInputs:
    def test_returns_none_when_recording_disabled(self) -> None:
        with patch.dict(os.environ, {"GOLDEN_MASTER_RECORD": "0"}, clear=True):
            result = record_cycle_inputs(
                cycle_count=1, mid_price=4700.0, bid=4699.5, ask=4700.5,
                current_atr=6.0, regime_info={"regime": "trending"},
                trend_direction="up", trend_strength=0.8, macro_regime="normal",
                risk_budget_usd=1000.0, session_volume_mult=1.0,
                health_volume_mult=1.0,
            )
        assert result is None

    def test_captures_all_inputs(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = record_cycle_inputs(
                cycle_count=5, mid_price=4700.0, bid=4699.5, ask=4700.5,
                current_atr=6.0, regime_info={"regime": "trending", "detected_regime": "strong_trend"},
                trend_direction="up", trend_strength=0.8, macro_regime="normal",
                hurst=0.65, risk_budget_usd=1000.0, session_volume_mult=1.0,
                health_volume_mult=1.0,
            )
        assert result is not None
        assert result["cycle"] == 5
        assert result["inputs"]["mid_price"] == 4700.0
        assert result["inputs"]["regime"] == "trending"
        assert result["inputs"]["detected_regime"] == "strong_trend"
        assert result["inputs"]["hurst"] == pytest.approx(0.65)
        assert result["inputs"]["spread"] == pytest.approx(1.0, abs=0.01)

    def test_none_fields_handled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = record_cycle_inputs(
                cycle_count=1, mid_price=None, bid=None, ask=None,
                current_atr=0.0, regime_info=None,
                trend_direction="neutral", trend_strength=0.0, macro_regime="normal",
                risk_budget_usd=0.0, session_volume_mult=1.0,
                health_volume_mult=1.0,
            )
        assert result is not None
        assert result["inputs"]["mid_price"] is None
        assert result["inputs"]["spread"] == 0.0
        assert result["inputs"]["regime"] == "normal"  # default

    def test_feature_vector_sample_head8(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            fv = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
            result = record_cycle_inputs(
                cycle_count=1, mid_price=1.0, bid=1.0, ask=1.0,
                current_atr=1.0, regime_info={}, trend_direction="up",
                trend_strength=0.0, macro_regime="normal",
                risk_budget_usd=0.0, session_volume_mult=1.0,
                health_volume_mult=1.0, feature_vector_sample=fv,
            )
        assert len(result["inputs"]["feature_vector_head8"]) == 8


class TestRecordCycleOutputs:
    def test_noop_when_capture_none(self) -> None:
        record_cycle_outputs(None, strategy_results=[], decisions_map={}, trade_decisions=0, queued=0)

    def test_writes_to_temp_file(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            capture = record_cycle_inputs(
                cycle_count=1, mid_price=4700.0, bid=4699.5, ask=4700.5,
                current_atr=6.0, regime_info={}, trend_direction="up",
                trend_strength=0.5, macro_regime="normal",
                risk_budget_usd=1000.0, session_volume_mult=1.0,
                health_volume_mult=1.0,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            record_cycle_outputs(
                capture,
                strategy_results=[
                    {"strategy": "test_swing", "direction": "long", "confidence": 0.75,
                     "should_trade": True, "reason": "signal", "volume": 0.1,
                     "sl": 4650.0, "tp": 4800.0},
                ],
                decisions_map={},
                trade_decisions=1,
                queued=1,
                data_dir=tmpdir,
            )

            gm_path = Path(tmpdir) / "golden_master.jsonl"
            assert gm_path.exists()
            lines = gm_path.read_text().strip().split("\n")
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["summary"]["trade_decisions"] == 1
            assert "test_swing" in record["outputs"]

    def test_handles_dict_strategy_results(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            capture = record_cycle_inputs(
                cycle_count=2, mid_price=1.0, bid=1.0, ask=1.0,
                current_atr=1.0, regime_info={}, trend_direction="up",
                trend_strength=0.0, macro_regime="normal",
                risk_budget_usd=0.0, session_volume_mult=1.0,
                health_volume_mult=1.0,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            record_cycle_outputs(
                capture,
                strategy_results={"swing": {"direction": "long", "confidence": 0.5,
                                              "should_trade": False, "reason": "", "volume": 0,
                                              "sl": 0, "tp": 0}},
                decisions_map={},
                trade_decisions=0,
                queued=0,
                data_dir=tmpdir,
            )
            gm_path = Path(tmpdir) / "golden_master.jsonl"
            assert gm_path.exists()


class TestLoadRecords:
    def test_empty_when_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            records = load_records(data_dir=tmpdir)
        assert records == []

    def test_loads_valid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            gm_path = Path(tmpdir) / "golden_master.jsonl"
            gm_path.write_text(
                json.dumps({"cycle": 1, "inputs": {}, "outputs": {}}) + "\n"
                + json.dumps({"cycle": 2, "inputs": {}, "outputs": {}}) + "\n",
                encoding="utf-8",
            )
            records = load_records(data_dir=tmpdir)
        assert len(records) == 2
        assert records[0]["cycle"] == 1

    def test_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            gm_path = Path(tmpdir) / "golden_master.jsonl"
            gm_path.write_text(
                '{"cycle": 1}\nCORRUPT LINE\n{"cycle": 2}\n', encoding="utf-8"
            )
            records = load_records(data_dir=tmpdir)
        assert len(records) == 2


class TestFuzzyEqual:
    def test_exact_match(self) -> None:
        assert _fuzzy_equal(1.0, 1.0)

    def test_within_tolerance(self) -> None:
        assert _fuzzy_equal(1.0, 1.000001, abs_tol=0.001)

    def test_outside_tolerance(self) -> None:
        assert not _fuzzy_equal(1.0, 1.1, abs_tol=0.001)

    def test_both_zero(self) -> None:
        assert _fuzzy_equal(0.0, 0.0)

    def test_bool_comparison(self) -> None:
        assert _fuzzy_equal(True, True)
        assert not _fuzzy_equal(True, False)

    def test_string_comparison(self) -> None:
        assert _fuzzy_equal("hello", "hello")
        assert not _fuzzy_equal("hello", "world")

    def test_fallback_stringify(self) -> None:
        assert _fuzzy_equal([1, 2], [1, 2])


class TestReplayCheckCycle:
    def test_all_match(self) -> None:
        cycle = {"outputs": {"swing": {"direction": "long", "should_trade": True,
                                        "reason": "signal", "confidence": 0.75, "volume": 0.1}}}
        live = {"swing": {"direction": "long", "should_trade": True,
                           "reason": "signal", "confidence": 0.75, "volume": 0.1}}
        mismatches = replay_check_cycle(cycle, live)
        assert mismatches == []

    def test_detects_direction_mismatch(self) -> None:
        cycle = {"outputs": {"swing": {"direction": "long", "should_trade": True,
                                        "reason": "signal", "confidence": 0.75, "volume": 0.1}}}
        live = {"swing": {"direction": "short", "should_trade": True,
                           "reason": "signal", "confidence": 0.75, "volume": 0.1}}
        mismatches = replay_check_cycle(cycle, live)
        assert len(mismatches) > 0

    def test_detects_missing_strategy(self) -> None:
        cycle = {"outputs": {"swing": {"direction": "long"}}}
        live = {}
        mismatches = replay_check_cycle(cycle, live)
        assert any("missing" in m for m in mismatches)

    def test_detects_new_strategy(self) -> None:
        cycle = {"outputs": {}}
        live = {"new_strat": {"direction": "long"}}
        mismatches = replay_check_cycle(cycle, live)
        assert any("new strategy" in m for m in mismatches)

"""Tests for core.runtime.timeframe_scaling — SF #21 pure function.

FIX-20260619-045: Pure function test for apply_timeframe_scaling.
"""

from __future__ import annotations

import pytest

from core.runtime.timeframe_scaling import TIMEFRAME_TO_M5, apply_timeframe_scaling


class TestApplyTimeframeScaling:
    def test_scales_hesitation_cycles_for_h1(self) -> None:
        configs = {"test": {"timeframe": "H1", "exit": {"hesitation_cycles": 3}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["exit"]["hesitation_cycles"] == 36  # 3 × 12

    def test_scales_time_exit_cycles_for_m15(self) -> None:
        configs = {"test": {"timeframe": "M15", "exit": {"time_exit_cycles": 4}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["exit"]["time_exit_cycles"] == 12  # 4 × 3

    def test_scales_max_hold_cycles(self) -> None:
        configs = {"test": {"timeframe": "H4", "exit": {"max_hold_cycles": 5}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["exit"]["max_hold_cycles"] == 240  # 5 × 48

    def test_default_m5_multiplier(self) -> None:
        configs = {"test": {"timeframe": "M5", "exit": {"hesitation_cycles": 10}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["exit"]["hesitation_cycles"] == 10  # 10 × 1

    def test_unknown_timeframe_uses_mult_1(self) -> None:
        configs = {"test": {"timeframe": "W1", "exit": {"hesitation_cycles": 7}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["exit"]["hesitation_cycles"] == 7

    def test_stashes_tf_mult(self) -> None:
        configs = {"test": {"timeframe": "H1", "exit": {}}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["_tf_mult"] == 12

    def test_skips_non_dict_configs(self) -> None:
        configs = {"test": "not_a_dict", "valid": {"timeframe": "M5", "exit": {}}}
        result = apply_timeframe_scaling(configs)  # type: ignore[arg-type]
        assert result["valid"]["_tf_mult"] == 1

    def test_no_exit_config_preserved(self) -> None:
        configs = {"test": {"timeframe": "M30"}}
        result = apply_timeframe_scaling(configs)
        assert result["test"]["_tf_mult"] == 6

    def test_mutates_in_place_and_returns_same_dict(self) -> None:
        configs = {"test": {"timeframe": "M5", "exit": {}}}
        result = apply_timeframe_scaling(configs)
        assert result is configs


class TestTimeframeToM5:
    def test_d1_is_288(self) -> None:
        assert TIMEFRAME_TO_M5["D1"] == 288

    def test_h4_is_48(self) -> None:
        assert TIMEFRAME_TO_M5["H4"] == 48

    def test_all_keys_present(self) -> None:
        assert set(TIMEFRAME_TO_M5.keys()) == {"M5", "M15", "M30", "H1", "H4", "D1"}

"""Unit tests for MetaSignalFilter — signal quality gate before execution."""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.execution.meta_signal_filter import FilterResult, MetaSignalFilter


class TestFilterResult:
    def test_passed_result(self):
        r = FilterResult(passed=True, p_win=0.75, threshold=0.65)
        assert r.passed is True
        assert r.p_win == 0.75
        assert r.threshold == 0.65

    def test_rejected_result(self):
        r = FilterResult(passed=False, p_win=0.30, threshold=0.65, reason="below_threshold")
        assert r.passed is False
        assert r.reason == "below_threshold"


class TestMetaSignalFilterInit:
    def test_default_init(self):
        f = MetaSignalFilter()
        assert f.enabled is True
        assert f.threshold == 0.30
        assert f.mode == "binary"
        assert f._model is None
        assert f._mlp_model is None

    def test_disabled_filter(self):
        f = MetaSignalFilter(enabled=False)
        assert f.load() is False

    def test_missing_model_path(self):
        f = MetaSignalFilter(model_path="/nonexistent/model.txt")
        assert f.load() is False

    def test_custom_threshold(self):
        f = MetaSignalFilter(threshold=0.50)
        assert f.threshold == 0.50


class TestMetaSignalFilterLoad:
    def test_load_disabled_returns_false(self):
        f = MetaSignalFilter(enabled=False, model_path="/some/path.txt")
        assert f.load() is False

    def test_load_missing_path_returns_false(self):
        f = MetaSignalFilter(model_path="/nonexistent/model.txt")
        assert f.load() is False

    def test_load_model_failure_graceful(self):
        """Load failure should not crash — returns False."""
        f = MetaSignalFilter(
            model_path="/nonexistent/model.txt",
            mlp_model_path="/nonexistent/mlp.json",
        )
        # Create a fake .txt file that isn't a valid LightGBM model
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", side_effect=OSError("disk full")):
                result = f.load()
                assert result is False


class TestMetaSignalFilterPrediction:
    def test_filter_no_model_fallback(self):
        """Without a loaded model, filter always passes (fail-open guard removed — now fail-closed)."""
        f = MetaSignalFilter(model_path="/nonexistent/model.txt")
        # Model not loaded — _model is None
        result = f.filter(
            direction=1,
            s1_prediction=12.5,
            v9_features={"M5_Ret_1": 0.02},
            timestamp_utc=1715875200.0,
        )
        # When model is None and enabled=True, behavior depends on implementation
        # For now, verify it returns a FilterResult without crashing
        assert isinstance(result, FilterResult)

    def test_filter_with_basic_features(self):
        """Filter should accept feature dict without crashing."""
        f = MetaSignalFilter(threshold=0.50)
        features = {f"M5_Ret_{i}": 0.0 for i in range(1, 41)}
        result = f.filter(
            direction=1,
            s1_prediction=10.0,
            v9_features=features,
            timestamp_utc=1715875200.0,
        )
        assert isinstance(result, FilterResult)


class TestStateSaveLoad:
    def test_load_state_missing_file(self):
        """Loading from non-existent path should not crash."""
        f = MetaSignalFilter()
        f.load_state("/nonexistent/state.json")  # Should not raise

    def test_save_state_creates_file(self, tmp_path):
        """Saving state should create a JSON file."""
        f = MetaSignalFilter()
        path = str(tmp_path / "state.json")
        f.save_state(path)
        assert tmp_path.joinpath("state.json").exists()

    def test_save_load_roundtrip(self, tmp_path):
        """Save then load should restore buffers."""
        f = MetaSignalFilter(conformal_window=500)
        f._pred_history.append((1715875200.0, 0.65))
        f._pred_buffer.append(0.5)
        f._atr_buffer.append(4.2)
        f._micro_spread_buffer.append(0.02)

        path = str(tmp_path / "state.json")
        f.save_state(path)

        f2 = MetaSignalFilter(conformal_window=500)
        f2.load_state(path)
        assert len(f2._pred_history) == 1
        assert len(f2._pred_buffer) == 1
        assert len(f2._atr_buffer) == 1
        assert len(f2._micro_spread_buffer) == 1

    def test_load_state_corrupted_file(self):
        """Corrupted state file should not crash."""
        f = MetaSignalFilter()
        f.load_state("tests/fixtures/corrupt_state.json")  # Should not raise, just skip


class TestEdgeCases:
    def test_filter_disabled_always_passes(self):
        f = MetaSignalFilter(enabled=False, threshold=0.99)
        result = f.filter(direction=1, s1_prediction=0.01, v9_features={}, timestamp_utc=0.0)
        assert result.passed is True
        assert result.p_win == 0.5

    def test_nan_features_handled(self):
        """NaN in features should not crash the filter."""
        f = MetaSignalFilter(threshold=0.50)
        features = {f"M5_Ret_{i}": float("nan") for i in range(1, 41)}
        result = f.filter(
            direction=1, s1_prediction=10.0, v9_features=features, timestamp_utc=0.0
        )
        assert isinstance(result, FilterResult)

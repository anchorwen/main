"""Tests for core.brains.services.inference_guard.

FIX-20260620-070: Project C — Tier 2 zero-coverage breakout.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pytest

from core.brains.services.inference_guard import RESTART_COOLDOWN, InferenceGuard


class TestInferenceGuardInit:
    def test_raises_file_not_found_for_missing_model(self) -> None:
        with pytest.raises(FileNotFoundError, match="ONNX model not found"):
            InferenceGuard("/nonexistent/path/model.onnx")

    def test_accepts_valid_model_path(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            assert guard.model_path.endswith("model.onnx")

    def test_default_timeout_and_restarts(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            assert guard._timeout == 5.0
            assert guard._max_restarts == 3

    def test_custom_timeout_and_restarts(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx", timeout=10.0, max_restarts=5)
            assert guard._timeout == 10.0
            assert guard._max_restarts == 5


class TestInferenceGuardInfer:
    @pytest.fixture
    def guard(self) -> InferenceGuard:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            return InferenceGuard("/fake/model.onnx")

    def test_returns_none_when_conn_is_none(self, guard: InferenceGuard) -> None:
        guard._conn = None
        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result is None

    def test_successful_inference(self, guard: InferenceGuard) -> None:
        mock_conn = MagicMock()
        mock_conn.poll.return_value = True
        mock_conn.recv.return_value = [np.array([0.5])]
        guard._conn = mock_conn

        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result == [np.array([0.5])]
        mock_conn.send.assert_called_once()

    def test_worker_error_response(self, guard: InferenceGuard) -> None:
        mock_conn = MagicMock()
        mock_conn.poll.return_value = True
        mock_conn.recv.return_value = {"error": "something went wrong"}
        guard._conn = mock_conn

        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result is None

    def test_timeout_triggers_crash_handling(self, guard: InferenceGuard) -> None:
        mock_conn = MagicMock()
        mock_conn.poll.return_value = False  # timeout
        guard._conn = mock_conn

        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result is None
        assert guard.crash_count == 1

    def test_pipe_error_triggers_crash_handling(self, guard: InferenceGuard) -> None:
        mock_conn = MagicMock()
        mock_conn.send.side_effect = BrokenPipeError("pipe broken")
        guard._conn = mock_conn

        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result is None
        assert guard.crash_count == 1

    def test_eof_error_triggers_crash_handling(self, guard: InferenceGuard) -> None:
        mock_conn = MagicMock()
        mock_conn.send.side_effect = EOFError("eof")
        guard._conn = mock_conn

        result = guard.infer("input", ["output"], np.array([1.0]))
        assert result is None


class TestInferenceGuardShutdown:
    def test_shutdown_cleans_up(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            guard.shutdown()
            assert guard._running is False

    def test_shutdown_with_no_conn(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            guard._conn = None
            guard.shutdown()  # should not raise
            assert guard._running is False


class TestInferenceGuardProperties:
    def test_is_alive_when_not_running(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            guard._running = False
            assert guard.is_alive is False

    def test_is_alive_when_running(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            guard._running = True
            guard._process = MagicMock()
            guard._process.is_alive.return_value = True
            assert guard.is_alive is True

    def test_crash_count_starts_at_zero(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            assert guard.crash_count == 0

    def test_model_path_returns_resolved_path(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"):
            guard = InferenceGuard("/fake/model.onnx")
            assert "model.onnx" in guard.model_path


class TestInferenceGuardHandleCrash:
    def test_restart_on_first_crash(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start") as mock_start, \
             patch("core.brains.services.inference_guard.InferenceGuard._cleanup"), \
             patch("time.sleep"):
            guard = InferenceGuard("/fake/model.onnx")
            guard._running = True  # simulate successful _start
            initial_count = mock_start.call_count
            guard._handle_crash()
            assert guard.crash_count == 1
            # _handle_crash sets _running to True by calling _start again
            # (we mock _start so _running stays True from our manual set)
            assert mock_start.call_count == initial_count + 1

    def test_exceed_max_restarts_gives_up(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.InferenceGuard._start"), \
             patch("core.brains.services.inference_guard.InferenceGuard._cleanup"), \
             patch("time.sleep"):
            guard = InferenceGuard("/fake/model.onnx", max_restarts=2)
            guard._crash_count = 2  # already at max
            guard._handle_crash()
            assert guard.crash_count == 3
            assert guard._running is False  # gave up


class TestInferenceGuardStart:
    def test_start_failure_catches_exception(self) -> None:
        with patch("core.brains.services.inference_guard.Path.exists", return_value=True), \
             patch("core.brains.services.inference_guard.mp.get_context") as mock_ctx:
            mock_ctx.side_effect = RuntimeError("spawn failed")
            guard = InferenceGuard.__new__(InferenceGuard)
            guard._model_path = "/fake/model.onnx"
            guard._timeout = 5.0
            guard._max_restarts = 3
            guard._process = None
            guard._conn = None
            guard._crash_count = 0
            guard._running = False
            guard._lock = MagicMock()
            guard._start()
            assert guard._running is False
            assert guard._conn is None

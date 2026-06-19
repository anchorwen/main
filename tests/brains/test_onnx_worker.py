"""Tests for core.brains.services.onnx_worker — ONNX subprocess worker.

FIX-20260619-048: Tier 2 zero-coverage breakout #3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.brains.services.onnx_worker import run_worker


class TestRunWorker:
    def test_load_failure_sends_error_and_returns(self) -> None:
        conn = MagicMock()
        with patch("onnxruntime.InferenceSession", side_effect=RuntimeError("load err")), \
             patch("onnxruntime.set_default_logger_severity"):
            run_worker(conn, "/fake/model.onnx")

        conn.send.assert_called_once()
        assert "load_failed" in conn.send.call_args[0][0]["error"]
        conn.close.assert_called_once()

    def test_sentinel_none_breaks_loop(self) -> None:
        conn = MagicMock()
        conn.recv.return_value = None  # sentinel
        mock_session = MagicMock()
        mock_session.get_inputs.return_value = [MagicMock(name="input")]
        mock_session.get_outputs.return_value = []

        with patch("onnxruntime.InferenceSession", return_value=mock_session), \
             patch("onnxruntime.set_default_logger_severity"):
            run_worker(conn, "/fake/model.onnx")

        conn.send.assert_not_called()

    def test_eof_error_breaks_loop(self) -> None:
        conn = MagicMock()
        conn.recv.side_effect = EOFError()
        mock_session = MagicMock()
        mock_session.get_inputs.return_value = [MagicMock(name="input")]
        mock_session.get_outputs.return_value = []

        with patch("onnxruntime.InferenceSession", return_value=mock_session), \
             patch("onnxruntime.set_default_logger_severity"):
            run_worker(conn, "/fake/model.onnx")

    def test_invalid_request_type_sends_error(self) -> None:
        conn = MagicMock()
        conn.recv.side_effect = ["not_a_dict", None]
        mock_session = MagicMock()
        mock_session.get_inputs.return_value = [MagicMock(name="input")]
        mock_session.get_outputs.return_value = []

        with patch("onnxruntime.InferenceSession", return_value=mock_session), \
             patch("onnxruntime.set_default_logger_severity"):
            run_worker(conn, "/fake/model.onnx")

        error_call = conn.send.call_args_list[0]
        assert "invalid_request" in error_call[0][0]["error"]

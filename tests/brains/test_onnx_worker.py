"""Tests for core.brains.services.onnx_worker — ONNX subprocess worker.

FIX-20260619-048: Tier 2 zero-coverage breakout #3.
Tests worker logic without actual ONNX runtime.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from core.brains.services.onnx_worker import run_worker


class TestRunWorker:
    def test_load_failure_sends_error_and_returns(self) -> None:
        conn = MagicMock()
        with patch("core.brains.services.onnx_worker.onnxruntime") as mock_ort:
            mock_ort.InferenceSession.side_effect = RuntimeError("cannot load model")
            mock_ort.set_default_logger_severity = MagicMock()
            run_worker(conn, "/fake/model.onnx")

        conn.send.assert_called_once()
        args = conn.send.call_args[0][0]
        assert "load_failed" in args["error"]
        conn.close.assert_called_once()

    def test_sentinel_none_breaks_loop(self) -> None:
        conn = MagicMock()
        conn.recv.return_value = None  # sentinel

        with patch("core.brains.services.onnx_worker.onnxruntime") as mock_ort:
            mock_session = MagicMock()
            mock_session.get_inputs.return_value = [MagicMock(name="input")]
            mock_session.get_outputs.return_value = []
            mock_ort.InferenceSession.return_value = mock_session
            mock_ort.set_default_logger_severity = MagicMock()

            run_worker(conn, "/fake/model.onnx")

        # Should break on sentinel without error
        conn.send.assert_not_called()  # no error sent

    def test_eof_error_breaks_loop(self) -> None:
        conn = MagicMock()
        conn.recv.side_effect = EOFError()

        with patch("core.brains.services.onnx_worker.onnxruntime") as mock_ort:
            mock_session = MagicMock()
            mock_session.get_inputs.return_value = [MagicMock(name="input")]
            mock_session.get_outputs.return_value = []
            mock_ort.InferenceSession.return_value = mock_session
            mock_ort.set_default_logger_severity = MagicMock()

            run_worker(conn, "/fake/model.onnx")

        # Clean exit on EOF

    def test_invalid_request_type_sends_error(self) -> None:
        conn = MagicMock()
        conn.recv.side_effect = ["not_a_dict", None]

        with patch("core.brains.services.onnx_worker.onnxruntime") as mock_ort:
            mock_session = MagicMock()
            mock_session.get_inputs.return_value = [MagicMock(name="input")]
            mock_session.get_outputs.return_value = []
            mock_ort.InferenceSession.return_value = mock_session
            mock_ort.set_default_logger_severity = MagicMock()

            run_worker(conn, "/fake/model.onnx")

        # First send should be the error about invalid request
        error_call = conn.send.call_args_list[0]
        assert "invalid_request" in error_call[0][0]["error"]

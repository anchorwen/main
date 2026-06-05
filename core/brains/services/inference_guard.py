"""Subprocess isolation for ONNX inference.

Wraps ONNX Runtime model inference in a child process so that a C++
segfault cannot take down the entire trading loop.  Uses the spawn
multiprocessing context for maximum portability (no fork issues).

Usage:
    guard = InferenceGuard("path/to/model.onnx", timeout=5.0)
    outputs = guard.infer(input_name, output_names, model_input)
    # outputs is list[np.ndarray] on success, None on failure
    guard.shutdown()
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .onnx_worker import run_worker

logger = logging.getLogger(__name__)

RESTART_COOLDOWN = 3  # seconds before re-spawn after crash


class InferenceGuard:
    """Manages a child process that runs ONNX inference.

    Features:
    - Spawns on demand (lazy) or explicitly via start()
    - Timeout on every inference call
    - Automatic restart on crash (up to max_restarts per session)
    - Thread-safe (lock around pipe operations)
    - Graceful fallback — returns None when unavailable
    """

    def __init__(
        self,
        model_path: str,
        timeout: float = 5.0,
        max_restarts: int = 3,
    ):
        if not Path(model_path).exists():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")
        self._model_path = str(Path(model_path).resolve())
        self._timeout = timeout
        self._max_restarts = max_restarts
        self._process: Any = None  # mp.Process
        self._conn: Any = None  # mp.connection.Connection
        self._lock = threading.Lock()
        self._crash_count = 0
        self._running = False
        self._start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def infer(
        self,
        input_name: str,
        output_names: list[str] | None,
        model_input: np.ndarray,
    ) -> list[Any] | None:
        """Send inference request to child process.

        Returns list of output arrays on success, None on any failure
        (timeout, crash, pipe error).  Callers must already have a fallback
        path for None returns (the existing stub logic in each adapter).
        """
        with self._lock:
            if self._conn is None:
                return None

            request = {
                "input_name": input_name,
                "output_names": output_names,
                "model_input": model_input,
            }

            try:
                self._conn.send(request)
                if self._conn.poll(self._timeout):
                    response = self._conn.recv()
                    if isinstance(response, dict) and "error" in response:
                        logger.warning(
                            "InferenceGuard worker error for %s: %s",
                            self._model_path,
                            response["error"],
                        )
                        return None
                    return response  # list of np.ndarray
                else:
                    logger.warning(
                        "InferenceGuard timeout (%.1fs) for %s",
                        self._timeout,
                        self._model_path,
                    )
                    self._handle_crash()
                    return None
            except (BrokenPipeError, EOFError, ConnectionResetError, OSError) as exc:
                logger.warning(
                    "InferenceGuard pipe error for %s: %s",
                    self._model_path,
                    exc,
                )
                self._handle_crash()
                return None

    def shutdown(self) -> None:
        """Gracefully terminate the worker process."""
        with self._lock:
            self._running = False
            self._send_sentinel()
            self._cleanup()

    @property
    def is_alive(self) -> bool:
        return self._running and self._process is not None and self._process.is_alive()

    @property
    def crash_count(self) -> int:
        return self._crash_count

    @property
    def model_path(self) -> str:
        return self._model_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start(self) -> None:
        """Spawn the worker subprocess."""
        try:
            ctx = mp.get_context("spawn")
            parent_conn, child_conn = ctx.Pipe()
            self._process = ctx.Process(
                target=run_worker,
                args=(child_conn, self._model_path),
                name=f"onnx-worker-{Path(self._model_path).stem}",
                daemon=True,
            )
            self._process.start()
            child_conn.close()  # close child end in parent
            self._conn = parent_conn
            self._running = True
            logger.info(
                "InferenceGuard started worker pid=%s for %s",
                self._process.pid,
                self._model_path,
            )
        except Exception as exc:
            logger.error(
                "InferenceGuard failed to start worker for %s: %s",
                self._model_path,
                exc,
            )
            self._running = False
            self._conn = None

    def _handle_crash(self) -> None:
        """Called when the worker is unresponsive or pipe is broken."""
        self._crash_count += 1
        self._cleanup()

        if self._crash_count > self._max_restarts:
            logger.error(
                "InferenceGuard exceeded max restarts (%d) for %s — giving up",
                self._max_restarts,
                self._model_path,
            )
            self._running = False
            return

        logger.info(
            "InferenceGuard restarting worker for %s (attempt %d/%d)",
            self._model_path,
            self._crash_count,
            self._max_restarts,
        )
        import time

        time.sleep(RESTART_COOLDOWN)
        self._start()

    def _cleanup(self) -> None:
        """Terminate the worker process and close the pipe."""
        proc = self._process
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
        self._process = None

        if self._conn is not None:
            try:  # noqa: SIM105
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _send_sentinel(self) -> None:
        """Send None sentinel to signal graceful shutdown."""
        if self._conn is not None:
            try:  # noqa: SIM105
                self._conn.send(None)
            except Exception:
                pass

    def __del__(self) -> None:
        try:  # noqa: SIM105
            self.shutdown()
        except Exception:
            pass

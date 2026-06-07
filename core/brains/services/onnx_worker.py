"""Standalone ONNX inference worker — runs in a subprocess for crash isolation.

Protocol (via multiprocessing.Pipe):
  Request:  {"input_name": str, "output_names": list[str] | None,
             "model_input": np.ndarray}
  Response: list[np.ndarray] on success, {"error": str} on failure
  Sentinel: None — graceful shutdown

A segfault in the ONNX Runtime C++ code kills only this process,
not the main trading loop.
"""

from __future__ import annotations

import sys
from typing import Any


def run_worker(conn, model_path: str) -> None:
    """Load ONNX model and serve inference requests until sentinel received.

    Must be a module-level function so multiprocessing spawn can import it.
    """
    import numpy as np

    # Suppress ONNX Runtime telemetry
    import onnxruntime as ort

    ort.set_default_logger_severity(3)

    session: ort.InferenceSession | None = None
    input_name: str = ""
    output_names: list[str] = []

    try:
        session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        input_name = session.get_inputs()[0].name
        output_names = [o.name for o in session.get_outputs()]
    except Exception as exc:  # noqa: BLE001
        conn.send({"error": f"load_failed: {exc}"})
        conn.close()
        return

    # Main loop — block on recv, run inference, send result
    while True:
        try:
            request = conn.recv()
        except EOFError:
            break  # parent closed pipe — clean exit

        if request is None:
            break  # sentinel — graceful shutdown

        if not isinstance(request, dict):
            conn.send({"error": f"invalid_request: expected dict, got {type(request)}"})
            continue

        try:
            feed_input_name = request.get("input_name", input_name)
            feed_output_names = request.get("output_names") or output_names
            model_input = request["model_input"]

            # Ensure float32 for ONNX
            if isinstance(model_input, np.ndarray) and model_input.dtype != np.float32:
                model_input = model_input.astype(np.float32)

            outputs: list[Any] = session.run(feed_output_names, {feed_input_name: model_input})
            conn.send(outputs)
        except Exception as exc:  # noqa: BLE001
            conn.send({"error": f"inference_failed: {exc}"})


# ── Entry point for subprocess ──
if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    model_path = sys.argv[1]
    # When spawned via multiprocessing, conn is passed as arg; when run
    # standalone for testing, we don't have a pipe to use.
    sys.exit(0)

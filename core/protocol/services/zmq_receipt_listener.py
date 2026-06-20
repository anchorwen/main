"""ZeroMQ receipt listener — replaces file-based ACK polling with PUB/SUB push.

The bridge worker publishes execution receipts (acks) on a ZMQ_PUB socket.
This module provides a subscriber that replaces ``_poll_ack()`` /
``_validate_ack_sl_tp()`` file-polling loops in ``live_order_sender``,
``execution_queue``, and ``exit_watchdog``.

Usage:
    listener = ZMQReceiptListener(ack_endpoint="tcp://127.0.0.1:5557")
    listener.start()
    ...
    ack = listener.get_receipt(message_id, timeout=5.0)
    if ack:
        print(f"Received ack: {ack['ack_status']}")
    listener.stop()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import zmq

from core.runtime.fault_handler import fail_open_guard

logger = logging.getLogger(__name__)


class ZMQReceiptListener:
    """Subscribes to bridge worker ACK publications via ZMQ_SUB.

    Not intended as a singleton — each consumer process creates its own
    instance.  The SUB socket connects (not binds) to the bridge worker's
    PUB endpoint, so multiple consumers can subscribe simultaneously.

    Thread-safe: ``get_receipt()`` can be called from any thread.
    """

    def __init__(
        self,
        *,
        ack_endpoint: str = "tcp://127.0.0.1:5557",
        topic_filter: str = "ack",
        max_cached: int = 500,
        zmq_context: zmq.Context | None = None,  # type: ignore[name-defined]
    ):
        self._ack_endpoint = ack_endpoint
        self._topic_filter = topic_filter
        self._max_cached = max_cached
        self._ctx = zmq_context or zmq.Context.instance()  # type: ignore[attr-defined]
        self._sub: zmq.Socket | None = None  # type: ignore[name-defined]
        self._receipts: dict[str, dict[str, Any]] = {}
        self._condition = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background subscriber thread."""
        if self._running:
            return
        self._running = True
        self._sub = self._ctx.socket(zmq.SUB)  # type: ignore[attr-defined]
        self._sub.setsockopt(zmq.LINGER, 0)  # type: ignore[attr-defined]
        self._sub.setsockopt_string(zmq.SUBSCRIBE, self._topic_filter)  # type: ignore[attr-defined]
        self._sub.connect(self._ack_endpoint)
        self._thread = threading.Thread(
            target=self._recv_loop,
            daemon=True,
            name="ZMQReceiptListener",
        )
        self._thread.start()
        logger.info("ZMQReceiptListener started on %s", self._ack_endpoint)

    def stop(self) -> None:
        """Stop the background thread and close the socket."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sub is not None:
            self._sub.close()
            self._sub = None

    def _recv_loop(self) -> None:
        """Background thread: receive ACK messages and cache them."""
        assert self._sub is not None
        while self._running:
            try:
                # Non-blocking poll to respect _running flag
                if self._sub.poll(timeout=100):  # 100ms
                    raw = self._sub.recv_string()
                    # Format: "ack <json>"
                    _topic, _sep, payload = raw.partition(" ")
                    ack: dict[str, Any] = json.loads(payload)
                    msg_id = ack.get("message_id", "")
                    with self._condition:
                        self._receipts[msg_id] = ack
                        # Evict oldest when over capacity
                        if len(self._receipts) > self._max_cached:
                            oldest = next(iter(self._receipts))
                            del self._receipts[oldest]
                        self._condition.notify_all()
            except zmq.ZMQError:  # type: ignore[attr-defined]
                if self._running:
                    logger.debug("ZMQ error in recv loop", exc_info=True)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON ACK received")
            except Exception:  # BLE001:FOG
                with fail_open_guard("zmq_receipt_listener:_recv_loop"):
                    logger.error("Unexpected error in ACK recv loop", exc_info=True)
    def get_receipt(self, message_id: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Wait for a receipt matching *message_id*.

        Returns the receipt dict, or None if *timeout* expires.
        Thread-safe — can be called while the background thread is running.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if message_id in self._receipts:
                    return self._receipts.pop(message_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=min(remaining, 0.5))

    def get_cached(self, message_id: str) -> dict[str, Any] | None:
        """Return a previously-received receipt without waiting.

        Returns None if *message_id* hasn't been received yet.
        """
        with self._condition:
            return self._receipts.pop(message_id, None)


# ── Module-level singleton for cross-module access ──

_listener: ZMQReceiptListener | None = None
_listener_lock = threading.Lock()


def get_zmq_listener(
    ack_endpoint: str = "tcp://127.0.0.1:5557",
    auto_start: bool = True,
) -> ZMQReceiptListener | None:
    """Return (or create) the module-level ZMQReceiptListener singleton.

    Returns None if pyzmq is not available or the listener could not be
    created.  Callers should always handle the None case gracefully and
    fall back to file-based ACK polling.
    """
    global _listener
    if _listener is not None:
        return _listener
    with _listener_lock:
        if _listener is not None:
            return _listener
        try:
            _listener = ZMQReceiptListener(ack_endpoint=ack_endpoint)
            if auto_start:
                _listener.start()
            return _listener
        except zmq.ZMQError:  # type: ignore[attr-defined]
            logger.warning("Could not create ZMQ listener — falling back to file IPC")
            return None


def stop_zmq_listener() -> None:
    """Stop and clear the module-level listener singleton."""
    global _listener
    with _listener_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None


def resolve_ack(
    message_id: str,
    *,
    base_dir: str = "data",
    timeout: float = 5.0,
    poll_interval: float = 0.2,
) -> dict | None:
    """Resolve an ACK receipt — ZMQ fast path first, file polling fallback.

    This is the single entry point for all ACK consumers:
      - ``live_order_sender._validate_ack_sl_tp()``
      - ``execution_queue._flush_unsafe()``
      - ``exit_watchdog._poll_ack()``

    Returns the receipt dict, or None if neither ZMQ nor file polling
    produced a result within *timeout* seconds.
    """
    import json as _json
    import logging
    import time as _time
    from datetime import UTC
    from datetime import datetime as _datetime
    from pathlib import Path as _Path

    _logger = logging.getLogger(__name__)

    # ── ZMQ fast path ──
    # Phase 2 (DQAF-20260615-010/Phase2): BLE001 → fail_open_guard.
    # ZMQ receipt errors are non-fatal — the file fallback below will
    # catch any missed ACKs.  fail_open_guard ensures the error is logged
    # AND the loop continues rather than silently swallowing the exception.
    from core.runtime.fault_handler import fail_open_guard

    with fail_open_guard("ZMQ:ResolveAck"):
        listener = get_zmq_listener(auto_start=True)
        if listener is not None:
            ack = listener.get_receipt(message_id, timeout=timeout)
            if ack is not None:
                return ack

    # ── File polling fallback ──
    with fail_open_guard("ZMQ:FileAckFallback"):
        today = _datetime.now(UTC).strftime("%Y-%m-%d")
        ack_path = _Path(base_dir) / "receipts" / today / "exec_bridge" / f"{message_id}.ack.json"
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if ack_path.exists():
                ack = _json.loads(ack_path.read_text(encoding="utf-8"))
                return ack if isinstance(ack, dict) else None
            _time.sleep(poll_interval)

    return None

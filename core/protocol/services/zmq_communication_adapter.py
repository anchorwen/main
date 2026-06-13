import json
import logging
import threading
from typing import Any

import zmq

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.contracts.serialization.json_codec import to_dict
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT

logger = logging.getLogger(__name__)


class ZMQCommunicationAdapter:
    """ZeroMQ push adapter for sub-millisecond MT5 order dispatch.

    Replaces file-based outbox handoff (MT5CommunicationAdapter) with a
    ZMQ_PUSH socket.  The bridge worker receives orders via ZMQ_PULL,
    eliminating the ~1s file-polling latency.

    Backward-compatible: ``adapter_name="mt5"`` still routes to the file
    adapter; ``adapter_name="mt5_zmq"`` routes here (configured in
    ``environment_config.py``).

    Usage:
        adapter = ZMQCommunicationAdapter(
            order_endpoint="tcp://127.0.0.1:5556",
            terminal_path="D:\\MetaTrader 5\\terminal64.exe",
        )
        result = adapter.dispatch(request, envelope)
    """

    def __init__(
        self,
        *,
        order_endpoint: str = "tcp://127.0.0.1:5556",
        terminal_path: str = "",
        adapter_name: str = "mt5_zmq_adapter",
        zmq_context: zmq.Context | None = None,  # type: ignore[name-defined]
    ):
        self.adapter_name = adapter_name
        self._terminal_path = terminal_path
        self._order_endpoint = order_endpoint

        # Share ZMQ context when provided (multi-socket in same process)
        self._ctx = zmq_context or zmq.Context.instance()  # type: ignore[attr-defined]
        self._lock = threading.Lock()
        self._socket: zmq.Socket | None = None  # type: ignore[name-defined]

    def _ensure_connected(self) -> zmq.Socket:  # type: ignore[name-defined]
        """Lazy-connect the PUSH socket (thread-safe)."""
        if self._socket is not None:
            return self._socket
        with self._lock:
            if self._socket is not None:  # double-check
                return self._socket
            sock = self._ctx.socket(zmq.PUSH)  # type: ignore[attr-defined]
            sock.setsockopt(zmq.LINGER, 0)  # type: ignore[attr-defined]
            sock.setsockopt(zmq.SNDHWM, 1000)  # type: ignore[attr-defined]
            sock.connect(self._order_endpoint)
            self._socket = sock
            logger.info(
                "ZMQ PUSH connected to %s (adapter=%s)",
                self._order_endpoint,
                self.adapter_name,
            )
            return sock

    def dispatch(self, request: Any, envelope: Any) -> DispatchResult:
        """Push the dispatch envelope to the ZMQ bridge worker.

        Returns ``TRANSPORT_DELIVERED`` immediately — delivery is
        fire-and-forget (PUSH socket does not wait for consumer ACK).
        The bridge worker publishes execution receipts separately via
        PUB/SUB (see ``ZMQReceiptListener``).
        """
        sock = self._ensure_connected()

        payload: dict[str, Any] = {
            "request": {
                "dispatch_id": request.dispatch_id,
                "requested_at": request.requested_at,
                "route_policy": request.route_policy,
                "transport_hints": request.transport_hints,
                "governance": request.governance,
            },
            "envelope": envelope,
            "mt5": {
                "terminal_path": self._terminal_path,
            },
        }

        raw = json.dumps(to_dict(payload), ensure_ascii=False, separators=(",", ":"))
        sock.send_string(raw)

        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=request.requested_at,
            target=envelope.target,
            adapter_name=self.adapter_name,
            transport_metadata={
                "order_endpoint": self._order_endpoint,
                "bytes_sent": len(raw),
                "terminal_path": self._terminal_path,
            },
            protocol_metadata={
                "payload_format": "json",
                "delivery_channel": "mt5_zmq_push",
                "integration_mode": "zmq_bridge",
            },
            trace={"adapter": self.adapter_name},
        )

    def close(self) -> None:
        """Close the ZMQ socket (graceful shutdown)."""
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None

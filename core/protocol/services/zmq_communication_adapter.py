import json
import logging
import threading
from typing import Any

import zmq

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.contracts.serialization.json_codec import to_dict
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT
from core.protocol.services.resilience import CircuitBreaker  # Phase 2: ZMQ → File auto-failover

logger = logging.getLogger(__name__)


class CircuitBreakerOpenError(RuntimeError):
    """Raised when the ZMQ circuit breaker is OPEN and no fallback is available.

    Callers (CommunicationDispatcher) should catch this and route to a
    fallback adapter.  If no fallback exists, the dispatch fails with
    DEGRADED status.
    """


class ZMQCommunicationAdapter:
    """ZeroMQ push adapter for sub-millisecond MT5 order dispatch.

    Replaces file-based outbox handoff (MT5CommunicationAdapter) with a
    ZMQ_PUSH socket.  The bridge worker receives orders via ZMQ_PULL,
    eliminating the ~1s file-polling latency.

    Backward-compatible: ``adapter_name="mt5"`` still routes to the file
    adapter; ``adapter_name="mt5_zmq"`` routes here (configured in
    ``environment_config.py``).

    Phase 2 (DQAF-20260615-010/Phase2): Circuit Breaker integration.
    After 3 consecutive ZMQ failures the breaker opens, and dispatch
    automatically falls back to the file adapter if one is configured.

    Usage:
        adapter = ZMQCommunicationAdapter(
            order_endpoint="tcp://127.0.0.1:5556",
            terminal_path="D:\\MetaTrader 5\\terminal64.exe",
            fallback_adapter=mt5_file_adapter,   # optional — Phase 2
        )
        result = adapter.dispatch(request, envelope)
    """

    # Phase 2: Circuit breaker thresholds
    CB_FAILURE_THRESHOLD: int = 3       # consecutive ZMQ errors → OPEN
    CB_COOLDOWN_SECONDS: float = 30.0   # wait before HALF_OPEN probe

    def __init__(
        self,
        *,
        order_endpoint: str = "tcp://127.0.0.1:5556",
        terminal_path: str = "",
        adapter_name: str = "mt5_zmq_adapter",
        zmq_context: zmq.Context | None = None,  # type: ignore[name-defined]
        fallback_adapter: Any = None,  # Phase 2: MT5CommunicationAdapter for auto-failover
    ):
        self.adapter_name = adapter_name
        self._terminal_path = terminal_path
        self._order_endpoint = order_endpoint
        self._fallback_adapter = fallback_adapter

        # Share ZMQ context when provided (multi-socket in same process)
        self._ctx = zmq_context or zmq.Context.instance()  # type: ignore[attr-defined]
        self._lock = threading.Lock()
        self._socket: zmq.Socket | None = None  # type: ignore[name-defined]

        # ── Phase 2: Circuit breaker ──────────────────────────────────────
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=self.CB_FAILURE_THRESHOLD,
            cooldown_seconds=self.CB_COOLDOWN_SECONDS,
        )

    # ── Phase 2: Circuit breaker status API ──────────────────────────────

    @property
    def circuit_state(self) -> str:
        return self._circuit_breaker.state.value

    def circuit_status(self) -> dict:
        return self._circuit_breaker.get_status()

    # ── Connection management ────────────────────────────────────────────

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

    # ── Main dispatch ────────────────────────────────────────────────────

    def dispatch(self, request: Any, envelope: Any) -> DispatchResult:
        """Push the dispatch envelope to the ZMQ bridge worker.

        Phase 2: When the circuit breaker is OPEN, automatically falls back
        to the file adapter (if configured).  Otherwise raises
        ``CircuitBreakerOpenError`` for upstream handling by
        ``CommunicationDispatcher``.

        Returns ``TRANSPORT_DELIVERED`` immediately — delivery is
        fire-and-forget (PUSH socket does not wait for consumer ACK).
        The bridge worker publishes execution receipts separately via
        PUB/SUB (see ``ZMQReceiptListener``).
        """
        # ── Phase 2: Circuit breaker gate ─────────────────────────────────
        if not self._circuit_breaker.allow_request():
            # Breaker is OPEN or HALF_OPEN quota exhausted
            if self._fallback_adapter is not None:
                logger.warning(
                    "ZMQ circuit breaker %s — falling back to file adapter",
                    self._circuit_breaker.state.value,
                )
                result = self._fallback_adapter.dispatch(request, envelope)
                # Tag the result so upstream knows failover happened
                result.status = DispatchStatus.DEGRADED
                result.degrade_reason = (
                    f"ZMQ circuit breaker {self._circuit_breaker.state.value} "
                    f"(failures={self._circuit_breaker._failure_count})"
                )
                result.fallback_adapter_name = getattr(
                    self._fallback_adapter, "adapter_name", "mt5_file"
                )
                result.trace = {
                    **result.trace,
                    "zmq_circuit_breaker": self._circuit_breaker.state.value,
                    "zmq_failures": self._circuit_breaker._failure_count,
                }
                return result
            # No fallback — let upstream handle it
            raise CircuitBreakerOpenError(
                f"ZMQ circuit breaker is {self._circuit_breaker.state.value} "
                f"({self._circuit_breaker._failure_count} failures, "
                f"no fallback configured)"
            )

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

        try:
            sock.send_string(raw)
        except zmq.ZMQError:  # type: ignore[attr-defined]
            self._circuit_breaker.record_failure()
            logger.error(
                "ZMQ send failed (failure %d/%d) — circuit state: %s",
                self._circuit_breaker._failure_count,
                self.CB_FAILURE_THRESHOLD,
                self._circuit_breaker.state.value,
                exc_info=True,
            )
            # If breaker just tripped, try fallback immediately
            if self._circuit_breaker.state.value == "open" and self._fallback_adapter is not None:
                logger.warning("ZMQ circuit breaker tripped — failing over to file adapter")
                try:
                    result = self._fallback_adapter.dispatch(request, envelope)
                    result.status = DispatchStatus.DEGRADED
                    result.degrade_reason = "ZMQ send failed — circuit breaker OPEN"
                    result.fallback_adapter_name = getattr(
                        self._fallback_adapter, "adapter_name", "mt5_file"
                    )
                    result.trace = {
                        **result.trace,
                        "zmq_circuit_breaker": "open",
                        "zmq_failures": self._circuit_breaker._failure_count,
                    }
                    return result
                except Exception:
                    logger.error("Fallback adapter also failed", exc_info=True)
            raise

        # Success — reset breaker
        self._circuit_breaker.record_success()

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
                "zmq_circuit_state": self._circuit_breaker.state.value,  # Phase 2
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

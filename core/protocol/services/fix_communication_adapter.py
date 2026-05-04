"""FIX Communication Adapter — bridges FixGatewayAdapter → CommunicationDispatcher interface.

Wraps the dry-run ``FixGatewayAdapter`` as a ``dispatch(request, envelope)`` callable
so it can be registered in ``CommunicationAdapterRegistry`` and routed
by ``CommunicationDispatcher``.

Production replacement: swap the inner ``FixGatewayAdapter`` for a real QuickFIX-based adapter
while keeping this wrapper's dispatch contract unchanged.
"""

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.execution.fix_contracts import FixSessionConfig
from core.execution.fix_gateway_adapter import FixGatewayAdapter
from core.execution.gateway_contracts import OrderRequest
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT


class FixCommunicationAdapter:
    """FIX dispatch adapter implementing the ``dispatch(request, envelope)`` protocol.

    Parses the envelope payload into an ``OrderRequest``, delegates to a
    ``FixGatewayAdapter``, and wraps the result as a ``DispatchResult``.
    """

    def __init__(
        self,
        session_config: FixSessionConfig,
        adapter_name: str = "fix_adapter",
        venue: str = "",
        base_lot_size: float = 100.0,
    ):
        self.adapter_name = adapter_name
        self._session_config = session_config
        self._venue = venue or session_config.venue
        self._gateway = FixGatewayAdapter(session_config=session_config)
        self._connected = False
        self._base_lot_size = base_lot_size

    def ensure_connected(self) -> bool:
        if not self._connected:
            connect_result = self._gateway.connect()
            self._connected = connect_result.get("status") == "connected"
        return self._connected

    def disconnect(self) -> None:
        if self._connected:
            self._gateway.disconnect()
            self._connected = False

    def _calculate_quantity(self, payload: dict) -> float:
        fraction = float(payload.get("suggested_risk_fraction", 0.0))
        return max(round(fraction * self._base_lot_size, 2), 0.01)

    def dispatch(self, request, envelope) -> DispatchResult:
        """Parse envelope → OrderRequest → FIX submit → DispatchResult."""
        self.ensure_connected()
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}

        try:
            order_request = OrderRequest(
                order_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                symbol=payload.get("symbol", ""),
                side=payload.get("side", "buy"),
                order_type=payload.get("order_type", "market"),
                quantity=self._calculate_quantity(payload),
                limit_price=payload.get("limit_price"),
                venue=self._venue,
                metadata={
                    "intent_id": payload.get("intent_id", ""),
                    "suggested_risk_fraction": payload.get("suggested_risk_fraction"),
                },
            )

            state = self._gateway.submit_order(order_request)

            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name=self.adapter_name,
                ack_id=f"fix_{state.order_id}",
                transport_metadata={
                    "venue": self._venue,
                    "fix_session": self._session_config.sender_comp_id,
                    "dry_run": True,
                    "order_status": state.status,
                },
                protocol_metadata={
                    "payload_format": "fix_4.4",
                    "delivery_channel": "fix_gateway",
                    "integration_mode": "fix_engine",
                },
                trace={
                    "adapter": self.adapter_name,
                    "fix_order_id": state.order_id,
                },
            )
        except Exception as exc:
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name=self.adapter_name,
                failure_reason=str(exc),
                trace={"adapter": self.adapter_name},
            )

    def is_connected(self) -> bool:
        return self._connected

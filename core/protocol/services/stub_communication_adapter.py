from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT


class StubCommunicationAdapter:
    def __init__(self, adapter_name: str = "stub_adapter"):
        self.adapter_name = adapter_name

    def dispatch(self, request, envelope):
        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.PROTOCOL_VALIDATED,
            recorded_at=request.requested_at,
            target=envelope.target,
            adapter_name=self.adapter_name,
            ack_id=f"ack_{envelope.message_id}",
            transport_metadata={"stub": True},
            protocol_metadata={"validated": True},
            trace={"adapter": self.adapter_name},
        )

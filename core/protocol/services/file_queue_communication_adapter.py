import json
from pathlib import Path

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT
from core.contracts.serialization.json_codec import to_dict


class FileQueueCommunicationAdapter:
    def __init__(self, outbox_dir: str, adapter_name: str = "file_queue_adapter"):
        self.adapter_name = adapter_name
        self._outbox_dir = Path(outbox_dir)

    def dispatch(self, request, envelope):
        date_key = request.requested_at.strftime("%Y-%m-%d")
        target_dir = self._outbox_dir / date_key / envelope.target
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{envelope.message_id}.json"

        payload = {
            "request": {
                "dispatch_id": request.dispatch_id,
                "requested_at": request.requested_at,
                "route_policy": request.route_policy,
                "transport_hints": request.transport_hints,
                "governance": request.governance,
            },
            "envelope": envelope,
        }
        target_file.write_text(json.dumps(to_dict(payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=request.requested_at,
            target=envelope.target,
            adapter_name=self.adapter_name,
            transport_metadata={
                "outbox_path": str(target_file),
                "bytes_written": target_file.stat().st_size,
            },
            protocol_metadata={
                "payload_format": "json",
                "delivery_channel": "file_queue",
            },
            trace={"adapter": self.adapter_name},
        )






"""FIX dry-run message builder."""
from datetime import datetime

from core.execution.fix_contracts import FIX_ORDER_TYPE, FIX_SIDE, FixMessage, FixSessionConfig
from core.execution.gateway_contracts import OrderRequest


class FixMessageBuilder:
    """Builds deterministic FIX-like messages without network I/O."""

    def __init__(self, session_config: FixSessionConfig):
        self._config = session_config
        self._sequence = 1

    def build_new_order_single(self, request: OrderRequest) -> FixMessage:
        fields = self._base_fields()
        fields.update({
            "11": request.order_id,
            "55": request.symbol,
            "54": FIX_SIDE[request.side],
            "38": request.quantity,
            "40": FIX_ORDER_TYPE[request.order_type],
            "60": self._timestamp(request.created_at),
        })
        if request.limit_price is not None:
            fields["44"] = request.limit_price
        return FixMessage(msg_type="D", fields=fields)

    def build_cancel_request(self, order_id: str, symbol: str, side: str) -> FixMessage:
        fields = self._base_fields()
        fields.update({
            "11": f"cancel_{order_id}_{self._sequence}",
            "41": order_id,
            "55": symbol,
            "54": FIX_SIDE[side],
            "60": self._timestamp(datetime.utcnow()),
        })
        return FixMessage(msg_type="F", fields=fields)

    def _base_fields(self) -> dict:
        fields = {
            "8": self._config.begin_string,
            "34": self._sequence,
            "49": self._config.sender_comp_id,
            "52": self._timestamp(datetime.utcnow()),
            "56": self._config.target_comp_id,
        }
        self._sequence += 1
        return fields

    def _timestamp(self, value: datetime) -> str:
        return value.strftime("%Y%m%d-%H:%M:%S.%f")[:-3]

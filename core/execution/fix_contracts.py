"""FIX adapter contracts and constants."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


FIX_SIDE = {"buy": "1", "sell": "2"}
FIX_ORDER_TYPE = {"market": "1", "limit": "2"}
FIX_EXEC_TYPE_TO_STATUS = {
    "0": "accepted",
    "1": "partial",
    "2": "filled",
    "4": "cancelled",
    "8": "rejected",
    "C": "expired",
}


@dataclass(frozen=True)
class FixSessionConfig:
    sender_comp_id: str
    target_comp_id: str
    begin_string: str = "FIX.4.4"
    heartbeat_interval: int = 30
    venue: str = "FIX_DRY_RUN"

    def __post_init__(self) -> None:
        if not self.sender_comp_id:
            raise ValueError("sender_comp_id is required")
        if not self.target_comp_id:
            raise ValueError("target_comp_id is required")
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")


@dataclass(frozen=True)
class FixMessage:
    msg_type: str
    fields: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_tag_dict(self) -> dict[str, Any]:
        payload = dict(self.fields)
        payload["35"] = self.msg_type
        return payload

    def to_readable_string(self) -> str:
        fields = self.to_tag_dict()
        return "|".join(f"{tag}={value}" for tag, value in sorted(fields.items(), key=lambda x: int(x[0])))


@dataclass(frozen=True)
class FixExecutionReport:
    order_id: str
    exec_type: str
    ord_status: str
    venue_order_id: str | None = None
    last_qty: float = 0.0
    last_px: float = 0.0
    cum_qty: float = 0.0
    avg_px: float = 0.0
    text: str | None = None
    transact_time: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if self.exec_type not in FIX_EXEC_TYPE_TO_STATUS:
            raise ValueError(f"unsupported FIX ExecType: {self.exec_type}")

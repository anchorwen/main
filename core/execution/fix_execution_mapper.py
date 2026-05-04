"""FIX inbound execution report mapper."""

from datetime import UTC, datetime
from typing import Any

from core.contracts.ids import new_execution_event_id
from core.execution.fix_contracts import FIX_EXEC_TYPE_TO_STATUS, FixExecutionReport
from core.execution.gateway_contracts import Fill, OrderState
from core.execution.order_state_machine import OrderStateMachine


class FixExecutionReportMapper:
    """Maps FIX execution reports into canonical order state transitions."""

    def __init__(self, state_machine: OrderStateMachine | None = None):
        self._state_machine = state_machine or OrderStateMachine()

    def from_tag_dict(self, payload: dict[str, Any]) -> FixExecutionReport:
        return FixExecutionReport(
            order_id=str(payload["11"]),
            exec_type=str(payload["150"]),
            ord_status=str(payload.get("39", payload["150"])),
            venue_order_id=str(payload["37"]) if payload.get("37") is not None else None,
            last_qty=float(payload.get("32", 0) or 0),
            last_px=float(payload.get("31", 0) or 0),
            cum_qty=float(payload.get("14", 0) or 0),
            avg_px=float(payload.get("6", 0) or 0),
            text=str(payload["58"]) if payload.get("58") is not None else None,
            transact_time=self._parse_time(payload.get("60")),
        )

    def apply(self, state: OrderState, report: FixExecutionReport) -> OrderState:
        target = FIX_EXEC_TYPE_TO_STATUS[report.exec_type]
        if target == "accepted":
            if state.status == "created":
                self._state_machine.acknowledge(state)
            if state.status == "acknowledged":
                self._state_machine.accept(state)
            return state
        if target == "partial" or target == "filled":
            fill_qty = report.last_qty or max(0.0, report.cum_qty - state.filled_quantity)
            fill_px = report.last_px or report.avg_px
            fill = Fill(
                fill_id=new_execution_event_id().replace("exec_event_", "fix_fill_", 1),
                order_id=state.order_id,
                quantity=fill_qty,
                price=fill_px,
                filled_at=report.transact_time,
                liquidity="fix",
            )
            return self._state_machine.apply_fill(state, fill)
        if target == "cancelled":
            return self._state_machine.cancel(state)
        if target == "rejected":
            return self._state_machine.reject(state, report.text or "fix_rejected")
        if target == "expired":
            return self._state_machine.transition(state, "expired")
        raise ValueError(f"unsupported target status: {target}")

    def execution_event_type(self, report: FixExecutionReport) -> str:
        target = FIX_EXEC_TYPE_TO_STATUS[report.exec_type]
        return self._state_machine.event_type_for_status(target)

    def _parse_time(self, value) -> datetime:
        if value is None:
            return datetime.now(UTC).replace(tzinfo=None)
        if isinstance(value, datetime):
            return value
        text = str(value)
        for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return datetime.fromisoformat(text)

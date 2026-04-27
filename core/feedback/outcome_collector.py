class OutcomeCollector:
    """Collects execution outcomes for completed decision cycles.

    Reads execution events and reconciliation results to build a
    structured outcome record that can be attached to a decision record
    and fed into scoring/tracking.
    """

    def __init__(self, execution_event_reader, reconciliation_service=None):
        self._execution_event_reader = execution_event_reader
        self._reconciliation_service = reconciliation_service

    def collect(
        self,
        *,
        date_key: str,
        target: str,
        message_id: str,
        correlation_id: str,
        intended_action: str | None = None,
        intended_side: str | None = None,
        intended_quantity: float = 0,
    ) -> dict:
        timeline = self._execution_event_reader.build_execution_timeline(
            date_key=date_key,
            correlation_id=correlation_id,
            message_id=message_id,
        )

        reconciliation = None
        if self._reconciliation_service is not None:
            reconciliation = self._reconciliation_service.reconcile_message(
                date_key=date_key,
                target=target,
                message_id=message_id,
                correlation_id=correlation_id,
            )

        fill_quality = self._assess_fill_quality(timeline, intended_quantity)
        execution_outcome = self._determine_execution_outcome(timeline, reconciliation)

        return {
            "message_id": message_id,
            "correlation_id": correlation_id,
            "intended_action": intended_action,
            "intended_side": intended_side,
            "intended_quantity": intended_quantity,
            "timeline": timeline,
            "reconciliation": reconciliation,
            "fill_quality": fill_quality,
            "execution_outcome": execution_outcome,
        }

    def _assess_fill_quality(self, timeline: dict, intended_quantity: float) -> dict:
        filled = timeline.get("total_filled_quantity", 0)
        event_count = timeline.get("event_count", 0)
        is_terminal = timeline.get("is_terminal", False)
        terminal_type = timeline.get("terminal_event_type")

        if event_count == 0:
            return {"grade": "no_execution", "fill_ratio": 0.0, "slippage_events": 0}

        fill_ratio = filled / intended_quantity if intended_quantity > 0 else 0.0
        partial_fill_count = sum(
            1 for et in timeline.get("event_types", []) if et == "partially_filled"
        )

        if terminal_type == "filled" and (intended_quantity == 0 or filled == intended_quantity):
            grade = "clean_fill"
        elif terminal_type == "filled":
            grade = "quantity_mismatch_fill"
        elif terminal_type == "rejected":
            grade = "rejected"
        elif terminal_type in {"cancelled", "expired"}:
            grade = "cancelled" if filled == 0 else "partial_cancel"
        elif not is_terminal and filled > 0:
            grade = "partial_open"
        elif not is_terminal:
            grade = "pending"
        else:
            grade = "unknown"

        return {
            "grade": grade,
            "fill_ratio": round(fill_ratio, 4),
            "filled_quantity": filled,
            "partial_fill_count": partial_fill_count,
            "is_terminal": is_terminal,
        }

    def _determine_execution_outcome(self, timeline: dict, reconciliation: dict | None) -> str:
        recon_status = reconciliation.get("status") if reconciliation else None
        terminal = timeline.get("terminal_event_type")
        is_terminal = timeline.get("is_terminal", False)
        event_count = timeline.get("event_count", 0)

        if event_count == 0:
            return "no_execution"
        if recon_status == "matched":
            return "success"
        if recon_status == "breached":
            return "breach"
        if terminal == "rejected":
            return "rejected"
        if terminal in {"cancelled", "expired"}:
            return "cancelled"
        if recon_status == "partial":
            return "partial"
        if not is_terminal:
            return "pending"
        return "unknown"

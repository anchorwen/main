from datetime import datetime

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT

from core.deployment.domain_keys import (
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_ACTION_COUNTS,
    PAYLOAD_KEY_CAPTURED_AT,
    PAYLOAD_KEY_CAPTURED_INDEX,
    PAYLOAD_KEY_DISPATCH_ID,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_NULL_MODE,
    PAYLOAD_KEY_PAYLOAD_SUMMARY,
    PAYLOAD_KEY_REPLAY_MODE,
    PAYLOAD_KEY_SIDE,
    PAYLOAD_KEY_SYMBOL,
    PAYLOAD_KEY_SYMBOL_COUNTS,
    PAYLOAD_KEY_TARGET,
    PAYLOAD_KEY_TOTAL_DISPATCHES,
    TIMELINE_STATUS_UNKNOWN,
)


class ReplayDispatchAdapter:
    """Adapter that captures dispatch calls without sending to a real venue.

    Used in replay/backtest environments to record what *would* have
    been dispatched, while preventing any real execution.
    """

    adapter_name = "replay_adapter"

    def __init__(self):
        self._captured: list[dict] = []

    def dispatch(self, request, envelope) -> DispatchResult:
        captured = {
            PAYLOAD_KEY_DISPATCH_ID: request.dispatch_id,
            PAYLOAD_KEY_MESSAGE_ID: envelope.message_id,
            PAYLOAD_KEY_TARGET: envelope.target,
            PAYLOAD_KEY_CAPTURED_AT: datetime.utcnow().isoformat(),
            PAYLOAD_KEY_PAYLOAD_SUMMARY: {
                PAYLOAD_KEY_ACTION: envelope.payload.get(PAYLOAD_KEY_ACTION),
                PAYLOAD_KEY_SYMBOL: envelope.payload.get(PAYLOAD_KEY_SYMBOL),
                PAYLOAD_KEY_SIDE: envelope.payload.get(PAYLOAD_KEY_SIDE),
            },
        }
        self._captured.append(captured)

        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.PROTOCOL_VALIDATED,
            recorded_at=datetime.utcnow(),
            target=envelope.target,
            adapter_name=self.adapter_name,
            trace={
                PAYLOAD_KEY_REPLAY_MODE: True,
                PAYLOAD_KEY_CAPTURED_INDEX: len(self._captured) - 1,
            },
        )

    def get_captured(self) -> list[dict]:
        return list(self._captured)

    def get_captured_count(self) -> int:
        return len(self._captured)

    def reset(self) -> None:
        self._captured.clear()


class NullDispatchAdapter:
    """Drops all dispatch calls silently. For dry-run / validation modes."""

    adapter_name = "null_adapter"

    def dispatch(self, request, envelope) -> DispatchResult:
        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.PROTOCOL_VALIDATED,
            recorded_at=datetime.utcnow(),
            target=envelope.target,
            adapter_name=self.adapter_name,
            trace={PAYLOAD_KEY_NULL_MODE: True},
        )


class ReplayEnvironment:
    """Configures the service container for replay/backtest mode.

    Swaps the real dispatch adapter for a replay adapter and
    disables idempotency checks so historical data can be replayed.
    """

    def __init__(self, service_container):
        self._container = service_container
        self._replay_adapter = ReplayDispatchAdapter()
        self._original_dispatcher = None

    def activate(self) -> None:
        from core.protocol.services.communication_dispatcher import CommunicationDispatcher
        self._original_dispatcher = self._container.dispatcher
        self._container.dispatcher = CommunicationDispatcher(
            adapter=self._replay_adapter,
        )

    def deactivate(self) -> None:
        if self._original_dispatcher is not None:
            self._container.dispatcher = self._original_dispatcher
            self._original_dispatcher = None

    def get_captured_dispatches(self) -> list[dict]:
        return self._replay_adapter.get_captured()

    def get_replay_summary(self) -> dict:
        captured = self._replay_adapter.get_captured()
        action_counts: dict[str, int] = {}
        symbol_counts: dict[str, int] = {}
        for c in captured:
            action = c.get(PAYLOAD_KEY_PAYLOAD_SUMMARY, {}).get(PAYLOAD_KEY_ACTION, TIMELINE_STATUS_UNKNOWN)
            symbol = c.get(PAYLOAD_KEY_PAYLOAD_SUMMARY, {}).get(PAYLOAD_KEY_SYMBOL, TIMELINE_STATUS_UNKNOWN)
            action_counts[action] = action_counts.get(action, 0) + 1
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        return {
            PAYLOAD_KEY_TOTAL_DISPATCHES: len(captured),
            PAYLOAD_KEY_ACTION_COUNTS: action_counts,
            PAYLOAD_KEY_SYMBOL_COUNTS: symbol_counts,
        }

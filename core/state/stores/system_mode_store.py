from datetime import UTC, datetime

from core.contracts.domain.system_mode_state import SystemModeState
from core.contracts.enums import SystemMode
from core.state.schema_versions import SCHEMA_SYSTEM_MODE_STATE


class SystemModeStore:
    def __init__(self, initial_state: SystemModeState):
        self._state = initial_state
        self._history: list[SystemModeState] = [initial_state]

    def get_current(self) -> SystemModeState:
        return self._state

    def transition(self, new_mode: SystemMode, reason: str) -> SystemModeState:
        next_state = SystemModeState(
            schema_version=SCHEMA_SYSTEM_MODE_STATE,
            mode_state_id=self._state.mode_state_id,
            current_mode=new_mode,
            entered_at=datetime.now(UTC).replace(tzinfo=None),
            previous_mode=self._state.current_mode,
            reason=reason,
            constraints=dict(self._state.constraints),
            health_snapshot=dict(self._state.health_snapshot),
            transition_policy=dict(self._state.transition_policy),
            extensions=dict(self._state.extensions),
        )
        self._state = next_state
        self._history.append(next_state)
        return next_state

    def history(self) -> list[SystemModeState]:
        return list(self._history)

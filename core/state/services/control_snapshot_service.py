from datetime import UTC, datetime

from core.state.services.control_snapshot import ControlSnapshot


class ControlSnapshotService:
    def __init__(self, mode_store, override_store, brain_registry_service):
        self._mode_store = mode_store
        self._override_store = override_store
        self._brain_registry_service = brain_registry_service

    def freeze(self, symbol: str | None = None, regime: str | None = None) -> ControlSnapshot:
        mode_state = self._mode_store.get_current()
        active_overrides = self._override_store.list_active(
            now=datetime.now(UTC).replace(tzinfo=None),
            symbol=symbol,
            mode=mode_state.current_mode.value
            if hasattr(mode_state.current_mode, "value")
            else mode_state.current_mode,
            regime=regime,
        )

        return ControlSnapshot(
            captured_at=datetime.now(UTC).replace(tzinfo=None),
            mode_state=mode_state,
            active_overrides=active_overrides,
            budget_snapshot={},
            brain_registry_snapshot=self._brain_registry_service.list_active_entries(),
        )

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.contracts.domain.system_mode_state import SystemModeState
from core.contracts.enums import SystemMode
from core.state.schema_versions import SCHEMA_SYSTEM_MODE_STATE

MODE_STALE_HOURS = 24  # Reset to NORMAL if persisted mode is older than this


class SystemModeStore:
    """In-memory system mode store with disk persistence.

    On restart, the last mode (and constraints/health snapshot) are restored.
    If the persisted mode is older than MODE_STALE_HOURS, it resets to NORMAL
    to prevent stale halt/liquidation-only modes from persisting indefinitely.
    """

    def __init__(
        self,
        initial_state: SystemModeState,
        *,
        save_path: str | Path | None = None,
    ):
        self._state = initial_state
        self._history: list[SystemModeState] = [initial_state]
        self._save_path = Path(save_path) if save_path else None

    # ── public API ──

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
        self._save()
        return next_state

    def history(self) -> list[SystemModeState]:
        return list(self._history)

    # ── persistence ──

    def _save(self) -> None:
        if self._save_path is None:
            return
        try:
            self._save_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": SCHEMA_SYSTEM_MODE_STATE,
                "mode_state_id": self._state.mode_state_id,
                "current_mode": (
                    self._state.current_mode.value
                    if isinstance(self._state.current_mode, SystemMode)
                    else str(self._state.current_mode)
                ),
                "entered_at": self._state.entered_at.isoformat(),
                "previous_mode": (
                    self._state.previous_mode.value
                    if isinstance(self._state.previous_mode, SystemMode)
                    else str(self._state.previous_mode)
                    if self._state.previous_mode
                    else None
                ),
                "reason": self._state.reason,
                "constraints": self._state.constraints,
                "health_snapshot": self._state.health_snapshot,
                "transition_policy": self._state.transition_policy,
            }
            self._save_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # Disk write failure is non-fatal for trading

    @classmethod
    def load_latest(
        cls,
        save_path: str | Path,
        *,
        default_mode: SystemMode = SystemMode.NORMAL,
        stale_hours: int = MODE_STALE_HOURS,
    ) -> "SystemModeStore":
        """Restore from disk, falling back to default_mode if stale or missing."""
        path = Path(save_path)
        initial = SystemModeState(
            schema_version=SCHEMA_SYSTEM_MODE_STATE,
            mode_state_id="init",
            current_mode=default_mode,
            entered_at=datetime.now(UTC).replace(tzinfo=None),
            previous_mode=None,
            reason="initialized",
        )

        if not path.exists():
            return cls(initial, save_path=path)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(initial, save_path=path)

        mode_str = data.get("current_mode", "normal")
        try:
            mode = SystemMode(mode_str)
        except ValueError:
            mode = default_mode

        entered_str = data.get("entered_at", "")
        try:
            entered_at = datetime.fromisoformat(entered_str)
        except (ValueError, TypeError):
            entered_at = datetime.now(UTC).replace(tzinfo=None)

        # Reset if stale — prevents permanent halt/liquidation from old data
        age = datetime.now(UTC).replace(tzinfo=None) - entered_at
        if age > timedelta(hours=stale_hours):
            return cls(initial, save_path=path)

        restored = SystemModeState(
            schema_version=SCHEMA_SYSTEM_MODE_STATE,
            mode_state_id=data.get("mode_state_id", "restored"),
            current_mode=mode,
            entered_at=entered_at,
            previous_mode=data.get("previous_mode"),
            reason=data.get("reason", "restored from disk"),
            constraints=data.get("constraints", {}),
            health_snapshot=data.get("health_snapshot", {}),
            transition_policy=data.get("transition_policy", {}),
        )
        store = cls(restored, save_path=path)
        return store

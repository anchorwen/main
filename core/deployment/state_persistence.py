import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    BRAIN_STATUS_CANDIDATE,
    PAYLOAD_KEY_BRAIN_STATES,
    PAYLOAD_KEY_CLOSED_POSITIONS,
    PAYLOAD_KEY_LABEL,
    PAYLOAD_KEY_OPEN_POSITIONS,
    PAYLOAD_KEY_PATHS,
    PAYLOAD_KEY_RISK_CONTEXT,
    PAYLOAD_KEY_SAVED_AT,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARIES,
    PAYLOAD_KEY_TRANSITION_LOG,
    STATE_PERSISTENCE_KEY_GOVERNANCE,
    STATE_PERSISTENCE_KEY_POSITIONS,
    STATE_PERSISTENCE_KEY_TRACKER,
)


class StatePersistence:
    """Persists and restores runtime state for crash recovery.

    Handles governance brain states and brain performance tracker
    snapshots.  Uses simple JSON files partitioned by date.
    """

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def save_governance_state(self, governance_service, label: str = "latest") -> Path:
        states = governance_service.get_all_states()
        log = governance_service.get_transition_log()
        payload = {
            PAYLOAD_KEY_SAVED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_BRAIN_STATES: states,
            PAYLOAD_KEY_TRANSITION_LOG: log,
        }
        path = self._base_dir / STATE_PERSISTENCE_KEY_GOVERNANCE / f"{label}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def restore_governance_state(self, governance_service, label: str = "latest") -> dict | None:
        path = self._base_dir / STATE_PERSISTENCE_KEY_GOVERNANCE / f"{label}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        for brain_id, state in data.get(PAYLOAD_KEY_BRAIN_STATES, {}).items():
            existing = governance_service.get_brain_state(brain_id)
            if existing is None:
                governance_service.register_brain(
                    brain_id, state.get(PAYLOAD_KEY_STATUS, BRAIN_STATUS_CANDIDATE)
                )
        return data

    def save_brain_tracker(self, brain_tracker, label: str = "latest") -> Path:
        summaries = brain_tracker.get_all_summaries()
        payload = {
            PAYLOAD_KEY_SAVED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_SUMMARIES: summaries,
        }
        path = self._base_dir / STATE_PERSISTENCE_KEY_TRACKER / f"{label}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def save_positions(self, position_tracker, label: str = "latest") -> Path:
        payload = {
            PAYLOAD_KEY_SAVED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_OPEN_POSITIONS: position_tracker.list_open(),
            PAYLOAD_KEY_CLOSED_POSITIONS: position_tracker.list_closed(),
            PAYLOAD_KEY_RISK_CONTEXT: position_tracker.get_risk_context(),
        }
        path = self._base_dir / STATE_PERSISTENCE_KEY_POSITIONS / f"{label}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def save_all(self, container, label: str | None = None) -> dict:
        label = label or datetime.now(UTC).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
        paths: dict[str, str] = {}
        if container.governance_service:
            paths[STATE_PERSISTENCE_KEY_GOVERNANCE] = str(
                self.save_governance_state(container.governance_service, label)
            )
        if container.brain_tracker:
            paths[STATE_PERSISTENCE_KEY_TRACKER] = str(
                self.save_brain_tracker(container.brain_tracker, label)
            )
        if container.position_tracker:
            paths[STATE_PERSISTENCE_KEY_POSITIONS] = str(
                self.save_positions(container.position_tracker, label)
            )
        return {PAYLOAD_KEY_LABEL: label, PAYLOAD_KEY_PATHS: paths}

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.exceptions import BrainNotFoundError, InvalidTransitionError

GOVERNANCE_STATE_SCHEMA = "governance_state.v1"


class GovernanceService:
    """Brain lifecycle governance.

    Consumes health signals from BrainPerformanceTracker and applies
    promotion / demotion / freeze rules.  Maintains a governance
    ledger so that every state transition is auditable.
    """

    STATUS_LIVE = "live"
    STATUS_CANDIDATE = "candidate"
    STATUS_PROBATION = "probation"
    STATUS_FROZEN = "frozen"
    STATUS_RETIRED = "retired"

    VALID_TRANSITIONS = {
        "shadow": {"candidate", "probation", "frozen", "retired"},
        "candidate": {"live", "probation", "retired"},
        "live": {"probation", "frozen", "retired"},
        "probation": {"live", "frozen", "retired"},
        "frozen": {"probation", "retired"},
        "retired": set(),
    }

    def __init__(self, audit_log: Any = None):
        self._brain_states: dict[str, dict] = {}
        self._transition_log: list[dict] = []
        self._audit_log = audit_log

    # ── persistence ──

    def save(self, path: str | Path) -> Path:
        """Persist governance state to a JSON file."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": GOVERNANCE_STATE_SCHEMA,
            "brain_states": self._brain_states,
            "transition_log": self._transition_log,
        }
        out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return out

    @classmethod
    def load(cls, path: str | Path, audit_log: Any = None) -> "GovernanceService":
        """Load governance state from a JSON file."""
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"governance state file not found: {src}")
        data = json.loads(src.read_text(encoding="utf-8"))
        svc = cls(audit_log=audit_log)
        svc._brain_states = data.get("brain_states", {})
        svc._transition_log = data.get("transition_log", [])
        return svc

    def register_brain(self, brain_id: str, initial_status: str = "candidate") -> dict:
        state = {
            "brain_id": brain_id,
            "status": initial_status,
            "registered_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "last_transition_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "transition_count": 0,
            "freeze_count": 0,
        }
        self._brain_states[brain_id] = state
        return state

    def get_brain_state(self, brain_id: str) -> dict | None:
        return self._brain_states.get(brain_id)

    def get_all_states(self) -> dict[str, dict]:
        return dict(self._brain_states)

    def apply_recommendation(self, brain_id: str, recommendation: str, reason: str = "") -> dict:
        action_map = {
            "eligible_for_promotion": self._promote,
            "demote_to_probation": self._demote,
            "freeze": self._freeze,
            "limit_exposure": self._limit_exposure,
            "maintain": lambda bid, r: {"action": "no_change", "brain_id": bid},
            "observe": lambda bid, r: {"action": "no_change", "brain_id": bid},
        }
        handler = action_map.get(
            recommendation, lambda bid, r: {"action": "unknown", "brain_id": bid}
        )
        return handler(brain_id, reason)

    def transition(self, brain_id: str, new_status: str, reason: str = "") -> dict:
        state = self._brain_states.get(brain_id)
        if state is None:
            state = self.register_brain(brain_id, new_status)
            return {"action": "registered", "brain_id": brain_id, "new_status": new_status}

        current = state["status"]
        if new_status not in self.VALID_TRANSITIONS.get(current, set()):
            return {
                "action": "rejected",
                "brain_id": brain_id,
                "current_status": current,
                "requested_status": new_status,
                "reason": f"invalid transition from {current} to {new_status}",
            }

        old_status = current
        state["status"] = new_status
        state["last_transition_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
        state["transition_count"] += 1
        if new_status == self.STATUS_FROZEN:
            state["freeze_count"] += 1

        transition_record = {
            "brain_id": brain_id,
            "from_status": old_status,
            "to_status": new_status,
            "reason": reason,
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        self._transition_log.append(transition_record)

        if self._audit_log:
            self._audit_log.log_governance_signal(
                brain_id=brain_id,
                signal_type="status_transition",
                recommendation=new_status,
                health_signal=reason,
            )

        return {
            "action": "transitioned",
            "brain_id": brain_id,
            "from": old_status,
            "to": new_status,
        }

    def strict_transition(self, brain_id: str, new_status: str, reason: str = "") -> dict:
        """Like transition() but raises on invalid operations."""
        state = self._brain_states.get(brain_id)
        if state is None:
            raise BrainNotFoundError(brain_id)
        current = state["status"]
        if new_status not in self.VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(brain_id, current, new_status)
        return self.transition(brain_id, new_status, reason)

    def _promote(self, brain_id: str, reason: str) -> dict:
        state = self._brain_states.get(brain_id)
        if state is None:
            return {"action": "not_found", "brain_id": brain_id}
        target = self.STATUS_LIVE
        return self.transition(brain_id, target, reason or "promotion")

    def _demote(self, brain_id: str, reason: str) -> dict:
        return self.transition(brain_id, self.STATUS_PROBATION, reason or "demotion")

    def _freeze(self, brain_id: str, reason: str) -> dict:
        return self.transition(brain_id, self.STATUS_FROZEN, reason or "freeze")

    def _limit_exposure(self, brain_id: str, reason: str) -> dict:
        state = self._brain_states.get(brain_id)
        if state:
            state["exposure_limited"] = True
        return {"action": "exposure_limited", "brain_id": brain_id}

    def get_active_brain_ids(self) -> list[str]:
        return [
            bid
            for bid, s in self._brain_states.items()
            if s["status"] in {self.STATUS_LIVE, self.STATUS_PROBATION, self.STATUS_CANDIDATE}
        ]

    def get_transition_log(self) -> list[dict]:
        return list(self._transition_log)

    def process_feedback_signals(self, governance_signals: list[dict]) -> list[dict]:
        results = []
        for signal in governance_signals:
            brain_id = signal.get("brain_id")
            rec = signal.get("recommendation")
            if brain_id and rec:
                result = self.apply_recommendation(
                    brain_id, rec, reason=signal.get("health_signal", "")
                )
                results.append(result)
        return results

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from core.contracts.exceptions import BrainNotFoundError, InvalidTransitionError

GOVERNANCE_STATE_SCHEMA = "governance_state.v1"


class GovernanceService:
    """Brain lifecycle governance.

    Consumes health signals from BrainPerformanceTracker and applies
    promotion / demotion / freeze rules.  Maintains a governance
    ledger so that every state transition is auditable.

    Thread-safe: all reads/writes to ``_brain_states`` and
    ``_transition_log`` are protected by ``_lock``.
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
        self._lock = threading.RLock()  # RLock: transition() may call register_brain()
        # FIX-20260629-171: Ghost registration defense-in-depth.
        # When set (non-empty), register_brain() rejects brain_ids NOT in this set.
        # Populated from config SSOT (brains_dir/*.json) by callers that have
        # access to the authoritative brain config directory.
        self._valid_brain_ids: set[str] = set()

    def set_valid_brain_ids(self, brain_ids: set[str]) -> None:
        """Set the whitelist of valid brain IDs (from config SSOT on disk).

        When non-empty, ``register_brain()`` will reject any brain_id not in
        this set — preventing ghost registrations from PnL-ledger-only brains
        that have no config on disk (DQAF-063 / FIX-20260629-171).
        """
        with self._lock:
            self._valid_brain_ids = set(brain_ids)

    @staticmethod
    def resolve_valid_brain_ids(brains_dir: str | Path) -> set[str]:
        """Resolve valid brain IDs from config SSOT on disk.

        Reads all ``brain_registry_entry.v1`` configs from *brains_dir*.
        Only top-level ``*.json`` files are scanned — subdirectories
        (like ``archive/``) are excluded.

        Args:
            brains_dir: Path to the brains config directory (e.g.
                        ``configs/brains_btc`` or ``configs/brains``).

        Returns:
            Set of brain_id strings that have active configs on disk.
        """
        import json as _json

        _dir = Path(brains_dir)
        if not _dir.is_dir():
            return set()
        valid: set[str] = set()
        for _cfg_path in sorted(_dir.glob("*.json")):
            if "normalization" in _cfg_path.name.lower():
                continue
            try:
                _cfg = _json.loads(_cfg_path.read_text(encoding="utf-8"))
                if _cfg.get("schema_version") == "brain_registry_entry.v1":
                    _bid = _cfg.get("brain_id", "")
                    if _bid:
                        valid.add(_bid)
            except (_json.JSONDecodeError, OSError, KeyError):
                pass
        return valid

    # ── persistence ──

    def save(self, path: str | Path, *, lock_timeout: float = 5.0) -> Path:
        """Persist governance state to a JSON file.

        Uses cross-process advisory file lock + in-process thread lock +
        atomic tmp+replace.  Safe for concurrent writers across
        independent processes.

        Args:
            path: Target JSON file path.
            lock_timeout: Max seconds to wait for the cross-process lock.
                Live daemon callers should pass ``1.0`` to avoid blocking
                the main cycle.  Offline scripts may pass ``30.0``.

        Raises:
            RuntimeError: If the cross-process lock cannot be acquired
                within *lock_timeout* seconds.
        """
        from core.infrastructure.distributed_lock import FileLock

        lock = FileLock(
            name="governance_state",
            ttl_seconds=max(lock_timeout * 3, 10),
        )
        result = lock.acquire(blocking=True, timeout_seconds=lock_timeout)

        if not result.acquired:
            raise RuntimeError(
                f"Governance save aborted: lock acquisition timed out "
                f"({lock_timeout}s) — {result.error}"
            )

        try:
            with self._lock:
                # ── FIX-20260712-002: transition_log integrity gate ──
                # Detect and refuse to persist invalid transitions (e.g.
                # live→shadow which is not a valid governance status).
                # Sanitize: drop pre-existing invalid entries from transition_log.
                # FIX-20260712-002 integrity gate — log warning + remove rather
                # than blocking the save (pre-existing corruption must not prevent
                # reconciliation from writing corrected state).
                _clean_log = []
                _cleaned_count = 0
                for entry in self._transition_log:
                    _from = entry.get("from", "")
                    _to = entry.get("to", "")
                    if _from in self.VALID_TRANSITIONS and _to not in self.VALID_TRANSITIONS.get(
                        _from, set()
                    ):
                        logger.warning(
                            "Governance save: dropping invalid transition [%s] %s: %s→%s",
                            entry.get("brain_id", "?"),
                            entry.get("reason", "?"),
                            _from,
                            _to,
                        )
                        _cleaned_count += 1
                    else:
                        _clean_log.append(entry)
                if _cleaned_count:
                    logger.warning(
                        "Governance save: cleaned %d invalid transition(s) from log",
                        _cleaned_count,
                    )
                    self._transition_log = _clean_log

                payload = {
                    "schema_version": GOVERNANCE_STATE_SCHEMA,
                    "brain_states": dict(self._brain_states),
                    "transition_log": list(self._transition_log),
                }
                # DQAF-046 Plan B: delegate to StateWriter for schema
                # validation + atomic write (tmp → fsync → os.replace).
                from core.state.catalog import lookup
                from core.state.writer import StateWriter

                writer = StateWriter.from_state_path(path)
                writer.write_artifact(lookup("GOVERNANCE_STATE"), writer._symbol, payload)
                return Path(path)
        finally:
            lock.release()

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
        # FIX-20260629-171: Auto-resolve valid brain IDs from config SSOT
        # when loading.  The brains_dir is inferred from the state file path
        # (governance_state.json lives alongside the data directory for the
        # symbol, e.g. data_btc/ or data/).
        _data_dir = src.parent
        if _data_dir.name == "data_btc":
            svc.set_valid_brain_ids(cls.resolve_valid_brain_ids("configs/brains_btc"))
        elif _data_dir.name == "data":
            svc.set_valid_brain_ids(cls.resolve_valid_brain_ids("configs/brains"))
        return svc

    def register_brain(self, brain_id: str, initial_status: str = "candidate") -> dict:
        # ── FIX-20260629-171: Ghost registration defense-in-depth ──
        # Reject brain_ids that have no config on disk (e.g. archived brains
        # with PnL ledger entries).  This is the LAST LINE of defense — all
        # other registration paths (daily_ops, governance_scheduler,
        # scheduler_service) should also filter upstream, but this guarantees
        # no ghost can reach the transition log regardless of caller.
        if self._valid_brain_ids and brain_id not in self._valid_brain_ids:
            return {
                "action": "rejected_ghost",
                "brain_id": brain_id,
                "reason": (
                    "GHOST REGISTRATION BLOCKED: brain_id has PnL data but "
                    "no config on disk — archived or orphaned brain"
                ),
            }
        with self._lock:
            ts = datetime.now(UTC).replace(tzinfo=None).isoformat()
            state = {
                "brain_id": brain_id,
                "status": initial_status,
                "registered_at": ts,
                "last_transition_at": ts,
                "transition_count": 1,
                "freeze_count": 0,
            }
            self._brain_states[brain_id] = state
            # FIX-20260529-034: append transition_log entry for audit trail
            self._transition_log.append(
                {
                    "brain_id": brain_id,
                    "from": None,
                    "to": initial_status,
                    "reason": "brain_registered",
                    "timestamp": ts,
                    "fix_id": "FIX-20260529-034",
                }
            )
            return state

    def get_brain_state(self, brain_id: str) -> dict | None:
        with self._lock:
            return self._brain_states.get(brain_id)

    def get_all_states(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._brain_states)

    def set_performance_metrics(self, brain_id: str, metrics: dict[str, Any]) -> None:
        """Inject live performance metrics into governance state (P0 Visibility Fix).

        Fields: win_rate, profit_factor, sharpe_ratio, total_trades, pnl_r.
        """
        with self._lock:
            state = self._brain_states.get(brain_id)
            if state is not None:
                state["performance_metrics"] = metrics

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
        with self._lock:
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
        with self._lock:
            state = self._brain_states.get(brain_id)
            if state:
                state["exposure_limited"] = True
            return {"action": "exposure_limited", "brain_id": brain_id}

    def get_active_brain_ids(self) -> list[str]:
        with self._lock:
            return [
                bid
                for bid, s in self._brain_states.items()
                if s["status"] in {self.STATUS_LIVE, self.STATUS_PROBATION, self.STATUS_CANDIDATE}
            ]

    def get_transition_log(self) -> list[dict]:
        with self._lock:
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

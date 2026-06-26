"""Phantom Contracts — production audit stubs for UGR v3.1.

UGR v3.1 §修正1: Phantom stubs record complete input snapshots + WAL seq
references, enabling deterministic offline replay of predicate checks.

Architecture:
- ``hot_path=False`` (default): Production writes audit stub to WAL.
  Offline verifier replays against WAL-reconstructed state.
- ``hot_path=True``: NEVER writes stub (WAL overhead unacceptable).
  Uses ``__debug__`` assertion only. Violations caught by InvariantEngine.

Contract classification (from phantom_state_replay.md §6):

HOT-PATH (no stub):
  - risk_budget_non_negative, exit_latency_bounded,
  - position_count_consistent, no_silent_cap_unwrap

NON-HOT-PATH (stub → auditable offline):
  - training_readiness, governance_alignment,
  - model_card_completeness, data_health_report_completeness,
  - alpha_lifecycle_valid

Usage::

    from core.contracts.phantom_contract import phantom, PredicateRegistry

    # Define a predicate and register it
    def _check_risk(budget: float, **_state: Any) -> bool:
        return budget >= 0.0

    PredicateRegistry.register("risk_budget_non_negative", version=1)(_check_risk)

    # Apply to a function
    @phantom(
        predicate=_check_risk,
        message="Risk budget must be non-negative",
        contract_id="risk_budget_non_negative",
        hot_path=True,
    )
    def evaluate_risk(budget: float) -> bool:
        return budget >= 0.0
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.data.write_ahead_log import WALConfig, WriteAheadLog

# ═══════════════════════════════════════════════════════════════════════════
# PhantomStub — the audit record written to WAL
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PhantomStub:
    """A single phantom contract audit record.

    Written to WAL in production (-O mode, hot_path=False).
    Contains everything the offline verifier needs for deterministic replay.

    Per phantom_state_replay.md §1.1 — Required Fields.
    """

    contract_id: str  # Unique contract identifier
    recorded_at_wal_seq: int  # WAL sequence number for state reconstruction
    contract_version: int = 1  # Schema version
    input_snapshot: dict[str, Any] = field(default_factory=dict)  # Serialized args
    input_hash: str = ""  # SHA256 of serialized input_snapshot (dedup)
    assumed_ok: bool = True  # Production assumed predicate passed
    timestamp_wall: str = ""  # ISO-8601 wall clock
    caller_module: str = ""  # __name__ of calling module

    def to_payload(self) -> dict[str, Any]:
        """Serialize to WAL payload dict."""
        return {
            "contract_id": self.contract_id,
            "recorded_at_wal_seq": self.recorded_at_wal_seq,
            "contract_version": self.contract_version,
            "input_snapshot": self.input_snapshot,
            "input_hash": self.input_hash,
            "assumed_ok": self.assumed_ok,
            "timestamp_wall": self.timestamp_wall,
            "caller_module": self.caller_module,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> PhantomStub:
        """Deserialize from WAL payload dict."""
        return cls(
            contract_id=data.get("contract_id", ""),
            recorded_at_wal_seq=data.get("recorded_at_wal_seq", 0),
            contract_version=data.get("contract_version", 1),
            input_snapshot=data.get("input_snapshot", {}),
            input_hash=data.get("input_hash", ""),
            assumed_ok=data.get("assumed_ok", True),
            timestamp_wall=data.get("timestamp_wall", ""),
            caller_module=data.get("caller_module", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════
# PhantomSerializer — deterministic arg serialization
# ═══════════════════════════════════════════════════════════════════════════


class PhantomSerializer:
    """Serialize/deserialize PhantomStub input_snapshot values.

    Per phantom_state_replay.md §3 — JSON-first protocol.
    Deterministic: same input → same input_hash.
    """

    @staticmethod
    def serialize_args(args: tuple, kwargs: dict) -> dict[str, Any]:
        """Serialize function arguments into input_snapshot format.

        Returns: {"args": [...], "kwargs": {...}}
        """
        return {
            "args": [PhantomSerializer._serialize_value(a) for a in args],
            "kwargs": {k: PhantomSerializer._serialize_value(v) for k, v in kwargs.items()},
        }

    @staticmethod
    def deserialize_args(snapshot: dict[str, Any]) -> tuple[tuple, dict]:
        """Deserialize input_snapshot back to (args, kwargs)."""
        args_list = snapshot.get("args", [])
        kwargs_dict = snapshot.get("kwargs", {})
        return (
            tuple(PhantomSerializer._deserialize_value(v) for v in args_list),
            {k: PhantomSerializer._deserialize_value(v) for k, v in kwargs_dict.items()},
        )

    @staticmethod
    def compute_hash(snapshot: dict[str, Any]) -> str:
        """SHA256 of serialized input_snapshot (deterministic)."""
        content = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_value(obj: Any) -> Any:
        """Convert a Python value to a JSON-serializable structure."""
        import math as _math

        if obj is None:
            return None
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, int):
            return obj
        if isinstance(obj, float):
            # Special float values per spec §3.1
            if _math.isnan(obj):
                return "__NaN__"
            if _math.isinf(obj):
                return "__Inf__" if obj > 0 else "__NegInf__"
            return obj
        if isinstance(obj, str):
            return obj
        if isinstance(obj, list | tuple):
            return [PhantomSerializer._serialize_value(v) for v in obj]
        if isinstance(obj, dict):
            return {str(k): PhantomSerializer._serialize_value(v) for k, v in obj.items()}
        # Extended types per spec §3.1
        import datetime as _dt
        import decimal as _dec
        import reprlib

        if isinstance(obj, _dt.datetime):
            return {"__datetime__": obj.isoformat()}
        if isinstance(obj, _dec.Decimal):
            return {"__decimal__": str(obj)}
        # numpy types (optional import)
        try:
            import numpy as _np
        except ImportError:
            _np = None  # type: ignore[assignment]
        if _np is not None:
            if isinstance(obj, (_np.float32, _np.float64)):  # noqa: UP038
                return float(obj)
            if isinstance(obj, _np.ndarray):
                return {
                    "__ndarray__": obj.tolist(),
                    "__dtype__": str(obj.dtype),
                }
        # Everything else: WARNING marker
        return {"__unserializable__": reprlib.repr(obj)}

    @staticmethod
    def _deserialize_value(data: Any) -> Any:
        """Convert a JSON structure back to Python objects."""
        if data is None:
            return None
        if isinstance(data, bool):
            return data
        if isinstance(data, int | float):
            return data
        if isinstance(data, str):
            # Special float markers per spec §3.1
            if data == "__NaN__":
                return float("nan")
            if data == "__Inf__":
                return float("inf")
            if data == "__NegInf__":
                return float("-inf")
            return data
        if isinstance(data, list):
            return [PhantomSerializer._deserialize_value(v) for v in data]
        if isinstance(data, dict):
            # Check for special markers
            if "__datetime__" in data:
                import datetime as _dt

                return _dt.datetime.fromisoformat(data["__datetime__"])
            if "__decimal__" in data:
                import decimal as _dec

                return _dec.Decimal(data["__decimal__"])
            if "__ndarray__" in data:
                try:
                    import numpy as _np

                    return _np.array(data["__ndarray__"], dtype=data.get("__dtype__", "float64"))
                except ImportError:
                    return data["__ndarray__"]  # fallback to list
            if "__unserializable__" in data:
                return data  # Return as-is with warning marker
            return {k: PhantomSerializer._deserialize_value(v) for k, v in data.items()}
        return data


# ═══════════════════════════════════════════════════════════════════════════
# Predicate Registry — contract_id → predicate function
# ═══════════════════════════════════════════════════════════════════════════


class PredicateRegistry:
    """Global registry of phantom contract predicates.

    Maps contract_id → predicate function for offline replay.
    Per phantom_state_replay.md §5.

    UGR-B04: Added reset() for test isolation, duplicate registration
    warning, and required_state_keys per predicate.
    """

    _predicates: dict[str, Callable[..., bool]] = {}
    _versions: dict[str, int] = {}
    _required_state_keys: dict[str, set[str]] = {}

    @classmethod
    def register(
        cls,
        contract_id: str,
        *,
        version: int = 1,
        required_state_keys: set[str] | None = None,
    ):
        """Decorator to register a phantom predicate function.

        Args:
            contract_id: Unique contract identifier.
            version: Schema version — bump on predicate signature change.
            required_state_keys: Set of state keys this predicate depends on.
                StateProjector verifies these keys exist before passing _state
                to the predicate.  None or empty = no state dependency.

        Usage::

            @PredicateRegistry.register("risk_budget_non_negative", version=1)
            def _check_risk_budget(budget: float, **_state: Any) -> bool:
                return budget >= 0.0
        """

        def decorator(fn: Callable[..., bool]) -> Callable[..., bool]:
            if contract_id in cls._predicates and __debug__:
                import warnings

                warnings.warn(
                    f"PredicateRegistry: duplicate registration of '{contract_id}' "
                    f"(previous version={cls._versions.get(contract_id)}, "
                    f"new version={version}). "
                    f"Overwriting.",
                    stacklevel=2,
                )
            cls._predicates[contract_id] = fn
            cls._versions[contract_id] = version
            if required_state_keys:
                cls._required_state_keys[contract_id] = set(required_state_keys)
            return fn

        return decorator

    @classmethod
    def get(cls, contract_id: str) -> Callable[..., bool] | None:
        """Look up a predicate by contract_id."""
        return cls._predicates.get(contract_id)

    @classmethod
    def get_version(cls, contract_id: str) -> int:
        """Get the current version of a predicate."""
        return cls._versions.get(contract_id, 0)

    @classmethod
    def get_required_state_keys(cls, contract_id: str) -> set[str]:
        """Return the set of state keys required by this predicate.

        Empty set = no state dependency (input-only predicate).
        """
        return cls._required_state_keys.get(contract_id, set())

    @classmethod
    def list_contracts(cls) -> dict[str, int]:
        """Return all registered contracts and their versions."""
        return dict(cls._versions)

    @classmethod
    def reset(cls) -> None:
        """Clear all registered predicates.  For test isolation only.

        NOT for production use — resets the global registry.
        """
        cls._predicates.clear()
        cls._versions.clear()
        cls._required_state_keys.clear()


# ═══════════════════════════════════════════════════════════════════════════
# StateProjector — WAL state reconstruction for state-dependent predicates
# ═══════════════════════════════════════════════════════════════════════════


class StateProjectionError(Exception):
    """Raised when state projection fails.

    Causes: missing required state keys, handler exception, timeout, overflow.
    The offline verifier treats any StateProjectionError as a verification
    failure for the affected stub — conservative: cannot confirm predicate result.
    """

    pass


class StateProjector:
    """Accumulates system state by applying WAL entries in sequence.

    UGR-B04 审核加固:
      - State completeness assertion: predicates declare required_state_keys;
        snapshot() verifies all declared keys exist and are not None.
      - Handler ordering: handlers execute in priority order; conflict detection
        warns when multiple handlers write the same key.
      - Error propagation: handler exceptions are caught and wrapped as
        StateProjectionError — never silently skipped.
      - Timeout / overflow protection: project_to() accepts max_replay_entries
        and timeout_seconds to prevent CI blockage.
    """

    def __init__(self, checkpoint_snapshot: dict[str, Any] | None = None) -> None:
        """Initialize projector, optionally from a checkpoint snapshot."""
        self._state: dict[str, Any] = dict(checkpoint_snapshot) if checkpoint_snapshot else {}
        self._handlers: dict[str, tuple[int, Callable]] = {}  # event_type → (priority, handler)
        self._handler_writes: dict[str, set[str]] = {}  # event_type → keys written
        self._all_written_keys: set[str] = set()
        self._required_keys: dict[str, set[str]] = {}  # contract_id → required keys
        self._entry_count: int = 0
        self._errors: list[str] = []

    # ── Handler registration ────────────────────────────────────────────

    def register_handler(
        self,
        event_type: str,
        handler: Callable[["StateProjector", dict], None],  # noqa: UP037 (forward ref)
        *,
        priority: int = 0,
        writes_keys: set[str] | None = None,
    ) -> None:
        """Register a state-update handler for a WAL event type.

        Args:
            event_type: WAL record type to match.
            handler: Callable(projector, entry) -> None.  Must be idempotent.
            priority: Lower = earlier execution.  Same priority = registration order.
            writes_keys: State keys this handler modifies (conflict detection).

        Raises UserWarning if writes_keys overlap with another handler.
        """
        write_set = writes_keys or set()
        conflicts = write_set & self._all_written_keys
        if conflicts:
            import warnings

            warnings.warn(
                f"StateProjector: key conflict — {event_type} writes "
                f"{sorted(conflicts)} which another handler also writes. "
                f"Ordering matters: results depend on execution sequence.",
                stacklevel=2,
            )
        self._handlers[event_type] = (priority, handler)
        self._handler_writes[event_type] = write_set
        self._all_written_keys |= write_set

    # ── Required state keys ─────────────────────────────────────────────

    def declare_required_keys(self, contract_id: str, keys: set[str]) -> None:
        """Declare the state keys that a contract predicate depends on.

        snapshot() verifies all declared keys exist before returning state.
        Missing keys → StateProjectionError.
        """
        self._required_keys[contract_id] = set(keys)

    def get_required_keys(self, contract_id: str) -> set[str]:
        """Return the required state keys for a contract (empty if none)."""
        return self._required_keys.get(contract_id, set())

    # ── WAL replay ──────────────────────────────────────────────────────

    def apply(self, entry: dict[str, Any]) -> None:
        """Apply one WAL entry to accumulated state.

        Dispatches to registered handler for entry["type"].
        Non-matching entries (no handler) are silently skipped.

        Raises StateProjectionError if the handler raises.
        """
        event_type = entry.get("type", "")
        handler_info = self._handlers.get(event_type)
        if handler_info is None:
            return
        _, handler = handler_info
        try:
            handler(self, entry)
        except Exception as exc:
            raise StateProjectionError(
                f"Handler for '{event_type}' raised {type(exc).__name__}: {exc}"
            ) from exc
        self._entry_count += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of accumulated state.

        Validates ALL declared required keys across ALL contracts.
        For contract-specific validation, use snapshot_for(contract_id).
        """
        for contract_id, keys in self._required_keys.items():
            for key in keys:
                if key not in self._state:
                    raise StateProjectionError(
                        f"Required state key '{key}' (declared by '{contract_id}') "
                        f"not found in projected state. Available keys: "
                        f"{sorted(self._state.keys())}"
                    )
                if self._state[key] is None:
                    raise StateProjectionError(
                        f"Required state key '{key}' (declared by '{contract_id}') "
                        f"is None — state incomplete."
                    )
        return dict(self._state)

    def snapshot_for(self, contract_id: str) -> dict[str, Any]:
        """Return state validated only for *contract_id*'s required keys.

        Unlike snapshot(), this only checks keys needed by one contract,
        avoiding false errors from unrelated contracts' key requirements.
        """
        keys = self._required_keys.get(contract_id, set())
        for key in keys:
            if key not in self._state:
                raise StateProjectionError(
                    f"Required state key '{key}' (declared by '{contract_id}') "
                    f"not found in projected state. Available keys: "
                    f"{sorted(self._state.keys())}"
                )
            if self._state[key] is None:
                raise StateProjectionError(
                    f"Required state key '{key}' (declared by '{contract_id}') "
                    f"is None — state incomplete."
                )
        return dict(self._state)

    def project_to(
        self,
        wal: Any,  # WriteAheadLog (avoid circular import)
        target_seq: int,
        *,
        max_replay_entries: int = 100_000,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Replay WAL from current position to target_seq, return snapshot.

        Args:
            wal: WriteAheadLog instance.
            target_seq: WAL sequence number to project state at.
            max_replay_entries: Max entries before StateProjectionError.
            timeout_seconds: Max wall-clock seconds before StateProjectionError.

        Raises StateProjectionError on timeout, overflow, or handler error.
        """
        import time as _time

        start = _time.monotonic()
        count = 0

        for record in wal:
            count += 1
            if count > max_replay_entries:
                raise StateProjectionError(
                    f"State projection exceeded max_replay_entries={max_replay_entries}. "
                    f"Increase checkpoint frequency or use incremental mode."
                )
            if _time.monotonic() - start > timeout_seconds:
                raise StateProjectionError(
                    f"State projection timed out after {timeout_seconds:.0f}s "
                    f"({count} entries replayed)."
                )

            # Stop before target stub's own sequence
            if record.seq >= target_seq:
                break

            payload = record.payload if isinstance(record.payload, dict) else {}
            event_type = record.type or payload.get("type", "")
            if event_type and event_type != "phantom_stub" and event_type in self._handlers:
                self.apply({"type": event_type, **payload})

        # Return raw state — caller validates per-contract keys via snapshot_for()
        return dict(self._state)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset accumulated state and entry counter.  Handlers persist."""
        self._state.clear()
        self._entry_count = 0
        self._errors.clear()


# ═══════════════════════════════════════════════════════════════════════════
# MVP Predicate: risk_budget_non_negative
# ═══════════════════════════════════════════════════════════════════════════


@PredicateRegistry.register("risk_budget_non_negative", version=1)
def _predicate_risk_budget_non_negative(budget: float, **_: Any) -> bool:
    """Hot-path: risk budget must be ≥ 0.

    No state dependency — purely checks the input parameter.
    """
    return budget >= 0.0


# ═══════════════════════════════════════════════════════════════════════════
# UGR-B04: 8 additional predicates
# ═══════════════════════════════════════════════════════════════════════════


@PredicateRegistry.register("exit_latency_bounded", version=1)
def _predicate_exit_latency_bounded(latency_ms: float, max_ms: float = 500.0, **_: Any) -> bool:
    """Hot-path: exit latency must not exceed max threshold."""
    return latency_ms <= max_ms


@PredicateRegistry.register(
    "position_count_consistent",
    version=1,
    required_state_keys={"positions"},
)
def _predicate_position_count_consistent(
    declared: int, *, _state: dict | None = None, **_: Any
) -> bool:
    """Hot-path: declared open positions must match WAL-reconstructed count."""
    if _state is None:
        return True  # No state available — cannot verify, assume pass
    positions = _state.get("positions", {})
    return len(positions) == declared


@PredicateRegistry.register("no_silent_cap_unwrap", version=1)
def _predicate_no_silent_cap_unwrap(is_ok: bool, error_msg: str = "", **_: Any) -> bool:
    """Hot-path: CapResult must not be unwrapped silently when is_ok=False."""
    if not is_ok and not error_msg:
        return False  # Silent unwrap detected
    return True


@PredicateRegistry.register(
    "training_readiness",
    version=1,
    required_state_keys={"brain_states"},
)
def _predicate_training_readiness(
    training_prereqs: dict, *, _state: dict | None = None, **_: Any
) -> bool:
    """Non-hot-path: training prerequisites must be met."""
    required = training_prereqs.get("required", [])
    if not required:
        return True
    if _state is None:
        return True  # No state available — assume pass
    brain_states = _state.get("brain_states", {})
    for prereq in required:
        if prereq not in brain_states:
            return False
        if not brain_states[prereq].get("ready", False):
            return False
    return True


@PredicateRegistry.register(
    "governance_alignment",
    version=1,
    required_state_keys={"brain_states"},
)
def _predicate_governance_alignment(brain_id: str, *, _state: dict | None = None, **_: Any) -> bool:
    """Non-hot-path: brain must be registered in governance state."""
    if _state is None:
        return True
    brain_states = _state.get("brain_states", {})
    return brain_id in brain_states


@PredicateRegistry.register("model_card_completeness", version=1)
def _predicate_model_card_completeness(model_card: dict, **_: Any) -> bool:
    """Non-hot-path: model card must contain all required fields."""
    required_fields = {"brain_id", "version", "feature_set", "training_date"}
    for fld in required_fields:
        if fld not in model_card or model_card[fld] is None:
            return False
    return True


@PredicateRegistry.register("data_health_report_completeness", version=1)
def _predicate_data_health_report_completeness(report: dict, **_: Any) -> bool:
    """Non-hot-path: data health report must pass all checks."""
    checks = report.get("checks", {})
    if not checks:
        return False  # Empty report is invalid
    for _check_name, result in checks.items():
        if not result.get("passed", False):
            return False
    return True


@PredicateRegistry.register(
    "alpha_lifecycle_valid",
    version=1,
    required_state_keys={"brain_states", "positions"},
)
def _predicate_alpha_lifecycle_valid(
    alpha_states: dict, *, _state: dict | None = None, **_: Any
) -> bool:
    """Non-hot-path: alpha lifecycle states must be internally consistent."""
    if _state is None:
        return True
    # Verify each alpha's brain_id exists in governance
    brain_states = _state.get("brain_states", {})
    for _alpha_id, alpha_info in alpha_states.items():
        brain_id = alpha_info.get("brain_id", "")
        if brain_id and brain_id not in brain_states:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Built-in StateProjector handlers
# ═══════════════════════════════════════════════════════════════════════════


def _handle_position_open(projector: StateProjector, entry: dict) -> None:
    """Apply a position_open event to projector state."""
    pos_id = entry.get("position_id", entry.get("ticket", ""))
    if pos_id:
        projector._state.setdefault("positions", {})[pos_id] = {
            "size": entry.get("size", 0.0),
            "symbol": entry.get("symbol", ""),
            "open_time": entry.get("open_time", ""),
        }


def _handle_position_close(projector: StateProjector, entry: dict) -> None:
    """Apply a position_close event to projector state (idempotent remove)."""
    pos_id = entry.get("position_id", entry.get("ticket", ""))
    if pos_id and "positions" in projector._state:
        projector._state["positions"].pop(pos_id, None)


def _handle_budget_update(projector: StateProjector, entry: dict) -> None:
    """Apply a budget_update event to projector state."""
    brain_id = entry.get("brain_id", "")
    if brain_id:
        projector._state.setdefault("risk_budget", {})[brain_id] = {
            "remaining": entry.get("remaining", 0.0),
            "allocated": entry.get("allocated", 0.0),
        }


def _handle_brain_state(projector: StateProjector, entry: dict) -> None:
    """Apply a brain_state_change event to projector state (idempotent)."""
    brain_id = entry.get("brain_id", "")
    if brain_id:
        projector._state.setdefault("brain_states", {})[brain_id] = {
            "ready": entry.get("ready", False),
            "status": entry.get("status", "unknown"),
            "updated_at": entry.get("updated_at", ""),
        }


# Register built-in handlers at module import time
def _register_builtin_handlers() -> None:
    """Register the default StateProjector handlers.

    Called once at module import time.  The module-level StateProjector
    prototype instance is NOT created here — each verifier creates its own.
    """
    # Handlers are registered on individual StateProjector instances,
    # not globally.  This function documents the expected handler contract.
    pass


# ═══════════════════════════════════════════════════════════════════════════
# Phantom decorator
# ═══════════════════════════════════════════════════════════════════════════


def phantom(
    *,
    predicate: Callable[..., bool],
    message: str,
    contract_id: str,
    severity: str = "critical",
    hot_path: bool = False,
) -> Callable:
    """Phantom Contract decorator — UGR v3.1 §修正1.

    In ``__debug__`` mode (development/testing): executes the predicate as
    an assertion.  If the predicate fails, raises ``ContractViolation``.

    In production (``-O``, ``__debug__ == False``):
      - ``hot_path=False``: writes a ``PhantomStub`` to the WAL for offline
        audit.  The predicate is NOT executed — production assumes it passes.
      - ``hot_path=True``: does nothing (zero overhead).  Violations are
        caught by ``InvariantEngine`` checking invariants on every cycle.

    Args:
        predicate: The binary predicate function to audit.
        message: Human-readable message on violation.
        contract_id: Unique contract identifier (must be registered).
        severity: Alert severity if violated.
        hot_path: If True, never write stubs (performance-critical paths).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if __debug__ and not _FORCE_PRODUCTION_MODE:
                # Development/testing: execute predicate as assertion
                if not predicate(*args, **kwargs):
                    _alert_violation(contract_id, message, severity)
                    raise ContractViolation(f"[{contract_id}] {message}")
            elif not hot_path:
                # Production (or test-mode production), non-hot-path: write audit stub
                _write_phantom_stub(
                    contract_id=contract_id,
                    args=args,
                    kwargs=kwargs,
                    predicate=predicate,
                    caller_module=fn.__module__ or "",
                )
            # hot_path + production: nothing (zero cost)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


# Module-level WAL reference — set by the application at startup.
# Not a hard import to avoid circular dependency on WAL config.
_wal: WriteAheadLog | None = None

# UGR-A10: whether the phantom WAL was created by this module (owned)
_phantom_wal_owned: bool = False

# Test-only flag: force production behavior (stub recording) even when
# __debug__ is True.  Set by test fixtures; never used in production.
_FORCE_PRODUCTION_MODE: bool = False


def set_phantom_wal(wal: WriteAheadLog) -> None:
    """Set the WAL instance for phantom stub recording.

    Called once at application startup.  Phantom stubs are silently
    dropped if no WAL is configured.
    """
    global _wal, _phantom_wal_owned
    _wal = wal
    _phantom_wal_owned = False


def init_phantom_wal(config: WALConfig) -> WriteAheadLog:
    """Create an independent WAL instance for phantom stub recording (UGR-A10).

    Unlike set_phantom_wal() which reuses an externally-provided WAL,
    this creates a dedicated WAL with its own fsync policy and disk quota.
    The independent WAL isolates audit traffic from the main application WAL.

    Returns the created WriteAheadLog instance.
    """
    global _wal, _phantom_wal_owned
    _wal = WriteAheadLog(config)
    _phantom_wal_owned = True
    return _wal


def get_phantom_wal() -> WriteAheadLog | None:
    """Return the current phantom WAL instance, if any."""
    return _wal


def _write_phantom_stub(
    *,
    contract_id: str,
    args: tuple,
    kwargs: dict,
    predicate: Callable[..., bool],
    caller_module: str,
) -> None:
    """Serialize and write a PhantomStub to the WAL.

    Called from the phantom decorator in production mode.
    Silent no-op if no WAL is configured (fail-open for audit path).
    """
    if _wal is None:
        return

    import datetime as _dt

    input_snapshot = PhantomSerializer.serialize_args(args, kwargs)
    input_hash = PhantomSerializer.compute_hash(input_snapshot)

    # Execute predicate to record assumed_ok
    try:
        assumed = predicate(*args, **kwargs)
    except Exception:  # noqa: BLE001
        assumed = False

    stub = PhantomStub(
        contract_id=contract_id,
        recorded_at_wal_seq=len(_wal),  # Current WAL length (next seq)
        contract_version=PredicateRegistry.get_version(contract_id),
        input_snapshot=input_snapshot,
        input_hash=input_hash,
        assumed_ok=assumed,
        timestamp_wall=_dt.datetime.now(_dt.UTC).isoformat(),
        caller_module=caller_module,
    )

    # UGR-A10: disk quota guard — reject append if phantom WAL exceeds quota
    within_quota, quota_reason = _wal.check_quota()
    if not within_quota:
        import sys as _sys

        print(f"[phantom] WAL quota exceeded, dropping stub: {quota_reason}", file=_sys.stderr)
        return

    with contextlib.suppress(Exception):
        _wal.append(stub.to_payload(), record_type="phantom_stub")


# Track violations per contract_id for operational metrics (UGR-B04 审核 R2#7)
_violation_counts: dict[str, int] = {}
_violation_lock: Any = None  # lazily created threading.Lock


def get_violation_counts() -> dict[str, int]:
    """Return a copy of the violation count map.  Thread-safe."""
    global _violation_lock
    if _violation_lock is None:
        import threading as _threading

        _violation_lock = _threading.Lock()
    with _violation_lock:
        return dict(_violation_counts)


def _alert_violation(contract_id: str, message: str, severity: str) -> None:
    """Emit an alert when a phantom contract is violated.

    Routes through LiveAlertHub if available; falls back to stderr.
    Increments per-contract violation counter for operational metrics.
    """
    import sys

    # Increment counter (thread-safe)
    global _violation_lock
    if _violation_lock is None:
        import threading as _threading

        _violation_lock = _threading.Lock()
    with _violation_lock:
        _violation_counts[contract_id] = _violation_counts.get(contract_id, 0) + 1

    # Route through alert hub if available
    try:
        from core.observability.live_alert_hub import LiveAlertHub

        hub = LiveAlertHub(base_dir="data")
        hub.send_critical(
            reason=f"phantom:{contract_id}",
            detail={"contract_id": contract_id, "severity": severity, "message": message},
        )
        return
    except ImportError:
        pass  # Alert hub unavailable — fall back to stderr

    print(
        f"[PhantomContract] VIOLATION: [{contract_id}] {message} severity={severity}",
        file=sys.stderr,
    )


class ContractViolation(Exception):
    """Raised when a phantom contract predicate fails in __debug__ mode."""

    pass

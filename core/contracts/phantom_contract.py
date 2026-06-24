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

from core.data.write_ahead_log import WriteAheadLog

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
        if obj is None:
            return None
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, int | float):
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
    """

    _predicates: dict[str, Callable[..., bool]] = {}
    _versions: dict[str, int] = {}

    @classmethod
    def register(cls, contract_id: str, *, version: int = 1):
        """Decorator to register a phantom predicate function.

        Usage::

            @PredicateRegistry.register("risk_budget_non_negative", version=1)
            def _check_risk_budget(budget: float, **_state: Any) -> bool:
                return budget >= 0.0
        """

        def decorator(fn: Callable[..., bool]) -> Callable[..., bool]:
            cls._predicates[contract_id] = fn
            cls._versions[contract_id] = version
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
    def list_contracts(cls) -> dict[str, int]:
        """Return all registered contracts and their versions."""
        return dict(cls._versions)


# ═══════════════════════════════════════════════════════════════════════════
# MVP Predicate: risk_budget_non_negative
# ═══════════════════════════════════════════════════════════════════════════


@PredicateRegistry.register("risk_budget_non_negative", version=1)
def _predicate_risk_budget_non_negative(budget: float, **_: Any) -> bool:
    """MVP predicate: risk budget must be ≥ 0.

    No state dependency — purely checks the input parameter.
    Per phantom_state_replay.md §2.3, state reconstruction is a no-op for MVP.
    """
    return budget >= 0.0


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

# Test-only flag: force production behavior (stub recording) even when
# __debug__ is True.  Set by test fixtures; never used in production.
_FORCE_PRODUCTION_MODE: bool = False


def set_phantom_wal(wal: WriteAheadLog) -> None:
    """Set the WAL instance for phantom stub recording.

    Called once at application startup.  Phantom stubs are silently
    dropped if no WAL is configured.
    """
    global _wal
    _wal = wal


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

    with contextlib.suppress(Exception):
        _wal.append(stub.to_payload(), record_type="phantom_stub")


def _alert_violation(contract_id: str, message: str, severity: str) -> None:
    """Emit an alert when a phantom contract is violated.

    Attempts to use the alert bus if available; falls back to stderr.
    """
    import sys

    print(
        f"[PhantomContract] VIOLATION: [{contract_id}] {message} " f"severity={severity}",
        file=sys.stderr,
    )


class ContractViolation(Exception):
    """Raised when a phantom contract predicate fails in __debug__ mode."""

    pass

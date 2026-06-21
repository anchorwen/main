"""Unified exception hierarchy for the decision system.

All domain-level exceptions inherit from DomainError, allowing
callers to catch broad categories or specific error types.
"""


class DomainError(Exception):
    """Base class for all domain exceptions."""

    def __init__(self, message: str, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.code = code or self.__class__.__name__
        self.detail = detail or {}


# --- Risk layer ---


class RiskError(DomainError):
    """Raised when risk evaluation encounters an invalid state."""


class RiskPolicyViolation(RiskError):
    """A specific risk policy was violated."""

    def __init__(self, policy_name: str, reason: str, **detail):
        super().__init__(
            f"Risk policy '{policy_name}' violated: {reason}",
            code="risk_policy_violation",
            detail={"policy": policy_name, "reason": reason, **detail},
        )


# --- Governance layer ---


class GovernanceError(DomainError):
    """Raised when governance operations fail."""


class InvalidTransitionError(GovernanceError):
    """Brain state transition is not allowed."""

    def __init__(self, brain_id: str, from_status: str, to_status: str):
        super().__init__(
            f"Invalid transition for '{brain_id}': {from_status} -> {to_status}",
            code="invalid_transition",
            detail={"brain_id": brain_id, "from": from_status, "to": to_status},
        )


class BrainNotFoundError(GovernanceError):
    """Brain ID not found in governance registry."""

    def __init__(self, brain_id: str):
        super().__init__(
            f"Brain not found: {brain_id}", code="brain_not_found", detail={"brain_id": brain_id}
        )


# --- Execution layer ---


class ExecutionError(DomainError):
    """Raised when execution operations fail."""


class OrderNotFoundError(ExecutionError):
    """Order not found in execution manager."""

    def __init__(self, message_id: str):
        super().__init__(
            f"Order not found: {message_id}",
            code="order_not_found",
            detail={"message_id": message_id},
        )


class DuplicateOrderError(ExecutionError):
    """Order already registered."""

    def __init__(self, message_id: str):
        super().__init__(
            f"Duplicate order: {message_id}",
            code="duplicate_order",
            detail={"message_id": message_id},
        )


# --- Protocol layer ---


class ProtocolError(DomainError):
    """Raised when communication protocol operations fail."""


class DispatchError(ProtocolError):
    """Dispatch to venue failed."""

    def __init__(self, reason: str, venue: str = "unknown"):
        super().__init__(
            f"Dispatch failed to {venue}: {reason}",
            code="dispatch_failed",
            detail={"venue": venue, "reason": reason},
        )


class IdempotencyError(ProtocolError):
    """Duplicate idempotency key detected."""

    def __init__(self, key: str):
        super().__init__(
            f"Duplicate idempotency key: {key}", code="idempotency_duplicate", detail={"key": key}
        )


# --- Configuration layer ---


class ConfigurationError(DomainError):
    """Raised when configuration is invalid."""


class ContractViolationError(DomainError):
    """Raised when a data contract is violated."""

    def __init__(self, violations: list):
        msg = f"{len(violations)} contract violation(s)"
        super().__init__(
            msg,
            code="contract_violation",
            detail={
                "violations": [v.to_dict() if hasattr(v, "to_dict") else str(v) for v in violations]
            },
        )


# --- Data Integrity layer (DQAF-20260621-042) ---


class DataIntegrityError(DomainError):
    """Raised when data fails schema validation — POISON PILL.

    Per Iron Law IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION:
    If a materialized view (leaderboard, governance) cannot be generated
    with complete data, the system MUST halt rather than produce
    silently-corrupted output that downstream consumers (trading, alerts)
    would act upon.

    This is a Fail-Closed poison pill: catch → log → terminate the cycle.
    """

    def __init__(self, message: str, source: str = "unknown"):
        super().__init__(
            message,
            code="data_integrity_error",
            detail={"source": source},
        )

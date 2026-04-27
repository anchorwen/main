"""Contract validators for runtime data integrity.

Validates that data flowing through the system conforms to expected
schemas at key boundaries. Useful as a debugging tool and for
pre-production validation.
"""
from datetime import datetime


class ContractViolation:
    __slots__ = ("field", "expected", "actual", "message")

    def __init__(self, field: str, expected: str, actual, message: str = ""):
        self.field = field
        self.expected = expected
        self.actual = actual
        self.message = message or f"{field}: expected {expected}, got {type(actual).__name__}={actual}"

    def to_dict(self) -> dict:
        return {"field": self.field, "expected": self.expected,
                "actual": str(self.actual), "message": self.message}


class ContractValidator:
    """Validates data contracts at system boundaries."""

    @staticmethod
    def validate_intent(intent) -> list[ContractViolation]:
        errors = []
        for attr in ("intent_id", "candidate_id", "snapshot_id", "symbol", "venue"):
            val = getattr(intent, attr, None)
            if not val or not isinstance(val, str):
                errors.append(ContractViolation(attr, "non-empty str", val))

        action = getattr(intent, "action", None)
        if action is None:
            errors.append(ContractViolation("action", "DecisionAction", None))

        side = getattr(intent, "side", None)
        if side is None:
            errors.append(ContractViolation("side", "DecisionSide", None))

        conviction = getattr(intent, "conviction", None)
        if conviction is not None and not (0.0 <= float(conviction) <= 1.0):
            errors.append(ContractViolation("conviction", "0.0-1.0", conviction))

        event_time = getattr(intent, "event_time", None)
        if event_time is not None and not isinstance(event_time, datetime):
            errors.append(ContractViolation("event_time", "datetime", event_time))

        return errors

    @staticmethod
    def validate_envelope(envelope) -> list[ContractViolation]:
        errors = []
        for attr in ("message_id", "correlation_id", "producer", "target"):
            val = getattr(envelope, attr, None)
            if not val or not isinstance(val, str):
                errors.append(ContractViolation(attr, "non-empty str", val))

        event_time = getattr(envelope, "event_time", None)
        if not isinstance(event_time, datetime):
            errors.append(ContractViolation("event_time", "datetime", event_time))

        payload = getattr(envelope, "payload", None)
        if payload is not None and not isinstance(payload, dict):
            errors.append(ContractViolation("payload", "dict", payload))

        return errors

    @staticmethod
    def validate_verdict(verdict) -> list[ContractViolation]:
        errors = []
        verdict_id = getattr(verdict, "verdict_id", None)
        if not verdict_id:
            errors.append(ContractViolation("verdict_id", "non-empty str", verdict_id))

        status = getattr(verdict, "status", None)
        if status is None:
            errors.append(ContractViolation("status", "RiskDecisionStatus", None))

        blocking = getattr(verdict, "blocking_reasons", None)
        if blocking is not None and not isinstance(blocking, (list, tuple)):
            errors.append(ContractViolation("blocking_reasons", "list", blocking))

        return errors

    @staticmethod
    def validate_risk_context(ctx: dict) -> list[ContractViolation]:
        errors = []
        required_keys = ["open_position_count", "positions_per_symbol",
                         "current_notional_exposure", "current_drawdown_pct"]
        for key in required_keys:
            if key not in ctx:
                errors.append(ContractViolation(key, "present", "missing"))

        count = ctx.get("open_position_count")
        if count is not None and (not isinstance(count, int) or count < 0):
            errors.append(ContractViolation("open_position_count", "non-negative int", count))

        return errors

    @staticmethod
    def validate_cycle_outcome(outcome) -> list[ContractViolation]:
        errors = []
        if not hasattr(outcome, "cycle_id"):
            errors.append(ContractViolation("cycle_id", "present", None))
        if not hasattr(outcome, "trigger"):
            errors.append(ContractViolation("trigger", "present", None))
        if not hasattr(outcome, "audit_entries"):
            errors.append(ContractViolation("audit_entries", "present", None))
        return errors

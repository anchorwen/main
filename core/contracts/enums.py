from enum import Enum


class BrainRole(str, Enum):
    ALPHA = "alpha_brain"
    REGIME = "regime_brain"
    EXECUTION = "execution_brain"
    RISK = "risk_brain"


class BrainStatus(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"
    CANDIDATE = "candidate"
    PROBATION = "probation"
    LIMITED = "limited"
    FROZEN = "frozen"
    RETIRED = "retired"


class DecisionAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    REDUCE = "reduce"
    REVERSE = "reverse"
    ABSTAIN = "abstain"
    OBSERVE = "observe"


class DecisionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class RiskDecisionStatus(str, Enum):
    ALLOW = "allow"
    ALLOW_LIMITED = "allow_limited"
    DEFER = "defer"
    DENY = "deny"
    FORCE_REDUCE = "force_reduce"
    LIQUIDATE_ONLY = "liquidate_only"


class SystemMode(str, Enum):
    NORMAL = "normal"
    CAUTIOUS = "cautious"
    DEGRADED = "degraded"
    OBSERVE_ONLY = "observe_only"
    LIQUIDATION_ONLY = "liquidation_only"
    HALTED = "halted"


class OverrideStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class CommunicationMessageType(str, Enum):
    DECISION_INTENT = "decision_intent"
    EXECUTION_DISPATCH = "execution_dispatch"
    EXECUTION_ACK = "execution_ack"
    STATE_SYNC = "state_sync"
    ALERT = "alert"


class CommunicationPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class DispatchStatus(str, Enum):
    ACCEPTED = "accepted"
    TRANSPORT_DELIVERED = "transport_delivered"
    PROTOCOL_VALIDATED = "protocol_validated"
    SEMANTICALLY_ACKNOWLEDGED = "semantically_acknowledged"
    DEGRADED = "degraded"
    FAILED = "failed"


class ReplayGateDecision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"


class ExecutionEventType(str, Enum):
    ACK = "ack"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    AMENDED = "amended"
    EXPIRED = "expired"


class ReconciliationStatus(str, Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    PARTIAL = "partial"
    BREACHED = "breached"
    STALE = "stale"

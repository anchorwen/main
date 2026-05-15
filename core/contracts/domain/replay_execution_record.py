from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.domain_keys import (
    EXECUTION_MODE_VALUE_FULL,
    EXECUTION_MODE_VALUE_TARGETED,
    PAYLOAD_KEY_BLOCKED_MESSAGES,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DISPATCH_RESULT,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_STATE,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_POSTURE,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_REPLAY_TRACE,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_SKIPPED_MESSAGES,
    PAYLOAD_KEY_STATUS,
    REPLAY_EXECUTION_RECORD_ERROR_REPLAY_ID_REQUIRED,
    REPLAY_EXECUTION_RECORD_ERROR_SCOPE_REQUIRED,
    REPLAY_EXECUTION_STATUS_BLOCKED,
    TIMELINE_STATUS_UNKNOWN,
)
from core.contracts.schema_versions import SCHEMA_REPLAY_EXECUTION_RECORD
from core.ledger.governance_sources import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED as GOVERNANCE_SUMMARY_SOURCE_DERIVED,
)
from core.ledger.governance_sources import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS as GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
)
from core.ledger.governance_sources import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE as GOVERNANCE_SUMMARY_SOURCE_GATE,
)
from core.ledger.governance_sources import (
    SUPPORTED_REPLAY_GOVERNANCE_SUMMARY_SOURCES,
    is_supported_replay_governance_summary_source,
)
from core.ledger.services.replay_trace_refs import (
    correlation_id as trace_correlation_id,
)
from core.ledger.services.replay_trace_refs import (
    execution_state as trace_execution_state,
)
from core.ledger.services.replay_trace_refs import (
    message_id as trace_message_id,
)
from core.ledger.services.replay_trace_refs import (
    scope as trace_scope,
)


@dataclass
class ReplayExecutionRecord:
    """Replay record with governance fields split by role.

    Canonical governance summary sources:
    - REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS
    - REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE
    - REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED

    Execution-oriented projection fields:
    - execution.governance_posture
    - execution.governance_decision
    """

    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS = GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE = GOVERNANCE_SUMMARY_SOURCE_GATE
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED = GOVERNANCE_SUMMARY_SOURCE_DERIVED
    SUPPORTED_GOVERNANCE_SUMMARY_SOURCES = SUPPORTED_REPLAY_GOVERNANCE_SUMMARY_SOURCES

    schema_version: str
    replay_id: str
    scope: str
    source_message_id: str | None
    source_correlation_id: str | None
    executed_at: datetime
    gate_decision: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    skipped_messages: list[dict] = field(default_factory=list)
    blocked_messages: list[dict] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.replay_id:
            raise ValueError(REPLAY_EXECUTION_RECORD_ERROR_REPLAY_ID_REQUIRED)
        if not self.scope:
            raise ValueError(REPLAY_EXECUTION_RECORD_ERROR_SCOPE_REQUIRED)

    @classmethod
    def from_execution_result(
        cls, *, replay_id: str, executed_at: datetime, execution_result: dict
    ) -> "ReplayExecutionRecord":
        replay_trace = execution_result.get(PAYLOAD_KEY_REPLAY_TRACE, {})
        gate_decision = execution_result.get(PAYLOAD_KEY_GATE_DECISION, {})
        governance_summary = gate_decision.get(PAYLOAD_KEY_GOVERNANCE_SUMMARY, {})
        execution_projection = cls._build_execution_projection(
            execution_result, replay_trace, governance_summary
        )
        return cls(
            schema_version=SCHEMA_REPLAY_EXECUTION_RECORD,
            replay_id=replay_id,
            scope=trace_scope(replay_trace) or TIMELINE_STATUS_UNKNOWN,
            source_message_id=trace_message_id(replay_trace),
            source_correlation_id=trace_correlation_id(replay_trace),
            executed_at=executed_at,
            gate_decision=gate_decision,
            execution=execution_projection,
            results={
                PAYLOAD_KEY_DISPATCH_RESULT: cls._serialize_dispatch_result(
                    execution_result.get(PAYLOAD_KEY_DISPATCH_RESULT)
                ),
                PAYLOAD_KEY_RESULTS: cls._serialize_result_items(
                    execution_result.get(PAYLOAD_KEY_RESULTS, [])
                ),
            },
            skipped_messages=cls._serialize_message_entries(
                execution_result.get(PAYLOAD_KEY_SKIPPED_MESSAGES, [])
            ),
            blocked_messages=cls._serialize_message_entries(
                execution_result.get(PAYLOAD_KEY_BLOCKED_MESSAGES, [])
            ),
            trace=replay_trace,
            extensions={
                PAYLOAD_KEY_GOVERNANCE_SUMMARY: governance_summary,
            },
        )

    @staticmethod
    def _serialize_dispatch_result(dispatch_result: Any) -> Any:
        if dispatch_result is None:
            return None
        if hasattr(dispatch_result, "__dataclass_fields__"):
            return asdict(dispatch_result)
        return dispatch_result

    @classmethod
    def _serialize_result_items(cls, items: list[dict]) -> list[dict]:
        serialized_items = []
        for item in items:
            serialized_items.append(
                {
                    **item,
                    PAYLOAD_KEY_DISPATCH_RESULT: cls._serialize_dispatch_result(
                        item.get(PAYLOAD_KEY_DISPATCH_RESULT)
                    ),
                }
            )
        return serialized_items

    @staticmethod
    def _serialize_message_entries(items: list[dict]) -> list[dict]:
        return [dict(item) for item in items]

    @classmethod
    def is_supported_governance_summary_source(cls, source: str | None) -> bool:
        return is_supported_replay_governance_summary_source(source)

    @staticmethod
    def _build_execution_projection(
        execution_result: dict, replay_trace: dict, governance_summary: dict
    ) -> dict:
        """Build execution-facing governance projection from the canonical summary."""
        skipped_messages = execution_result.get(PAYLOAD_KEY_SKIPPED_MESSAGES, [])
        if execution_result.get(PAYLOAD_KEY_STATUS) == REPLAY_EXECUTION_STATUS_BLOCKED:
            execution_mode = REPLAY_EXECUTION_STATUS_BLOCKED
        elif skipped_messages:
            execution_mode = EXECUTION_MODE_VALUE_TARGETED
        else:
            execution_mode = EXECUTION_MODE_VALUE_FULL
        return {
            PAYLOAD_KEY_STATUS: execution_result.get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_EXECUTION_STATE: trace_execution_state(replay_trace),
            PAYLOAD_KEY_GOVERNANCE_POSTURE: governance_summary.get(PAYLOAD_KEY_POSTURE),
            PAYLOAD_KEY_GOVERNANCE_DECISION: governance_summary.get(PAYLOAD_KEY_DECISION),
            PAYLOAD_KEY_EXECUTION_MODE: execution_mode,
        }

from core.contracts.domain_keys import (
    OPERATIONS_POSTURE_SOURCE_GOVERNANCE_SUMMARY,
    OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
    OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_SUMMARY,
    OPERATIONS_POSTURE_VALUE_ACTION_REQUIRED,
    OPERATIONS_POSTURE_VALUE_AUTO_REPLAY,
    OPERATIONS_POSTURE_VALUE_BLOCKED,
    OPERATIONS_POSTURE_VALUE_HEALTHY,
    OPERATIONS_POSTURE_VALUE_OBSERVE,
    OPERATIONS_POSTURE_VALUE_REVIEW_REQUIRED,
    OPERATIONS_POSTURE_VALUE_TARGETED_REPLAY,
    OPERATIONS_POSTURE_VALUE_UNKNOWN,
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTION_GOVERNANCE_PROJECTION,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_EXECUTION_SUMMARY,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_POSTURE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_TAGS,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_RECEIPT,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_RECONCILIATION,
    PAYLOAD_KEY_RECONCILIATION_STATUS,
    PAYLOAD_KEY_RECORD,
    PAYLOAD_KEY_REPLAY_GATE,
    PAYLOAD_KEY_REPLAY_PLAN,
    PAYLOAD_KEY_REPLAY_RECORD,
    PAYLOAD_KEY_REPLAY_STATUS,
    PAYLOAD_KEY_REPLAY_TRACE,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_REVIEW_ISSUE_CODES,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY_SOURCE,
    PAYLOAD_KEY_TARGET_ISSUE_CODES,
    PAYLOAD_KEY_TARGETED_MESSAGE_IDS,
    PAYLOAD_KEY_TRACE,
    REPLAY_TRACE_SCOPE_CORRELATION,
    REPLAY_TRACE_SCOPE_MESSAGE,
)
from core.contracts.domain_keys import (
    REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION as GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
)
from core.contracts.domain_keys import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED as GOVERNANCE_SUMMARY_SOURCE_DERIVED,
)
from core.contracts.domain_keys import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS as GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
)
from core.contracts.domain_keys import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE as GOVERNANCE_SUMMARY_SOURCE_GATE,
)
from core.ledger.services.communication_replay_gate import build_replay_governance_summary
from core.ledger.services.communication_trace_refs import delivery_posture as trace_delivery_posture
from core.ledger.services.gate_decision_refs import governance_summary as gate_governance_summary
from core.ledger.services.replay_plan_refs import governance_summary as plan_governance_summary
from core.ledger.services.replay_record_refs import (
    blocked_messages as replay_blocked_messages,
)
from core.ledger.services.replay_record_refs import (
    execution_block,
    gate_block,
    results_block,
    trace_block,
)
from core.ledger.services.replay_record_refs import (
    execution_governance_projection as replay_execution_governance_projection,
)
from core.ledger.services.replay_record_refs import (
    execution_mode as replay_execution_mode,
)
from core.ledger.services.replay_record_refs import (
    governance_sources as replay_governance_sources,
)
from core.ledger.services.replay_record_refs import (
    governance_summary as replay_governance_summary,
)
from core.ledger.services.replay_record_refs import (
    grouped_reasons as replay_grouped_reasons,
)
from core.ledger.services.replay_record_refs import (
    message_ids as replay_message_ids,
)
from core.ledger.services.replay_record_refs import (
    skipped_messages as replay_skipped_messages,
)
from core.ledger.services.replay_record_refs import (
    targeted_message_ids as replay_targeted_message_ids,
)


class CommunicationOperationsService:
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS = GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE = GOVERNANCE_SUMMARY_SOURCE_GATE
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED = GOVERNANCE_SUMMARY_SOURCE_DERIVED
    REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION = GOVERNANCE_PROJECTION_SOURCE_EXECUTION

    OPERATIONS_POSTURE_HEALTHY = OPERATIONS_POSTURE_VALUE_HEALTHY
    OPERATIONS_POSTURE_OBSERVE = OPERATIONS_POSTURE_VALUE_OBSERVE
    OPERATIONS_POSTURE_ACTION_REQUIRED = OPERATIONS_POSTURE_VALUE_ACTION_REQUIRED
    OPERATIONS_POSTURE_AUTO_REPLAY = OPERATIONS_POSTURE_VALUE_AUTO_REPLAY
    OPERATIONS_POSTURE_TARGETED_REPLAY = OPERATIONS_POSTURE_VALUE_TARGETED_REPLAY
    OPERATIONS_POSTURE_REVIEW_REQUIRED = OPERATIONS_POSTURE_VALUE_REVIEW_REQUIRED
    OPERATIONS_POSTURE_BLOCKED = OPERATIONS_POSTURE_VALUE_BLOCKED
    OPERATIONS_POSTURE_UNKNOWN = OPERATIONS_POSTURE_VALUE_UNKNOWN

    def __init__(
        self,
        communication_reader,
        inspection_service,
        replay_service,
        replay_gate,
        replay_reader=None,
        receipt_reader=None,
        reconciliation_service=None,
    ):
        self._communication_reader = communication_reader
        self._inspection_service = inspection_service
        self._replay_service = replay_service
        self._replay_gate = replay_gate
        self._replay_reader = replay_reader
        self._receipt_reader = receipt_reader
        self._reconciliation_service = reconciliation_service

    def get_message_operations_view(self, *, date_key: str, target: str, message_id: str) -> dict:
        record = self._communication_reader.find_by_message_id(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        trace = self._inspection_service.get_message_trace(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        replay_plan = self._replay_service.build_message_replay_plan(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        replay_gate = self._replay_gate.evaluate_message_plan(replay_plan)
        receipt = None
        if self._receipt_reader is not None:
            receipt = self._receipt_reader.find_by_message_id(
                date_key=date_key,
                target=target,
                message_id=message_id,
            )
        governance_summary = self._resolve_governance_summary(replay_plan, replay_gate)
        operations_posture, posture_sources = self._resolve_operations_posture(trace=trace)
        reconciliation = self._run_reconciliation(
            date_key=date_key,
            target=target,
            message_id=message_id,
            correlation_id=record.get(PAYLOAD_KEY_CORRELATION_ID) if record else None,
        )
        return {
            PAYLOAD_KEY_RECORD: record,
            PAYLOAD_KEY_TRACE: trace,
            PAYLOAD_KEY_REPLAY_PLAN: replay_plan,
            PAYLOAD_KEY_REPLAY_GATE: replay_gate,
            PAYLOAD_KEY_RECEIPT: receipt,
            PAYLOAD_KEY_GOVERNANCE_SUMMARY: governance_summary,
            PAYLOAD_KEY_RECONCILIATION: reconciliation,
            PAYLOAD_KEY_OPERATIONS_POSTURE: operations_posture,
            PAYLOAD_KEY_OPERATIONS_SUMMARY: self._build_operations_summary(
                operations_posture=operations_posture,
                posture_sources=posture_sources,
                governance_summary=governance_summary,
                governance_sources=None,
                reconciliation=reconciliation,
            ),
            PAYLOAD_KEY_POSTURE_SOURCES: posture_sources,
        }

    def get_correlation_operations_view(
        self, *, date_key: str, target: str, correlation_id: str
    ) -> dict:
        trace = self._inspection_service.get_correlation_trace(
            date_key=date_key,
            target=target,
            correlation_id=correlation_id,
        )
        replay_plan = self._replay_service.build_correlation_replay_plan(
            date_key=date_key,
            target=target,
            correlation_id=correlation_id,
        )
        replay_gate = self._replay_gate.evaluate_correlation_plan(replay_plan)
        governance_summary = self._resolve_governance_summary(replay_plan, replay_gate)
        operations_posture, posture_sources = self._resolve_operations_posture(
            trace=trace, scope=REPLAY_TRACE_SCOPE_CORRELATION
        )
        return {
            PAYLOAD_KEY_TRACE: trace,
            PAYLOAD_KEY_REPLAY_PLAN: replay_plan,
            PAYLOAD_KEY_REPLAY_GATE: replay_gate,
            PAYLOAD_KEY_GOVERNANCE_SUMMARY: governance_summary,
            PAYLOAD_KEY_OPERATIONS_POSTURE: operations_posture,
            PAYLOAD_KEY_OPERATIONS_SUMMARY: self._build_operations_summary(
                operations_posture=operations_posture,
                posture_sources=posture_sources,
                governance_summary=governance_summary,
                governance_sources=None,
            ),
            PAYLOAD_KEY_POSTURE_SOURCES: posture_sources,
        }

    def get_replay_operations_view(
        self, *, date_key: str, target: str, replay_id: str
    ) -> dict | None:
        if self._replay_reader is None:
            return None
        replay_record = self._replay_reader.find_by_replay_id(
            date_key=date_key,
            target=target,
            replay_id=replay_id,
        )
        if replay_record is None:
            return None
        execution_summary = self._build_replay_execution_summary(replay_record)
        governance_sources = self._build_governance_sources(replay_record)
        governance_summary = self._resolve_replay_governance_summary(replay_record)
        execution_governance_projection = self._build_execution_governance_projection(replay_record)
        operations_posture, posture_sources = self._resolve_operations_posture(
            governance_summary=governance_summary
        )
        return {
            PAYLOAD_KEY_REPLAY_RECORD: replay_record,
            PAYLOAD_KEY_REPLAY_STATUS: execution_block(replay_record).get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_GATE_DECISION: gate_block(replay_record),
            PAYLOAD_KEY_REPLAY_TRACE: trace_block(replay_record),
            PAYLOAD_KEY_EXECUTION_SUMMARY: execution_summary,
            PAYLOAD_KEY_EXECUTION_GOVERNANCE_PROJECTION: execution_governance_projection,
            PAYLOAD_KEY_GOVERNANCE_SOURCES: governance_sources,
            PAYLOAD_KEY_GOVERNANCE_SUMMARY: governance_summary,
            PAYLOAD_KEY_OPERATIONS_POSTURE: operations_posture,
            PAYLOAD_KEY_OPERATIONS_SUMMARY: self._build_replay_operations_summary(
                operations_posture=operations_posture,
                posture_sources=posture_sources,
                governance_summary=governance_summary,
                governance_sources=governance_sources,
                execution_summary=execution_summary,
            ),
            PAYLOAD_KEY_POSTURE_SOURCES: posture_sources,
        }

    def _run_reconciliation(
        self,
        *,
        date_key: str,
        target: str,
        message_id: str,
        correlation_id: str | None,
    ) -> dict | None:
        if self._reconciliation_service is None or correlation_id is None:
            return None
        return self._reconciliation_service.reconcile_message(
            date_key=date_key,
            target=target,
            message_id=message_id,
            correlation_id=correlation_id,
        )

    def _resolve_governance_summary(
        self, replay_plan: dict | None, replay_gate: dict | None
    ) -> dict:
        gate_summary = gate_governance_summary(replay_gate)
        if gate_summary is not None:
            return gate_summary
        plan_summary = plan_governance_summary(replay_plan)
        if plan_summary is not None:
            return plan_summary
        return build_replay_governance_summary(replay_plan, replay_gate)

    def _resolve_replay_governance_summary(self, replay_record: dict) -> dict:
        return replay_governance_summary(
            replay_record,
            fallback_builder=build_replay_governance_summary,
        )

    def _resolve_operations_posture(
        self,
        *,
        trace: dict | None = None,
        governance_summary: dict | None = None,
        scope: str = REPLAY_TRACE_SCOPE_MESSAGE,
    ) -> tuple[str, dict]:
        if governance_summary is not None:
            posture = governance_summary.get(PAYLOAD_KEY_POSTURE)
            if posture in {
                self.OPERATIONS_POSTURE_AUTO_REPLAY,
                self.OPERATIONS_POSTURE_TARGETED_REPLAY,
                self.OPERATIONS_POSTURE_REVIEW_REQUIRED,
                self.OPERATIONS_POSTURE_BLOCKED,
            }:
                return posture, {
                    PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: (
                        OPERATIONS_POSTURE_SOURCE_GOVERNANCE_SUMMARY
                    ),
                }
            return self.OPERATIONS_POSTURE_UNKNOWN, {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: None,
            }

        posture_source = None
        if trace is not None:
            posture_source = (
                OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE
                if scope == REPLAY_TRACE_SCOPE_MESSAGE
                else OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_SUMMARY
            )
        if trace is None:
            return self.OPERATIONS_POSTURE_UNKNOWN, {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: posture_source,
            }
        return trace_delivery_posture(
            trace,
            scope=scope,
            default=self.OPERATIONS_POSTURE_UNKNOWN,
        ), {
            PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: posture_source,
        }

    def _build_operations_summary(
        self,
        *,
        operations_posture: str,
        posture_sources: dict,
        governance_summary: dict | None,
        governance_sources: dict | None,
        reconciliation: dict | None = None,
    ) -> dict:
        summary = {
            PAYLOAD_KEY_POSTURE: operations_posture,
            PAYLOAD_KEY_POSTURE_SOURCE: posture_sources.get(PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE),
            PAYLOAD_KEY_GOVERNANCE_DECISION: None
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_DECISION),
            PAYLOAD_KEY_GOVERNANCE_POSTURE: None
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_POSTURE),
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: None
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_RECOMMENDED_STRATEGY),
            PAYLOAD_KEY_TARGET_ISSUE_CODES: []
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_TARGET_ISSUE_CODES, []),
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: []
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_REVIEW_ISSUE_CODES, []),
            PAYLOAD_KEY_GOVERNANCE_TAGS: []
            if governance_summary is None
            else governance_summary.get(PAYLOAD_KEY_GOVERNANCE_TAGS, []),
            PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: None
            if governance_sources is None
            else governance_sources.get(PAYLOAD_KEY_SUMMARY_SOURCE),
            PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None
            if governance_sources is None
            else governance_sources.get(PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE),
            PAYLOAD_KEY_RECONCILIATION_STATUS: None
            if reconciliation is None
            else reconciliation.get(PAYLOAD_KEY_STATUS),
        }
        return summary

    def _build_replay_operations_summary(
        self,
        *,
        operations_posture: str,
        posture_sources: dict,
        governance_summary: dict | None,
        governance_sources: dict | None,
        execution_summary: dict,
    ) -> dict:
        return {
            **self._build_operations_summary(
                operations_posture=operations_posture,
                posture_sources=posture_sources,
                governance_summary=governance_summary,
                governance_sources=governance_sources,
            ),
            **{
                key: execution_summary[key]
                for key in [
                    PAYLOAD_KEY_EXECUTION_MODE,
                    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
                    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
                    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
                    PAYLOAD_KEY_SKIP_REASONS,
                    PAYLOAD_KEY_BLOCK_REASONS,
                ]
            },
        }

    def _build_replay_execution_summary(self, replay_record: dict) -> dict:
        inner = results_block(replay_record)
        results = inner.get(PAYLOAD_KEY_RESULTS, [])
        skipped_messages = replay_skipped_messages(replay_record)
        blocked_messages = replay_blocked_messages(replay_record)
        targeted_message_ids = replay_targeted_message_ids(replay_record)
        executed_message_ids = replay_message_ids(results)
        skipped_message_ids = replay_message_ids(skipped_messages)
        blocked_message_ids = replay_message_ids(blocked_messages)

        return {
            PAYLOAD_KEY_TARGETED_MESSAGE_IDS: targeted_message_ids,
            PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: executed_message_ids,
            PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: skipped_message_ids,
            PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: blocked_message_ids,
            PAYLOAD_KEY_SKIP_REASONS: replay_grouped_reasons(skipped_messages),
            PAYLOAD_KEY_BLOCK_REASONS: replay_grouped_reasons(blocked_messages),
            PAYLOAD_KEY_EXECUTION_MODE: replay_execution_mode(
                replay_record,
                skipped_message_ids=skipped_message_ids,
            ),
        }

    def _build_execution_governance_projection(self, replay_record: dict) -> dict:
        return replay_execution_governance_projection(replay_record)

    def _build_governance_sources(self, replay_record: dict) -> dict:
        return replay_governance_sources(
            replay_record,
            summary_source_derived=self.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
            summary_source_extensions=self.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            summary_source_gate=self.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            projection_source_execution=self.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        )

"""Operations timeline aggregation.

Stores and summarizes operational events such as release gates,
deployment executions, evidence bundles, and rollback drills as a local
JSON timeline for audit and postmortem workflows.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    OPERATIONS_TIMELINE_FILE,
    PAYLOAD_KEY_ACTOR,
    PAYLOAD_KEY_CHANGED_KEYS,
    PAYLOAD_KEY_CHANGES,
    PAYLOAD_KEY_CLEARED,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVENT_ID_PREFIX,
    PAYLOAD_KEY_EVENT_TYPE,
    PAYLOAD_KEY_EVENT_TYPE_COUNTS,
    PAYLOAD_KEY_EVENTS,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EXPORTED_AT,
    PAYLOAD_KEY_FILE_COUNT,
    PAYLOAD_KEY_FIRST_EVENT_AT,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_ID,
    PAYLOAD_KEY_LABEL,
    PAYLOAD_KEY_LAST_EVENT_AT,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PATH,
    PAYLOAD_KEY_PAYLOAD,
    PAYLOAD_KEY_RECENT_EVENTS,
    PAYLOAD_KEY_RECOMMENDATION,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_RELOADED,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SOURCE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_STRATEGY,
    PAYLOAD_KEY_STRICT,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMESTAMP,
    PAYLOAD_KEY_VERIFIED,
    PAYLOAD_KEY_VERSION,
    PAYLOAD_KEY_WARNING_TOTAL,
    RELEASE_PIPELINE_DEFAULT_ACTOR,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE,
    TIMELINE_EVENT_DEPLOYMENT_EXECUTION,
    TIMELINE_EVENT_ENGINE_CONFIG,
    TIMELINE_EVENT_EVIDENCE_BUNDLE,
    TIMELINE_EVENT_RELEASE_GATE,
    TIMELINE_EVENT_ROLLBACK_DRILL,
    TIMELINE_STATUS_FAILED,
    TIMELINE_STATUS_PASSED,
    TIMELINE_STATUS_UNKNOWN,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.schema_versions import (
    SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
    SCHEMA_ENGINE_CONFIG_RELOAD_EVENT,
    SCHEMA_OPERATIONS_TIMELINE_EXPORT,
    SCHEMA_OPERATIONS_TIMELINE_SUMMARY,
)


class OperationsTimelineService:
    """Append-only local JSON operations timeline."""

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / OPERATIONS_TIMELINE_FILE

    @property
    def path(self) -> str:
        return str(self._path)

    def record(
        self, event_type: str, payload: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        events = self._load_events()
        event = {
            PAYLOAD_KEY_ID: f"{PAYLOAD_KEY_EVENT_ID_PREFIX}{len(events) + 1:06d}",
            PAYLOAD_KEY_TIMESTAMP: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_EVENT_TYPE: event_type,
            PAYLOAD_KEY_ACTOR: actor,
            PAYLOAD_KEY_STATUS: self._infer_status(payload),
            PAYLOAD_KEY_SUMMARY: self._summarize_payload(event_type, payload),
            PAYLOAD_KEY_PAYLOAD: payload,
        }
        events.append(event)
        self._write_events(events)
        return event

    def record_release_gate(
        self, report: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        return self.record(TIMELINE_EVENT_RELEASE_GATE, report, actor=actor)

    def record_deployment_execution(
        self, result: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        return self.record(TIMELINE_EVENT_DEPLOYMENT_EXECUTION, result, actor=actor)

    def record_rollback_drill(
        self, result: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        return self.record(TIMELINE_EVENT_ROLLBACK_DRILL, result, actor=actor)

    def record_evidence_bundle(
        self, result: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        return self.record(TIMELINE_EVENT_EVIDENCE_BUNDLE, result, actor=actor)

    def record_alpha_budget_governance(
        self, result: dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        return self.record(TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE, result, actor=actor)

    def list_events(self, *, event_type: str | None = None, limit: int | None = None) -> list[dict]:
        events = self._load_events()
        if event_type:
            events = [e for e in events if e.get(PAYLOAD_KEY_EVENT_TYPE) == event_type]
        events = sorted(events, key=lambda e: e.get(PAYLOAD_KEY_TIMESTAMP, ""))
        if limit is not None:
            events = events[-limit:]
        return events

    def summarize(self) -> dict:
        events = self._load_events()
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for event in events:
            by_type[event[PAYLOAD_KEY_EVENT_TYPE]] = (
                by_type.get(event[PAYLOAD_KEY_EVENT_TYPE], 0) + 1
            )
            by_status[event[PAYLOAD_KEY_STATUS]] = by_status.get(event[PAYLOAD_KEY_STATUS], 0) + 1
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_OPERATIONS_TIMELINE_SUMMARY,
            PAYLOAD_KEY_PATH: str(self._path),
            PAYLOAD_KEY_EVENT_COUNT: len(events),
            PAYLOAD_KEY_EVENT_TYPE_COUNTS: by_type,
            PAYLOAD_KEY_STATUS_COUNTS: by_status,
            PAYLOAD_KEY_FIRST_EVENT_AT: events[0][PAYLOAD_KEY_TIMESTAMP] if events else None,
            PAYLOAD_KEY_LAST_EVENT_AT: events[-1][PAYLOAD_KEY_TIMESTAMP] if events else None,
            PAYLOAD_KEY_RECENT_EVENTS: events[-5:],
        }

    def export(self, output: str) -> str:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_OPERATIONS_TIMELINE_EXPORT,
            PAYLOAD_KEY_EXPORTED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_SUMMARY: self.summarize(),
            PAYLOAD_KEY_EVENTS: self._load_events(),
        }
        atomic_write_json(target, payload)
        return str(target)

    def clear(self) -> dict:
        count = len(self._load_events())
        self._write_events([])
        return {PAYLOAD_KEY_CLEARED: count, PAYLOAD_KEY_PATH: str(self._path)}

    def _load_events(self) -> list[dict]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write_events(self, events: list[dict]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, events)

    def _infer_status(self, payload: dict) -> str:
        if PAYLOAD_KEY_DECISION in payload:
            decision = payload.get(PAYLOAD_KEY_DECISION)
            return (
                TIMELINE_STATUS_PASSED
                if decision
                in {RELEASE_PIPELINE_GATE_DECISION_ALLOW, RELEASE_PIPELINE_GATE_DECISION_WARN}
                else TIMELINE_STATUS_FAILED
            )
        if PAYLOAD_KEY_PASSED in payload:
            return (
                TIMELINE_STATUS_PASSED
                if payload.get(PAYLOAD_KEY_PASSED)
                else TIMELINE_STATUS_FAILED
            )
        if PAYLOAD_KEY_VERIFIED in payload:
            return (
                TIMELINE_STATUS_PASSED
                if payload.get(PAYLOAD_KEY_VERIFIED)
                else TIMELINE_STATUS_FAILED
            )
        if payload.get(PAYLOAD_KEY_SCHEMA_VERSION) == SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT:
            return payload.get(PAYLOAD_KEY_STATUS, TIMELINE_STATUS_UNKNOWN)
        if payload.get(PAYLOAD_KEY_SCHEMA_VERSION) == SCHEMA_ENGINE_CONFIG_RELOAD_EVENT:
            return (
                TIMELINE_STATUS_PASSED
                if not payload.get(PAYLOAD_KEY_ERROR)
                else TIMELINE_STATUS_FAILED
            )
        if PAYLOAD_KEY_GATE_DECISION in payload:
            return (
                TIMELINE_STATUS_PASSED
                if payload.get(PAYLOAD_KEY_GATE_DECISION)
                in {RELEASE_PIPELINE_GATE_DECISION_ALLOW, RELEASE_PIPELINE_GATE_DECISION_WARN}
                else TIMELINE_STATUS_FAILED
            )
        return payload.get(PAYLOAD_KEY_STATUS, TIMELINE_STATUS_UNKNOWN)

    def _summarize_payload(self, event_type: str, payload: dict) -> dict:
        if event_type == TIMELINE_EVENT_RELEASE_GATE:
            return {
                PAYLOAD_KEY_DECISION: payload.get(PAYLOAD_KEY_DECISION),
                PAYLOAD_KEY_STRICT: payload.get(PAYLOAD_KEY_STRICT),
            }
        if event_type == TIMELINE_EVENT_DEPLOYMENT_EXECUTION:
            return {
                PAYLOAD_KEY_STATUS: payload.get(PAYLOAD_KEY_STATUS),
                PAYLOAD_KEY_VERSION: payload.get(PAYLOAD_KEY_VERSION),
                PAYLOAD_KEY_STRATEGY: payload.get(PAYLOAD_KEY_STRATEGY),
            }
        if event_type == TIMELINE_EVENT_ROLLBACK_DRILL:
            return {
                PAYLOAD_KEY_STATUS: payload.get(PAYLOAD_KEY_STATUS),
                PAYLOAD_KEY_VERSION: payload.get(PAYLOAD_KEY_VERSION),
                PAYLOAD_KEY_RECOMMENDATION: payload.get(PAYLOAD_KEY_RECOMMENDATION),
            }
        if event_type == TIMELINE_EVENT_EVIDENCE_BUNDLE:
            return {
                PAYLOAD_KEY_LABEL: payload.get(PAYLOAD_KEY_LABEL),
                PAYLOAD_KEY_GATE_DECISION: payload.get(PAYLOAD_KEY_GATE_DECISION),
                PAYLOAD_KEY_FILE_COUNT: payload.get(PAYLOAD_KEY_FILE_COUNT),
            }
        if event_type == TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE:
            return {
                PAYLOAD_KEY_STATUS: payload.get(PAYLOAD_KEY_STATUS),
                PAYLOAD_KEY_SOURCE: payload.get(PAYLOAD_KEY_SOURCE),
                PAYLOAD_KEY_RECORD_COUNT: payload.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                PAYLOAD_KEY_EVIDENCE_COUNT: payload.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: payload.get(
                    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
                ),
                PAYLOAD_KEY_WARNING_TOTAL: payload.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            }
        if event_type == TIMELINE_EVENT_ENGINE_CONFIG:
            ch = payload.get(PAYLOAD_KEY_CHANGES) or {}
            return {
                PAYLOAD_KEY_RELOADED: payload.get(PAYLOAD_KEY_RELOADED, False),
                PAYLOAD_KEY_CHANGED_KEYS: list(ch.keys()) if isinstance(ch, dict) else [],
                PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE: payload.get(PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE),
            }
        return {PAYLOAD_KEY_STATUS: payload.get(PAYLOAD_KEY_STATUS)}

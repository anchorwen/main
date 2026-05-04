from types import SimpleNamespace

from apps.engine.communication_summary_contract import (
    build_summary_mirror_fields_from_operations_summary,
)
from apps.engine.main_v9_shadow import apply_stable_output_contract, build_output_extension_fields
from core.deployment.domain_keys import (
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_SUMMARY_SOURCE,
)


def _targeted_summary() -> dict:
    return {
        PAYLOAD_KEY_POSTURE: "targeted_replay",
        PAYLOAD_KEY_POSTURE_SOURCE: "summary.posture",
        PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: "summary.source",
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: "summary.execution",
        PAYLOAD_KEY_EXECUTION_MODE: "targeted",
        PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: ["message_001"],
        PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: ["message_002"],
        PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: [],
        PAYLOAD_KEY_SKIP_REASONS: {"skip_acknowledged_message": ["message_002"]},
        PAYLOAD_KEY_BLOCK_REASONS: {},
    }


def test_runtime_contract_guard_mirrors_replay_execution_fields():
    payload = build_summary_mirror_fields_from_operations_summary(
        {PAYLOAD_KEY_OPERATIONS_SUMMARY: _targeted_summary()}
    )
    assert payload[PAYLOAD_KEY_OPERATIONS_POSTURE] == "targeted_replay"
    assert payload[PAYLOAD_KEY_POSTURE_SOURCES] == {"operations_posture_source": "summary.posture"}
    assert payload[PAYLOAD_KEY_EXECUTION_MODE] == "targeted"
    assert payload[PAYLOAD_KEY_EXECUTED_MESSAGE_IDS] == ["message_001"]
    assert payload[PAYLOAD_KEY_SKIPPED_MESSAGE_IDS] == ["message_002"]
    assert payload[PAYLOAD_KEY_SKIP_REASONS] == {"skip_acknowledged_message": ["message_002"]}


def test_runtime_contract_guard_backfills_operations_summary_from_runtime_result():
    payload = {"scenario": "long_case"}
    result = SimpleNamespace(
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_POSTURE: "targeted_replay",
            PAYLOAD_KEY_POSTURE_SOURCES: {"operations_posture_source": "summary.posture"},
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: "summary.source",
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: "summary.execution",
            },
            PAYLOAD_KEY_OPERATIONS_SUMMARY: _targeted_summary(),
        }
    )
    normalized = apply_stable_output_contract(build_output_extension_fields(payload, result))
    assert normalized[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_EXECUTION_MODE] == "targeted"
    assert normalized[PAYLOAD_KEY_GOVERNANCE_SOURCES] == {
        PAYLOAD_KEY_SUMMARY_SOURCE: "summary.source",
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: "summary.execution",
    }

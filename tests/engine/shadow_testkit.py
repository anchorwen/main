from __future__ import annotations

"""Minimal testkit for V9 shadow session-stream regression tests.

This module is intended for shared helpers across V9 shadow contracts,
SSE utils, smoke, and real-input integration tests that exercise the
manager/SSE/client session stream shape.

It is not intended to be a generic engine-wide test framework, and should
not be expanded to cover runtime-loop orchestration, replay/ledger domain
flows, or unrelated communication service tests.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.engine.main_v9_shadow import ShadowSessionManager, stream_session_sse
from apps.engine.v9_shadow_sse import SessionSSEClientBuffer, iter_sse_messages_from_chunks
from core.contracts.domain_keys import (
    OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_BLOCKED_MESSAGES,
    PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH,
    PAYLOAD_KEY_COMMUNICATION_RECORD_ID,
    PAYLOAD_KEY_CONVICTION,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DISPATCH_RESULT,
    PAYLOAD_KEY_DISPATCH_STATUS,
    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_FEATURE_FILE,
    PAYLOAD_KEY_FEATURE_SOURCE_TYPE,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_POSTURE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_TAGS,
    PAYLOAD_KEY_LEDGER_PATH,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MODE,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_RECORD_ID,
    PAYLOAD_KEY_REPLAY_RECORD,
    PAYLOAD_KEY_REPLAY_TRACE,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_REVIEW_ISSUE_CODES,
    PAYLOAD_KEY_RISK_STATUS,
    PAYLOAD_KEY_SAMPLE_DESCRIPTION,
    PAYLOAD_KEY_SCENARIO,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_SKIPPED_MESSAGES,
    PAYLOAD_KEY_SUMMARY_SOURCE,
    PAYLOAD_KEY_SYMBOL,
    PAYLOAD_KEY_TARGET_ISSUE_CODES,
    REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION,
)
from core.contracts.enums import DispatchStatus
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.ledger.services.gate_decision_refs import governance_summary as gate_governance_summary
from core.ledger.services.replay_record_refs import grouped_reasons as grouped_message_reasons
from core.ledger.services.replay_record_refs import message_ids as grouped_message_ids
from core.ledger.services.replay_trace_refs import message_id as trace_message_id
from core.ledger.stream_names import (
    LEDGER_STREAM_COMMUNICATIONS,
    LEDGER_STREAM_DECISIONS,
    stream_jsonl_filename,
)

_FAKE_LEDGER_ROOT = Path("D:/cursor/data")
_FAKE_LEDGER_DATE = "2026-04-24"
_FAKE_TARGET = "exec_bridge"
_FAKE_SYMBOL = "XAUUSD"
_FAKE_BATCH_FEATURE_FILE = "D:/cursor/data/snapshots/v9_shadow_batch.json"
_FAKE_MESSAGE_ID = "message_001"
_FAKE_COMMUNICATION_RECORD_ID = "communication_record_001"
_FAKE_RECORD_ID = "record_001"

# Keep test payload keys aligned with core contract keys from domain_keys.
# Test-specific literal values (scenario names, sample descriptions, etc.) can
# remain inline when they represent fixtures rather than contracts.


def _fake_communication_ledger_path() -> Path:
    return (
        _FAKE_LEDGER_ROOT
        / _FAKE_LEDGER_DATE
        / stream_jsonl_filename(_FAKE_TARGET, LEDGER_STREAM_COMMUNICATIONS)
    )


def _fake_decision_ledger_path() -> Path:
    return (
        _FAKE_LEDGER_ROOT
        / "decisions"
        / _FAKE_LEDGER_DATE
        / stream_jsonl_filename(_FAKE_SYMBOL, LEDGER_STREAM_DECISIONS)
    )


# ---- shadow veto test sync (FIX-20260819-002) ----


def apply_shadow_veto_stub(monkeypatch) -> None:
    """Stub the Shadow Veto's live.yaml read (FIX-20260819-002 test sync).

    Shadow CLI/session-stream tests build the shadow container to verify
    output contracts with stub feature data — a legitimate stub-adapter
    scenario. The veto (bootstrap_v9.build_v9_shadow_container) reads the real
    configs/live.yaml (adapter=mt5_zmq) at build time and hard-crashes. This
    helper intercepts only that one file read so adapter resolves to "stub";
    every other file (brains / meta pipeline / model artifacts) still loads
    from the real repo — container behavior is identical to pre-veto
    (EnvironmentConfig.development defaulted adapter_name="stub").
    """
    import builtins
    import io
    import os

    from apps.engine import bootstrap_v9

    real_open = builtins.open
    live_yaml = os.fspath(bootstrap_v9._repo_root() / "configs" / "live.yaml")

    def _open(path, *args, **kwargs):
        if os.fspath(path) == live_yaml:
            return io.StringIO("adapter:\n  name: stub\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)


# ---- cli helpers ----


def run_engine_cli(main_func, *args: str) -> str:
    stdout = io.StringIO()
    with patch("sys.argv", ["main_v9_shadow.py", *args]), redirect_stdout(stdout):
        main_func()
    return stdout.getvalue()


def run_engine_cli_allow_exit(main_func, *args: str) -> tuple[str, int | None]:
    stdout = io.StringIO()
    exit_code = None
    with patch("sys.argv", ["main_v9_shadow.py", *args]), redirect_stdout(stdout):
        try:
            main_func()
        except SystemExit as exc:
            code = exc.code
            exit_code = int(code) if isinstance(code, int | float) else None
    return stdout.getvalue(), exit_code


# ---- flow collection helpers ----


def collect_session_flow_triplet(args, *, stream_plan):
    manager_events = list(ShadowSessionManager(stream_plan=stream_plan).stream_run(args))
    sse_chunks = list(stream_session_sse(args, stream_plan=stream_plan))
    sse_messages = list(iter_sse_messages_from_chunks(sse_chunks))
    client = SessionSSEClientBuffer()
    for chunk in sse_chunks:
        client.feed(chunk)
    client.finish()
    return {
        "manager_events": manager_events,
        "sse_chunks": sse_chunks,
        "sse_messages": sse_messages,
        "client": client,
        "client_state": client.state,
    }


# ---- assertion helpers ----


def _event_sequence(events: list[dict]) -> list[str]:
    return [event["event"] for event in events]


def assert_completed_flow_alignment(
    flow: dict,
    *,
    expected_events: list[str],
    final_event_name: str,
) -> None:
    assert _event_sequence(flow["manager_events"]) == expected_events
    assert _event_sequence(flow["sse_messages"]) == expected_events
    assert flow["client_state"]["status"] == "completed"
    assert flow["client_state"]["ok"] is True
    assert flow["client_state"]["final_completed"]["event"] == final_event_name


def assert_error_flow_alignment(
    flow: dict,
    *,
    expected_events: list[str],
    final_event_name: str,
    error_message: str,
) -> None:
    assert _event_sequence(flow["manager_events"]) == expected_events
    assert _event_sequence(flow["sse_messages"]) == expected_events
    assert flow["client_state"]["status"] == "error"
    assert flow["client_state"]["ok"] is False
    assert flow["client_state"]["error_message"] == error_message
    assert flow["client_state"]["final_error"]["event"] == final_event_name


def assert_client_completed_terminal_message(
    client: SessionSSEClientBuffer,
    *,
    final_event_name: str,
    payload: dict,
    latest_progress_payload: dict | None = None,
) -> None:
    assert client.state["status"] == "completed"
    assert client.state["ok"] is True
    if latest_progress_payload is not None:
        assert client.state["latest_progress"]["data"]["data"] == latest_progress_payload
    assert client.state["final_completed"]["event"] == final_event_name
    final_payload = client.state["final_completed"]["data"]["data"]
    for key, value in payload.items():
        assert final_payload[key] == value
    assert client.state["final_error"] is None


def assert_client_error_terminal_message(
    client: SessionSSEClientBuffer,
    *,
    final_event_name: str,
    error_message: str,
    error_type: str,
    latest_progress_payload: dict | None = None,
) -> None:
    assert client.state["status"] == "error"
    assert client.state["ok"] is False
    if latest_progress_payload is not None:
        assert client.state["latest_progress"]["data"]["data"] == latest_progress_payload
    assert client.state["error_message"] == error_message
    assert client.state["final_error"]["event"] == final_event_name
    assert client.state["final_error"]["data"]["data"]["error_type"] == error_type


def assert_manager_sse_completed_terminal_payloads(
    manager_completed: dict,
    sse_completed: dict,
    *,
    final_event_name: str,
    results,
    meta: dict,
    stats: dict | None = None,
) -> None:
    assert manager_completed["event"] == sse_completed["event"] == final_event_name
    assert manager_completed["data"]["event"] == sse_completed["data"]["event"] == final_event_name
    assert manager_completed["data"]["results"] == results
    sse_payload = sse_completed["data"]["data"]
    for key, value in meta.items():
        if key == "generated_at":
            assert key in sse_payload["meta"]
            continue
        assert sse_payload["meta"][key] == value
    sse_results = sse_payload["results"]
    if isinstance(results, list):
        assert len(sse_results) == len(results)
    assert manager_completed["data"]["meta"] == meta
    if stats is None:
        assert "stats" not in manager_completed["data"]
        assert "stats" not in sse_payload
    else:
        assert manager_completed["data"]["stats"] == stats
        assert sse_payload["stats"] == stats


def assert_manager_sse_error_terminal_payloads(
    manager_error: dict,
    sse_error: dict,
    *,
    final_event_name: str,
    error_payload: dict,
) -> None:
    assert manager_error["event"] == sse_error["event"] == final_event_name
    assert manager_error["data"] == error_payload
    assert sse_error["data"]["event"] == final_event_name
    assert sse_error["data"]["data"] == error_payload


# ---- payload builders ----


def build_fallback_operations_summary() -> dict:
    return {
        PAYLOAD_KEY_DISPATCH_STATUS: DispatchStatus.PROTOCOL_VALIDATED,
        PAYLOAD_KEY_MESSAGE_ID: _FAKE_MESSAGE_ID,
        PAYLOAD_KEY_COMMUNICATION_RECORD_ID: _FAKE_COMMUNICATION_RECORD_ID,
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: str(_fake_communication_ledger_path()),
        PAYLOAD_KEY_POSTURE: "action_required",
        PAYLOAD_KEY_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
        PAYLOAD_KEY_GOVERNANCE_DECISION: "allow",
        PAYLOAD_KEY_GOVERNANCE_POSTURE: "auto_replay",
        PAYLOAD_KEY_RECOMMENDED_STRATEGY: "direct_replay_candidate",
        PAYLOAD_KEY_TARGET_ISSUE_CODES: ["dispatch_pending"],
        PAYLOAD_KEY_REVIEW_ISSUE_CODES: [],
        PAYLOAD_KEY_GOVERNANCE_TAGS: ["auto_replay_eligible"],
        PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
        PAYLOAD_KEY_EXECUTION_MODE: "targeted",
        PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: [_FAKE_MESSAGE_ID],
        PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: ["message_002"],
        PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: [],
        PAYLOAD_KEY_SKIP_REASONS: {"skip_acknowledged_message": ["message_002"]},
        PAYLOAD_KEY_BLOCK_REASONS: {},
    }


def assert_runtime_summary_matches_governance_contract(
    summary: dict,
    *,
    posture: str,
    governance_decision,
    governance_posture: str,
    recommended_strategy: str,
    target_issue_codes: list[str],
    review_issue_codes: list[str],
    governance_tags: list[str],
    execution_projection_source: str | None,
    execution_mode: str | None,
    executed_message_ids: list[str],
    skipped_message_ids: list[str],
    blocked_message_ids: list[str],
    skip_reasons: dict,
    block_reasons: dict,
) -> None:
    assert summary[PAYLOAD_KEY_POSTURE] == posture
    assert summary[PAYLOAD_KEY_GOVERNANCE_DECISION] == governance_decision
    assert summary[PAYLOAD_KEY_GOVERNANCE_POSTURE] == governance_posture
    assert summary[PAYLOAD_KEY_RECOMMENDED_STRATEGY] == recommended_strategy
    assert summary[PAYLOAD_KEY_TARGET_ISSUE_CODES] == target_issue_codes
    assert summary[PAYLOAD_KEY_REVIEW_ISSUE_CODES] == review_issue_codes
    assert summary[PAYLOAD_KEY_GOVERNANCE_TAGS] == governance_tags
    assert summary[PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE] == execution_projection_source
    assert summary[PAYLOAD_KEY_EXECUTION_MODE] == execution_mode
    assert summary[PAYLOAD_KEY_EXECUTED_MESSAGE_IDS] == executed_message_ids
    assert summary[PAYLOAD_KEY_SKIPPED_MESSAGE_IDS] == skipped_message_ids
    assert summary[PAYLOAD_KEY_BLOCKED_MESSAGE_IDS] == blocked_message_ids
    assert summary[PAYLOAD_KEY_SKIP_REASONS] == skip_reasons
    assert summary[PAYLOAD_KEY_BLOCK_REASONS] == block_reasons


def build_runtime_summary_from_execution_result(execution_result: dict) -> dict:
    gate_decision = execution_result.get(PAYLOAD_KEY_GATE_DECISION, {})
    governance_summary = gate_governance_summary(gate_decision) or execution_result.get(
        PAYLOAD_KEY_GOVERNANCE_SUMMARY, {}
    )
    skipped_messages = execution_result.get(PAYLOAD_KEY_SKIPPED_MESSAGES, [])
    blocked_messages = execution_result.get(PAYLOAD_KEY_BLOCKED_MESSAGES, [])
    results = execution_result.get(PAYLOAD_KEY_RESULTS, [])
    replay_trace = execution_result.get(PAYLOAD_KEY_REPLAY_TRACE, {})
    replay_record = execution_result.get(PAYLOAD_KEY_REPLAY_RECORD)

    executed_message_ids = grouped_message_ids(results)
    skipped_message_ids = grouped_message_ids(skipped_messages)
    blocked_message_ids = grouped_message_ids(blocked_messages)
    skip_reasons = execution_result.get(PAYLOAD_KEY_SKIP_REASONS)
    if not isinstance(skip_reasons, dict):
        skip_reasons = grouped_message_reasons(skipped_messages)
    block_reasons = execution_result.get(PAYLOAD_KEY_BLOCK_REASONS)
    if not isinstance(block_reasons, dict):
        block_reasons = grouped_message_reasons(blocked_messages)

    dispatch_result = execution_result.get(PAYLOAD_KEY_DISPATCH_RESULT)
    dispatch_status = None if dispatch_result is None else dispatch_result.status

    posture = governance_summary.get(PAYLOAD_KEY_POSTURE)
    if posture == "review_required":
        posture = "blocked"

    execution_projection_source = None
    execution_mode = None
    if replay_record is not None:
        execution_projection_source = REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION
        execution_mode = replay_record.execution.get(PAYLOAD_KEY_EXECUTION_MODE)

    governance_tags = governance_summary.get(PAYLOAD_KEY_GOVERNANCE_TAGS)
    if governance_tags is None:
        governance_tags = governance_summary.get("tags", [])

    return {
        PAYLOAD_KEY_DISPATCH_STATUS: dispatch_status,
        PAYLOAD_KEY_MESSAGE_ID: trace_message_id(replay_trace)
        or (executed_message_ids[0] if executed_message_ids else None),
        PAYLOAD_KEY_COMMUNICATION_RECORD_ID: _FAKE_COMMUNICATION_RECORD_ID,
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: str(_fake_communication_ledger_path()),
        PAYLOAD_KEY_POSTURE: posture,
        PAYLOAD_KEY_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
        PAYLOAD_KEY_GOVERNANCE_DECISION: governance_summary.get(PAYLOAD_KEY_DECISION),
        PAYLOAD_KEY_GOVERNANCE_POSTURE: governance_summary.get(PAYLOAD_KEY_POSTURE),
        PAYLOAD_KEY_RECOMMENDED_STRATEGY: governance_summary.get(PAYLOAD_KEY_RECOMMENDED_STRATEGY),
        PAYLOAD_KEY_TARGET_ISSUE_CODES: governance_summary.get(PAYLOAD_KEY_TARGET_ISSUE_CODES, []),
        PAYLOAD_KEY_REVIEW_ISSUE_CODES: governance_summary.get(PAYLOAD_KEY_REVIEW_ISSUE_CODES, []),
        PAYLOAD_KEY_GOVERNANCE_TAGS: governance_tags,
        PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
        PAYLOAD_KEY_EXECUTION_MODE: execution_mode,
        PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: executed_message_ids,
        PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: skipped_message_ids,
        PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: blocked_message_ids,
        PAYLOAD_KEY_SKIP_REASONS: skip_reasons,
        PAYLOAD_KEY_BLOCK_REASONS: block_reasons,
    }


def build_fallback_expected_operations_summary(*, communication_ledger_path) -> dict:
    return {
        **build_fallback_operations_summary(),
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: communication_ledger_path,
    }


def build_blocked_operations_summary() -> dict:
    summary = build_fallback_operations_summary()
    summary.update(
        {
            PAYLOAD_KEY_POSTURE: "blocked",
            PAYLOAD_KEY_GOVERNANCE_DECISION: "deny",
            PAYLOAD_KEY_GOVERNANCE_POSTURE: "blocked",
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: "replay_with_governance_review",
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: ["stale_receipt"],
            PAYLOAD_KEY_GOVERNANCE_TAGS: ["requires_governance_review"],
            PAYLOAD_KEY_EXECUTION_MODE: "blocked",
            PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: [],
            PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: [],
            PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: ["message_001"],
            PAYLOAD_KEY_SKIP_REASONS: {},
            PAYLOAD_KEY_BLOCK_REASONS: {"block_review_required": ["message_001"]},
        }
    )
    return summary


def build_terminal_message_receipt_operations_summary() -> dict:
    summary = build_fallback_operations_summary()
    summary.update(
        {
            PAYLOAD_KEY_POSTURE: "healthy",
            PAYLOAD_KEY_GOVERNANCE_DECISION: "deny",
            PAYLOAD_KEY_GOVERNANCE_POSTURE: "blocked",
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: "do_not_replay_terminal_receipt",
            PAYLOAD_KEY_TARGET_ISSUE_CODES: ["receipt_filled"],
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: [],
            PAYLOAD_KEY_GOVERNANCE_TAGS: ["replay_not_required", "terminal_receipt"],
            PAYLOAD_KEY_EXECUTION_MODE: "blocked",
            PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: [],
            PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: [],
            PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: ["message_001"],
            PAYLOAD_KEY_SKIP_REASONS: {},
            PAYLOAD_KEY_BLOCK_REASONS: {"block_terminal_receipt": ["message_001"]},
        }
    )
    return summary


def build_terminal_partially_filled_message_receipt_operations_summary() -> dict:
    summary = build_fallback_operations_summary()
    summary.update(
        {
            PAYLOAD_KEY_POSTURE: "healthy",
            PAYLOAD_KEY_GOVERNANCE_DECISION: "deny",
            PAYLOAD_KEY_GOVERNANCE_POSTURE: "blocked",
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: "do_not_replay_terminal_receipt",
            PAYLOAD_KEY_TARGET_ISSUE_CODES: ["receipt_partially_filled"],
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: ["receipt_partially_filled"],
            PAYLOAD_KEY_GOVERNANCE_TAGS: ["replay_not_required", "terminal_receipt"],
            PAYLOAD_KEY_EXECUTION_MODE: "blocked",
            PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: [],
            PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: [],
            PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: ["message_partial"],
            PAYLOAD_KEY_SKIP_REASONS: {},
            PAYLOAD_KEY_BLOCK_REASONS: {"block_terminal_receipt": ["message_partial"]},
        }
    )
    return summary


def build_terminal_correlation_mixed_operations_summary() -> dict:
    summary = build_fallback_operations_summary()
    summary.update(
        {
            PAYLOAD_KEY_POSTURE: "action_required",
            PAYLOAD_KEY_GOVERNANCE_DECISION: "deny",
            PAYLOAD_KEY_GOVERNANCE_POSTURE: "blocked",
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: "do_not_replay_terminal_receipts",
            PAYLOAD_KEY_TARGET_ISSUE_CODES: ["receipt_timeout"],
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: [],
            PAYLOAD_KEY_GOVERNANCE_TAGS: ["replay_not_required", "terminal_receipt"],
            PAYLOAD_KEY_EXECUTION_MODE: "blocked",
            PAYLOAD_KEY_EXECUTED_MESSAGE_IDS: [],
            PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: ["message_accepted", "message_acked"],
            PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: ["message_timeout"],
            PAYLOAD_KEY_SKIP_REASONS: {
                "skip_not_targeted": ["message_accepted"],
                "skip_acknowledged_message": ["message_acked"],
            },
            PAYLOAD_KEY_BLOCK_REASONS: {"block_terminal_receipt": ["message_timeout"]},
        }
    )
    return summary


def build_blocked_expected_operations_summary(*, communication_ledger_path) -> dict:
    return {
        **build_blocked_operations_summary(),
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: communication_ledger_path,
    }


def build_terminal_message_receipt_expected_operations_summary(
    *, communication_ledger_path
) -> dict:
    return {
        **build_terminal_message_receipt_operations_summary(),
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: communication_ledger_path,
    }


def build_terminal_partially_filled_message_receipt_expected_operations_summary(
    *, communication_ledger_path
) -> dict:
    return {
        **build_terminal_partially_filled_message_receipt_operations_summary(),
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: communication_ledger_path,
    }


def build_terminal_correlation_mixed_expected_operations_summary(
    *, communication_ledger_path
) -> dict:
    return {
        **build_terminal_correlation_mixed_operations_summary(),
        PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: communication_ledger_path,
    }


def build_terminal_receipt_operations_summary() -> dict:
    return build_terminal_message_receipt_operations_summary()


def build_fallback_cli_contract() -> dict:
    return {
        PAYLOAD_KEY_OPERATIONS_SUMMARY: build_fallback_operations_summary(),
        PAYLOAD_KEY_OPERATIONS_POSTURE: "action_required",
        PAYLOAD_KEY_POSTURE_SOURCES: {
            PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
        },
        PAYLOAD_KEY_GOVERNANCE_SOURCES: {
            PAYLOAD_KEY_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
        },
    }


def build_fallback_manager_payload() -> dict:
    return {
        PAYLOAD_KEY_SCENARIO: "long_case",
        PAYLOAD_KEY_FEATURE_SOURCE_TYPE: "batch_file",
        PAYLOAD_KEY_FEATURE_FILE: _FAKE_BATCH_FEATURE_FILE,
        PAYLOAD_KEY_SAMPLE_DESCRIPTION: "Lower H1_Hurst to push the model into an open long decision.",
        "manifest": None,
        PAYLOAD_KEY_SYMBOL: "XAUUSD",
        PAYLOAD_KEY_MODE: "normal",
        "action": "open",
        "side": "long",
        PAYLOAD_KEY_CONVICTION: 0.88,
        PAYLOAD_KEY_RISK_STATUS: "allow",
        PAYLOAD_KEY_DISPATCH_STATUS: "protocol_validated",
        PAYLOAD_KEY_OPERATIONS_SUMMARY: build_fallback_operations_summary(),
        "brain_count": 1,
        PAYLOAD_KEY_LEDGER_PATH: str(_fake_decision_ledger_path()),
        PAYLOAD_KEY_RECORD_ID: _FAKE_RECORD_ID,
    }


def build_terminal_message_receipt_manager_payload() -> dict:
    payload = build_fallback_manager_payload()
    payload[PAYLOAD_KEY_OPERATIONS_SUMMARY] = build_terminal_message_receipt_operations_summary()
    return payload


def build_terminal_partially_filled_message_receipt_manager_payload() -> dict:
    payload = build_fallback_manager_payload()
    payload[PAYLOAD_KEY_OPERATIONS_SUMMARY] = (
        build_terminal_partially_filled_message_receipt_operations_summary()
    )
    return payload


def build_terminal_correlation_mixed_manager_payload() -> dict:
    payload = build_fallback_manager_payload()
    payload[PAYLOAD_KEY_OPERATIONS_SUMMARY] = build_terminal_correlation_mixed_operations_summary()
    return payload


def build_terminal_receipt_manager_payload() -> dict:
    return build_terminal_message_receipt_manager_payload()


def build_blocked_manager_payload() -> dict:
    payload = build_fallback_manager_payload()
    payload[PAYLOAD_KEY_OPERATIONS_SUMMARY] = build_blocked_operations_summary()
    return payload


def build_fallback_manager_result():
    return SimpleNamespace(communication_operations=build_fallback_cli_contract())


def build_terminal_message_receipt_manager_result():
    return SimpleNamespace(
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_SUMMARY: build_terminal_message_receipt_operations_summary(),
            PAYLOAD_KEY_OPERATIONS_POSTURE: "healthy",
            PAYLOAD_KEY_POSTURE_SOURCES: {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
            },
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
            },
        }
    )


def build_terminal_partially_filled_message_receipt_manager_result():
    return SimpleNamespace(
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_SUMMARY: build_terminal_partially_filled_message_receipt_operations_summary(),
            PAYLOAD_KEY_OPERATIONS_POSTURE: "healthy",
            PAYLOAD_KEY_POSTURE_SOURCES: {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
            },
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
            },
        }
    )


def build_terminal_correlation_mixed_manager_result():
    return SimpleNamespace(
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_SUMMARY: build_terminal_correlation_mixed_operations_summary(),
            PAYLOAD_KEY_OPERATIONS_POSTURE: "action_required",
            PAYLOAD_KEY_POSTURE_SOURCES: {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
            },
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
            },
        }
    )


def build_terminal_receipt_manager_result():
    return build_terminal_message_receipt_manager_result()


def build_blocked_manager_result():
    return SimpleNamespace(
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_SUMMARY: build_blocked_operations_summary(),
            PAYLOAD_KEY_OPERATIONS_POSTURE: "blocked",
            PAYLOAD_KEY_POSTURE_SOURCES: {
                PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: OPERATIONS_POSTURE_SOURCE_TRACE_DELIVERY_STATE,
            },
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: None,
            },
        }
    )


def build_fallback_session_payload() -> dict:
    return {
        PAYLOAD_KEY_SCENARIO: "long",
        PAYLOAD_KEY_FEATURE_SOURCE_TYPE: "scenario",
        PAYLOAD_KEY_FEATURE_FILE: None,
        PAYLOAD_KEY_SAMPLE_DESCRIPTION: None,
        "manifest": None,
        PAYLOAD_KEY_SYMBOL: "XAUUSD",
        PAYLOAD_KEY_MODE: "normal",
        "action": "open",
        "side": "long",
        PAYLOAD_KEY_CONVICTION: 0.88,
        PAYLOAD_KEY_RISK_STATUS: "allow",
        PAYLOAD_KEY_DISPATCH_STATUS: DispatchStatus.PROTOCOL_VALIDATED,
        PAYLOAD_KEY_OPERATIONS_SUMMARY: build_fallback_operations_summary(),
        "brain_count": 1,
        PAYLOAD_KEY_LEDGER_PATH: str(_fake_decision_ledger_path()),
        PAYLOAD_KEY_RECORD_ID: _FAKE_RECORD_ID,
    }


def build_fallback_summary_result():
    return SimpleNamespace(
        verdict=SimpleNamespace(mode="normal", status=SimpleNamespace(value="allow")),
        intent=SimpleNamespace(
            symbol="XAUUSD",
            action=SimpleNamespace(value="open"),
            side=SimpleNamespace(value="long"),
            conviction=0.88,
        ),
        dispatch_result={"status": DispatchStatus.PROTOCOL_VALIDATED},
        communication_operations={
            PAYLOAD_KEY_OPERATIONS_SUMMARY: build_fallback_operations_summary(),
        },
        communication_record=SimpleNamespace(
            message_id=_FAKE_MESSAGE_ID,
            record_id=_FAKE_COMMUNICATION_RECORD_ID,
        ),
        communication_ledger_path=_fake_communication_ledger_path(),
        proposals=[object()],
        ledger_path=Path(str(_fake_decision_ledger_path())),
        record=SimpleNamespace(record_id=_FAKE_RECORD_ID),
    )


def build_batch_session_payload(
    scenario: str,
    *,
    action: str,
    side: str,
    conviction: float,
    risk_status: str,
    dispatch_status,
    manifest: dict | None = None,
) -> dict:
    return {
        PAYLOAD_KEY_SCENARIO: scenario,
        PAYLOAD_KEY_FEATURE_SOURCE_TYPE: "batch_file",
        PAYLOAD_KEY_FEATURE_FILE: None,
        PAYLOAD_KEY_SAMPLE_DESCRIPTION: f"description for {scenario}",
        "manifest": manifest,
        PAYLOAD_KEY_SYMBOL: "XAUUSD",
        PAYLOAD_KEY_MODE: "normal",
        "action": action,
        "side": side,
        PAYLOAD_KEY_CONVICTION: conviction,
        PAYLOAD_KEY_RISK_STATUS: risk_status,
        PAYLOAD_KEY_DISPATCH_STATUS: dispatch_status,
        PAYLOAD_KEY_OPERATIONS_SUMMARY: None,
        "brain_count": 1,
        PAYLOAD_KEY_LEDGER_PATH: str(_fake_decision_ledger_path()),
        PAYLOAD_KEY_RECORD_ID: f"record_{scenario}",
    }

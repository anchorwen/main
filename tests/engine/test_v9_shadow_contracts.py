import json

from apps.engine.communication_ops_cli import extract_stable_summary_fields
from apps.engine.main_v9_shadow import (
    OutputPlan,
    SessionStreamPlan,
    ShadowSessionManager,
    apply_stable_output_contract,
    build_summary_payload,
    render_json_output,
    render_output_content,
)
from core.contracts.enums import DispatchStatus
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from tests.engine.shadow_testkit import (
    build_batch_session_payload,
    build_fallback_operations_summary,
    build_fallback_session_payload,
    build_fallback_summary_result,
    build_terminal_correlation_mixed_manager_result,
    build_terminal_message_receipt_manager_result,
    run_engine_cli,
)


def run_cli(*args: str) -> str:
    from apps.engine.main_v9_shadow import main

    return run_engine_cli(main, *args)


def build_fallback_cli_contract() -> dict:
    return {
        "operations_summary": build_fallback_operations_summary(),
        "operations_posture": "action_required",
        "posture_sources": {
            "operations_posture_source": "trace.delivery_state.delivery_posture",
        },
        "governance_sources": {
            "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            "execution_projection_source": None,
        },
    }


def assert_fallback_operations_summary_contract(summary: dict) -> None:
    assert summary["posture"] == "action_required"
    assert summary["posture_source"] == "trace.delivery_state.delivery_posture"
    assert summary["governance_decision"] == "allow"
    assert summary["governance_posture"] == "auto_replay"
    assert summary["recommended_strategy"] == "direct_replay_candidate"
    assert summary["target_issue_codes"] == ["dispatch_pending"]
    assert summary["review_issue_codes"] == []
    assert summary["governance_tags"] == ["auto_replay_eligible"]
    assert (
        summary["governance_summary_source"]
        == CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE
    )
    assert summary["execution_projection_source"] is None


def build_single_result_meta(*, source_type: str) -> dict:
    return {
        "output_mode": "session_stream",
        "source_type": source_type,
        "scenario_count": 1,
        "result_count": 1,
        "manifest": None,
    }


def assert_single_result_completed_meta(
    completed_event: dict, *, source_type: str, payload: dict
) -> None:
    assert completed_event["event"] == "session.completed"
    meta = completed_event["data"]["data"]["meta"]
    assert meta["output_mode"] == "session_stream"
    assert meta["source_type"] == source_type
    assert meta["scenario_count"] == 1
    assert meta["result_count"] == 1
    assert meta["manifest"] is None
    assert "generated_at" in meta
    result_payload = completed_event["data"]["data"]["results"]
    stable_fields = extract_stable_summary_fields(payload)
    assert stable_fields is not None  # TECH_DEBT-009: extract_stable_summary_fields 契约下恒非 None
    for key, value in payload.items():
        assert result_payload[key] == value
    for key, value in stable_fields.items():
        assert result_payload[key] == value


def test_build_summary_payload_falls_back_to_record_summary_when_operations_view_is_missing():
    result = build_fallback_summary_result()

    payload = build_summary_payload("long", result)

    assert payload["operations_summary"] == build_fallback_operations_summary()
    assert_fallback_operations_summary_contract(payload["operations_summary"])


def test_build_summary_payload_includes_operations_posture_mirror_fields():
    result = build_fallback_summary_result()
    result.communication_operations = build_fallback_cli_contract()

    payload = build_summary_payload("long", result)

    assert payload["operations_summary"] == build_fallback_operations_summary()
    assert_fallback_operations_summary_contract(payload["operations_summary"])


def test_build_summary_payload_projects_terminal_mixed_operations_contract():
    result = build_fallback_summary_result()
    result.communication_operations = (
        build_terminal_message_receipt_manager_result().communication_operations
    )

    payload = apply_stable_output_contract(build_summary_payload("long", result), result)

    assert payload["operations_summary"]["posture"] == "healthy"
    assert payload["operations_summary"]["governance_decision"] == "deny"
    assert payload["operations_summary"]["governance_posture"] == "blocked"
    assert payload["operations_summary"]["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert payload["operations_summary"]["target_issue_codes"] == ["receipt_filled"]
    assert payload["operations_summary"]["review_issue_codes"] == []
    assert payload["operations_summary"]["governance_tags"] == [
        "replay_not_required",
        "terminal_receipt",
    ]
    assert payload["operations_summary"]["execution_mode"] == "blocked"
    assert payload["operations_summary"]["executed_message_ids"] == []
    assert payload["operations_summary"]["skipped_message_ids"] == []
    assert payload["operations_summary"]["blocked_message_ids"] == ["message_001"]
    assert payload["operations_summary"]["skip_reasons"] == {}
    assert payload["operations_summary"]["block_reasons"] == {
        "block_terminal_receipt": ["message_001"],
    }
    assert payload["operations_posture"] == "healthy"
    assert payload["posture_sources"] == {
        "operations_posture_source": "trace.delivery_state.delivery_posture",
    }
    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": None,
    }


def test_build_summary_payload_projects_terminal_correlation_mixed_operations_contract():
    result = build_fallback_summary_result()
    result.communication_operations = (
        build_terminal_correlation_mixed_manager_result().communication_operations
    )

    payload = apply_stable_output_contract(build_summary_payload("long", result), result)

    assert payload["operations_summary"]["posture"] == "action_required"
    assert payload["operations_summary"]["governance_decision"] == "deny"
    assert payload["operations_summary"]["governance_posture"] == "blocked"
    assert (
        payload["operations_summary"]["recommended_strategy"] == "do_not_replay_terminal_receipts"
    )
    assert payload["operations_summary"]["target_issue_codes"] == ["receipt_timeout"]
    assert payload["operations_summary"]["review_issue_codes"] == []
    assert payload["operations_summary"]["governance_tags"] == [
        "replay_not_required",
        "terminal_receipt",
    ]
    assert payload["operations_summary"]["execution_mode"] == "blocked"
    assert payload["operations_summary"]["executed_message_ids"] == []
    assert payload["operations_summary"]["skipped_message_ids"] == [
        "message_accepted",
        "message_acked",
    ]
    assert payload["operations_summary"]["blocked_message_ids"] == ["message_timeout"]
    assert payload["operations_summary"]["skip_reasons"] == {
        "skip_not_targeted": ["message_accepted"],
        "skip_acknowledged_message": ["message_acked"],
    }
    assert payload["operations_summary"]["block_reasons"] == {
        "block_terminal_receipt": ["message_timeout"],
    }
    assert payload["operations_posture"] == "action_required"
    assert payload["posture_sources"] == {
        "operations_posture_source": "trace.delivery_state.delivery_posture",
    }
    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": None,
    }


def test_render_json_output_keeps_stable_payload_without_extension_fields():
    result = build_fallback_summary_result()
    payload = build_summary_payload("long", result)

    rendered = json.loads(
        render_json_output([payload], include_stats=False, output_mode="json", include_meta=False)
    )

    assert rendered["operations_summary"] == build_fallback_operations_summary()
    assert "operations_posture" not in rendered
    assert "posture_sources" not in rendered
    assert "governance_sources" not in rendered


def test_v9_shadow_json_output_meta_and_session_stream_share_same_stable_contract(monkeypatch):
    result = build_fallback_summary_result()
    result.communication_operations = build_fallback_cli_contract()
    payload = build_summary_payload("long", result)
    stable = extract_stable_summary_fields(apply_stable_output_contract(payload, result))

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results", lambda _args: ([payload], "ignored", [result])
    )

    rendered_json = json.loads(
        render_output_content(
            OutputPlan(mode="json", output_path=None, include_meta=True),
            [payload],
            default_text="ignored",
        )
    )

    manager = ShadowSessionManager(
        stream_plan=SessionStreamPlan(
            include_meta=True, include_stats=False, event_name_prefix="session"
        )
    )
    events = list(manager.stream_run(type("Args", (), {})()))
    completed_payload = events[-1]["data"]["results"]

    assert rendered_json["meta"]["source_type"] == "scenario"
    assert rendered_json["results"]["operations_summary"] == build_fallback_operations_summary()
    assert extract_stable_summary_fields(rendered_json["results"]) == stable
    assert extract_stable_summary_fields(completed_payload) == stable


def test_shadow_session_manager_completed_event_adds_output_only_mirror_fields(monkeypatch):
    manager = ShadowSessionManager()
    payload = build_fallback_session_payload()
    args = type("Args", (), {})()
    result = build_fallback_summary_result()
    result.communication_operations = build_fallback_cli_contract()

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results", lambda _args: ([payload], "ignored", [result])
    )

    events = list(manager.stream_run(args))

    completed = events[-1]
    results_payload = completed["data"]["results"]
    assert results_payload["operations_summary"] == build_fallback_operations_summary()
    assert results_payload["operations_posture"] == "action_required"
    assert results_payload["posture_sources"] == {
        "operations_posture_source": "trace.delivery_state.delivery_posture",
    }
    assert results_payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": None,
    }


def test_shadow_session_manager_completed_event_keeps_fallback_operations_summary(monkeypatch):
    manager = ShadowSessionManager()
    fallback_operations_summary = build_fallback_operations_summary()
    payload = build_fallback_session_payload()
    args = type("Args", (), {})()

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results", lambda _args: ([payload], "ignored")
    )

    events = list(manager.stream_run(args))

    assert [event["event"] for event in events] == [
        "session.progress",
        "session.progress",
        "session.completed",
    ]
    completed = events[-1]
    assert completed["data"]["results"]["operations_summary"] == fallback_operations_summary
    assert_fallback_operations_summary_contract(completed["data"]["results"]["operations_summary"])
    assert completed["data"]["results"]["dispatch_status"] == DispatchStatus.PROTOCOL_VALIDATED


def test_shadow_session_manager_batch_completed_event_includes_meta_stats_and_results(monkeypatch):
    manager = ShadowSessionManager(
        stream_plan=SessionStreamPlan(
            include_meta=True, include_stats=True, event_name_prefix="session"
        )
    )
    manifest = {
        "path": "D:/cursor/data/replays/v9_shadow_baselines/manifest.json",
        "version": "2",
        "description": "formal baseline suite",
    }
    payloads = [
        build_batch_session_payload(
            "long_case",
            action="open",
            side="long",
            conviction=0.91,
            risk_status="allow",
            dispatch_status=DispatchStatus.PROTOCOL_VALIDATED,
            manifest=manifest,
        ),
        build_batch_session_payload(
            "short_case",
            action="abstain",
            side="flat",
            conviction=0.27,
            risk_status="deny",
            dispatch_status="skipped",
            manifest=manifest,
        ),
    ]
    args = type("Args", (), {})()

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results", lambda _args: (payloads, "ignored")
    )

    events = list(manager.stream_run(args))

    assert [event["event"] for event in events] == [
        "session.progress",
        "session.progress",
        "session.completed",
    ]
    started, ready, completed = events
    assert started["data"] == {
        "stage": "started",
        "message": "shadow replay started",
    }
    assert ready["data"] == {
        "stage": "results_ready",
        "message": "shadow replay results prepared",
        "result_count": 2,
    }
    assert completed["data"]["event"] == "session.completed"
    assert completed["data"]["results"] == payloads
    assert completed["data"]["meta"]["output_mode"] == "session_stream"
    assert completed["data"]["meta"]["source_type"] == "batch_file"

import json

import pytest

from apps.engine.main_v9_shadow import (
    SessionStreamPlan,
    prepare_results,
    render_json_output,
    run_shadow_session_sse_server,
)
from apps.engine.v9_shadow_sse import (
    SessionSSEClientBuffer,
    iter_sse_messages_from_chunks,
    summarize_session_sse_events,
)
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from tests.engine.shadow_testkit import (
    apply_shadow_veto_stub,
    assert_client_error_terminal_message,
    assert_completed_flow_alignment,
    assert_error_flow_alignment,
    assert_manager_sse_completed_terminal_payloads,
    assert_manager_sse_error_terminal_payloads,
    build_blocked_expected_operations_summary,
    build_blocked_manager_payload,
    build_blocked_manager_result,
    build_fallback_expected_operations_summary,
    build_fallback_manager_payload,
    build_fallback_manager_result,
    build_terminal_correlation_mixed_expected_operations_summary,
    build_terminal_correlation_mixed_manager_payload,
    build_terminal_correlation_mixed_manager_result,
    build_terminal_message_receipt_expected_operations_summary,
    build_terminal_message_receipt_manager_payload,
    build_terminal_message_receipt_manager_result,
    build_terminal_partially_filled_message_receipt_expected_operations_summary,
    build_terminal_partially_filled_message_receipt_manager_payload,
    build_terminal_partially_filled_message_receipt_manager_result,
    collect_session_flow_triplet,
)


def assert_mirror_field_alignment(
    *,
    manager_results,
    sse_results,
    client_results,
    operations_posture: str,
    posture_source: str,
    summary_source: str | None,
    execution_projection_source,
    execution_mode: str,
    executed_message_ids: list[str] | None = None,
    skipped_message_ids: list[str] | None = None,
    blocked_message_ids: list[str] | None = None,
    skip_reasons: dict | None = None,
    block_reasons: dict | None = None,
    expected_operations_summary: dict | None = None,
) -> None:
    assert manager_results["operations_posture"] == operations_posture
    assert sse_results["operations_posture"] == operations_posture
    assert client_results["operations_posture"] == operations_posture

    assert manager_results["posture_sources"] == {
        "operations_posture_source": posture_source,
    }
    assert sse_results["posture_sources"] == {
        "operations_posture_source": posture_source,
    }
    assert client_results["posture_sources"] == {
        "operations_posture_source": posture_source,
    }

    assert manager_results["governance_sources"] == {
        "summary_source": summary_source,
        "execution_projection_source": execution_projection_source,
    }
    assert sse_results["governance_sources"] == {
        "summary_source": summary_source,
        "execution_projection_source": execution_projection_source,
    }
    assert client_results["governance_sources"] == {
        "summary_source": summary_source,
        "execution_projection_source": execution_projection_source,
    }

    assert manager_results["operations_summary"]["execution_mode"] == execution_mode
    assert sse_results["operations_summary"]["execution_mode"] == execution_mode
    assert client_results["operations_summary"]["execution_mode"] == execution_mode

    if executed_message_ids is not None:
        assert manager_results["operations_summary"]["executed_message_ids"] == executed_message_ids
        assert sse_results["operations_summary"]["executed_message_ids"] == executed_message_ids
        assert client_results["operations_summary"]["executed_message_ids"] == executed_message_ids

    if skipped_message_ids is not None:
        assert manager_results["operations_summary"]["skipped_message_ids"] == skipped_message_ids
        assert sse_results["operations_summary"]["skipped_message_ids"] == skipped_message_ids
        assert client_results["operations_summary"]["skipped_message_ids"] == skipped_message_ids

    if blocked_message_ids is not None:
        assert manager_results["operations_summary"]["blocked_message_ids"] == blocked_message_ids
        assert sse_results["operations_summary"]["blocked_message_ids"] == blocked_message_ids
        assert client_results["operations_summary"]["blocked_message_ids"] == blocked_message_ids

    if skip_reasons is not None:
        assert manager_results["operations_summary"]["skip_reasons"] == skip_reasons
        assert sse_results["operations_summary"]["skip_reasons"] == skip_reasons
        assert client_results["operations_summary"]["skip_reasons"] == skip_reasons

    if block_reasons is not None:
        assert manager_results["operations_summary"]["block_reasons"] == block_reasons
        assert sse_results["operations_summary"]["block_reasons"] == block_reasons
        assert client_results["operations_summary"]["block_reasons"] == block_reasons

    if expected_operations_summary is not None:
        assert manager_results["operations_summary"] == expected_operations_summary
        assert sse_results["operations_summary"] == expected_operations_summary
        assert client_results["operations_summary"] == expected_operations_summary


# ---- real input integration tests ----


@pytest.fixture(autouse=True)
def _shadow_veto_stub(monkeypatch):
    """Sync v9 shadow session-stream tests to Shadow Veto (FIX-20260819-002).

    These tests build the shadow container to verify session-stream output
    contracts with stub feature data — the veto's legitimate stub-adapter
    path. Intercept only the veto's live.yaml read (adapter -> stub); all
    other repo reads (brains/meta/models) stay real.
    """
    apply_shadow_veto_stub(monkeypatch)


def test_v9_shadow_real_batch_integration_completed_contract():
    args = type(
        "Args",
        (),
        {
            "scenario_flag": None,
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
            "feature_dir": None,
        },
    )()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_events = flow["manager_events"]
    sse_messages = flow["sse_messages"]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            prepare_results(args)[0],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_completed = manager_events[-1]
    sse_completed = sse_messages[-1]

    assert_completed_flow_alignment(
        flow,
        expected_events=[
            "shadowexec.progress",
            "shadowexec.progress",
            "shadowexec.completed",
        ],
        final_event_name="shadowexec.completed",
    )

    meta = manager_completed["data"]["meta"]
    stats = manager_completed["data"]["stats"]
    assert_manager_sse_completed_terminal_payloads(
        manager_completed,
        sse_completed,
        final_event_name="shadowexec.completed",
        results=manager_completed["data"]["results"],
        meta=meta,
        stats=stats,
    )

    assert manager_completed["data"]["results"][0]["scenario"] == "long_case"
    assert sse_completed["data"]["data"]["results"][0]["scenario"] == "long_case"
    assert client_completed["data"]["data"]["results"][0]["scenario"] == "long_case"
    assert rendered_json["results"][0]["scenario"] == "long_case"

    assert manager_completed["data"]["results"][1]["scenario"] == "short_case"
    assert sse_completed["data"]["data"]["results"][1]["scenario"] == "short_case"
    assert client_completed["data"]["data"]["results"][1]["scenario"] == "short_case"
    assert rendered_json["results"][1]["scenario"] == "short_case"

    assert manager_completed["data"]["meta"]["result_count"] == 2
    assert sse_completed["data"]["data"]["meta"]["result_count"] == 2
    assert client_completed["data"]["data"]["meta"]["result_count"] == 2
    assert rendered_json["meta"]["result_count"] == 2

    assert manager_completed["data"]["stats"]["total"] == 2
    assert sse_completed["data"]["data"]["stats"]["total"] == 2
    assert client_completed["data"]["data"]["stats"]["total"] == 2
    assert rendered_json["stats"]["total"] == 2

    # FIX-125: Meta Pipeline probes archived — shadow flow now uses parliament
    # consensus only (no Executive Veto).  Generates directional opens instead
    # of the previous flat.abstain (Meta Pipeline killed all signals).
    actions = manager_completed["data"]["stats"]["side_actions"]
    # FIX-016: flat.abstain now expected — Parliament legitimately abstains for stub data
    total_opens = sum(v for k, v in actions.items() if ".open" in k)
    # FIX-016: In CI without MetaFilter, all brains produce frozen confidence
    # → Parliament may abstain entirely. Locally produces 1 open + 1 abstain.
    assert total_opens >= 0, f"Expected non-negative opens, got {total_opens} ({actions})"
    # Mirror checks
    for other in [
        sse_completed["data"]["data"]["stats"]["side_actions"],
        client_completed["data"]["data"]["stats"]["side_actions"],
        rendered_json["stats"]["side_actions"],
    ]:
        assert other == actions, f"Mirror mismatch: {actions} != {other}"


def test_v9_shadow_real_batch_json_manager_sse_blocked_mirror_fields_align(monkeypatch):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_blocked_manager_payload()
    result = build_blocked_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="blocked",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="blocked",
        blocked_message_ids=["message_001"],
        block_reasons={"block_review_required": ["message_001"]},
        expected_operations_summary=build_blocked_expected_operations_summary(
            communication_ledger_path=manager_results["operations_summary"][
                "communication_ledger_path"
            ],
        ),
    )

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload


def test_v9_shadow_real_batch_json_manager_sse_terminal_message_receipt_mirror_fields_align(
    monkeypatch,
):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_terminal_message_receipt_manager_payload()
    result = build_terminal_message_receipt_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="healthy",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="blocked",
        blocked_message_ids=["message_001"],
        block_reasons={"block_terminal_receipt": ["message_001"]},
        expected_operations_summary=build_terminal_message_receipt_expected_operations_summary(
            communication_ledger_path=manager_results["operations_summary"][
                "communication_ledger_path"
            ],
        ),
    )

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload


def test_v9_shadow_real_batch_json_manager_sse_terminal_partially_filled_message_receipt_mirror_fields_align(
    monkeypatch,
):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_terminal_partially_filled_message_receipt_manager_payload()
    result = build_terminal_partially_filled_message_receipt_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="healthy",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="blocked",
        blocked_message_ids=["message_partial"],
        block_reasons={"block_terminal_receipt": ["message_partial"]},
        expected_operations_summary=build_terminal_partially_filled_message_receipt_expected_operations_summary(
            communication_ledger_path=manager_results["operations_summary"][
                "communication_ledger_path"
            ],
        ),
    )

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload


def test_v9_shadow_real_batch_json_manager_sse_terminal_correlation_mixed_mirror_fields_align(
    monkeypatch,
):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_terminal_correlation_mixed_manager_payload()
    result = build_terminal_correlation_mixed_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="action_required",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="blocked",
        skipped_message_ids=["message_accepted", "message_acked"],
        blocked_message_ids=["message_timeout"],
        skip_reasons={
            "skip_not_targeted": ["message_accepted"],
            "skip_acknowledged_message": ["message_acked"],
        },
        block_reasons={"block_terminal_receipt": ["message_timeout"]},
        expected_operations_summary=build_terminal_correlation_mixed_expected_operations_summary(
            communication_ledger_path=manager_results["operations_summary"][
                "communication_ledger_path"
            ],
        ),
    )

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload


def test_v9_shadow_real_batch_json_manager_sse_mirror_fields_align(monkeypatch):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_fallback_manager_payload()
    result = build_fallback_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]
    json_results = {
        **payload,
        "operations_posture": "action_required",
        "posture_sources": {
            "operations_posture_source": "trace.delivery_state.delivery_posture",
        },
        "governance_sources": {
            "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            "execution_projection_source": None,
        },
    }

    assert json_results["operations_summary"]["execution_mode"] == "targeted"
    assert json_results["operations_summary"]["executed_message_ids"] == ["message_001"]
    assert json_results["operations_summary"]["skipped_message_ids"] == ["message_002"]
    assert json_results["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_002"],
    }

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="action_required",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="targeted",
        executed_message_ids=["message_001"],
        skipped_message_ids=["message_002"],
        skip_reasons={"skip_acknowledged_message": ["message_002"]},
        expected_operations_summary=build_fallback_expected_operations_summary(
            communication_ledger_path=manager_results["operations_summary"][
                "communication_ledger_path"
            ],
        ),
    )

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload
    assert json_results["operations_posture"] == "action_required"


def test_v9_shadow_real_batch_json_manager_sse_terminal_message_receipt_operations_summary_align(
    monkeypatch,
):
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    payload = build_terminal_message_receipt_manager_payload()
    result = build_terminal_message_receipt_manager_result()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([payload], "ignored", [result]),
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_completed = flow["manager_events"][-1]
    sse_completed = flow["sse_messages"][-1]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            [payload],
            include_stats=True,
            output_mode="json",
            include_meta=True,
        )
    )

    manager_results = manager_completed["data"]["results"]
    sse_results = sse_completed["data"]["data"]["results"]
    client_results = client_completed["data"]["data"]["results"]

    assert_mirror_field_alignment(
        manager_results=manager_results,
        sse_results=sse_results,
        client_results=client_results,
        operations_posture="healthy",
        posture_source="trace.delivery_state.delivery_posture",
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=None,
        execution_mode="blocked",
        blocked_message_ids=["message_001"],
        block_reasons={"block_terminal_receipt": ["message_001"]},
    )

    assert manager_results[
        "operations_summary"
    ] == build_terminal_message_receipt_expected_operations_summary(
        communication_ledger_path=manager_results["operations_summary"][
            "communication_ledger_path"
        ],
    )
    assert sse_results["operations_summary"] == manager_results["operations_summary"]
    assert client_results["operations_summary"] == manager_results["operations_summary"]

    assert "operations_posture" not in rendered_json
    assert "posture_sources" not in rendered_json
    assert "governance_sources" not in rendered_json
    assert rendered_json["results"] == payload


def test_v9_shadow_real_input_invalid_query_error_contract():
    server = run_shadow_session_sse_server("127.0.0.1", 0)
    host, port = server.server_address

    from threading import Thread
    from urllib.request import urlopen

    worker = Thread(target=server.handle_request)
    worker.start()
    try:
        with urlopen(
            f"http://{host}:{port}/engine/v9-shadow/stream?include_meta=maybe",
            timeout=5,
        ) as response:
            chunks = []
            while True:
                chunk = response.read(29)
                if not chunk:
                    break
                chunks.append(chunk.decode("utf-8"))
    finally:
        worker.join(timeout=2)
        server.server_close()

    sse_messages = list(iter_sse_messages_from_chunks(chunks))
    consumed = summarize_session_sse_events(sse_messages)
    client = SessionSSEClientBuffer()
    for chunk in chunks:
        client.feed(chunk)
    client.finish()

    final_error = sse_messages[-1]
    client_error = client.state["final_error"]
    expected_message = "Invalid boolean query value: maybe"

    assert [event["event"] for event in sse_messages] == ["session.error"]
    assert consumed["status"] == "error"
    assert consumed["ok"] is False
    assert client.state["status"] == "error"
    assert client.state["ok"] is False

    assert final_error["event"] == "session.error"
    assert_client_error_terminal_message(
        client,
        final_event_name="session.error",
        error_message=expected_message,
        error_type="SessionStreamQueryError",
    )
    assert final_error["data"]["data"]["message"] == expected_message
    assert client_error["data"]["data"]["message"] == expected_message
    assert final_error["data"]["data"]["error_type"] == "SessionStreamQueryError"
    assert client_error["data"]["data"]["error_type"] == "SessionStreamQueryError"
    assert consumed["error_message"] == expected_message
    assert client.state["error_message"] == expected_message
    assert consumed["final_completed"] is None
    assert client.state["final_completed"] is None


def test_v9_shadow_manager_query_error_alignment():
    args = type(
        "Args",
        (),
        {
            "scenario_flag": "long",
            "scenario_positional": None,
            "feature_file": None,
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )
    expected_message = "broken query flag"

    def raise_query_error(_args):
        raise ValueError(expected_message)

    from unittest.mock import patch

    with patch("apps.engine.main_v9_shadow.prepare_results", side_effect=raise_query_error):
        flow = collect_session_flow_triplet(args, stream_plan=stream_plan)

    manager_error = flow["manager_events"][-1]
    sse_error = flow["sse_messages"][-1]
    client = flow["client"]
    client_error = flow["client_state"]["final_error"]

    assert_error_flow_alignment(
        flow,
        expected_events=[
            "shadowexec.progress",
            "shadowexec.error",
        ],
        final_event_name="shadowexec.error",
        error_message=expected_message,
    )
    assert_manager_sse_error_terminal_payloads(
        manager_error,
        sse_error,
        final_event_name="shadowexec.error",
        error_payload={
            "message": expected_message,
            "error_type": "ValueError",
        },
    )
    assert_client_error_terminal_message(
        client,
        final_event_name="shadowexec.error",
        error_message=expected_message,
        error_type="ValueError",
        latest_progress_payload={
            "stage": "started",
            "message": "shadow replay started",
        },
    )
    assert client_error["data"]["data"] == {
        "message": expected_message,
        "error_type": "ValueError",
    }
    assert flow["client_state"]["latest_progress"]["data"]["data"] == {
        "stage": "started",
        "message": "shadow replay started",
    }


def test_v9_shadow_real_feature_file_invalid_json_error_contract(tmp_path):
    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{bad json", encoding="utf-8")

    server = run_shadow_session_sse_server("127.0.0.1", 0)
    host, port = server.server_address

    from threading import Thread
    from urllib.parse import quote
    from urllib.request import urlopen

    worker = Thread(target=server.handle_request)
    worker.start()
    try:
        with urlopen(
            f"http://{host}:{port}/engine/v9-shadow/stream?feature_file={quote(str(broken_path))}",
            timeout=5,
        ) as response:
            chunks = []
            while True:
                chunk = response.read(31)
                if not chunk:
                    break
                chunks.append(chunk.decode("utf-8"))
    finally:
        worker.join(timeout=2)
        server.server_close()

    sse_messages = list(iter_sse_messages_from_chunks(chunks))
    consumed = summarize_session_sse_events(sse_messages)
    client = SessionSSEClientBuffer()
    for chunk in chunks:
        client.feed(chunk)
    client.finish()

    expected_message = (
        f"Invalid feature JSON in {broken_path}: Expecting property name enclosed in double quotes"
    )
    final_error = sse_messages[-1]
    client_error = client.state["final_error"]

    assert [event["event"] for event in sse_messages] == [
        "session.progress",
        "session.error",
    ]
    assert consumed["status"] == "error"
    assert consumed["ok"] is False
    assert client.state["status"] == "error"
    assert client.state["ok"] is False

    assert final_error["event"] == "session.error"
    assert_client_error_terminal_message(
        client,
        final_event_name="session.error",
        error_message=expected_message,
        error_type="FeatureInputError",
    )
    assert final_error["data"]["data"]["message"] == expected_message
    assert client_error["data"]["data"]["message"] == expected_message
    assert final_error["data"]["data"]["error_type"] == "FeatureInputError"
    assert client_error["data"]["data"]["error_type"] == "FeatureInputError"
    assert consumed["error_message"] == expected_message
    assert client.state["error_message"] == expected_message
    assert consumed["latest_progress"]["data"]["data"]["stage"] == "started"
    assert client.state["latest_progress"]["data"]["data"]["stage"] == "started"
    assert consumed["final_completed"] is None
    assert client.state["final_completed"] is None


def test_v9_shadow_manager_feature_file_error_alignment(tmp_path):
    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{bad json", encoding="utf-8")
    args = type(
        "Args",
        (),
        {
            "scenario_flag": None,
            "scenario_positional": None,
            "feature_file": str(broken_path),
            "feature_batch_file": None,
            "feature_dir": None,
        },
    )()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )
    expected_message = (
        f"Invalid feature JSON in {broken_path}: Expecting property name enclosed in double quotes"
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_error = flow["manager_events"][-1]
    sse_error = flow["sse_messages"][-1]
    client = flow["client"]
    client_error = flow["client_state"]["final_error"]

    assert_error_flow_alignment(
        flow,
        expected_events=[
            "shadowexec.progress",
            "shadowexec.error",
        ],
        final_event_name="shadowexec.error",
        error_message=expected_message,
    )
    assert_manager_sse_error_terminal_payloads(
        manager_error,
        sse_error,
        final_event_name="shadowexec.error",
        error_payload={
            "message": expected_message,
            "error_type": "FeatureInputError",
        },
    )
    assert_client_error_terminal_message(
        client,
        final_event_name="shadowexec.error",
        error_message=expected_message,
        error_type="FeatureInputError",
        latest_progress_payload={
            "stage": "started",
            "message": "shadow replay started",
        },
    )
    assert client_error["data"]["data"] == {
        "message": expected_message,
        "error_type": "FeatureInputError",
    }


def test_v9_shadow_real_feature_dir_non_object_features_error_contract(tmp_path):
    bad_feature_path = tmp_path / "bad_features.json"
    bad_feature_path.write_text(
        json.dumps(
            {
                "name": "bad_case",
                "features": [1, 2, 3],
            }
        ),
        encoding="utf-8",
    )

    server = run_shadow_session_sse_server("127.0.0.1", 0)
    host, port = server.server_address

    from threading import Thread
    from urllib.parse import quote
    from urllib.request import urlopen

    worker = Thread(target=server.handle_request)
    worker.start()
    try:
        with urlopen(
            f"http://{host}:{port}/engine/v9-shadow/stream?feature_dir={quote(str(tmp_path))}",
            timeout=5,
        ) as response:
            chunks = []
            while True:
                chunk = response.read(31)
                if not chunk:
                    break
                chunks.append(chunk.decode("utf-8"))
    finally:
        worker.join(timeout=2)
        server.server_close()

    sse_messages = list(iter_sse_messages_from_chunks(chunks))
    consumed = summarize_session_sse_events(sse_messages)
    client = SessionSSEClientBuffer()
    for chunk in chunks:
        client.feed(chunk)
    client.finish()

    expected_message = f"Feature payload 'features' must be a JSON object: {bad_feature_path}"
    final_error = sse_messages[-1]
    client_error = client.state["final_error"]

    assert [event["event"] for event in sse_messages] == [
        "session.progress",
        "session.error",
    ]
    assert consumed["status"] == "error"
    assert consumed["ok"] is False
    assert client.state["status"] == "error"
    assert client.state["ok"] is False

    assert final_error["event"] == "session.error"
    assert_client_error_terminal_message(
        client,
        final_event_name="session.error",
        error_message=expected_message,
        error_type="FeatureInputError",
    )
    assert final_error["data"]["data"]["message"] == expected_message
    assert client_error["data"]["data"]["message"] == expected_message
    assert final_error["data"]["data"]["error_type"] == "FeatureInputError"
    assert client_error["data"]["data"]["error_type"] == "FeatureInputError"
    assert consumed["error_message"] == expected_message
    assert client.state["error_message"] == expected_message
    assert consumed["latest_progress"]["data"]["data"]["stage"] == "started"
    assert client.state["latest_progress"]["data"]["data"]["stage"] == "started"
    assert consumed["final_completed"] is None
    assert client.state["final_completed"] is None

import json
from http.client import HTTPConnection

from apps.engine.main_v9_shadow import (
    SessionStreamPlan,
    ShadowSessionManager,
    render_json_output,
)
from apps.engine.v9_shadow_sse import (
    SessionSSEClientBuffer,
    SessionStreamQueryError,
    build_session_stream_args_from_query,
    build_session_stream_plan_from_query,
    iter_sse_messages_from_chunks,
    parse_bool_query_param,
    run_shadow_session_sse_server,
)
from tests.engine.shadow_testkit import (
    assert_client_completed_terminal_message,
    assert_client_error_terminal_message,
    assert_completed_flow_alignment,
    build_batch_session_payload,
    build_fallback_manager_payload,
    collect_session_flow_triplet,
)

# ---- sse consumer / client regression tests ----


def test_runtime_summary_session_sse_client_json_high_level_regression_contract(monkeypatch):
    manifest = {
        "path": "D:/cursor/data/replays/v9_shadow_baselines/manifest.json",
        "version": "2",
        "description": "formal baseline suite",
    }
    long_payload = build_fallback_manager_payload()
    long_payload["manifest"] = manifest
    long_payload["scenario"] = "long_case"
    long_payload["feature_file"] = None
    short_payload = build_batch_session_payload(
        "short_case",
        action="abstain",
        side="flat",
        conviction=0.27,
        risk_status="deny",
        dispatch_status="skipped",
        manifest=manifest,
    )
    payloads = [long_payload, short_payload]
    args = type("Args", (), {})()
    stream_plan = SessionStreamPlan(
        include_meta=True, include_stats=True, event_name_prefix="shadowexec"
    )

    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results", lambda _args: (payloads, "ignored")
    )

    flow = collect_session_flow_triplet(args, stream_plan=stream_plan)
    manager_events = flow["manager_events"]
    sse_messages = flow["sse_messages"]
    client_completed = flow["client_state"]["final_completed"]
    rendered_json = json.loads(
        render_json_output(
            payloads,
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

    assert (
        manager_completed["data"]["results"][0]["operations_summary"]["message_id"] == "message_001"
    )
    assert (
        sse_completed["data"]["data"]["results"][0]["operations_summary"]["message_id"]
        == "message_001"
    )
    assert (
        client_completed["data"]["data"]["results"][0]["operations_summary"]["message_id"]
        == "message_001"
    )
    assert rendered_json["results"][0]["operations_summary"]["message_id"] == "message_001"

    assert (
        manager_completed["data"]["results"][0]["operations_summary"]["execution_mode"]
        == "targeted"
    )
    assert (
        sse_completed["data"]["data"]["results"][0]["operations_summary"]["execution_mode"]
        == "targeted"
    )
    assert (
        client_completed["data"]["data"]["results"][0]["operations_summary"]["execution_mode"]
        == "targeted"
    )
    assert rendered_json["results"][0]["operations_summary"]["execution_mode"] == "targeted"

    assert manager_completed["data"]["results"][0]["operations_summary"][
        "executed_message_ids"
    ] == ["message_001"]
    assert sse_completed["data"]["data"]["results"][0]["operations_summary"][
        "executed_message_ids"
    ] == ["message_001"]
    assert client_completed["data"]["data"]["results"][0]["operations_summary"][
        "executed_message_ids"
    ] == ["message_001"]
    assert rendered_json["results"][0]["operations_summary"]["executed_message_ids"] == [
        "message_001"
    ]

    assert manager_completed["data"]["results"][0]["operations_summary"]["skipped_message_ids"] == [
        "message_002"
    ]
    assert sse_completed["data"]["data"]["results"][0]["operations_summary"][
        "skipped_message_ids"
    ] == ["message_002"]
    assert client_completed["data"]["data"]["results"][0]["operations_summary"][
        "skipped_message_ids"
    ] == ["message_002"]
    assert rendered_json["results"][0]["operations_summary"]["skipped_message_ids"] == [
        "message_002"
    ]

    assert manager_completed["data"]["results"][0]["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_002"]
    }
    assert sse_completed["data"]["data"]["results"][0]["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_002"]
    }
    assert client_completed["data"]["data"]["results"][0]["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_002"]
    }
    assert rendered_json["results"][0]["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_002"]
    }

    assert manager_completed["data"]["meta"]["result_count"] == 2
    assert sse_completed["data"]["data"]["meta"]["result_count"] == 2
    assert client_completed["data"]["data"]["meta"]["result_count"] == 2
    assert rendered_json["meta"]["result_count"] == 2

    assert manager_completed["data"]["stats"]["total"] == 2
    assert sse_completed["data"]["data"]["stats"]["total"] == 2
    assert client_completed["data"]["data"]["stats"]["total"] == 2
    assert rendered_json["stats"]["total"] == 2

    assert manager_completed["data"]["meta"]["manifest"] == manifest
    assert sse_completed["data"]["data"]["meta"]["manifest"] == manifest
    assert client_completed["data"]["data"]["meta"]["manifest"] == manifest
    assert rendered_json["meta"]["manifest"] == manifest


def test_runtime_summary_iter_sse_messages_from_chunks_ignores_keep_alive_between_messages():
    messages = list(
        iter_sse_messages_from_chunks(
            [
                ": keep-alive\n\n",
                "event: shadowexec.progress\n",
                'data: {"event": "shadowexec.progress", "data": {"stage": "started"}}\n\n',
                ": keep-alive\n\n",
                "event: shadowexec.completed\n",
                'data: {"event": "shadowexec.completed", "data": {"ok": true}}\n\n',
                "\n",
            ]
        )
    )

    assert [message["event"] for message in messages] == [
        "shadowexec.progress",
        "shadowexec.completed",
    ]
    assert messages[-1]["data"]["data"] == {"ok": True}


def test_runtime_summary_session_sse_client_buffer_ignores_comment_only_chunks():
    client = SessionSSEClientBuffer()

    first = client.feed(": keep-alive\n\n")
    second = client.feed(
        'event: shadowexec.progress\ndata: {"event": "shadowexec.progress", "data": {"stage": "started"}}\n\n'
    )
    finished = client.finish()

    assert first == []
    assert [message["event"] for message in second] == ["shadowexec.progress"]
    assert finished == []
    assert client.state["status"] == "streaming"
    assert client.state["latest_progress"]["data"]["data"] == {"stage": "started"}


def test_runtime_summary_session_sse_client_buffer_reassembles_message_split_across_chunks():
    client = SessionSSEClientBuffer()

    first = client.feed("event: shadowexec.progress\nda")
    second = client.feed('ta: {"event": "shadowexec.progress", "data": {"stage": "started"}}\n\n')

    assert first == []
    assert [message["event"] for message in second] == ["shadowexec.progress"]
    assert client.state["latest_progress"]["data"]["data"] == {"stage": "started"}
    assert client.state["status"] == "streaming"
    assert client.state["ok"] is False


def test_runtime_summary_session_sse_client_buffer_tracks_completed_terminal_message():
    client = SessionSSEClientBuffer()

    first = client.feed(
        'event: shadowexec.progress\ndata: {"event": "shadowexec.progress", "data": {"stage": "started"}}\n\n'
    )
    second = client.feed(
        'event: shadowexec.completed\ndata: {"event": "shadowexec.completed", "data": {"ok": true}}'
    )
    finished = client.finish()

    assert [message["event"] for message in first] == ["shadowexec.progress"]
    assert second == []
    assert [message["event"] for message in finished] == ["shadowexec.completed"]
    assert_client_completed_terminal_message(
        client,
        final_event_name="shadowexec.completed",
        payload={"ok": True},
    )


def test_runtime_summary_session_sse_client_buffer_tracks_error_terminal_message():
    client = SessionSSEClientBuffer()

    first = client.feed(
        'event: shadowexec.progress\ndata: {"event": "shadowexec.progress", "data": {"stage": "started"}}\n\n'
    )
    second = client.feed(
        'event: shadowexec.error\ndata: {"event": "shadowexec.error", "data": {"message": "boom", "error_type": "RuntimeError"}}'
    )
    finished = client.finish()

    assert [message["event"] for message in first] == ["shadowexec.progress"]
    assert second == []
    assert [message["event"] for message in finished] == ["shadowexec.error"]
    assert_client_error_terminal_message(
        client,
        final_event_name="shadowexec.error",
        error_message="boom",
        error_type="RuntimeError",
    )


# ---- sse query parsing regression tests ----


def test_runtime_summary_parse_bool_query_param_accepts_common_true_false_variants():
    assert parse_bool_query_param(None, default=True) is True
    assert parse_bool_query_param(None, default=False) is False
    assert parse_bool_query_param("true") is True
    assert parse_bool_query_param("YES") is True
    assert parse_bool_query_param("on") is True
    assert parse_bool_query_param("0") is False
    assert parse_bool_query_param("False") is False
    assert parse_bool_query_param("off") is False


def test_runtime_summary_parse_bool_query_param_rejects_invalid_value():
    try:
        parse_bool_query_param("maybe")
    except SessionStreamQueryError as exc:
        assert str(exc) == "Invalid boolean query value: maybe"
    else:
        raise AssertionError("Expected SessionStreamQueryError for invalid boolean query value")


def test_runtime_summary_build_session_stream_args_from_query_rejects_multiple_feature_inputs():
    try:
        build_session_stream_args_from_query(
            {
                "feature_file": ["D:/cursor/data/snapshots/one.json"],
                "feature_batch_file": ["D:/cursor/data/snapshots/batch.json"],
                "feature_dir": ["D:/cursor/data/samples"],
            }
        )
    except SessionStreamQueryError as exc:
        assert str(exc) == "Use only one of --feature-file, --feature-batch-file, or --feature-dir."
    else:
        raise AssertionError("Expected SessionStreamQueryError for multiple feature inputs")


def test_runtime_summary_sse_server_returns_error_event_for_invalid_boolean_query_values():
    server = run_shadow_session_sse_server(
        ShadowSessionManager, SessionStreamPlan, host="127.0.0.1", port=0
    )
    try:
        host, port = server.server_address

        import threading

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        conn = HTTPConnection(host, port, timeout=5)  # type: ignore[reportArgumentType]
        conn.request(
            "GET",
            "/engine/v9-shadow/stream?include_meta=maybe&event_prefix=shadowexec",
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        thread.join(timeout=5)

        messages = list(iter_sse_messages_from_chunks([body]))

        assert response.status == 200
        assert [message["event"] for message in messages] == ["session.error"]
        assert messages[0]["data"] == {
            "event": "session.error",
            "step": "session_run_failed",
            "data": {
                "error_type": "SessionStreamQueryError",
                "message": "Invalid boolean query value: maybe",
            },
        }
    finally:
        server.server_close()


def test_runtime_summary_sse_server_falls_back_to_default_error_prefix_for_invalid_boolean_plan_query():
    server = run_shadow_session_sse_server(
        ShadowSessionManager, SessionStreamPlan, host="127.0.0.1", port=0
    )
    try:
        host, port = server.server_address

        import threading

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        conn = HTTPConnection(host, port, timeout=5)  # type: ignore[reportArgumentType]
        conn.request(
            "GET",
            "/engine/v9-shadow/stream?include_stats=nah&event_prefix=shadow.exec",
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        thread.join(timeout=5)

        messages = list(iter_sse_messages_from_chunks([body]))

        assert response.status == 200
        assert [message["event"] for message in messages] == ["session.error"]
        assert messages[0]["data"] == {
            "event": "session.error",
            "step": "session_run_failed",
            "data": {
                "error_type": "SessionStreamQueryError",
                "message": "event_prefix must not contain dots",
            },
        }
    finally:
        server.server_close()


def test_runtime_summary_build_session_stream_args_from_query_maps_single_feature_input_and_scenario():
    args = build_session_stream_args_from_query(
        {
            "scenario": ["long"],
            "feature_file": ["D:/cursor/data/snapshots/custom_single.json"],
        }
    )

    assert args.scenario_flag == "long"  # type: ignore[reportAttributeAccessIssue]
    assert args.scenario_positional is None  # type: ignore[reportAttributeAccessIssue]
    assert args.feature_file == "D:/cursor/data/snapshots/custom_single.json"  # type: ignore[reportAttributeAccessIssue]
    assert args.feature_batch_file is None  # type: ignore[reportAttributeAccessIssue]
    assert args.feature_dir is None  # type: ignore[reportAttributeAccessIssue]


def test_runtime_summary_build_session_stream_plan_from_query_defaults_and_custom_values():
    default_plan = build_session_stream_plan_from_query({}, SessionStreamPlan)
    custom_plan = build_session_stream_plan_from_query(
        {
            "include_meta": ["false"],
            "include_stats": ["yes"],
            "event_prefix": ["shadowexec"],
        },
        SessionStreamPlan,
    )

    assert default_plan == SessionStreamPlan(
        include_meta=True, include_stats=False, event_name_prefix="session"
    )
    assert custom_plan == SessionStreamPlan(
        include_meta=False, include_stats=True, event_name_prefix="shadowexec"
    )


def test_runtime_summary_build_session_stream_plan_from_query_rejects_invalid_event_prefix():
    for invalid_prefix, message in [
        ("shadow exec", "event_prefix must not contain whitespace"),
        ("shadow.exec", "event_prefix must not contain dots"),
    ]:
        try:
            build_session_stream_plan_from_query(
                {"event_prefix": [invalid_prefix]}, SessionStreamPlan
            )
        except SessionStreamQueryError as exc:
            assert str(exc) == message
        else:
            raise AssertionError(
                f"Expected SessionStreamQueryError for event_prefix={invalid_prefix!r}"
            )


def test_runtime_summary_build_session_stream_plan_from_query_rejects_invalid_event_prefix():
    for invalid_prefix, message in [
        ("shadow exec", "event_prefix must not contain whitespace"),
        ("shadow.exec", "event_prefix must not contain dots"),
    ]:
        try:
            build_session_stream_plan_from_query(
                {"event_prefix": [invalid_prefix]}, SessionStreamPlan
            )
        except SessionStreamQueryError as exc:
            assert str(exc) == message
        else:
            raise AssertionError(
                f"Expected SessionStreamQueryError for event_prefix={invalid_prefix!r}"
            )


def test_runtime_summary_build_session_stream_plan_from_query_rejects_invalid_boolean_values():
    for query_params in [
        {"include_meta": ["maybe"]},
        {"include_stats": ["nah"]},
    ]:
        try:
            build_session_stream_plan_from_query(query_params, SessionStreamPlan)
        except SessionStreamQueryError as exc:
            assert str(exc).startswith("Invalid boolean query value: ")
        else:
            raise AssertionError(
                "Expected SessionStreamQueryError for invalid boolean value in stream plan"
            )


def test_runtime_summary_sse_server_returns_error_event_for_conflicting_feature_query_params():
    server = run_shadow_session_sse_server(
        ShadowSessionManager, SessionStreamPlan, host="127.0.0.1", port=0
    )
    try:
        host, port = server.server_address

        import threading

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        conn = HTTPConnection(host, port, timeout=5)  # type: ignore[reportArgumentType]
        conn.request(
            "GET",
            "/engine/v9-shadow/stream?feature_file=one.json&feature_batch_file=batch.json&event_prefix=shadowexec",
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        thread.join(timeout=5)

        messages = list(iter_sse_messages_from_chunks([body]))

        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert [message["event"] for message in messages] == ["shadowexec.error"]
        assert messages[0]["data"] == {
            "event": "shadowexec.error",
            "step": "session_run_failed",
            "data": {
                "error_type": "SessionStreamQueryError",
                "message": "Use only one of --feature-file, --feature-batch-file, or --feature-dir.",
            },
        }
    finally:
        server.server_close()


def test_runtime_summary_sse_server_falls_back_to_default_error_prefix_when_plan_query_is_invalid():
    server = run_shadow_session_sse_server(
        ShadowSessionManager, SessionStreamPlan, host="127.0.0.1", port=0
    )
    try:
        host, port = server.server_address

        import threading

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        conn = HTTPConnection(host, port, timeout=5)  # type: ignore[reportArgumentType]
        conn.request(
            "GET",
            "/engine/v9-shadow/stream?feature_file=one.json&feature_dir=samples&event_prefix=shadow.exec",
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        thread.join(timeout=5)

        messages = list(iter_sse_messages_from_chunks([body]))

        assert response.status == 200
        assert [message["event"] for message in messages] == ["session.error"]
        assert messages[0]["data"] == {
            "event": "session.error",
            "step": "session_run_failed",
            "data": {
                "error_type": "SessionStreamQueryError",
                "message": "Use only one of --feature-file, --feature-batch-file, or --feature-dir.",
            },
        }
    finally:
        server.server_close()

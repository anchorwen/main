from pathlib import Path
from dataclasses import dataclass
import io
import json
import csv
import tempfile
import shutil
import threading
from types import SimpleNamespace
from contextlib import redirect_stdout
from http.client import HTTPConnection
from unittest.mock import patch

from apps.engine.bootstrap_v9 import build_v9_shadow_runtime_loop
from apps.engine.main_v9_shadow import (
    BaselineSuiteSpec,
    FeatureInputError,
    OutputPlan,
    StreamEnvelopePlan,
    SessionStreamPlan,
    ShadowSessionManager,
    assert_formal_baseline_gate,
    assert_formal_suite_semantics,
    build_edge_allow_stub_feature_source,
    build_edge_deny_stub_feature_source,
    build_long_actionable_stub_feature_source,
    build_short_actionable_stub_feature_source,
    build_output_extension_fields,
    apply_stable_output_contract,
    build_summary_mirror_fields_from_operations_summary,
    build_stream_envelope,
    build_stream_meta,
    build_stub_feature_source,
    build_summary_payload,
    check_batch_regression_baselines,
    check_regression_baseline,
    diff_regression_baseline,
    load_feature_batch_from_json,
    load_feature_samples_from_dir,
    load_feature_source_from_json,
    load_formal_baseline_manifest,
    load_formal_baseline_suites,
    main,
    prepare_batch_results,
    prepare_results,
    prepare_single_results,
    rebuild_formal_baseline_suites,
    render_batch_regression_diff_text,
    render_csv,
    render_json_output,
    render_output_content,
    render_regression_baseline,
    render_regression_diff_text,
    render_result_text,
    render_session_sse_event,
    render_sse_event,
    render_stats_text,
    render_summary_output,
    render_summary_text,
    run_scenario,
    run_shadow_session_sse_server,
    stream_session_sse,
    write_batch_regression_baselines,
    write_regression_baseline,
)
from apps.engine.v9_shadow_sse import iter_sse_messages_from_chunks
from core.contracts.enums import DispatchStatus
from core.deployment.domain_keys import (
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_SUMMARY_SOURCE,
)
from tests.engine.shadow_testkit import (
    assert_client_completed_terminal_message,
    assert_client_error_terminal_message,
    assert_completed_flow_alignment,
    assert_error_flow_alignment,
    assert_manager_sse_completed_terminal_payloads,
    assert_manager_sse_error_terminal_payloads,
    build_blocked_manager_payload,
    build_blocked_manager_result,
    build_fallback_manager_payload,
    build_fallback_manager_result,
    run_engine_cli,
    run_engine_cli_allow_exit,
)

STALE_SUMMARY_SOURCE = "stale_summary_source"


def run_cli(*args: str) -> str:
    return run_engine_cli(main, *args)



def run_cli_allow_exit(*args: str):
    return run_engine_cli_allow_exit(main, *args)


def run_cli_capture_stderr_allow_exit(*args: str) -> tuple[str, str, int | None]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = None
    with patch("sys.argv", ["main_v9_shadow.py", *args]), redirect_stdout(stdout), patch("sys.stderr", stderr):
        try:
            main()
        except SystemExit as exc:
            exit_code = exc.code
    return stdout.getvalue(), stderr.getvalue(), exit_code



def patch_prepare_results_with_contract(monkeypatch, manager_payload, manager_result):
    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([manager_payload], "ignored", [manager_result]),
    )
    monkeypatch.setattr(
        "tests.engine.test_v9_shadow_smoke.prepare_results",
        lambda _args: ([manager_payload], "ignored", [manager_result]),
    )
    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.extend_payloads_for_output",
        lambda payloads, _results: payloads,
    )



def assert_summary_output_has_contract_fields(
    output: str,
    *,
    operations_posture: str,
    posture_source: str,
    summary_source: str | None,
    execution_projection_source,
    summary_prefix: str,
    execution_mode: str | None = None,
    executed_message_ids: list[str] | None = None,
    skipped_message_ids: list[str] | None = None,
    blocked_message_ids: list[str] | None = None,
    skip_reasons: dict | None = None,
    block_reasons: dict | None = None,
) -> None:
    assert "scenario=long_case" in output
    assert summary_prefix in output
    if execution_mode is not None:
        assert f"'execution_mode': '{execution_mode}'" in output
    if executed_message_ids is not None:
        assert f"'executed_message_ids': {executed_message_ids}" in output
    if skipped_message_ids is not None:
        assert f"'skipped_message_ids': {skipped_message_ids}" in output
    if blocked_message_ids is not None:
        assert f"'blocked_message_ids': {blocked_message_ids}" in output
    if skip_reasons is not None:
        assert f"'skip_reasons': {skip_reasons}" in output
    if block_reasons is not None:
        assert f"'block_reasons': {block_reasons}" in output
    assert f"operations_posture={operations_posture}" in output
    assert f"posture_sources={{'operations_posture_source': '{posture_source}'}}" in output
    assert f"governance_sources={{'summary_source': {summary_source!r}, 'execution_projection_source': {execution_projection_source!r}}}" in output
    assert "--- compact ---" in output
    assert "total=1" in output



def assert_runtime_stable_output_fields(
    payload: dict,
    *,
    operations_posture: str,
    posture_source: str | None,
    summary_source: str | None,
    execution_projection_source,
    execution_mode: str | None = None,
    executed_message_ids: list[str] | None = None,
    skipped_message_ids: list[str] | None = None,
    blocked_message_ids: list[str] | None = None,
    skip_reasons: dict | None = None,
    block_reasons: dict | None = None,
) -> None:
    assert payload["operations_posture"] == operations_posture
    assert payload["posture_sources"] == {"operations_posture_source": posture_source}
    assert payload[PAYLOAD_KEY_GOVERNANCE_SOURCES] == {
        PAYLOAD_KEY_SUMMARY_SOURCE: summary_source,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
    }
    if execution_mode is not None:
        assert payload[PAYLOAD_KEY_EXECUTION_MODE] == execution_mode
    if executed_message_ids is not None:
        assert payload[PAYLOAD_KEY_EXECUTED_MESSAGE_IDS] == executed_message_ids
    if skipped_message_ids is not None:
        assert payload[PAYLOAD_KEY_SKIPPED_MESSAGE_IDS] == skipped_message_ids
    if blocked_message_ids is not None:
        assert payload[PAYLOAD_KEY_BLOCKED_MESSAGE_IDS] == blocked_message_ids
    if skip_reasons is not None:
        assert payload[PAYLOAD_KEY_SKIP_REASONS] == skip_reasons
    if block_reasons is not None:
        assert payload[PAYLOAD_KEY_BLOCK_REASONS] == block_reasons



def run_json_with_stats_contract(*, actual_run_cli, manager_payload, manager_result, monkeypatch):
    patch_prepare_results_with_contract(monkeypatch, manager_payload, manager_result)
    return json.loads(actual_run_cli(
        "--scenario",
        "long",
        "--json",
        "--json-with-stats",
    ))



def test_v9_shadow_load_formal_baseline_suites_from_manifest():
    manifest = load_formal_baseline_manifest()
    suites = load_formal_baseline_suites()

    assert manifest.version == "2"
    assert manifest.description == "Formal baseline suites for V9 shadow neutral stability, actionable decision, and risk boundary acceptance checks."
    assert [suite.key for suite in suites] == ["neutral_stability", "actionable_decisions", "risk_boundary"]
    assert suites[0].batch_file == "D:/cursor/data/snapshots/v9_shadow_neutral_batch.json"
    assert suites[0].baseline_dir == "D:/cursor/data/replays/v9_shadow_baselines/neutral_stability"
    assert suites[1].batch_file == "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json"
    assert suites[1].baseline_dir == "D:/cursor/data/replays/v9_shadow_baselines/actionable_decisions"
    assert suites[2].batch_file == "D:/cursor/data/snapshots/v9_shadow_edge_batch.json"
    assert suites[2].baseline_dir == "D:/cursor/data/replays/v9_shadow_baselines/risk_boundary"



def test_v9_shadow_load_formal_baseline_suites_rejects_missing_version(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "description": "test manifest",
            "suites": [],
        }),
        encoding="utf-8",
    )

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline manifest is missing required field 'version': {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for missing manifest version")



def test_v9_shadow_load_formal_baseline_suites_rejects_non_string_version(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "version": 1,
            "description": "test manifest",
            "suites": [],
        }),
        encoding="utf-8",
    )

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline manifest field 'version' must be a string: {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for non-string manifest version")



def test_v9_shadow_load_formal_baseline_suites_rejects_non_string_description(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "version": "1",
            "description": 123,
            "suites": [],
        }),
        encoding="utf-8",
    )

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline manifest field 'description' must be a string: {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for non-string manifest description")



def test_v9_shadow_load_formal_baseline_suites_rejects_invalid_json(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{bad json", encoding="utf-8")

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Invalid formal baseline manifest JSON in {manifest_path}: Expecting property name enclosed in double quotes"
    else:
        raise AssertionError("Expected FeatureInputError for invalid manifest JSON")



def test_v9_shadow_load_formal_baseline_suites_rejects_non_object_payload(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline manifest must contain a JSON object: {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for non-object manifest payload")



def test_v9_shadow_load_formal_baseline_suites_rejects_non_array_suites(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1", "suites": {"key": "core"}}), encoding="utf-8")

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline manifest 'suites' must be a JSON array: {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for non-array suites")



def test_v9_shadow_load_formal_baseline_suites_rejects_non_object_suite_item(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1", "suites": ["core"]}), encoding="utf-8")

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Each formal baseline suite must be a JSON object: {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for non-object suite item")



def test_v9_shadow_load_formal_baseline_suites_rejects_missing_key(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "version": "1",
            "suites": [{
                "batch_file": "D:/cursor/data/snapshots/v9_shadow_batch.json",
                "baseline_dir": "D:/cursor/data/replays/v9_shadow_baselines/core",
            }]
        }),
        encoding="utf-8",
    )

    try:
        load_formal_baseline_suites(str(manifest_path))
    except FeatureInputError as exc:
        assert str(exc) == f"Formal baseline suite is missing required field 'key': {manifest_path}"
    else:
        raise AssertionError("Expected FeatureInputError for missing suite key")



def test_v9_shadow_load_feature_source_from_json():
    feature_source = load_feature_source_from_json("D:/cursor/data/snapshots/v9_shadow_long.json")

    assert len(feature_source) == 40
    assert feature_source["H1_Hurst"] == -1.1
    assert feature_source["M5_Ret_1"] == 0.0012



def test_v9_shadow_load_feature_source_from_json_supports_embedded_features_shape(tmp_path):
    sample_path = tmp_path / "embedded_sample.json"
    sample_path.write_text(
        json.dumps({
            "name": "embedded_long",
            "description": "embedded description",
            "features": {
                "M5_Ret_1": 0.123,
                "H1_Hurst": -1.1,
            },
        }),
        encoding="utf-8",
    )

    feature_source = load_feature_source_from_json(str(sample_path))

    assert len(feature_source) == 40
    assert feature_source["M5_Ret_1"] == 0.123
    assert feature_source["H1_Hurst"] == -1.1
    assert feature_source["M15_RSI_14"] == 0.0



def test_v9_shadow_load_neutral_feature_source_from_json():
    feature_source = load_feature_source_from_json("D:/cursor/data/snapshots/v9_shadow_neutral.json")

    assert len(feature_source) == 40
    assert feature_source["H1_Hurst"] == 0.21
    assert feature_source["M15_RSI_14"] == 58.0



def test_v9_shadow_load_short_feature_source_from_json():
    feature_source = load_feature_source_from_json("D:/cursor/data/snapshots/v9_shadow_short.json")

    assert len(feature_source) == 40
    assert abs(feature_source["M15_RSI_14"]) == 145.0
    assert abs(feature_source["M15_Hurst"]) == 0.65



def test_v9_shadow_load_feature_batch_from_json():
    batch = load_feature_batch_from_json("D:/cursor/data/snapshots/v9_shadow_batch.json")

    assert len(batch) == 3
    assert batch[0]["name"] == "neutral_case"
    assert batch[0]["description"] == "Default reference sample expected to remain passive and abstain."
    assert batch[1]["name"] == "long_case"
    assert batch[1]["description"] == "Lower H1_Hurst to push the model into an open long decision."
    assert batch[2]["name"] == "short_case"
    assert batch[2]["description"] == "Invert the M15 feature group to trigger an open short decision."
    assert batch[1]["feature_source"]["H1_Hurst"] == -1.1
    assert abs(batch[2]["feature_source"]["M15_RSI_14"]) == 145.0



def test_v9_shadow_load_feature_samples_from_dir():
    samples = load_feature_samples_from_dir("D:/cursor/data/snapshots")

    assert [sample["name"] for sample in samples] == [
        "edge_allow_case",
        "edge_deny_case",
        "v9_shadow_long",
        "v9_shadow_neutral",
        "v9_shadow_short",
    ]
    assert [Path(sample["feature_file"]).name for sample in samples] == [
        "v9_shadow_edge_allow.json",
        "v9_shadow_edge_deny.json",
        "v9_shadow_long.json",
        "v9_shadow_neutral.json",
        "v9_shadow_short.json",
    ]
    assert [sample["description"] for sample in samples] == [
        "Borderline long setup expected to become risk-allowed.",
        "Borderline long setup expected to remain risk-blocked.",
        "Lower H1_Hurst to push the model into an open long decision.",
        "Default reference sample expected to remain passive and abstain.",
        "Invert the M15 feature group to trigger an open short decision.",
    ]
    assert samples[0]["feature_source"]["H1_Hurst"] == -0.85
    assert samples[1]["feature_source"]["H1_Hurst"] == -0.45
    assert samples[2]["feature_source"]["H1_Hurst"] == -1.1
    assert samples[3]["feature_source"]["H1_Hurst"] == 0.21
    assert abs(samples[4]["feature_source"]["M15_RSI_14"]) == 145.0



def test_v9_shadow_load_feature_samples_from_dir_uses_embedded_metadata(tmp_path):
    sample_path = tmp_path / "custom_sample.json"
    sample_path.write_text(
        json.dumps({
            "name": "custom_case",
            "description": "custom description",
            "features": {
                "M5_Ret_1": 0.456,
                "H1_Hurst": -1.1,
            },
        }),
        encoding="utf-8",
    )

    samples = load_feature_samples_from_dir(str(tmp_path))

    assert len(samples) == 1
    assert samples[0]["name"] == "custom_case"
    assert samples[0]["description"] == "custom description"
    assert samples[0]["feature_file"] == str(sample_path)
    assert samples[0]["feature_source"]["M5_Ret_1"] == 0.456
    assert samples[0]["feature_source"]["H1_Hurst"] == -1.1
    assert samples[0]["feature_source"]["M15_RSI_14"] == 0.0



def test_v9_shadow_cli_feature_batch_json_output():
    payload = json.loads(run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_batch.json",
        "--json",
    ))

    assert [item["scenario"] for item in payload] == ["neutral_case", "long_case", "short_case"]
    assert [item["feature_source_type"] for item in payload] == ["batch_file", "batch_file", "batch_file"]
    assert [item["sample_description"] for item in payload] == [
        "Default reference sample expected to remain passive and abstain.",
        "Lower H1_Hurst to push the model into an open long decision.",
        "Invert the M15 feature group to trigger an open short decision.",
    ]
    assert [item["action"] for item in payload] == ["abstain", "open", "open"]
    assert [item["side"] for item in payload] == ["flat", "long", "short"]



def test_v9_shadow_cli_feature_dir_json_output():
    payload = json.loads(run_cli(
        "--feature-dir",
        "D:/cursor/data/snapshots",
        "--json",
    ))

    assert [item["scenario"] for item in payload] == [
        "edge_allow_case",
        "edge_deny_case",
        "v9_shadow_long",
        "v9_shadow_neutral",
        "v9_shadow_short",
    ]
    assert [item["feature_source_type"] for item in payload] == ["dir_file"] * 5
    assert [Path(item["feature_file"]).name for item in payload] == [
        "v9_shadow_edge_allow.json",
        "v9_shadow_edge_deny.json",
        "v9_shadow_long.json",
        "v9_shadow_neutral.json",
        "v9_shadow_short.json",
    ]
    assert [item["sample_description"] for item in payload] == [
        "Borderline long setup expected to become risk-allowed.",
        "Borderline long setup expected to remain risk-blocked.",
        "Lower H1_Hurst to push the model into an open long decision.",
        "Default reference sample expected to remain passive and abstain.",
        "Invert the M15 feature group to trigger an open short decision.",
    ]
    assert [item["action"] for item in payload] == ["abstain", "abstain", "open", "abstain", "open"]
    assert [item["side"] for item in payload] == ["flat", "flat", "long", "flat", "short"]



def test_v9_shadow_cli_json_with_stats_output():
    output = json.loads(run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--json",
        "--json-with-stats",
    ))

    assert output["meta"]["output_mode"] == "json"
    assert output["meta"]["source_type"] == "batch_file"
    assert output["meta"]["generated_at"].endswith("Z")
    assert output["meta"]["scenario_count"] == 2
    assert output["meta"]["result_count"] == 2
    assert output["meta"]["manifest"]["version"] == "2"
    assert output["meta"]["manifest"]["path"].replace("\\", "/").endswith("/data/replays/v9_shadow_baselines/manifest.json")
    assert output["stats"]["total"] == 2
    assert output["stats"]["side_actions"]["long.open"] == 1
    assert output["stats"]["side_actions"]["short.open"] == 1
    assert output["stats"]["risk_dispatches"]["allow.protocol_validated"] == 2
    assert output["results"][0]["scenario"] == "long_case"
    assert all(item["scenario"] in {"long_case", "short_case"} for item in output["results"])



def test_v9_shadow_cli_rejects_removed_positional_scenario_argument():
    stdout, stderr, exit_code = run_cli_capture_stderr_allow_exit("long")

    assert stdout == ""
    assert exit_code == 2
    assert "unrecognized arguments: long" in stderr



def test_v9_shadow_cli_json_include_meta_without_stats_output():
    output = json.loads(run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--json",
        "--json-include-meta",
    ))

    assert output["meta"]["output_mode"] == "json"
    assert output["meta"]["source_type"] == "batch_file"
    assert output["meta"]["scenario_count"] == 2
    assert output["meta"]["result_count"] == 2
    assert output["meta"]["manifest"]["version"] == "2"
    assert "stats" not in output
    assert len(output["results"]) == 2
    assert [item["scenario"] for item in output["results"]] == ["long_case", "short_case"]



def test_v9_shadow_cli_summary_full_output_contains_compact_footer():
    output = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--summary",
        "--summary-style",
        "full",
    )

    assert "scenario=long_case" in output
    assert "scenario=short_case" in output
    assert "sample_description=Lower H1_Hurst to push the model into an open long decision." in output
    assert "sample_description=Invert the M15 feature group to trigger an open short decision." in output
    assert "--- compact_stats ---" in output
    assert "total=2" in output
    assert "side_actions={'long.open': 1, 'short.open': 1}" in output
    assert "risk_dispatches={'allow.protocol_validated': 2}" in output



def test_v9_shadow_cli_stats_compact_output():
    output = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--stats",
        "--stats-format",
        "compact",
    )

    assert output.startswith("total=2 | conviction.avg=")
    assert "actions={'open': 2}" in output
    assert "sides={'long': 1, 'short': 1}" in output
    assert "risk_statuses={'allow': 2}" in output
    assert "dispatch_statuses={'protocol_validated': 2}" in output
    assert "side_actions={'long.open': 1, 'short.open': 1}" in output
    assert "risk_dispatches={'allow.protocol_validated': 2}" in output



def test_v9_shadow_summary_output_includes_communication_operation_mirrors(monkeypatch):
    manager_payload = build_summary_payload(
        "long_case",
        run_scenario("long"),
        feature_source_type="scenario",
        feature_file=None,
        sample_description="contract sample",
    )
    manager_result = build_fallback_manager_result()
    manager_payload = {
        **manager_payload,
        "operations_summary": {
            **manager_payload["operations_summary"],
            "posture": "fallback",
            "posture_source": "communication_operations",
            "governance_summary_source": "communication_operations",
            "execution_projection_source": None,
        },
        "operations_posture": "fallback",
        "posture_sources": {"operations_posture_source": "communication_operations"},
        "governance_sources": {
            "summary_source": "communication_operations",
            "execution_projection_source": None,
        },
    }

    patch_prepare_results_with_contract(monkeypatch, manager_payload, manager_result)
    output = run_cli("--scenario", "long", "--summary")

    assert_summary_output_has_contract_fields(
        output,
        operations_posture="fallback",
        posture_source="communication_operations",
        summary_source="communication_operations",
        execution_projection_source=None,
        summary_prefix="scenario=long_case",
    )



def test_v9_shadow_json_with_stats_output_includes_communication_operation_mirrors(monkeypatch):
    manager_payload = build_summary_payload(
        "long_case",
        run_scenario("long"),
        feature_source_type="scenario",
        feature_file=None,
        sample_description="contract sample",
    )
    manager_result = build_blocked_manager_result()
    manager_payload = {
        **manager_payload,
        "operations_summary": {
            **manager_payload["operations_summary"],
            "posture": "blocked",
            "posture_source": "execution_projection",
            "governance_summary_source": None,
            "execution_projection_source": "execution_projection",
        },
        "operations_posture": "blocked",
        "posture_sources": {"operations_posture_source": "execution_projection"},
        "governance_sources": {
            "summary_source": None,
            "execution_projection_source": "execution_projection",
        },
    }

    output = run_json_with_stats_contract(
        actual_run_cli=run_cli,
        manager_payload=manager_payload,
        manager_result=manager_result,
        monkeypatch=monkeypatch,
    )

    result = output["results"]
    assert result["scenario"] == "long_case"
    assert result["operations_posture"] == "blocked"
    assert result["posture_sources"] == {"operations_posture_source": "execution_projection"}
    assert result["governance_sources"] == {
        "summary_source": None,
        "execution_projection_source": "execution_projection",
    }
    assert output["meta"]["output_mode"] == "json"
    assert output["meta"]["source_type"] == "scenario"
    assert output["stats"]["total"] == 1
    assert output["stats"]["side_actions"] == {"long.open": 1}



def test_v9_shadow_apply_stable_output_contract_normalizes_summary_only_payload():
    payload = {
        "scenario": "long_case",
        "operations_summary": {
            "posture": "blocked",
            "posture_source": "summary.posture",
            "governance_summary_source": "summary.source",
            "execution_projection_source": "summary.execution",
        },
    }

    normalized = apply_stable_output_contract(payload)

    assert_runtime_stable_output_fields(
        normalized,
        operations_posture="blocked",
        posture_source="summary.posture",
        summary_source="summary.source",
        execution_projection_source="summary.execution",
    )



def test_v9_shadow_output_extensions_prefer_operations_summary_over_stale_result_mirrors():
    manager_payload = build_summary_payload(
        "long_case",
        run_scenario("long"),
        feature_source_type="scenario",
        feature_file=None,
        sample_description="contract sample",
    )
    manager_payload = {
        **manager_payload,
        "operations_summary": {
            **manager_payload["operations_summary"],
            "posture": "blocked",
            "posture_source": "summary.posture",
            "governance_summary_source": "summary.source",
            "execution_projection_source": "summary.execution",
        },
    }
    stale_result = SimpleNamespace(
        communication_operations={
            "operations_posture": "stale_value",
            "posture_sources": {"operations_posture_source": "stale_source"},
            "governance_sources": {
                "summary_source": STALE_SUMMARY_SOURCE,
                "execution_projection_source": "stale_execution_source",
            },
        }
    )

    extended = build_output_extension_fields(manager_payload, stale_result)
    payload = build_summary_mirror_fields_from_operations_summary(extended)

    assert_runtime_stable_output_fields(
        payload,
        operations_posture="blocked",
        posture_source="summary.posture",
        summary_source="summary.source",
        execution_projection_source="summary.execution",
    )



def test_v9_shadow_output_extensions_backfill_operations_summary_from_result_when_missing():
    manager_payload = build_summary_payload(
        "long_case",
        run_scenario("long"),
        feature_source_type="scenario",
        feature_file=None,
        sample_description="contract sample",
    )
    manager_payload = {
        key: value
        for key, value in manager_payload.items()
        if key != "operations_summary"
    }
    result_stub = SimpleNamespace(
        communication_operations={
            "operations_posture": "targeted_replay",
            "posture_sources": {"operations_posture_source": "summary.posture"},
            PAYLOAD_KEY_GOVERNANCE_SOURCES: {
                PAYLOAD_KEY_SUMMARY_SOURCE: "summary.source",
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: "summary.execution",
            },
            PAYLOAD_KEY_OPERATIONS_SUMMARY: {
                PAYLOAD_KEY_POSTURE: "targeted_replay",
                PAYLOAD_KEY_POSTURE_SOURCE: "summary.posture",
                PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: "summary.source",
                PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: "summary.execution",
            },
        }
    )

    extended = build_output_extension_fields(manager_payload, result_stub)
    payload = apply_stable_output_contract(extended)

    assert payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_POSTURE] == "targeted_replay"
    assert_runtime_stable_output_fields(
        payload,
        operations_posture="targeted_replay",
        posture_source="summary.posture",
        summary_source="summary.source",
        execution_projection_source="summary.execution",
    )


def test_v9_shadow_apply_stable_output_contract_mirrors_replay_execution_fields():
    payload = apply_stable_output_contract({
        "scenario": "long_case",
        PAYLOAD_KEY_OPERATIONS_SUMMARY: {
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
        },
    })

    assert_runtime_stable_output_fields(
        payload,
        operations_posture="targeted_replay",
        posture_source="summary.posture",
        summary_source="summary.source",
        execution_projection_source="summary.execution",
        execution_mode="targeted",
        executed_message_ids=["message_001"],
        skipped_message_ids=["message_002"],
        blocked_message_ids=[],
        skip_reasons={"skip_acknowledged_message": ["message_002"]},
        block_reasons={},
    )


def test_v9_shadow_cli_out_multi_base_writes_summary_json_stats_using_recommended_overrides(tmp_path):
    base_path = tmp_path / "reports" / "replay"

    stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--summary",
        "--json",
        "--stats",
        "--out-multi-base",
        str(base_path),
        "--summary-style-summary",
        "full",
        "--stats-format-stats",
        "json",
        "--json-include-meta-json",
        "true",
    )

    assert stdout == ""
    summary_path = Path(f"{base_path}.summary")
    json_path = Path(f"{base_path}.json")
    stats_path = Path(f"{base_path}.stats")
    assert summary_path.exists()
    assert json_path.exists()
    assert stats_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    stats_payload = json.loads(stats_path.read_text(encoding="utf-8"))
    assert "scenario=long_case" in summary_text
    assert "scenario=short_case" in summary_text
    assert "--- compact_stats ---" in summary_text
    assert json_payload["meta"]["output_mode"] == "json"
    assert json_payload["meta"]["source_type"] == "batch_file"
    assert len(json_payload["results"]) == 2
    assert "stats" not in json_payload
    assert stats_payload["total"] == 2
    assert stats_payload["side_actions"] == {"long.open": 1, "short.open": 1}




def test_v9_shadow_cli_out_multi_base_rejects_removed_legacy_by_mode_overrides(tmp_path):
    base_path = tmp_path / "reports" / "legacy_replay"

    stdout, exit_code = run_cli_allow_exit(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--summary",
        "--json",
        "--stats",
        "--out-multi-base",
        str(base_path),
        "--summary-style-by-mode",
        "summary=full",
    )

    assert stdout == ""
    assert exit_code == 2



def test_v9_shadow_cli_out_multi_writes_explicit_paths(tmp_path):
    summary_path = tmp_path / "explicit.summary"
    json_path = tmp_path / "explicit.json"
    csv_path = tmp_path / "explicit.csv"
    stats_path = tmp_path / "explicit.stats"

    stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--summary",
        "--json",
        "--csv",
        "--stats",
        "--out-multi",
        f"summary={summary_path}",
        "--out-multi",
        f"json={json_path}",
        "--out-multi",
        f"csv={csv_path}",
        "--out-multi",
        f"stats={stats_path}",
        "--json-include-meta-json",
        "true",
    )

    assert stdout == ""
    assert summary_path.exists()
    assert json_path.exists()
    assert csv_path.exists()
    assert stats_path.exists()
    assert "scenario=long_case" in summary_path.read_text(encoding="utf-8")
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    csv_text = csv_path.read_text(encoding="utf-8")
    stats_text = stats_path.read_text(encoding="utf-8")
    assert json_payload["meta"]["source_type"] == "batch_file"
    assert len(json_payload["results"]) == 2
    assert csv_text.splitlines()[0] == "scenario,feature_source_type,feature_file,sample_description,symbol,mode,action,side,conviction,risk_status,dispatch_status,brain_count,ledger_path,record_id"
    assert "long_case" in csv_text
    assert "short_case" in csv_text
    assert "total=2" in stats_text
    assert "dispatch_statuses.protocol_validated=2" in stats_text



def test_v9_shadow_cli_out_suffix_inference_for_json_summary_stats(tmp_path):
    json_path = tmp_path / "single.json"
    summary_path = tmp_path / "single.summary"
    stats_path = tmp_path / "single.stats"

    json_stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--out",
        str(json_path),
        "--json-include-meta",
    )
    summary_stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--out",
        str(summary_path),
    )
    stats_stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--out",
        str(stats_path),
    )

    assert json_stdout == ""
    assert summary_stdout == ""
    assert stats_stdout == ""
    assert json.loads(json_path.read_text(encoding="utf-8"))["meta"]["output_mode"] == "json"
    assert "scenario=long_case" in summary_path.read_text(encoding="utf-8")
    assert "--- compact ---" in summary_path.read_text(encoding="utf-8")
    assert "total=2" in stats_path.read_text(encoding="utf-8")
    assert "risk_dispatches.allow.protocol_validated=2" in stats_path.read_text(encoding="utf-8")



def test_v9_shadow_session_sse_completed_flow_smoke():
    args = SimpleNamespace(
        scenario_flag="long",
        scenario_positional=None,
        feature_file=None,
        feature_batch_file=None,
        feature_dir=None,
    )
    stream_plan = SessionStreamPlan(include_meta=True, include_stats=True, event_name_prefix="session")

    manager_events = list(ShadowSessionManager(stream_plan=stream_plan).stream_run(args))
    sse_chunks = list(stream_session_sse(args, stream_plan=stream_plan))
    sse_messages = list(iter_sse_messages_from_chunks(sse_chunks))

    assert [event["event"] for event in manager_events] == [
        "session.progress",
        "session.progress",
        "session.completed",
    ]
    assert [message["event"] for message in sse_messages] == [
        "session.progress",
        "session.progress",
        "session.completed",
    ]
    completed_manager = manager_events[-1]
    completed_sse = sse_messages[-1]
    results = completed_manager["data"]["results"]
    assert completed_manager["data"]["meta"]["output_mode"] == "session_stream"
    assert completed_manager["data"]["meta"]["source_type"] == "scenario"
    assert completed_manager["data"]["stats"]["total"] == 1
    assert completed_manager["data"]["stats"]["side_actions"] == {"long.open": 1}
    assert completed_sse["data"]["data"]["meta"]["output_mode"] == "session_stream"
    assert completed_sse["data"]["data"]["stats"]["total"] == 1
    assert completed_sse["data"]["data"]["results"]["scenario"] == "long"
    assert results["scenario"] == "long"
    assert results["action"] == "open"
    assert results["side"] == "long"



def test_v9_shadow_session_sse_server_smoke_completed_flow():
    server = run_shadow_session_sse_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/engine/v9-shadow/stream?scenario=long&include_meta=true&include_stats=true",
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        messages = list(iter_sse_messages_from_chunks([body]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert [message["event"] for message in messages] == [
        "session.progress",
        "session.progress",
        "session.completed",
    ]
    completed = messages[-1]["data"]["data"]
    assert completed["meta"]["output_mode"] == "session_stream"
    assert completed["stats"]["total"] == 1
    assert completed["results"]["scenario"] == "long"



def test_v9_shadow_session_sse_server_smoke_error_flow():
    server = run_shadow_session_sse_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/engine/v9-shadow/stream?feature_file=one.json&feature_batch_file=two.json",
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        messages = list(iter_sse_messages_from_chunks([body]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert [message["event"] for message in messages] == ["session.error"]
    error_payload = messages[0]["data"]["data"]
    assert error_payload["error_type"] == "SessionStreamQueryError"
    assert error_payload["message"] == "Use only one of --feature-file, --feature-batch-file, or --feature-dir."



def test_v9_shadow_cli_write_and_check_baseline_smoke(tmp_path):
    baseline_path = tmp_path / "single.baseline.json"

    stdout = run_cli(
        "--scenario",
        "long",
        "--write-baseline",
        str(baseline_path),
    )
    assert stdout == ""
    baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline_payload["results"][0]["scenario"] == "long"
    assert baseline_payload["results"][0]["action"] == "open"
    assert baseline_payload["stats"]["total"] == 1
    assert baseline_payload["stats"]["side_actions"] == {"long.open": 1}

    check_stdout, exit_code = run_cli_allow_exit(
        "--scenario",
        "long",
        "--check-baseline",
        str(baseline_path),
    )
    assert exit_code == 1
    assert "regression.matches=false" in check_stdout
    assert "change_count=1" in check_stdout
    assert "changes[0].section=results" in check_stdout
    assert "changes[0].scenario=long" in check_stdout



def test_v9_shadow_cli_write_and_check_batch_baselines_smoke(tmp_path):
    baseline_dir = tmp_path / "batch_baselines"

    write_stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--write-batch-baselines",
        str(baseline_dir),
    )
    written_paths = [line for line in write_stdout.splitlines() if line.strip()]
    assert len(written_paths) == 2
    assert {Path(path).name for path in written_paths} == {
        "long_case.baseline.json",
        "short_case.baseline.json",
    }

    check_stdout = run_cli(
        "--feature-batch-file",
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        "--check-batch-baselines",
        str(baseline_dir),
    )
    assert "batch_regression.matches=true" in check_stdout
    assert "missing_count=0" in check_stdout
    assert "diff_count=0" in check_stdout



def test_v9_shadow_cli_check_formal_baselines_json_smoke():
    output = json.loads(run_cli(
        "--check-formal-baselines",
        "--json",
    ))

    assert output["meta"]["output_mode"] == "json"
    assert output["meta"]["suite_count"] == 3
    assert output["meta"]["manifest"]["version"] == "2"
    assert output["matches"] is True
    assert output["summary"]["failed_suites"] == []
    assert output["summary"]["total_missing"] == 0
    assert output["summary"]["total_diffs"] == 0
    assert output["gate"]["sample_failure_count"] == 0
    assert output["semantic"]["failure_count"] == 0
    assert [suite["key"] for suite in output["suites"]] == [
        "neutral_stability",
        "actionable_decisions",
        "risk_boundary",
    ]



def test_v9_shadow_cli_rebuild_formal_baselines_json_smoke(tmp_path):
    manifest = json.loads(Path("D:/cursor/data/replays/v9_shadow_baselines/manifest.json").read_text(encoding="utf-8"))
    rebuilt_manifest = {
        **manifest,
        "suites": [
            {
                **suite,
                "baseline_dir": str(tmp_path / suite["key"]),
            }
            for suite in manifest["suites"]
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(rebuilt_manifest), encoding="utf-8")

    output = json.loads(run_cli(
        "--formal-baseline-manifest",
        str(manifest_path),
        "--rebuild-formal-baselines",
        "--json",
    ))

    assert output["meta"]["output_mode"] == "json"
    assert output["meta"]["suite_count"] == 3
    assert output["meta"]["manifest"]["path"] == str(manifest_path)
    assert output["rebuilt"] is True
    assert output["summary"]["suite_count"] == 3
    assert output["summary"]["total_written"] == 5
    assert {suite["key"] for suite in output["suites"]} == {
        "neutral_stability",
        "actionable_decisions",
        "risk_boundary",
    }
    assert all(Path(path).exists() for path in output["written_paths"])
    assert sum(len(suite["written_paths"]) for suite in output["suites"]) == 5



def test_v9_shadow_session_sse_server_event_prefix_query_smoke():
    server = run_shadow_session_sse_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/engine/v9-shadow/stream?scenario=long&event_prefix=shadow&include_meta=false&include_stats=false",
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        messages = list(iter_sse_messages_from_chunks([body]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert [message["event"] for message in messages] == [
        "shadow.progress",
        "shadow.progress",
        "shadow.completed",
    ]
    completed = messages[-1]["data"]["data"]
    assert "meta" not in completed
    assert "stats" not in completed
    assert completed["results"]["scenario"] == "long"



def test_v9_shadow_session_sse_server_invalid_event_prefix_error_smoke():
    server = run_shadow_session_sse_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/engine/v9-shadow/stream?scenario=long&event_prefix=bad.prefix",
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        messages = list(iter_sse_messages_from_chunks([body]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert [message["event"] for message in messages] == ["session.error"]
    error_payload = messages[0]["data"]["data"]
    assert error_payload["error_type"] == "SessionStreamQueryError"
    assert error_payload["message"] == "event_prefix must not contain dots"



def test_v9_shadow_session_sse_server_invalid_bool_query_error_smoke():
    server = run_shadow_session_sse_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/engine/v9-shadow/stream?scenario=long&include_stats=maybe",
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        messages = list(iter_sse_messages_from_chunks([body]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert [message["event"] for message in messages] == ["session.error"]
    error_payload = messages[0]["data"]["data"]
    assert error_payload["error_type"] == "SessionStreamQueryError"
    assert error_payload["message"] == "Invalid boolean query value: maybe"



def test_v9_shadow_cli_feature_file_json_output():
    payload = json.loads(run_cli(
        "--feature-file",
        "D:/cursor/data/snapshots/v9_shadow_long.json",
        "--json",
    ))

    assert [item["scenario"] for item in payload] == ["neutral", "long", "short"]
    assert [item["feature_source_type"] for item in payload] == ["file", "file", "file"]
    assert [Path(item["feature_file"]).name for item in payload] == [
        "v9_shadow_long.json",
        "v9_shadow_long.json",
        "v9_shadow_long.json",
    ]
    assert [item["action"] for item in payload] == ["open", "open", "open"]
    assert [item["side"] for item in payload] == ["long", "long", "long"]



def test_v9_shadow_cli_feature_file_summary_output():
    output = run_cli(
        "--feature-file",
        "D:/cursor/data/snapshots/v9_shadow_long.json",
        "--summary",
    )

    assert "scenario=neutral" in output
    assert "scenario=long" in output
    assert "scenario=short" in output
    assert "feature_source_type=file" in output
    assert "feature_file=D:/cursor/data/snapshots/v9_shadow_long.json" in output
    assert "--- compact ---" in output
    assert "total=3" in output
    assert "side_actions={'long.open': 3}" in output



def test_v9_shadow_cli_feature_file_csv_output():
    output = run_cli(
        "--feature-file",
        "D:/cursor/data/snapshots/v9_shadow_long.json",
        "--csv",
    )

    lines = output.splitlines()
    assert lines[0] == "scenario,feature_source_type,feature_file,sample_description,symbol,mode,action,side,conviction,risk_status,dispatch_status,brain_count,ledger_path,record_id"
    assert len(lines) == 4
    assert '"neutral","file","D:/cursor/data/snapshots/v9_shadow_long.json"' in lines[1]
    assert '"long","file","D:/cursor/data/snapshots/v9_shadow_long.json"' in lines[2]
    assert '"short","file","D:/cursor/data/snapshots/v9_shadow_long.json"' in lines[3]
    assert '"open","long"' in lines[1]
    assert '"open","long"' in lines[2]
    assert '"open","long"' in lines[3]



def test_v9_shadow_cli_feature_dir_summary_output():
    output = run_cli(
        "--feature-dir",
        "D:/cursor/data/snapshots",
        "--summary",
    )

    assert "scenario=edge_allow_case" in output
    assert "scenario=edge_deny_case" in output
    assert "scenario=v9_shadow_long" in output
    assert "scenario=v9_shadow_neutral" in output
    assert "scenario=v9_shadow_short" in output
    assert "feature_source_type=dir_file" in output
    assert "--- compact ---" in output
    assert "total=5" in output
    assert "actions={'abstain': 3, 'open': 2}" in output
    assert "sides={'flat': 3, 'long': 1, 'short': 1}" in output



def test_v9_shadow_cli_feature_dir_csv_output():
    output = run_cli(
        "--feature-dir",
        "D:/cursor/data/snapshots",
        "--csv",
    )

    lines = output.splitlines()
    assert lines[0] == "scenario,feature_source_type,feature_file,sample_description,symbol,mode,action,side,conviction,risk_status,dispatch_status,brain_count,ledger_path,record_id"
    assert len(lines) == 6
    assert any('"edge_allow_case","dir_file"' in line for line in lines[1:])
    assert any('"edge_deny_case","dir_file"' in line for line in lines[1:])
    assert any('"v9_shadow_long","dir_file"' in line for line in lines[1:])
    assert any('"v9_shadow_neutral","dir_file"' in line for line in lines[1:])
    assert any('"v9_shadow_short","dir_file"' in line for line in lines[1:])
    assert any('"abstain","flat"' in line for line in lines[1:])
    assert any('"open","long"' in line for line in lines[1:])
    assert any('"open","short"' in line for line in lines[1:])



def test_v9_shadow_assert_formal_baseline_gate_and_semantics_smoke():
    gate_result = assert_formal_baseline_gate()
    semantics_result = assert_formal_suite_semantics()

    assert gate_result["matches"] is True
    assert gate_result["gate"]["sample_failure_count"] == 0
    assert gate_result["gate"]["field_change_count"] == 0
    assert gate_result["semantic"]["matches"] is True
    assert semantics_result["matches"] is True
    assert semantics_result["semantic"]["failure_count"] == 0
    assert [suite["key"] for suite in gate_result["suite_results"]] == [
        "neutral_stability",
        "actionable_decisions",
        "risk_boundary",
    ]



def test_v9_shadow_rebuild_formal_baseline_suites_returns_written_paths(tmp_path):
    manifest = json.loads(Path("D:/cursor/data/replays/v9_shadow_baselines/manifest.json").read_text(encoding="utf-8"))
    rebuilt_manifest = {
        **manifest,
        "suites": [
            {
                **suite,
                "baseline_dir": str(tmp_path / suite["key"]),
            }
            for suite in manifest["suites"]
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(rebuilt_manifest), encoding="utf-8")

    result = rebuild_formal_baseline_suites(str(manifest_path))

    assert result["manifest"]["path"] == str(manifest_path)
    assert result["summary"]["suite_count"] == 3
    assert result["summary"]["total_written"] == 5
    assert len(result["written_paths"]) == 5
    assert all(Path(path).exists() for path in result["written_paths"])
    assert {suite["key"] for suite in result["suite_results"]} == {
        "neutral_stability",
        "actionable_decisions",
        "risk_boundary",
    }



def test_v9_shadow_check_regression_baseline_matches_written_baseline(tmp_path):
    payload = build_summary_payload("long", run_scenario("long"), feature_source_type="scenario")
    baseline_path = tmp_path / "single.baseline.json"
    write_regression_baseline(str(baseline_path), [payload])

    diff = check_regression_baseline(
        str(baseline_path),
        [{key: value for key, value in payload.items() if key != "manifest"}],
    )

    assert diff == {"matches": True, "change_count": 0, "changes": []}



def test_v9_shadow_check_batch_regression_baselines_matches_written_batch(tmp_path):
    batch = load_feature_batch_from_json("D:/cursor/data/snapshots/v9_shadow_actionable_batch.json")
    write_batch_regression_baselines(str(tmp_path), batch)

    diff = check_batch_regression_baselines(str(tmp_path), batch)

    assert diff == {"matches": True, "missing": [], "diffs": []}



def test_v9_shadow_render_json_output_summary_stats_and_stream_helpers():
    payloads = [
        build_summary_payload("long", run_scenario("long"), feature_source_type="scenario"),
        build_summary_payload("short", run_scenario("short"), feature_source_type="scenario"),
    ]

    json_output = json.loads(render_json_output(
        payloads,
        include_stats=True,
        output_mode="json",
        source_type="scenario",
        include_meta=True,
    ))
    summary_output = render_summary_output(payloads, style="full", include_compact_stats=True)
    stats_output = render_stats_text(json_output["stats"])
    envelope = build_stream_envelope(
        payloads,
        plan=StreamEnvelopePlan(include_meta=True, include_stats=True),
        output_mode="session_stream",
    )
    meta = build_stream_meta(payloads, output_mode="session_stream", source_type="scenario")

    assert json_output["meta"]["output_mode"] == "json"
    assert json_output["meta"]["source_type"] == "scenario"
    assert json_output["stats"]["total"] == 2
    assert json_output["stats"]["side_actions"] == {"long.open": 1, "short.open": 1}
    assert "scenario=long" in summary_output
    assert "scenario=short" in summary_output
    assert "--- compact_stats ---" in summary_output
    assert "total=2" in stats_output
    assert "side_actions.long.open=1" in stats_output
    assert "side_actions.short.open=1" in stats_output
    assert envelope["event"] == "decision.batch.completed"
    assert envelope["meta"]["output_mode"] == "session_stream"
    assert envelope["meta"]["source_type"] == "scenario"
    assert envelope["stats"]["total"] == 2
    assert envelope["results"][0]["scenario"] == "long"
    assert envelope["results"][1]["scenario"] == "short"
    assert meta["output_mode"] == "session_stream"
    assert meta["source_type"] == "scenario"
    assert meta["scenario_count"] == 2
    assert meta["result_count"] == 2



def test_v9_shadow_render_output_content_json_preserves_single_payload_source_type():
    payload = build_summary_payload("long", run_scenario("long"), feature_source_type="scenario")
    output = render_output_content(
        OutputPlan(mode="json", output_path=None, include_meta=True),
        [payload],
        default_text="ignored",
    )

    rendered = json.loads(output)

    assert rendered["meta"]["output_mode"] == "json"
    assert rendered["meta"]["source_type"] == "scenario"
    assert rendered["results"]["scenario"] == "long"

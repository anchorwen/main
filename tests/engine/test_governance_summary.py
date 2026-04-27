"""Governance summary helper contract tests."""

import importlib

from core.deployment.domain_keys import (
    COMPLIANCE_LEVEL_PASS,
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_STATUS,
)
from core.deployment.governance_summary import (
    build_governance_summary,
    count_governance_warnings,
    extract_governance_summary,
)


def test_extract_governance_summary_defaults_when_payload_missing():
    summary = extract_governance_summary(None)
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_extract_governance_summary_defaults_when_keys_absent():
    summary = extract_governance_summary({"other": 1})
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_extract_governance_summary_preserves_existing_values():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [
            {"name": "registry_deep_validation_present", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN}
        ],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 1,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == payload[PAYLOAD_KEY_GOVERNANCE_FOCUS]
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1


def test_extract_governance_summary_normalizes_invalid_types():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: "invalid",
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: "2",
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_extract_governance_summary_filters_non_dict_focus_items():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [
            {"name": "ok"},
            "bad",
            123,
            {PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN},
        ],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 2,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == [{"name": "ok"}, {PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN}]
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1


def test_extract_governance_summary_counts_warn_from_status_field():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [
            {"control_id": "GOV-003", PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_WARN},
            {"control_id": "GOV-004", PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_PASS},
        ],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 999,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1


def test_extract_governance_summary_counts_mixed_warn_fields():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [
            {"name": "registry_deep_validation_present", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN},
            {"control_id": "GOV-004", PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_WARN},
            {"name": "ok", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_PASS},
        ],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 0,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 2


def test_extract_governance_summary_treats_either_level_or_status_as_warn():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [
            {"name": "a", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_PASS, PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_WARN},
            {"name": "b", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN, PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_PASS},
            {"name": "c", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_PASS, PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_PASS},
        ],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 0,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 2


def test_extract_governance_summary_falls_back_for_non_numeric_warning_count():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: "NaN",
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_extract_governance_summary_clamps_negative_warning_count():
    payload = {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: [],
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: -3,
    }
    summary = extract_governance_summary(payload)
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_build_governance_summary_normalizes_invalid_focus_and_warning_count():
    summary = build_governance_summary(focus="invalid", warning_count="3")
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_build_governance_summary_clamps_negative_warning_count():
    summary = build_governance_summary(
        focus=[{"name": "registry_deep_validation_present", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN}],
        warning_count=-9,
    )
    assert len(summary[PAYLOAD_KEY_GOVERNANCE_FOCUS]) == 1
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1


def test_build_governance_summary_filters_non_dict_focus_items():
    summary = build_governance_summary(
        focus=[{"name": "keep"}, "drop", None],
        warning_count=1,
    )
    assert summary[PAYLOAD_KEY_GOVERNANCE_FOCUS] == [{"name": "keep"}]
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_build_governance_summary_ignores_external_warning_count():
    summary = build_governance_summary(
        focus=[
            {"name": "a", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN},
            {"name": "b", PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_PASS},
        ],
        warning_count=999,
    )
    assert summary[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1


def test_count_governance_warnings_handles_level_and_status_fields():
    focus = [
        {"name": "a", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_WARN},
        {"name": "b", PAYLOAD_KEY_STATUS: COMPLIANCE_LEVEL_WARN},
        {"name": "c", PAYLOAD_KEY_LEVEL: COMPLIANCE_LEVEL_PASS},
    ]
    assert count_governance_warnings(focus) == 2


def test_governance_summary_module_exports_public_api():
    mod = importlib.import_module("core.deployment.governance_summary")
    assert set(mod.__all__) == {
        "build_governance_summary",
        "count_governance_warnings",
        "extract_governance_summary",
    }
    for name in mod.__all__:
        assert callable(getattr(mod, name))

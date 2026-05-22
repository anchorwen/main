"""Validation mode default and override precedence tests."""

from core.contracts.domain_keys import (
    COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
    COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
    VALIDATION_MODE_DEEP,
    VALIDATION_MODE_FAST,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer


def _container(tmp_path, *, validation_mode: str):
    cfg = EnvironmentConfig.development(str(tmp_path), validation_mode=validation_mode)
    return ServiceContainer(cfg).build()


def test_container_default_validation_mode_applies_to_readiness(tmp_path):
    c = _container(tmp_path, validation_mode=VALIDATION_MODE_FAST)
    report = c.release_readiness.build_report()
    names = {check["name"] for check in report["checks"]}
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED not in names
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR not in names


def test_explicit_validation_mode_overrides_container_default(tmp_path):
    c = _container(tmp_path, validation_mode=VALIDATION_MODE_FAST)
    report = c.release_readiness.build_report(validation_mode=VALIDATION_MODE_DEEP)
    names = {check["name"] for check in report["checks"]}
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED in names
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR in names


def test_runbook_preflight_respects_default_and_explicit_override(tmp_path):
    c = _container(tmp_path, validation_mode=VALIDATION_MODE_FAST)
    fast_result = c.runbook_engine.preflight()
    fast_names = {check["name"] for check in fast_result["checks"]}
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED not in fast_names

    deep_result = c.runbook_engine.preflight(validation_mode=VALIDATION_MODE_DEEP)
    deep_names = {check["name"] for check in deep_result["checks"]}
    assert COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED in deep_names

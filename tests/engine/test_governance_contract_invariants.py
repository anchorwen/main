"""Cross-service governance summary contract invariants."""

from core.contracts.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.governance_summary import count_governance_warnings
from core.deployment.service_container import ServiceContainer


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


def _assert_governance_shape(payload: dict):
    focus = payload[PAYLOAD_KEY_GOVERNANCE_FOCUS]
    warning_count = payload[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT]
    assert isinstance(focus, list)
    assert all(isinstance(item, dict) for item in focus)
    assert isinstance(warning_count, int)
    assert warning_count >= 0
    assert warning_count == count_governance_warnings(focus)


def test_governance_contract_invariants_across_outputs(tmp_path):
    c = _container(tmp_path / "data")
    pipeline = c.release_pipeline.run(version="9.0.0", output_dir=str(tmp_path / "pipeline"))
    cert = c.release_certification.certify(pipeline_summary=pipeline)
    c.release_registry.register(cert)

    readiness = c.release_readiness.build_report()
    ops_maturity = c.ops_maturity.evaluate()
    postmortem = c.postmortem_report.generate(incident_id="inv-1")
    final_audit = c.final_audit.build_report()
    compliance_audit = c.compliance_audit.generate()
    compliance_matrix = c.compliance_control_matrix.generate()
    registry_summary = c.release_registry.summarize()
    evidence = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="inv")

    _assert_governance_shape(readiness["summary"])
    _assert_governance_shape(ops_maturity["summary"])
    _assert_governance_shape(postmortem["summary"])
    _assert_governance_shape(final_audit["summary"])
    _assert_governance_shape(compliance_audit["summary"])
    _assert_governance_shape(compliance_matrix["summary"])
    _assert_governance_shape(registry_summary)
    _assert_governance_shape(cert)

    manifest = c.evidence_bundle.verify_bundle(evidence["manifest_path"])
    assert manifest["verified"] is True

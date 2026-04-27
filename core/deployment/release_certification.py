"""Release certification generation.

Creates a final audit certificate from a release pipeline summary and
verifies key artifact existence/checksums.
"""
from datetime import datetime
from pathlib import Path
import hashlib
import json

from core.deployment.domain_keys import (
    ARTIFACT_DEPLOYMENT_EXECUTION,
    ARTIFACT_DEPLOYMENT_PLAN,
    ARTIFACT_EVIDENCE_MANIFEST,
    ARTIFACT_EVIDENCE_SUMMARY,
    ARTIFACT_FINAL_AUDIT,
    ARTIFACT_GATE,
    ARTIFACT_OPERATIONS_TIMELINE,
    ARTIFACT_OPS_MATURITY,
    ARTIFACT_POSTMORTEM_REPORT,
    ARTIFACT_RELEASE_PIPELINE,
    ARTIFACT_ROLLBACK_DRILL,
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    PAYLOAD_KEY_ACTUAL_FINGERPRINT,
    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE,
    PAYLOAD_KEY_ALPHA_COUNT,
    PAYLOAD_KEY_APPROVER,
    PAYLOAD_KEY_ARTIFACT,
    PAYLOAD_KEY_ARTIFACT_CHECKS,
    PAYLOAD_KEY_ARTIFACTS,
    PAYLOAD_KEY_CERTIFICATE_FINGERPRINT,
    PAYLOAD_KEY_CERTIFICATE_PATH,
    PAYLOAD_KEY_CERTIFIED,
    PAYLOAD_KEY_EVIDENCE_VERIFICATION,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_EXISTS,
    PAYLOAD_KEY_EXPECTED_FINGERPRINT,
    PAYLOAD_KEY_FINAL_AUDIT_EVIDENCE,
    PAYLOAD_KEY_FINDING_COUNT,
    PAYLOAD_KEY_FINDINGS,
    PAYLOAD_KEY_GRADE,
    PAYLOAD_KEY_ISSUED_AT,
    PAYLOAD_KEY_MANIFEST_PATH,
    PAYLOAD_KEY_MATURITY_SCORE,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_OPS_MATURITY_EVIDENCE,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_PATH,
    PAYLOAD_KEY_PIPELINE,
    PAYLOAD_KEY_PRESENT,
    PAYLOAD_KEY_READY_FOR_PRODUCTION,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SECTION,
    PAYLOAD_KEY_SECTIONS,
    PAYLOAD_KEY_SHA256,
    PAYLOAD_KEY_SIZE_BYTES,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STRATEGY,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_USAGE_DATE,
    PAYLOAD_KEY_VALID,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERIFIED,
    PAYLOAD_KEY_VERSION,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNINGS,
    RELEASE_CERTIFICATION_DEFAULT_APPROVER,
    RELEASE_CERTIFICATION_MISSING_EVIDENCE_MANIFEST,
    RELEASE_CERTIFICATION_STATUS_CERTIFIED,
    RELEASE_CERTIFICATION_STATUS_REJECTED,
    RELEASE_PIPELINE_KEY_ARTIFACTS,
    RELEASE_PIPELINE_KEY_PASSED,
    RELEASE_PIPELINE_KEY_STRATEGY,
    RELEASE_PIPELINE_KEY_SUMMARY,
    RELEASE_PIPELINE_KEY_VERSION,
)
from core.deployment.schema_versions import (
    SCHEMA_RELEASE_CERTIFICATE,
    SCHEMA_RELEASE_CERTIFICATE_VERIFICATION,
)
from core.deployment.governance_summary import extract_governance_summary


class ReleaseCertificationService:
    """Generates final release certificates from pipeline artifacts."""

    REQUIRED_ARTIFACTS = [
        ARTIFACT_GATE,
        ARTIFACT_EVIDENCE_SUMMARY,
        ARTIFACT_EVIDENCE_MANIFEST,
        ARTIFACT_DEPLOYMENT_PLAN,
        ARTIFACT_DEPLOYMENT_EXECUTION,
        ARTIFACT_ROLLBACK_DRILL,
        ARTIFACT_POSTMORTEM_REPORT,
        ARTIFACT_FINAL_AUDIT,
        ARTIFACT_OPS_MATURITY,
        ARTIFACT_OPERATIONS_TIMELINE,
        ARTIFACT_RELEASE_PIPELINE,
    ]

    def __init__(self, container):
        self._container = container

    def certify(
        self,
        *,
        pipeline_summary: str | dict,
        approver: str = RELEASE_CERTIFICATION_DEFAULT_APPROVER,
        output: str | None = None,
    ) -> dict:
        pipeline = self._load_pipeline(pipeline_summary)
        artifact_checks = self._verify_artifacts(pipeline)
        evidence_verification = self._verify_evidence_manifest(pipeline)
        alpha_budget_evidence = self._alpha_budget_evidence(pipeline, evidence_verification)
        final_audit_evidence = self._final_audit_evidence(pipeline)
        ops_maturity_evidence = self._ops_maturity_evidence(pipeline)
        approved = (
            pipeline.get(RELEASE_PIPELINE_KEY_PASSED) is True
            and all(item[PAYLOAD_KEY_VALID] for item in artifact_checks)
            and evidence_verification.get(PAYLOAD_KEY_VERIFIED, False) is True
        )
        certificate = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_CERTIFICATE,
            PAYLOAD_KEY_ISSUED_AT: datetime.utcnow().isoformat(),
            PAYLOAD_KEY_APPROVER: approver,
            PAYLOAD_KEY_VERSION: pipeline.get(RELEASE_PIPELINE_KEY_VERSION),
            PAYLOAD_KEY_STRATEGY: pipeline.get(RELEASE_PIPELINE_KEY_STRATEGY),
            PAYLOAD_KEY_VALIDATION_MODE: pipeline.get(PAYLOAD_KEY_VALIDATION_MODE),
            PAYLOAD_KEY_CERTIFIED: approved,
            PAYLOAD_KEY_STATUS: RELEASE_CERTIFICATION_STATUS_CERTIFIED if approved else RELEASE_CERTIFICATION_STATUS_REJECTED,
            PAYLOAD_KEY_PIPELINE: {
                PAYLOAD_KEY_STATUS: pipeline.get(PAYLOAD_KEY_STATUS),
                RELEASE_PIPELINE_KEY_PASSED: pipeline.get(RELEASE_PIPELINE_KEY_PASSED),
                PAYLOAD_KEY_VALIDATION_MODE: pipeline.get(PAYLOAD_KEY_VALIDATION_MODE),
                RELEASE_PIPELINE_KEY_SUMMARY: pipeline.get(RELEASE_PIPELINE_KEY_SUMMARY, {}),
            },
            PAYLOAD_KEY_ARTIFACT_CHECKS: artifact_checks,
            PAYLOAD_KEY_EVIDENCE_VERIFICATION: evidence_verification,
            PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE: alpha_budget_evidence,
            PAYLOAD_KEY_FINAL_AUDIT_EVIDENCE: final_audit_evidence,
            PAYLOAD_KEY_OPS_MATURITY_EVIDENCE: ops_maturity_evidence,
            **extract_governance_summary(pipeline.get(RELEASE_PIPELINE_KEY_SUMMARY, {})),
            PAYLOAD_KEY_CERTIFICATE_FINGERPRINT: "",
        }
        certificate[PAYLOAD_KEY_CERTIFICATE_FINGERPRINT] = self._fingerprint(certificate)
        if output:
            certificate[PAYLOAD_KEY_OUTPUT_PATH] = self.save_certificate(certificate, output)
        return certificate

    def save_certificate(self, certificate: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(certificate, indent=2, default=str), encoding="utf-8")
        return str(target)

    def verify_certificate(self, certificate_path: str) -> dict:
        path = Path(certificate_path)
        certificate = json.loads(path.read_text(encoding="utf-8"))
        expected = certificate.get(PAYLOAD_KEY_CERTIFICATE_FINGERPRINT)
        candidate = dict(certificate)
        candidate[PAYLOAD_KEY_CERTIFICATE_FINGERPRINT] = ""
        candidate.pop(PAYLOAD_KEY_OUTPUT_PATH, None)
        actual = self._fingerprint(candidate)
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_CERTIFICATE_VERIFICATION,
            PAYLOAD_KEY_CERTIFICATE_PATH: str(path),
            PAYLOAD_KEY_VALIDATION_MODE: certificate.get(PAYLOAD_KEY_VALIDATION_MODE),
            PAYLOAD_KEY_VERIFIED: expected == actual,
            PAYLOAD_KEY_EXPECTED_FINGERPRINT: expected,
            PAYLOAD_KEY_ACTUAL_FINGERPRINT: actual,
            PAYLOAD_KEY_STATUS: certificate.get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_CERTIFIED: certificate.get(PAYLOAD_KEY_CERTIFIED, False),
        }

    def _load_pipeline(self, pipeline_summary: str | dict) -> dict:
        if isinstance(pipeline_summary, dict):
            return pipeline_summary
        return json.loads(Path(pipeline_summary).read_text(encoding="utf-8"))

    def _verify_artifacts(self, pipeline: dict) -> list[dict]:
        artifacts = pipeline.get(RELEASE_PIPELINE_KEY_ARTIFACTS, {})
        checks = []
        for name in self.REQUIRED_ARTIFACTS:
            raw_path = artifacts.get(name)
            path = Path(raw_path) if raw_path else None
            exists = bool(path and path.exists())
            checks.append({
                PAYLOAD_KEY_NAME: name,
                PAYLOAD_KEY_PATH: str(path) if path else None,
                PAYLOAD_KEY_EXISTS: exists,
                PAYLOAD_KEY_SHA256: self._sha256(path) if exists else None,
                PAYLOAD_KEY_SIZE_BYTES: path.stat().st_size if exists else 0,
                PAYLOAD_KEY_VALID: exists,
            })
        return checks

    def _verify_evidence_manifest(self, pipeline: dict) -> dict:
        manifest = pipeline.get(RELEASE_PIPELINE_KEY_ARTIFACTS, {}).get(ARTIFACT_EVIDENCE_MANIFEST)
        if not manifest:
            return {PAYLOAD_KEY_VERIFIED: False, PAYLOAD_KEY_ERROR: RELEASE_CERTIFICATION_MISSING_EVIDENCE_MANIFEST}
        try:
            return self._container.evidence_bundle.verify_bundle(manifest)
        except Exception as exc:
            return {PAYLOAD_KEY_VERIFIED: False, PAYLOAD_KEY_ERROR: str(exc)}

    def _alpha_budget_evidence(self, pipeline: dict, evidence_verification: dict) -> dict:
        manifest_path = pipeline.get(RELEASE_PIPELINE_KEY_ARTIFACTS, {}).get(ARTIFACT_EVIDENCE_MANIFEST)
        sections = [item.get(PAYLOAD_KEY_SECTION) for item in evidence_verification.get(PAYLOAD_KEY_RESULTS, [])]
        present = EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in sections
        artifact = None
        if manifest_path and present:
            manifest_dir = Path(manifest_path).parent
            artifact_path = manifest_dir / f"{EVIDENCE_SECTION_ALPHA_BUDGET_USAGE}.json"
            if artifact_path.exists():
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                artifact = {
                    PAYLOAD_KEY_PATH: str(artifact_path),
                    PAYLOAD_KEY_SCHEMA_VERSION: payload.get(PAYLOAD_KEY_SCHEMA_VERSION),
                    PAYLOAD_KEY_USAGE_DATE: payload.get(PAYLOAD_KEY_USAGE_DATE),
                    PAYLOAD_KEY_ALPHA_COUNT: payload.get(PAYLOAD_KEY_ALPHA_COUNT, 0),
                    PAYLOAD_KEY_WARNING_COUNT: payload.get(PAYLOAD_KEY_WARNING_COUNT, 0),
                    PAYLOAD_KEY_WARNINGS: payload.get(PAYLOAD_KEY_WARNINGS, []),
                    PAYLOAD_KEY_SHA256: self._sha256(artifact_path),
                    PAYLOAD_KEY_SIZE_BYTES: artifact_path.stat().st_size,
                }
        return {
            PAYLOAD_KEY_PRESENT: present,
            PAYLOAD_KEY_MANIFEST_PATH: manifest_path,
            PAYLOAD_KEY_SECTION: EVIDENCE_SECTION_ALPHA_BUDGET_USAGE if present else None,
            PAYLOAD_KEY_ARTIFACT: artifact,
        }

    def _final_audit_evidence(self, pipeline: dict) -> dict:
        artifact_path = pipeline.get(RELEASE_PIPELINE_KEY_ARTIFACTS, {}).get(ARTIFACT_FINAL_AUDIT)
        if not artifact_path:
            return {PAYLOAD_KEY_PRESENT: False, PAYLOAD_KEY_ARTIFACT: None}
        path = Path(artifact_path)
        if not path.exists():
            return {PAYLOAD_KEY_PRESENT: False, PAYLOAD_KEY_ARTIFACT: None}
        payload = json.loads(path.read_text(encoding="utf-8"))
        artifact = {
            PAYLOAD_KEY_PATH: str(path),
            PAYLOAD_KEY_SCHEMA_VERSION: payload.get(PAYLOAD_KEY_SCHEMA_VERSION),
            PAYLOAD_KEY_READY_FOR_PRODUCTION: payload.get(PAYLOAD_KEY_READY_FOR_PRODUCTION),
            PAYLOAD_KEY_FINDING_COUNT: len(payload.get(PAYLOAD_KEY_FINDINGS, [])),
            PAYLOAD_KEY_SHA256: self._sha256(path),
            PAYLOAD_KEY_SIZE_BYTES: path.stat().st_size,
        }
        return {PAYLOAD_KEY_PRESENT: True, PAYLOAD_KEY_ARTIFACT: artifact}

    def _ops_maturity_evidence(self, pipeline: dict) -> dict:
        artifact_path = pipeline.get(RELEASE_PIPELINE_KEY_ARTIFACTS, {}).get(ARTIFACT_OPS_MATURITY)
        if not artifact_path:
            return {PAYLOAD_KEY_PRESENT: False, PAYLOAD_KEY_ARTIFACT: None}
        path = Path(artifact_path)
        if not path.exists():
            return {PAYLOAD_KEY_PRESENT: False, PAYLOAD_KEY_ARTIFACT: None}
        payload = json.loads(path.read_text(encoding="utf-8"))
        artifact = {
            PAYLOAD_KEY_PATH: str(path),
            PAYLOAD_KEY_SCHEMA_VERSION: payload.get(PAYLOAD_KEY_SCHEMA_VERSION),
            PAYLOAD_KEY_MATURITY_SCORE: payload.get(PAYLOAD_KEY_MATURITY_SCORE, 0),
            PAYLOAD_KEY_GRADE: payload.get(PAYLOAD_KEY_GRADE),
            PAYLOAD_KEY_SHA256: self._sha256(path),
            PAYLOAD_KEY_SIZE_BYTES: path.stat().st_size,
        }
        return {PAYLOAD_KEY_PRESENT: True, PAYLOAD_KEY_ARTIFACT: artifact}

    def _fingerprint(self, certificate: dict) -> str:
        payload = json.dumps(certificate, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

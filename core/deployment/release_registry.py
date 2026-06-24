"""Release certificate registry.

Maintains a local append-only registry of release certificates for audit,
approval, and long-term traceability.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    PAYLOAD_KEY_ACTOR,
    PAYLOAD_KEY_ACTUAL_FINGERPRINT,
    PAYLOAD_KEY_ACTUAL_SHA256,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE,
    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_PRESENT,
    PAYLOAD_KEY_ALPHA_BUDGET_WARNING_COUNT,
    PAYLOAD_KEY_ARTIFACT,
    PAYLOAD_KEY_ARTIFACT_CHECKS,
    PAYLOAD_KEY_ARTIFACT_COUNT,
    PAYLOAD_KEY_CERTIFICATE_FINGERPRINT,
    PAYLOAD_KEY_CERTIFICATE_SHA256,
    PAYLOAD_KEY_CERTIFIED,
    PAYLOAD_KEY_CERTIFIED_COUNT,
    PAYLOAD_KEY_CLEARED,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EVIDENCE_VERIFICATION,
    PAYLOAD_KEY_EVIDENCE_VERIFIED,
    PAYLOAD_KEY_EXPORTED_AT,
    PAYLOAD_KEY_FINAL_AUDIT,
    PAYLOAD_KEY_FINAL_AUDIT_EVIDENCE,
    PAYLOAD_KEY_FINAL_AUDIT_PRESENT,
    PAYLOAD_KEY_FINAL_AUDIT_READY_FOR_PRODUCTION,
    PAYLOAD_KEY_ID,
    PAYLOAD_KEY_LATEST,
    PAYLOAD_KEY_MATURITY_SCORE,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_MISSING_SCORE_COUNT,
    PAYLOAD_KEY_NOT_READY_COUNT,
    PAYLOAD_KEY_OPS_MATURITY,
    PAYLOAD_KEY_OPS_MATURITY_EVIDENCE,
    PAYLOAD_KEY_OPS_MATURITY_PRESENT,
    PAYLOAD_KEY_OPS_MATURITY_SCORE,
    PAYLOAD_KEY_PATH,
    PAYLOAD_KEY_PIPELINE,
    PAYLOAD_KEY_PIPELINE_STATUS,
    PAYLOAD_KEY_PRESENT,
    PAYLOAD_KEY_READY_COUNT,
    PAYLOAD_KEY_READY_FOR_PRODUCTION,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_RECORD_ID,
    PAYLOAD_KEY_RECORDS,
    PAYLOAD_KEY_REGISTERED_AT,
    PAYLOAD_KEY_REGISTERED_FINGERPRINT,
    PAYLOAD_KEY_REGISTERED_SHA256,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SCORE_AVG,
    PAYLOAD_KEY_SCORE_MAX,
    PAYLOAD_KEY_SCORE_MIN,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_STRATEGY,
    PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE,
    PAYLOAD_KEY_STRATEGY_COUNTS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_UNKNOWN_READINESS_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
    PAYLOAD_KEY_VERIFIED,
    PAYLOAD_KEY_VERSION,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    RELEASE_PIPELINE_DEFAULT_ACTOR,
    RELEASE_REGISTRY_FILE,
    RELEASE_REGISTRY_ID_PREFIX,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.governance_summary import extract_governance_summary
from core.deployment.schema_versions import (
    SCHEMA_RELEASE_REGISTRY_EXPORT,
    SCHEMA_RELEASE_REGISTRY_SUMMARY,
    SCHEMA_RELEASE_REGISTRY_VERIFICATION,
)


class ReleaseRegistryService:
    """Registers and verifies release certificates."""

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / RELEASE_REGISTRY_FILE

    @property
    def path(self) -> str:
        return str(self._path)

    def register(
        self, certificate: str | dict, *, actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR
    ) -> dict:
        cert = self._load_certificate(certificate)
        records = self._load_records()
        record = {
            PAYLOAD_KEY_ID: f"{RELEASE_REGISTRY_ID_PREFIX}{len(records) + 1:06d}",
            PAYLOAD_KEY_REGISTERED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_ACTOR: actor,
            PAYLOAD_KEY_VERSION: cert.get(PAYLOAD_KEY_VERSION),
            PAYLOAD_KEY_STRATEGY: cert.get(PAYLOAD_KEY_STRATEGY),
            PAYLOAD_KEY_VALIDATION_MODE: cert.get(PAYLOAD_KEY_VALIDATION_MODE),
            PAYLOAD_KEY_STATUS: cert.get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_CERTIFIED: bool(cert.get(PAYLOAD_KEY_CERTIFIED, False)),
            PAYLOAD_KEY_CERTIFICATE_FINGERPRINT: cert.get(PAYLOAD_KEY_CERTIFICATE_FINGERPRINT),
            PAYLOAD_KEY_CERTIFICATE_SHA256: self._fingerprint(cert),
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_PIPELINE_STATUS: cert.get(PAYLOAD_KEY_PIPELINE, {}).get(
                    PAYLOAD_KEY_STATUS
                ),
                PAYLOAD_KEY_ARTIFACT_COUNT: len(cert.get(PAYLOAD_KEY_ARTIFACT_CHECKS, [])),
                PAYLOAD_KEY_EVIDENCE_VERIFIED: cert.get(PAYLOAD_KEY_EVIDENCE_VERIFICATION, {}).get(
                    PAYLOAD_KEY_VERIFIED, False
                ),
                PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_PRESENT: cert.get(
                    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE, {}
                ).get(PAYLOAD_KEY_PRESENT, False),
                PAYLOAD_KEY_ALPHA_BUDGET_WARNING_COUNT: self._alpha_budget_warning_count(cert),
                PAYLOAD_KEY_VALIDATION_MODE: cert.get(PAYLOAD_KEY_VALIDATION_MODE),
                **self._governance_summary_fields(cert),
            },
        }
        records.append(record)
        self._write_records(records)
        return record

    def list_records(
        self, *, version: str | None = None, certified: bool | None = None
    ) -> list[dict]:
        records = self._load_records()
        if version is not None:
            records = [r for r in records if r.get(PAYLOAD_KEY_VERSION) == version]
        if certified is not None:
            records = [r for r in records if r.get(PAYLOAD_KEY_CERTIFIED) is certified]
        return records

    def latest(self, *, version: str | None = None) -> dict | None:
        records = self.list_records(version=version)
        return records[-1] if records else None

    def summarize(self) -> dict:
        records = self._load_records()
        by_status: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        by_validation_mode: dict[str, int] = {}
        for record in records:
            by_status[str(record.get(PAYLOAD_KEY_STATUS))] = (
                by_status.get(str(record.get(PAYLOAD_KEY_STATUS)), 0) + 1
            )
            by_strategy[str(record.get(PAYLOAD_KEY_STRATEGY))] = (
                by_strategy.get(str(record.get(PAYLOAD_KEY_STRATEGY)), 0) + 1
            )
            mode = str(record.get(PAYLOAD_KEY_VALIDATION_MODE))
            by_validation_mode[mode] = by_validation_mode.get(mode, 0) + 1
        alpha_budget_evidence_count = len(
            [
                r
                for r in records
                if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_PRESENT)
            ]
        )
        alpha_budget_warning_release_count = len(
            [
                r
                for r in records
                if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_ALPHA_BUDGET_WARNING_COUNT, 0) > 0
            ]
        )
        alpha_budget_warning_total = sum(
            r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_ALPHA_BUDGET_WARNING_COUNT, 0)
            for r in records
        )
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_REGISTRY_SUMMARY,
            PAYLOAD_KEY_PATH: str(self._path),
            PAYLOAD_KEY_RECORD_COUNT: len(records),
            PAYLOAD_KEY_CERTIFIED_COUNT: len([r for r in records if r.get(PAYLOAD_KEY_CERTIFIED)]),
            PAYLOAD_KEY_STATUS_COUNTS: by_status,
            PAYLOAD_KEY_STRATEGY_COUNTS: by_strategy,
            PAYLOAD_KEY_VALIDATION_MODE_COUNTS: by_validation_mode,
            PAYLOAD_KEY_VALIDATION_MODE: (
                records[-1].get(PAYLOAD_KEY_VALIDATION_MODE) if records else None
            ),
            **extract_governance_summary(
                records[-1].get(PAYLOAD_KEY_SUMMARY, {}) if records else {}
            ),
            PAYLOAD_KEY_ALPHA_BUDGET: {
                PAYLOAD_KEY_EVIDENCE_COUNT: alpha_budget_evidence_count,
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: len(records) - alpha_budget_evidence_count,
                PAYLOAD_KEY_WARNING_RELEASE_COUNT: alpha_budget_warning_release_count,
                PAYLOAD_KEY_WARNING_TOTAL: alpha_budget_warning_total,
            },
            PAYLOAD_KEY_FINAL_AUDIT: self._summarize_final_audit(records),
            PAYLOAD_KEY_OPS_MATURITY: self._summarize_ops_maturity(records),
            PAYLOAD_KEY_LATEST: records[-1] if records else None,
        }

    def verify_record(self, record_id: str, certificate: str | dict) -> dict:
        cert = self._load_certificate(certificate)
        records = self._load_records()
        matches = [r for r in records if r.get(PAYLOAD_KEY_ID) == record_id]
        if not matches:
            return {
                PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_REGISTRY_VERIFICATION,
                PAYLOAD_KEY_RECORD_ID: record_id,
                PAYLOAD_KEY_VERIFIED: False,
                PAYLOAD_KEY_ERROR: "record not found",
            }
        record = matches[0]
        cert_sha = self._fingerprint(cert)
        verified = record.get(PAYLOAD_KEY_CERTIFICATE_SHA256) == cert_sha and record.get(
            PAYLOAD_KEY_CERTIFICATE_FINGERPRINT
        ) == cert.get(PAYLOAD_KEY_CERTIFICATE_FINGERPRINT)
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_REGISTRY_VERIFICATION,
            PAYLOAD_KEY_RECORD_ID: record_id,
            PAYLOAD_KEY_VERIFIED: verified,
            PAYLOAD_KEY_REGISTERED_SHA256: record.get(PAYLOAD_KEY_CERTIFICATE_SHA256),
            PAYLOAD_KEY_ACTUAL_SHA256: cert_sha,
            PAYLOAD_KEY_REGISTERED_FINGERPRINT: record.get(PAYLOAD_KEY_CERTIFICATE_FINGERPRINT),
            PAYLOAD_KEY_ACTUAL_FINGERPRINT: cert.get(PAYLOAD_KEY_CERTIFICATE_FINGERPRINT),
        }

    def export(self, output: str) -> str:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_REGISTRY_EXPORT,
            PAYLOAD_KEY_EXPORTED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_SUMMARY: self.summarize(),
            PAYLOAD_KEY_RECORDS: self._load_records(),
        }
        atomic_write_json(target, payload)
        return str(target)

    def clear(self) -> dict:
        count = len(self._load_records())
        self._write_records([])
        return {PAYLOAD_KEY_CLEARED: count, PAYLOAD_KEY_PATH: str(self._path)}

    def _load_certificate(self, certificate: str | dict) -> dict:
        if isinstance(certificate, dict):
            return certificate
        return json.loads(Path(certificate).read_text(encoding="utf-8"))

    def _load_records(self) -> list[dict]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write_records(self, records: list[dict]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, records)

    def _alpha_budget_warning_count(self, cert: dict) -> int:
        artifact = cert.get(PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE, {}).get(PAYLOAD_KEY_ARTIFACT) or {}
        return int(artifact.get(PAYLOAD_KEY_WARNING_COUNT, 0))

    def _governance_summary_fields(self, cert: dict) -> dict:
        final_audit = cert.get(PAYLOAD_KEY_FINAL_AUDIT_EVIDENCE) or {}
        fa_present = bool(final_audit.get(PAYLOAD_KEY_PRESENT))
        fa_ready: bool | None = None
        if fa_present:
            art = final_audit.get(PAYLOAD_KEY_ARTIFACT) or {}
            if PAYLOAD_KEY_READY_FOR_PRODUCTION in art:
                fa_ready = bool(art.get(PAYLOAD_KEY_READY_FOR_PRODUCTION))
        ops = cert.get(PAYLOAD_KEY_OPS_MATURITY_EVIDENCE) or {}
        op_present = bool(ops.get(PAYLOAD_KEY_PRESENT))
        op_score: float | None = None
        if op_present:
            art = ops.get(PAYLOAD_KEY_ARTIFACT) or {}
            raw = art.get(PAYLOAD_KEY_MATURITY_SCORE)
            if raw is not None:
                op_score = float(raw)
        return {
            PAYLOAD_KEY_FINAL_AUDIT_PRESENT: fa_present,
            PAYLOAD_KEY_FINAL_AUDIT_READY_FOR_PRODUCTION: fa_ready,
            PAYLOAD_KEY_OPS_MATURITY_PRESENT: op_present,
            PAYLOAD_KEY_OPS_MATURITY_SCORE: op_score,
            **extract_governance_summary(cert),
        }

    def _summarize_final_audit(self, records: list[dict]) -> dict:
        with_evidence = [
            r
            for r in records
            if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_FINAL_AUDIT_PRESENT) is True
        ]
        ready = [
            r
            for r in with_evidence
            if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_FINAL_AUDIT_READY_FOR_PRODUCTION)
            is True
        ]
        not_ready = [
            r
            for r in with_evidence
            if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_FINAL_AUDIT_READY_FOR_PRODUCTION)
            is False
        ]
        unknown = [
            r
            for r in with_evidence
            if r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_FINAL_AUDIT_READY_FOR_PRODUCTION)
            is None
        ]
        return {
            PAYLOAD_KEY_EVIDENCE_COUNT: len(with_evidence),
            PAYLOAD_KEY_READY_COUNT: len(ready),
            PAYLOAD_KEY_NOT_READY_COUNT: len(not_ready),
            PAYLOAD_KEY_UNKNOWN_READINESS_COUNT: len(unknown),
            PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: len(records) - len(with_evidence),
        }

    def _summarize_ops_maturity(self, records: list[dict]) -> dict:
        scores: list[float] = []
        for r in records:
            s = r.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_OPS_MATURITY_SCORE)
            if s is not None:
                scores.append(float(s))
        n = len(records)
        with_score = len(scores)
        if not scores:
            return {
                PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE: 0,
                PAYLOAD_KEY_SCORE_MIN: None,
                PAYLOAD_KEY_SCORE_MAX: None,
                PAYLOAD_KEY_SCORE_AVG: None,
                PAYLOAD_KEY_MISSING_SCORE_COUNT: n,
            }
        return {
            PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE: with_score,
            PAYLOAD_KEY_SCORE_MIN: min(scores),
            PAYLOAD_KEY_SCORE_MAX: max(scores),
            PAYLOAD_KEY_SCORE_AVG: round(sum(scores) / len(scores), 2),
            PAYLOAD_KEY_MISSING_SCORE_COUNT: n - with_score,
        }

    def _fingerprint(self, payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

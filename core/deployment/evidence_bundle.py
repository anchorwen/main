"""Evidence bundle generation for release audit.

Creates a directory of machine-readable evidence files plus a manifest
with SHA-256 checksums for traceability.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    ENGINE_CONFIG_KEY_HOT_RELOAD,
    ENGINE_CONFIG_KEY_RUNTIME_METRICS,
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    EVIDENCE_SECTION_DIAGNOSTICS,
    EVIDENCE_SECTION_DOCTOR,
    EVIDENCE_SECTION_ENGINE_CONFIG,
    EVIDENCE_SECTION_FINAL_AUDIT,
    EVIDENCE_SECTION_GATE,
    EVIDENCE_SECTION_MANIFEST,
    EVIDENCE_SECTION_OPS_MATURITY,
    EVIDENCE_SECTION_PREFLIGHT,
    EVIDENCE_SECTION_READINESS,
    EVIDENCE_SECTION_SLO,
    PAYLOAD_KEY_ACTUAL_SHA256,
    PAYLOAD_KEY_AVAILABLE,
    PAYLOAD_KEY_BUNDLE_DIR,
    PAYLOAD_KEY_CHECKSUM_MATCHES,
    PAYLOAD_KEY_CONFIG_PATH,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_EFFECTIVE,
    PAYLOAD_KEY_ENGINE_CONFIG_POLL_INTERVAL_SECONDS,
    PAYLOAD_KEY_EXISTS,
    PAYLOAD_KEY_EXPECTED_SHA256,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FILE_COUNT,
    PAYLOAD_KEY_FILE_EXISTS,
    PAYLOAD_KEY_FILES,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_LABEL,
    PAYLOAD_KEY_MANIFEST_CHECKSUM,
    PAYLOAD_KEY_MANIFEST_PATH,
    PAYLOAD_KEY_MAX_DRAWDOWN_PCT,
    PAYLOAD_KEY_MAX_OPEN_POSITIONS,
    PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_RELATIVE_PATH,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SECTION,
    PAYLOAD_KEY_SECTIONS,
    PAYLOAD_KEY_SHA256,
    PAYLOAD_KEY_SIZE_BYTES,
    PAYLOAD_KEY_SNAPSHOT,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_SYSTEM_MODE,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERIFIED,
)
from core.deployment.governance_summary import build_governance_summary, extract_governance_summary
from core.deployment.schema_versions import (
    SCHEMA_ENGINE_CONFIG_EVIDENCE,
    SCHEMA_EVIDENCE_BUNDLE,
    SCHEMA_EVIDENCE_MANIFEST,
    SCHEMA_EVIDENCE_VERIFICATION,
)
from core.deployment.validation_mode import resolve_validation_mode
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL


class EvidenceBundleService:
    """Generates release evidence bundles from runtime services."""

    DEFAULT_SECTIONS = [
        EVIDENCE_SECTION_READINESS,
        EVIDENCE_SECTION_GATE,
        EVIDENCE_SECTION_SLO,
        EVIDENCE_SECTION_PREFLIGHT,
        EVIDENCE_SECTION_DOCTOR,
        EVIDENCE_SECTION_DIAGNOSTICS,
    ]

    def __init__(self, container):
        self._container = container

    def engine_config_snapshot(self) -> dict:
        """Point-in-time engine JSON hot-reload and effective config (reusable in reports)."""
        return self._build_engine_config()

    def build_bundle(
        self,
        output_dir: str,
        *,
        label: str | None = None,
        alpha_budget_usage_report: dict | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        label = label or datetime.now(UTC).replace(tzinfo=None).strftime("bundle_%Y%m%d%H%M%S")
        target_dir = Path(output_dir) / label
        target_dir.mkdir(parents=True, exist_ok=True)

        files = []
        generated = {
            EVIDENCE_SECTION_READINESS: self._container.release_readiness.build_report(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_GATE: self._container.release_gate.evaluate(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_SLO: self._container.slo_service.evaluate(),
            EVIDENCE_SECTION_PREFLIGHT: self._container.runbook_engine.preflight(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_DOCTOR: self._container.runbook_engine.doctor(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_DIAGNOSTICS: self._build_diagnostics(),
            EVIDENCE_SECTION_FINAL_AUDIT: self._container.final_audit.build_report(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_OPS_MATURITY: self._container.ops_maturity.evaluate(
                validation_mode=validation_mode
            ),
            EVIDENCE_SECTION_ENGINE_CONFIG: self._build_engine_config(),
        }

        if alpha_budget_usage_report is not None:
            generated[EVIDENCE_SECTION_ALPHA_BUDGET_USAGE] = alpha_budget_usage_report

        for name, payload in generated.items():
            path = target_dir / f"{name}.json"
            self._write_json(path, payload)
            files.append(self._file_manifest(path, section=name))

        governance_summary = extract_governance_summary(
            generated[EVIDENCE_SECTION_FINAL_AUDIT].get(PAYLOAD_KEY_SUMMARY, {}),
        )
        manifest = self._build_manifest(
            label=label,
            target_dir=target_dir,
            files=files,
            validation_mode=validation_mode,
            governance_summary=governance_summary,
        )
        manifest_path = target_dir / "manifest.json"
        self._write_json(manifest_path, manifest)
        manifest_file = self._file_manifest(manifest_path, section=EVIDENCE_SECTION_MANIFEST)
        manifest[PAYLOAD_KEY_FILES].append(manifest_file)
        self._write_json(manifest_path, manifest)

        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_EVIDENCE_BUNDLE,
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_BUNDLE_DIR: str(target_dir),
            PAYLOAD_KEY_MANIFEST_PATH: str(manifest_path),
            PAYLOAD_KEY_FILE_COUNT: len(manifest[PAYLOAD_KEY_FILES]),
            PAYLOAD_KEY_MANIFEST_CHECKSUM: self._sha256(manifest_path),
            PAYLOAD_KEY_SECTIONS: list(generated.keys()),
            PAYLOAD_KEY_GATE_DECISION: generated[EVIDENCE_SECTION_GATE].get(PAYLOAD_KEY_DECISION),
            PAYLOAD_KEY_READY: generated[EVIDENCE_SECTION_READINESS].get(PAYLOAD_KEY_READY),
        }

    def verify_bundle(self, manifest_path: str) -> dict:
        manifest_file = Path(manifest_path)
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        base = manifest_file.parent
        results = []
        for item in manifest.get(PAYLOAD_KEY_FILES, []):
            path = base / item[PAYLOAD_KEY_RELATIVE_PATH]
            exists = path.exists()
            checksum = self._sha256(path) if exists else None
            expected = item.get(PAYLOAD_KEY_SHA256)
            ok = (
                exists
                if item.get(PAYLOAD_KEY_SECTION) == EVIDENCE_SECTION_MANIFEST
                else exists and checksum == expected
            )
            results.append(
                {
                    PAYLOAD_KEY_SECTION: item.get(PAYLOAD_KEY_SECTION),
                    PAYLOAD_KEY_RELATIVE_PATH: item.get(PAYLOAD_KEY_RELATIVE_PATH),
                    PAYLOAD_KEY_EXISTS: exists,
                    PAYLOAD_KEY_CHECKSUM_MATCHES: ok,
                    PAYLOAD_KEY_EXPECTED_SHA256: expected,
                    PAYLOAD_KEY_ACTUAL_SHA256: checksum,
                }
            )
        failed = [r for r in results if not r[PAYLOAD_KEY_CHECKSUM_MATCHES]]
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_EVIDENCE_VERIFICATION,
            PAYLOAD_KEY_VALIDATION_MODE: manifest.get(PAYLOAD_KEY_VALIDATION_MODE),
            PAYLOAD_KEY_MANIFEST_PATH: str(manifest_file),
            PAYLOAD_KEY_VERIFIED: not failed,
            PAYLOAD_KEY_FILE_COUNT: len(results),
            PAYLOAD_KEY_FAILED_COUNT: len(failed),
            PAYLOAD_KEY_RESULTS: results,
        }

    def _build_diagnostics(self) -> dict:
        diagnostics = getattr(self._container, "diagnostics", None)
        if diagnostics is None:
            return {PAYLOAD_KEY_AVAILABLE: False}
        return {PAYLOAD_KEY_AVAILABLE: True, PAYLOAD_KEY_SNAPSHOT: diagnostics.build_snapshot()}

    def _build_engine_config(self) -> dict:
        hr = getattr(self._container, "config_hot_reload", None)
        if hr is None:
            return {
                PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_ENGINE_CONFIG_EVIDENCE,
                PAYLOAD_KEY_AVAILABLE: False,
            }
        path = hr._path
        exists = bool(path) and path.is_file()
        m = getattr(self._container, "metrics", None)
        payload: dict = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_ENGINE_CONFIG_EVIDENCE,
            PAYLOAD_KEY_AVAILABLE: True,
            PAYLOAD_KEY_CONFIG_PATH: str(path) if path else None,
            PAYLOAD_KEY_FILE_EXISTS: exists,
            ENGINE_CONFIG_KEY_HOT_RELOAD: hr.get_status(),
            PAYLOAD_KEY_EFFECTIVE: {
                PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE: self._container.config.ops_maturity_min_score,
                PAYLOAD_KEY_ENGINE_CONFIG_POLL_INTERVAL_SECONDS: float(
                    getattr(
                        self._container.config,
                        "engine_config_poll_interval_seconds",
                        60.0,
                    )
                ),
                PAYLOAD_KEY_MAX_OPEN_POSITIONS: self._container.config.max_open_positions,
                PAYLOAD_KEY_MAX_DRAWDOWN_PCT: self._container.config.max_drawdown_pct,
                PAYLOAD_KEY_SYSTEM_MODE: self._container.config.system_mode,
            },
        }
        if m is not None:
            payload[ENGINE_CONFIG_KEY_RUNTIME_METRICS] = {
                ENGINE_CONFIG_RELOAD_TOTAL: float(m.get_counter(ENGINE_CONFIG_RELOAD_TOTAL)),
            }
        return payload

    def _build_manifest(
        self,
        *,
        label: str,
        target_dir: Path,
        files: list[dict],
        validation_mode: str,
        governance_summary: dict,
    ) -> dict:
        normalized_governance_summary = build_governance_summary(
            focus=governance_summary.get(PAYLOAD_KEY_GOVERNANCE_FOCUS, []),
        )
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_EVIDENCE_MANIFEST,
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_BUNDLE_DIR: str(target_dir),
            PAYLOAD_KEY_FILES: list(files),
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_FILE_COUNT: len(files) + 1,
                PAYLOAD_KEY_SECTIONS: [f[PAYLOAD_KEY_SECTION] for f in files]
                + [EVIDENCE_SECTION_MANIFEST],
                **normalized_governance_summary,
            },
        }

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _file_manifest(self, path: Path, *, section: str) -> dict:
        return {
            PAYLOAD_KEY_SECTION: section,
            PAYLOAD_KEY_RELATIVE_PATH: path.name,
            PAYLOAD_KEY_SIZE_BYTES: path.stat().st_size,
            PAYLOAD_KEY_SHA256: self._sha256(path),
        }

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

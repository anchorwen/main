"""Brain Registration Gate — single-entry validation before any brain enters production.

All training pipelines MUST pass this gate before writing a brain config to disk.
No check parses model file internals — artifact integrity is verified by SHA-256
hash contract, which is immune to library format changes and file corruption.

Gate checks (12 checks, any ERROR = REJECT):
  0. Required fields present (brain_id, brain_type, feature_schema_id, artifact_path,
     artifact_hash, features, status, magic)
  1. brain_type exists in BRAIN_TYPE_MAP
  2. feature_schema_id is a known schema
  3. features list is non-empty (prevents bypass of dimension validation)
  4. features length matches schema dimension
  5. All feature names exist in canonical schema name set
  6. artifact_path file exists on disk
  7. artifact_hash matches sha256(artifact_path) — cryptographic integrity
  8. magic number is unique across all brain configs
  9. brain_id is unique across all brain configs
  10. vote_weight in [0.0, 1.0] (WARNING)
  11. contract_group matches a known strategy_line in live.yaml (WARNING)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.brains.adapters import BRAIN_TYPE_MAP
from core.deployment.brain_config_validator import (
    SCHEMA_ALIASES,
    SCHEMA_DIMENSIONS,
    _get_schema_feature_names,
)


@dataclass
class GateResult:
    brain_id: str = ""
    passed: bool = False
    checks_run: int = 0
    checks_passed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # [(check_name, detail)]
    warnings: list[str] = field(default_factory=list)
    validated_config: dict[str, Any] | None = None


class BrainRegistrationGate:
    """Registration-time validation gate for brain configs.

    Usage:
        gate = BrainRegistrationGate(project_root=Path("."))
        result = gate.validate(config_dict)
        if not result.passed:
            for check, detail in result.failures:
                print(f"[REJECTED] {check}: {detail}")
    """

    REQUIRED_FIELDS = frozenset(
        {
            "brain_id",
            "brain_type",
            "feature_schema_id",
            "artifact_path",
            "artifact_hash",
            "features",
            "status",
            "magic",
        }
    )

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = project_root or Path.cwd()
        self._brains_dir = self._project_root / "configs" / "brains"
        self._live_yaml_path = self._project_root / "configs" / "live.yaml"

    def validate(self, entry: dict[str, Any]) -> GateResult:
        brain_id = entry.get("brain_id", "?")
        result = GateResult(brain_id=brain_id)

        # Run all checks in order
        checks = [
            ("required_fields", self._check_required_fields),
            ("brain_type_valid", self._check_brain_type),
            ("schema_known", self._check_feature_schema),
            ("features_non_empty", self._check_features_non_empty),
            ("features_dimension", self._check_features_dimension),
            ("feature_names_valid", self._check_feature_names),
            ("artifact_exists", self._check_artifact_exists),
            ("artifact_hash_match", self._check_artifact_hash),
            ("magic_unique", self._check_magic_unique),
            ("brain_id_unique", self._check_brain_id_unique),
        ]

        for check_name, check_fn in checks:
            result.checks_run += 1
            try:
                ok = check_fn(entry, result)
                if ok:
                    result.checks_passed += 1
            except Exception as exc:
                result.failures.append((check_name, f"unexpected error: {exc}"))

        # Non-blocking warnings
        self._check_vote_weight(entry, result)
        self._check_contract_group(entry, result)

        result.passed = len(result.failures) == 0
        if result.passed:
            result.validated_config = entry

        return result

    # ── private check methods ──

    @staticmethod
    def _check_required_fields(entry: dict, result: GateResult) -> bool:
        missing = BrainRegistrationGate.REQUIRED_FIELDS - set(entry.keys())
        if missing:
            result.failures.append(("required_fields", f"missing: {sorted(missing)}"))
            return False
        return True

    @staticmethod
    def _check_brain_type(entry: dict, result: GateResult) -> bool:
        brain_type = entry.get("brain_type", "")
        if brain_type not in BRAIN_TYPE_MAP:
            result.failures.append(
                ("brain_type_valid", f"'{brain_type}' not in {sorted(BRAIN_TYPE_MAP)}")
            )
            return False
        return True

    @staticmethod
    def _check_feature_schema(entry: dict, result: GateResult) -> bool:
        schema_id = entry.get("feature_schema_id", "")
        canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
        if canonical not in SCHEMA_DIMENSIONS:
            result.failures.append(
                ("schema_known", f"'{schema_id}' not in {sorted(SCHEMA_DIMENSIONS)}")
            )
            return False
        return True

    @staticmethod
    def _check_features_non_empty(entry: dict, result: GateResult) -> bool:
        features = entry.get("features")
        if not features:
            result.failures.append(
                (
                    "features_non_empty",
                    "features list is missing or empty — dimension validation bypassed",
                )
            )
            return False
        return True

    @staticmethod
    def _check_features_dimension(entry: dict, result: GateResult) -> bool:
        features = entry.get("features", [])
        schema_id = entry.get("feature_schema_id", "")
        canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
        expected = SCHEMA_DIMENSIONS.get(canonical)
        if expected is None:
            return True  # unknown schema, already caught by schema_known check
        if len(features) != expected:
            result.failures.append(
                (
                    "features_dimension",
                    f"len(features)={len(features)} != schema {schema_id} expected={expected}",
                )
            )
            return False
        return True

    @staticmethod
    def _check_feature_names(entry: dict, result: GateResult) -> bool:
        features = entry.get("features", [])
        schema_id = entry.get("feature_schema_id", "")
        expected_names = _get_schema_feature_names(schema_id)
        if not expected_names:
            return True  # can't validate names without canonical list
        invalid = [f"[{i}]='{n}'" for i, n in enumerate(features) if n not in expected_names]
        if invalid:
            result.failures.append(
                (
                    "feature_names_valid",
                    f"names not in schema {schema_id}: {', '.join(invalid[:5])}"
                    f"{'...' if len(invalid) > 5 else ''}",
                )
            )
            return False
        return True

    def _check_artifact_exists(self, entry: dict, result: GateResult) -> bool:
        path = entry.get("artifact_path", "")
        if not path:
            result.failures.append(("artifact_exists", "artifact_path is empty"))
            return False
        full = self._resolve_path(path)
        if not full.exists():
            result.failures.append(("artifact_exists", f"file not found: {full}"))
            return False
        return True

    def _check_artifact_hash(self, entry: dict, result: GateResult) -> bool:
        expected = entry.get("artifact_hash", "")
        if not expected:
            result.failures.append(
                ("artifact_hash_match", "artifact_hash field is empty or missing")
            )
            return False
        path = entry.get("artifact_path", "")
        full = self._resolve_path(path)
        try:
            actual = hashlib.sha256(full.read_bytes()).hexdigest()
        except OSError as exc:
            result.failures.append(("artifact_hash_match", f"cannot read file: {exc}"))
            return False
        if actual != expected:
            result.failures.append(
                ("artifact_hash_match", f"hash mismatch: config={expected} file={actual}")
            )
            return False
        return True

    def _check_magic_unique(self, entry: dict, result: GateResult) -> bool:
        magic = entry.get("magic")
        if magic is None:
            return True
        brain_id = entry.get("brain_id", "?")
        group = entry.get("contract_group", "")
        existing = self._scan_all_configs()
        for bid, cfg in existing.items():
            if bid == brain_id:
                continue
            if cfg.get("magic") == magic:
                # Same contract_group can share magic (same strategy line)
                other_group = cfg.get("contract_group", "")
                if group and other_group and group == other_group:
                    continue
                result.failures.append(
                    ("magic_unique", f"magic={magic} already used by brain_id='{bid}'")
                )
                return False
        return True

    def _check_brain_id_unique(self, entry: dict, result: GateResult) -> bool:
        brain_id = entry.get("brain_id", "")
        if not brain_id:
            return True
        existing = self._scan_all_configs()
        if brain_id in existing:
            result.failures.append(
                ("brain_id_unique", f"brain_id='{brain_id}' already exists in configs/brains/")
            )
            return False
        return True

    @staticmethod
    def _check_vote_weight(entry: dict, result: GateResult) -> None:
        vw = entry.get("vote_weight")
        if vw is not None and not (0.0 <= float(vw) <= 1.0):
            result.warnings.append(f"vote_weight={vw} outside [0.0, 1.0] range")

    def _check_contract_group(self, entry: dict, result: GateResult) -> None:
        cg = entry.get("contract_group", "")
        if not cg:
            return
        known = self._known_contract_groups()
        if known and cg not in known:
            result.warnings.append(
                f"contract_group='{cg}' not in live.yaml strategy_lines: {sorted(known)}"
            )

    # ── helpers ──

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str)
        if not p.is_absolute():
            p = (self._project_root / p).resolve()
        return p

    def _scan_all_configs(self) -> dict[str, dict]:
        """Return {brain_id: config} for all brain configs on disk."""
        result: dict[str, dict] = {}
        if not self._brains_dir.exists():
            return result
        for cfg_path in sorted(self._brains_dir.glob("*.json")):
            if "normalization" in cfg_path.name.lower():
                continue
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                bid = data.get("brain_id")
                if bid:
                    result[bid] = data
            except (json.JSONDecodeError, OSError):
                continue
        return result

    def _known_contract_groups(self) -> set[str]:
        """Extract contract group names from live.yaml strategy_lines."""
        if not self._live_yaml_path.exists():
            return set()
        try:
            import yaml

            live = yaml.safe_load(self._live_yaml_path.read_text(encoding="utf-8")) or {}
            return set(live.get("strategy_lines", {}).keys())
        except Exception:
            return set()

    # ── CLI entry point ──

    @classmethod
    def validate_all(cls, project_root: Path | None = None) -> dict[str, GateResult]:
        """Validate all existing brain configs. Returns {brain_id: GateResult}."""
        gate = cls(project_root)
        results: dict[str, GateResult] = {}
        for bid, cfg in gate._scan_all_configs().items():
            results[bid] = gate.validate(cfg)
        return results


if __name__ == "__main__":
    import sys

    root = Path.cwd()
    if "--project-root" in sys.argv:
        idx = sys.argv.index("--project-root")
        root = Path(sys.argv[idx + 1])

    gate = BrainRegistrationGate(project_root=root)
    results = gate.validate_all(project_root=root)

    total = len(results)
    passed = sum(1 for r in results.values() if r.passed)
    failed = total - passed

    print(f"\n{'='*60}")
    print("Brain Registration Gate — Validate All")
    print(f"{'='*60}")
    print(f"Total: {total}  Passed: {passed}  Failed: {failed}")
    print()

    for bid, r in sorted(results.items()):
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {bid}")
        for check, detail in r.failures:
            print(f"    ERROR [{check}]: {detail}")
        for w in r.warnings:
            print(f"    WARN  {w}")

    sys.exit(0 if failed == 0 else 1)

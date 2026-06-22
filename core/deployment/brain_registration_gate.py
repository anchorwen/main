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

Relationship with brain_config_validator.py
────────────────────────────────────────────
Both modules validate brain configs and share similarly-named ``_check_*``
methods, but they serve DIFFERENT lifecycle stages by DESIGN:

  brain_config_validator.py   →  runs at LOAD time   →  lenient, WARNING-only
  brain_registration_gate.py  →  runs at REGISTRATION →  strict, FAILURE-on-error

DO NOT copy-paste checks between them.  The Validator is intentionally
lenient (to avoid blocking startup); the Gate is intentionally strict
(to prevent bad brains entering production).  If you add a new check
category to one, evaluate whether the other also needs it — but
implement it with the appropriate severity for that lifecycle stage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from core.brains.adapters import BRAIN_TYPE_MAP
from core.deployment.brain_config_validator import (
    SCHEMA_ALIASES,
    SCHEMA_DIMENSIONS,
    _get_schema_feature_names,
)
from core.runtime.fault_handler import fail_open_guard


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
            except Exception as exc:  # BLE001:FOG
                with fail_open_guard("brain_registration_gate:validate"):
                    result.failures.append((check_name, f"unexpected error: {exc}"))
        # Non-blocking warnings
        self._check_vote_weight(entry, result)
        self._check_contract_group(entry, result)
        self._check_behavioral_diversity(entry, result)

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

    # ── FIX-20260622-057 Phase 2 B1: Behavioral Diversity Check ─────────
    # Detects data saturation — when a new brain model produces near-identical
    # raw_score predictions to an existing brain sharing the same feature schema.
    # Correlation > 0.95 triggers a WARNING (not ERROR — this is advisory).
    # Gracefully degrades if feature store or BrainFactory is unavailable.

    _BEHAVIORAL_DIVERSITY_CORR_THRESHOLD = 0.95
    _BEHAVIORAL_DIVERSITY_SAMPLE_LIMIT = 200

    def _check_behavioral_diversity(self, entry: dict, result: GateResult) -> None:
        brain_id = entry.get("brain_id", "")
        schema_id = entry.get("feature_schema_id", "")
        if not brain_id or not schema_id:
            return

        # Find comparable brains: same feature_schema_id, different brain_id
        existing = self._scan_all_configs()
        comparable = {
            bid: cfg
            for bid, cfg in existing.items()
            if bid != brain_id and cfg.get("feature_schema_id") == schema_id
        }
        if len(comparable) < 1:
            return  # nothing to compare against

        # Load feature vectors from feature store
        feature_vectors = self._load_feature_store_vectors(schema_id)
        if not feature_vectors:
            return  # feature store unavailable — skip silently

        # Build adapter for the new brain
        new_adapter = self._build_adapter_safe(entry)
        if new_adapter is None:
            return

        # Run inference on the new brain
        new_scores = self._collect_raw_scores(new_adapter, entry, feature_vectors)
        if new_scores is None or len(new_scores) < 10:
            return

        # Compare against each comparable existing brain
        for other_id, other_cfg in comparable.items():
            other_adapter = self._build_adapter_safe(other_cfg)
            if other_adapter is None:
                continue
            other_scores = self._collect_raw_scores(other_adapter, other_cfg, feature_vectors)
            if other_scores is None or len(other_scores) < 10:
                continue

            # Pearson correlation of raw scores
            try:
                corr = float(np.corrcoef(new_scores, other_scores)[0, 1])
            except (ValueError, IndexError):
                continue
            if np.isnan(corr):
                continue

            if corr >= self._BEHAVIORAL_DIVERSITY_CORR_THRESHOLD:
                result.warnings.append(
                    f"near-identical predictions to '{other_id}' "
                    f"(raw_score r={corr:.3f}) — possible data saturation"
                )

    @staticmethod
    def _load_feature_store_vectors(
        schema_id: str,
        limit: int | None = None,
    ) -> list[dict[str, float]]:
        """Load the last N feature records from the M5 feature store."""
        if limit is None:
            limit = BrainRegistrationGate._BEHAVIORAL_DIVERSITY_SAMPLE_LIMIT
        store_path = Path("data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl")
        if not store_path.exists():
            return []
        records: list[dict[str, float]] = []
        try:
            with open(store_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        vals = r.get("values", {})
                        if isinstance(vals, dict) and len(vals) >= 10:
                            records.append(vals)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            return []
        return records[-limit:]

    @staticmethod
    def _build_adapter_safe(entry: dict[str, Any]):
        """Build a brain adapter, returning None on any failure."""
        try:
            from core.brains.services.brain_factory import BrainFactory

            return BrainFactory().build(entry)
        except Exception:
            return None

    @staticmethod
    def _collect_raw_scores(
        adapter,
        entry: dict[str, Any],
        feature_vectors: list[dict[str, float]],
    ) -> list[float] | None:
        """Run inference on all feature vectors, collecting raw_score values."""
        feature_names: list[str] = entry.get("features", [])
        if not feature_names:
            return None
        scores: list[float] = []
        for vec_dict in feature_vectors:
            vec = np.zeros(len(feature_names), dtype=np.float32)
            for i, name in enumerate(feature_names):
                vec[i] = float(vec_dict.get(name, 0.0))
            try:
                raw = adapter.infer(vec)
            except Exception:
                continue
            if isinstance(raw, dict):
                score = raw.get("raw_score")
            elif isinstance(raw, int | float):
                score = float(raw)
            else:
                continue
            if score is not None:
                scores.append(float(score))
        return scores if len(scores) >= 10 else None

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
        except Exception:  # BLE001:FOG
            with fail_open_guard("brain_registration_gate:_known_contract_groups"):
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

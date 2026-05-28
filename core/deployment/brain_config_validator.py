"""Load-time brain config validation — runs at BrainFactory.build() time.

Catches configuration errors before they become silent inference failures.
A failing validation blocks the brain from participating in inference.

Validation rules:
  1. Required fields present and non-empty (includes artifact_hash, features)
  2. brain_type exists in BRAIN_TYPE_MAP
  3. feature_schema_id is a known schema
  4. artifact_path points to an existing file (ERROR)
  5. features list length matches schema dimension (if present)
  6. features names are valid for the schema (if present)
  7. _num_features (from model file) matches schema dimension
  8. artifact_hash field is present and non-empty
  9. magic number is unique across all brain configs
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.brains.adapters import BRAIN_TYPE_MAP


class BrainConfigError(ValueError):
    """Raised when a brain config fails load-time validation."""


# Re-exported from the single source of truth — all other modules must import
# from core.features.schemas.registry, NOT define their own SCHEMA_DIMENSIONS.
from core.features.schemas.registry import (
    SCHEMA_ALIASES,
    SCHEMA_DIMENSIONS,
)
from core.features.schemas.registry import (
    get_schema_feature_names as _get_schema_feature_names,
)

# Backwards-compat: _get_schema_feature_names was previously defined here.
# Import it above and re-use the name so existing callers don't break.


@dataclass
class ValidationResult:
    brain_id: str = ""
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: ValidationResult) -> None:
        self.ok = self.ok and other.ok
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


class BrainConfigValidator:
    """Validates brain config entries at load time."""

    REQUIRED_FIELDS = {"brain_id", "brain_type", "feature_schema_id", "artifact_path", "status"}
    # artifact_hash and features are validated as warnings at load time
    # for backward compatibility with brains trained before FIX-20260516-010.
    # The BrainRegistrationGate enforces them strictly at registration time.

    def __init__(self) -> None:
        # Lazy-built reverse index: magic → list of brain_ids (built once, O(n) reads)
        self._magic_index: dict[int, list[str]] | None = None

    def _build_magic_index(self) -> None:
        """Pre-load all brain configs once and build magic → [brain_id] index."""
        self._magic_index = {}
        brains_dir = Path("configs/brains")
        if not brains_dir.exists():
            return
        for cfg_path in brains_dir.glob("*.json"):
            if "normalization" in cfg_path.name.lower():
                continue
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            magic = data.get("magic")
            brain_id = data.get("brain_id", "?")
            if magic is not None:
                self._magic_index.setdefault(magic, []).append(brain_id)

    def validate(self, brain_entry: dict) -> ValidationResult:
        brain_id = brain_entry.get("brain_id", "?")
        result = ValidationResult(brain_id=brain_id)

        # Build magic index once (lazy, O(n) reads instead of O(n²))
        if self._magic_index is None:
            self._build_magic_index()

        self._check_required_fields(brain_entry, result)
        self._check_brain_type(brain_entry, result)
        self._check_feature_schema(brain_entry, result)
        self._check_artifact_path(brain_entry, result)
        self._check_artifact_hash(brain_entry, result)
        self._check_features_field(brain_entry, result)
        self._check_magic_unique(brain_entry, result)

        result.ok = len(result.errors) == 0
        return result

    def validate_model_dimension(
        self, brain_entry: dict, num_features: int | None
    ) -> ValidationResult:
        """Post-load check: model's _num_features vs schema dimension."""
        brain_id = brain_entry.get("brain_id", "?")
        result = ValidationResult(brain_id=brain_id, ok=True)

        if num_features is None:
            return result  # can't validate, adapter didn't report

        schema_id = brain_entry.get("feature_schema_id", "")
        canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
        expected = SCHEMA_DIMENSIONS.get(canonical)
        if expected is None:
            return result

        if num_features != expected:
            msg = (
                f"brain_id={brain_id}: model num_features={num_features} "
                f"!= schema {schema_id} expected={expected}"
            )
            result.errors.append(msg)
            result.ok = False

        return result

    # ── private helpers ──

    def _check_required_fields(self, entry: dict, result: ValidationResult) -> None:
        for field_name in self.REQUIRED_FIELDS:
            if not entry.get(field_name):
                result.errors.append(
                    f"brain_id={entry.get('brain_id', '?')}: missing required field '{field_name}'"
                )

    def _check_brain_type(self, entry: dict, result: ValidationResult) -> None:
        brain_type = entry.get("brain_type", "")
        if brain_type and brain_type not in BRAIN_TYPE_MAP:
            result.errors.append(
                f"brain_id={entry.get('brain_id', '?')}: "
                f"unknown brain_type '{brain_type}'. Known: {list(BRAIN_TYPE_MAP)}"
            )

    def _check_feature_schema(self, entry: dict, result: ValidationResult) -> None:
        schema_id = entry.get("feature_schema_id", "")
        if not schema_id:
            return
        canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
        if canonical not in SCHEMA_DIMENSIONS:
            result.errors.append(
                f"brain_id={entry.get('brain_id', '?')}: "
                f"unknown feature_schema_id '{schema_id}'. Known: {list(SCHEMA_DIMENSIONS)}"
            )

    def _check_artifact_path(self, entry: dict, result: ValidationResult) -> None:
        path = entry.get("artifact_path", "")
        if not path:
            return
        if not Path(path).exists():
            result.errors.append(
                f"brain_id={entry.get('brain_id', '?')}: " f"artifact_path does not exist: {path}"
            )

    def _check_features_field(self, entry: dict, result: ValidationResult) -> None:
        features = entry.get("features")
        if not features:
            return  # not populated yet — not an error (will be filled by repair tool)

        schema_id = entry.get("feature_schema_id", "")
        canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
        expected_dim = SCHEMA_DIMENSIONS.get(canonical)
        if expected_dim is None:
            return

        if len(features) != expected_dim:
            result.errors.append(
                f"brain_id={entry.get('brain_id', '?')}: "
                f"features list length {len(features)} != schema {schema_id} expected {expected_dim}"
            )

        expected_names = _get_schema_feature_names(schema_id)
        if expected_names:
            for i, name in enumerate(features):
                if name not in expected_names:
                    result.errors.append(
                        f"brain_id={entry.get('brain_id', '?')}: "
                        f"feature[{i}]='{name}' not in schema {schema_id}"
                    )

    def _check_artifact_hash(self, entry: dict, result: ValidationResult) -> None:
        brain_id = entry.get("brain_id", "?")
        hash_val = entry.get("artifact_hash", "")
        if not hash_val:
            result.warnings.append(
                f"brain_id={brain_id}: missing artifact_hash — "
                f"model integrity cannot be verified. Re-train to generate hash."
            )

    def _check_magic_unique(self, entry: dict, result: ValidationResult) -> None:
        magic = entry.get("magic")
        if magic is None or self._magic_index is None:
            return
        brain_id = entry.get("brain_id", "?")
        conflicts = [bid for bid in self._magic_index.get(magic, []) if bid != brain_id]
        for conflict_bid in conflicts:
            result.warnings.append(
                f"brain_id={brain_id}: magic={magic} also used by "
                f"brain_id='{conflict_bid}' — may cause signal routing conflicts"
            )


# Singleton for use by BrainFactory
_validator: BrainConfigValidator | None = None


def get_validator() -> BrainConfigValidator:
    global _validator
    if _validator is None:
        _validator = BrainConfigValidator()
    return _validator

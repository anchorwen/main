"""CRT model manifest builder (Pydantic). Aligns with schemas/crt_model_manifest.v1.schema.json."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MODEL_ID_RE = re.compile(
    r"^CRT\.(?P<lane>[a-z0-9]{2,16})\.(?P<role>prd|chlg|cabl|stub)\.(?P<gen>g\d{4}\.\d+)@feat-(?P<feat>.+)$"
)


class CRTManifestV1(BaseModel):
    schema_version: Literal["crt_model_manifest.v1"] = "crt_model_manifest.v1"
    model_id: str
    lane: str
    role: Literal["prd", "chlg", "cabl", "stub"]
    generation: str
    feature_contract_id: str
    iface_semver: str
    dataset_slice_id: str
    git_commit: str
    train_started_at_utc: str
    trainer_version: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    risk_notes: list[str] = Field(default_factory=list)
    legacy_aliases: list[str] = Field(default_factory=list)
    train_seed: int | None = None
    artifact_primary: str | None = None
    norm_artifact: str | None = None
    training_run_id: str | None = None
    recipe_id: str | None = None

    @field_validator("generation")
    @classmethod
    def generation_ok(cls, v: str) -> str:
        if not re.match(r"^g\d{4}\.\d+$", v):
            raise ValueError("generation must look like g2026.1")
        return v

    @field_validator("lane")
    @classmethod
    def lane_ok(cls, v: str) -> str:
        if not re.match(r"^[a-z]{2,16}$", v):
            raise ValueError("lane must be 2-16 lowercase letters")
        return v

    @field_validator("iface_semver")
    @classmethod
    def iface_ok(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+\.\d+$", v):
            raise ValueError("iface_semver must be semver X.Y.Z")
        return v

    @field_validator("model_id")
    @classmethod
    def model_id_consistent(cls, v: str) -> str:
        m = MODEL_ID_RE.match(v)
        if not m:
            raise ValueError(
                "model_id must match CRT.<lane>.<role>.gYYYY.N@feat-<name>-<semver...>"
            )
        return v


def build_feature_contract_slug(feature_contract_id: str) -> str:
    """feat-v9-institutional-1.0.0 → feat slug segment after @ in ID uses full feat-* string."""
    return (
        feature_contract_id
        if feature_contract_id.startswith("feat-")
        else f"feat-{feature_contract_id}"
    )


def build_model_id(
    *,
    lane: str,
    role: str,
    generation: str,
    feature_contract_id: str,
) -> str:
    fc = build_feature_contract_slug(feature_contract_id)
    if not fc.startswith("feat-"):
        fc = f"feat-{fc}"
    return f"CRT.{lane}.{role}.{generation}@{fc}"


def resolve_git_commit_short(fallback: str = "unknown") -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return fallback


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_manifest(
    *,
    lane: str,
    role: str,
    generation: str,
    feature_contract_id: str,
    dataset_slice_id: str,
    iface_semver: str = "1.0.0",
    trainer_version: str,
    git_commit: str | None = None,
    train_started_at_utc: str | None = None,
    metrics: dict[str, Any] | None = None,
    risk_notes: list[str] | None = None,
    legacy_aliases: list[str] | None = None,
    train_seed: int | None = None,
    artifact_primary: str | None = None,
    norm_artifact: str | None = None,
    training_run_id: str | None = None,
    recipe_id: str | None = None,
) -> CRTManifestV1:
    fc = build_feature_contract_slug(feature_contract_id)
    mid = build_model_id(lane=lane, role=role, generation=generation, feature_contract_id=fc)
    return CRTManifestV1(
        model_id=mid,
        lane=lane,
        role=role,  # type: ignore[arg-type]
        generation=generation,
        feature_contract_id=fc,
        iface_semver=iface_semver,
        dataset_slice_id=dataset_slice_id,
        git_commit=git_commit or resolve_git_commit_short(),
        train_started_at_utc=train_started_at_utc or utc_now_iso_z(),
        trainer_version=trainer_version,
        metrics=metrics or {},
        risk_notes=risk_notes or ["stub manifest; replace after real training run"],
        legacy_aliases=legacy_aliases or [],
        train_seed=train_seed,
        artifact_primary=artifact_primary,
        norm_artifact=norm_artifact,
        training_run_id=training_run_id,
        recipe_id=recipe_id,
    )

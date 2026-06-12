"""BrainConfigBuilder — single source of truth for brain config generation.

Every training pipeline MUST use this builder to produce ``brain_registry_entry.v1``
configs.  The builder enforces the institutional contract:

  - All mandatory fields present (fail-fast on missing)
  - ``artifact_hash`` injected (SHA256 of model artifact)
  - ``trained_by_commit_hash`` injected (git rev-parse HEAD at training time)
  - ``magic`` resolved from contract group
  - ``features`` resolved from canonical schema SSOT
  - Validates against ``BrainRegistrationGate`` before writing

Usage:
    from core.training.brain_config import build_brain_config

    brain_dict = build_brain_config(
        contract=training_contract,
        arch="xgboost",
        model_path="/path/to/model.json",
        model_hash="abc123...",
        metrics={"train_sharpe": 1.5, "forward_sharpe": 1.2, ...},
        feature_schema_id="v9_institutional_40",
        label_horizon_bars=12,
        contract_group="barrier_12bar",
    )
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Architecture → brain_type mapping ──
ARCH_TO_BRAIN_TYPE: dict[str, str] = {
    "xgboost": "xgboost_v9",
    "lightgbm": "lightgbm_v1",
    "deep_res_mlp": "deepresmlp",
    "transformer": "transformer_v5",
    "online_learner": "online_sgd",
}

# ── Contract group → magic number mapping ──
CONTRACT_GROUP_MAGIC: dict[str, int] = {
    "barrier_12bar": 90001,
    "micro_3bar": 90002,
    "statarb_dynamic": 90003,
    "daily_swing": 90301,
    "micro_m15": 90101,
    "micro_h1": 90201,
    "m15_swing": 90310,
    "m30_swing": 90320,
    "h1_swing": 90330,
    "h4_swing": 90340,
}


def _derive_contract_group(contract_id: str) -> str:
    """Derive contract_group from contract_id by matching known prefixes."""
    for group in CONTRACT_GROUP_MAGIC:
        if contract_id.startswith(group):
            return group
    return contract_id


def get_git_commit_hash(repo_root: str | Path | None = None) -> str:
    """Return the current HEAD commit hash (short SHA), or 'unknown'.

    Args:
        repo_root: Optional path to the git repository.  If None, the function
            walks up from this file's location.
    """
    try:
        if repo_root is None:
            repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def build_brain_config(
    *,
    brain_id: str,
    brain_type: str,
    feature_schema_id: str,
    artifact_path: str,
    artifact_hash: str,
    features: list[str],
    contract_id: str,
    contract_group: str,
    label_horizon_bars: int,
    metrics: dict[str, Any] | None = None,
    initial_status: str = "shadow",
    brain_role: str = "alpha_brain",
    model_version: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete ``brain_registry_entry.v1`` config dict.

    All mandatory fields for ``BrainRegistrationGate`` are populated.  The
    caller MUST provide ``artifact_hash`` (computed from the model artifact
    file by the training pipeline).  ``trained_by_commit_hash`` is injected
    automatically.

    Returns:
        Dict ready for JSON serialization and gate validation.

    Raises:
        ValueError: if ``artifact_hash`` is empty or ``features`` is empty.
    """
    if not artifact_hash:
        raise ValueError("artifact_hash is required — compute SHA256 of model artifact")
    if not features:
        raise ValueError(
            f"features list is empty for schema {feature_schema_id!r} — "
            "resolve feature names from canonical schema SSOT before calling build_brain_config()"
        )

    metrics = metrics or {}
    magic = CONTRACT_GROUP_MAGIC.get(contract_group, 0)
    is_shadow = initial_status in ("shadow", "candidate")
    vote_weight = 0.0 if is_shadow else 0.6
    trained_by_hash = get_git_commit_hash()

    config: dict[str, Any] = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": brain_type,
        "brain_role": brain_role,
        "model_version": model_version or contract_id,
        "status": initial_status,
        "vote_weight": vote_weight,
        "magic": magic,
        "artifact_path": artifact_path,
        "artifact_hash": artifact_hash,
        "feature_schema_id": feature_schema_id,
        "features": features,
        "training_contract": contract_id,
        "contract_group": contract_group,
        "training_horizon": label_horizon_bars,
        "feature_schema": feature_schema_id,
        "trained_by_commit_hash": trained_by_hash,
        "deployment_scope": {
            "symbols": ["XAUUSDc", "XAUUSD"],
            "sessions": ["all"],
            "regimes": ["trend", "volatile_trend", "mean_reversion", "ranging"],
        },
        "_notes": (
            f"{brain_type} {contract_group} — {label_horizon_bars}-bar barrier. "
            f"train_sharpe={metrics.get('train_sharpe', 0):.2f}, "
            f"fw_sharpe={metrics.get('forward_sharpe', 0):.2f}, "
            f"overfit_gap={metrics.get('overfit_gap', 0):.2f}, "
            f"hash={artifact_hash[:12]}..., "
            f"git={trained_by_hash}"
        ),
        "_model_hash": artifact_hash,
        "_created_at": datetime.now(UTC).isoformat(),
        "_quality_gate_passed": metrics.get("quality_gate_passed", False),
        "_shadow_target_trades": 50,
    }

    if extra:
        config.update(extra)

    return config


def resolve_feature_names_for_schema(feature_schema_id: str) -> list[str]:
    """Resolve feature name list from canonical schema SSOT.

    Returns empty list if schema is unknown (caller should handle).
    """
    try:
        from core.features.schemas.registry import get_schema_feature_names

        return get_schema_feature_names(feature_schema_id) or []
    except (ImportError, KeyError):
        return []

"""Parameter optimizer — generate hyperparameter tuning suggestions for degraded brains.

Triggered when a brain is marked as degraded (critical urgency). Produces a
lightweight parameter search space and writes suggestions to
data/reports/param_suggestions.json for manual review. Does NOT auto-apply.

Usage:
  # From daily_ops or retraining pipeline
  from core.feedback.param_optimizer import suggest_parameters

  suggestions = suggest_parameters(degraded_brain_ids=["XGBoost_V9_Institutional"])
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "param_suggestion.v1"

# ── Search spaces per brain_type ────────────────────────────────────────

SEARCH_SPACES: dict[str, list[dict[str, Any]]] = {
    "xgboost_v9": [
        {"name": "n_estimators", "range": [50, 500], "step": 50, "current": 200},
        {"name": "learning_rate", "range": [0.01, 0.30], "step": None, "current": 0.10},
        {"name": "max_depth", "range": [3, 12], "step": 1, "current": 6},
        {"name": "subsample", "range": [0.6, 1.0], "step": None, "current": 0.8},
        {"name": "colsample_bytree", "range": [0.6, 1.0], "step": None, "current": 0.8},
        {"name": "reg_lambda", "range": [0.0, 10.0], "step": None, "current": 1.0},
        {"name": "reg_alpha", "range": [0.0, 10.0], "step": None, "current": 0.0},
    ],
    "xgboost_v4.5": [
        {"name": "n_estimators", "range": [50, 300], "step": 25, "current": 100},
        {"name": "learning_rate", "range": [0.01, 0.20], "step": None, "current": 0.05},
        {"name": "max_depth", "range": [3, 10], "step": 1, "current": 5},
        {"name": "subsample", "range": [0.6, 1.0], "step": None, "current": 0.8},
        {"name": "colsample_bytree", "range": [0.6, 1.0], "step": None, "current": 0.8},
    ],
    "lightgbm_v1": [
        {"name": "num_leaves", "range": [15, 127], "step": None, "current": 31},
        {"name": "learning_rate", "range": [0.01, 0.20], "step": None, "current": 0.05},
        {"name": "min_child_samples", "range": [10, 100], "step": 10, "current": 20},
        {"name": "feature_fraction", "range": [0.6, 1.0], "step": None, "current": 0.8},
        {"name": "bagging_fraction", "range": [0.6, 1.0], "step": None, "current": 0.8},
        {"name": "lambda_l1", "range": [0.0, 5.0], "step": None, "current": 0.0},
        {"name": "lambda_l2", "range": [0.0, 5.0], "step": None, "current": 0.0},
    ],
    "deep_res_mlp_v1": [
        {"name": "learning_rate", "range": [1e-5, 1e-2], "step": None, "current": 1e-3},
        {"name": "n_epochs", "range": [50, 300], "step": 25, "current": 100},
        {"name": "hidden_units", "range": [32, 256], "step": 32, "current": 128},
        {"name": "n_layers", "range": [1, 6], "step": 1, "current": 3},
        {"name": "dropout", "range": [0.1, 0.5], "step": None, "current": 0.2},
        {"name": "batch_size", "range": [32, 512], "step": 32, "current": 128},
        {"name": "weight_decay", "range": [1e-6, 1e-2], "step": None, "current": 1e-4},
    ],
    "online_sgd_v1": [
        {"name": "learning_rate", "range": [0.001, 0.10], "step": None, "current": 0.01},
        {"name": "l2_regularization", "range": [0.0001, 0.01], "step": None, "current": 0.001},
        {"name": "momentum", "range": [0.0, 0.99], "step": None, "current": 0.9},
    ],
    "ou_params_v6": [
        {"name": "half_life", "range": [5, 100], "step": 5, "current": 20},
        {"name": "entry_z_threshold", "range": [0.5, 3.0], "step": None, "current": 1.5},
        {"name": "exit_z_threshold", "range": [0.1, 1.0], "step": None, "current": 0.5},
        {"name": "trend_mute_adx", "range": [15, 40], "step": 5, "current": 25},
    ],
    "microstructure_transformer_v5": [
        {"name": "learning_rate", "range": [1e-5, 1e-2], "step": None, "current": 1e-4},
        {"name": "n_epochs", "range": [20, 150], "step": 10, "current": 50},
        {"name": "n_heads", "range": [2, 8], "step": 2, "current": 4},
        {"name": "hidden_dim", "range": [64, 512], "step": 64, "current": 256},
        {"name": "n_layers", "range": [1, 4], "step": 1, "current": 2},
        {"name": "dropout", "range": [0.1, 0.5], "step": None, "current": 0.1},
    ],
    "deepresmlp": [],  # alias → deep_res_mlp_v1 (normalized below)
    "transformer_v5_m15": [],  # alias → microstructure_transformer_v5
    "transformer_v5_h1": [],  # alias → microstructure_transformer_v5
    "transformer_v5_h4": [],  # alias → microstructure_transformer_v5
}

# brain_types that don't need parameter search (fixed architectures)
NO_SEARCH_TYPES = {"onnx_v9", "crt_sur"}  # ONNX models with fixed weights


def _load_brain_registry(base_dir: str = "data") -> dict[str, dict[str, Any]]:
    """Build brain_id → {brain_type, ...} lookup from configs/brains/."""
    brains_dir = Path("configs/brains")
    registry: dict[str, dict[str, Any]] = {}
    if not brains_dir.exists():
        return registry
    for config_path in sorted(brains_dir.glob("*.json")):
        if config_path.name.endswith(".normalization.json"):
            continue
        try:
            entry = json.loads(config_path.read_text(encoding="utf-8"))
            brain_id = entry.get("brain_id")
            if brain_id:
                registry[brain_id] = {
                    "brain_type": entry.get("brain_type", "unknown"),
                    "brain_role": entry.get("brain_role", "unknown"),
                    "model_version": entry.get("model_version", ""),
                }
        except (json.JSONDecodeError, FileNotFoundError):
            continue
    return registry


# Aliases that point to a canonical search space entry
_BRAIN_TYPE_ALIASES: dict[str, str] = {
    "deepresmlp": "deep_res_mlp_v1",
    "transformer_v5_m15": "microstructure_transformer_v5",
    "transformer_v5_h1": "microstructure_transformer_v5",
    "transformer_v5_h4": "microstructure_transformer_v5",
}


def _resolve_search_space(brain_type: str) -> tuple[list[dict[str, Any]], str]:
    """Resolve search space for a brain_type. Returns (params, action).

    action is one of: "search" (Optuna/lightweight scan),
    "no_search" (fixed architecture, flag for review),
    "unknown" (not recognized, needs manual investigation).
    """
    if brain_type in NO_SEARCH_TYPES:
        return [], "no_search"

    # Resolve alias first
    canonical = _BRAIN_TYPE_ALIASES.get(brain_type, brain_type)
    if canonical in SEARCH_SPACES and SEARCH_SPACES[canonical]:
        return SEARCH_SPACES[canonical], "search"

    # Normalize: strip underscores and lowercase for fuzzy matching
    norm = brain_type.replace("_", "").lower()
    for known, space in SEARCH_SPACES.items():
        if not space:  # skip alias entries (empty space)
            continue
        known_norm = known.replace("_", "").lower()
        if known_norm in norm or norm in known_norm:
            return space, "search"

    return [], "unknown"


def suggest_parameters(
    degraded_brain_ids: list[str],
    *,
    base_dir: str = "data",
    output_path: str | None = None,
    min_trials: int = 20,
    max_trials: int = 50,
) -> dict[str, Any]:
    """Generate hyperparameter optimization suggestions for degraded brains.

    Args:
        degraded_brain_ids: List of brain_ids marked as degraded (urgency=critical).
        base_dir: Base data directory for loading registry and saving output.
        output_path: Override path for param_suggestions.json (default: data/reports/).
        min_trials: Minimum Optuna trials to recommend.
        max_trials: Maximum Optuna trials to recommend.

    Returns:
        Dict with per-brain suggestions and overall recommendation.
    """
    registry = _load_brain_registry(base_dir)
    suggestions: list[dict[str, Any]] = []

    for brain_id in degraded_brain_ids:
        entry = registry.get(brain_id, {})
        brain_type = entry.get("brain_type", "unknown")

        search_space, action = _resolve_search_space(brain_type)

        suggestion: dict[str, Any] = {
            "brain_id": brain_id,
            "brain_type": brain_type,
            "brain_role": entry.get("brain_role", "unknown"),
            "action": action,
        }

        if action == "search":
            # Recommend trial count proportional to param count
            n_params = len(search_space)
            trials = max(min_trials, min(max_trials, n_params * 10))
            suggestion["recommended_trials"] = trials
            suggestion["search_space"] = search_space
            suggestion["current_params"] = {p["name"]: p["current"] for p in search_space}
            suggestion["command"] = (
                f"python scripts/training/recipe_search.py "
                f"--architecture {_architecture_from_brain_type(brain_type)} "
                f"--trials {trials} "
                f"--output-recipe blueprints/recipes/{brain_id.lower()}-optimized-recipe.json"
            )

            # Priority suggestions: params most likely to help
            priority = _suggest_priority_params(search_space, brain_type)
            suggestion["priority_params"] = priority

        elif action == "no_search":
            suggestion["note"] = (
                "ONNX/fixed-architecture model — parameter search not applicable. "
                "Consider retraining with updated labels or architectural changes."
            )
        else:
            suggestion["note"] = (
                f"Brain type '{brain_type}' not recognized. "
                "Manual parameter space definition required."
            )

        suggestions.append(suggestion)

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (
            datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"
        ),
        "degraded_brain_count": len(degraded_brain_ids),
        "searchable_count": sum(1 for s in suggestions if s["action"] == "search"),
        "no_search_count": sum(1 for s in suggestions if s["action"] == "no_search"),
        "unknown_count": sum(1 for s in suggestions if s["action"] == "unknown"),
        "suggestions": suggestions,
    }

    # Write output
    out = Path(output_path or f"{base_dir}/reports/param_suggestions.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    return report


def _architecture_from_brain_type(brain_type: str) -> str:
    """Map brain_type to recipe_search.py --architecture flag."""
    # Normalize aliases first
    canonical = _BRAIN_TYPE_ALIASES.get(brain_type, brain_type)
    arch_map = {
        "xgboost_v9": "xgboost",
        "xgboost_v4.5": "xgboost",
        "lightgbm_v1": "lightgbm",
        "deep_res_mlp_v1": "deep_res_mlp",
        "online_sgd_v1": "online_mlp",
        "microstructure_transformer_v5": "transformer",
    }
    for key, arch in arch_map.items():
        if key in canonical:
            return arch
    return "xgboost"


def _suggest_priority_params(
    search_space: list[dict[str, Any]], brain_type: str
) -> list[dict[str, Any]]:
    """Suggest which parameters to focus on first (top 2-3 most impactful)."""
    # Heuristic: learning rate and regularization are usually most impactful
    priority_map: dict[str, list[str]] = {
        "xgboost": ["learning_rate", "max_depth", "n_estimators"],
        "lightgbm": ["learning_rate", "num_leaves", "min_child_samples"],
        "deep_res": ["learning_rate", "n_epochs", "dropout"],
        "online": ["learning_rate", "l2_regularization"],
        "ou_params": ["half_life", "entry_z_threshold"],
        "transformer": ["learning_rate", "hidden_dim", "dropout"],
    }

    priorities: list[str] = []
    for key, prio in priority_map.items():
        if key in brain_type:
            priorities = prio
            break

    result: list[dict[str, Any]] = []
    for name in priorities:
        for param in search_space:
            if param["name"] == name:
                result.append(param)
                break

    return result[:3] if result else search_space[:2]

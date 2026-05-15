"""Centralised default paths for brain configs, normalisation, and models.

All modules that need a default brain or feature path should import from here
rather than hardcoding strings.  This makes reference-integrity auditing trivial:
scan one file instead of the entire codebase.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── Brain entry defaults (required at startup) ──
DEFAULT_BRAIN_ENTRY = "configs/brains/deepresmlp_v2_new.json"
DEFAULT_NORM_CONFIG = "configs/brains/v9_institutional_01.normalization.json"

# ── Feature store ──
DEFAULT_FEATURE_STORE_DIR = "data/feature_store"

# ── Microstructure adapter (no external scaler needed for H4 model) ──
MICROSTRUCTURE_SCALER_PATH: str | None = None

# ── Online learner (optional — only if online brain is deployed) ──
ONLINE_BRAIN_PATH = "configs/brains/online_learner_v1.json"
ONLINE_WEIGHTS_PATH = "data/models/online_learner_weights.json"

# ── Meta exit model (trained on historical exits — optional) ──
META_EXIT_MODEL_PATH = "data/models/meta_exit_model.txt"

# ── Live config ──
LIVE_YAML_PATH = "configs/live.yaml"

# ── Governance (created at runtime — not validated at startup) ──
GOVERNANCE_STATE_PATH = "data/governance_state.json"
BRAIN_PERFORMANCE_PATH = "data/brain_performance.json"
BRAIN_PNL_LEDGER_PATH = "data/brain_pnl_ledger.json"

# ── Brains directory ──
BRAINS_DIR = "configs/brains"
RETIRED_BRAINS_DIR = "configs/brains/retired"

# Paths that MUST exist at startup for the pipeline to function.
REQUIRED_PATHS: set[str] = {
    "DEFAULT_BRAIN_ENTRY",
    "DEFAULT_NORM_CONFIG",
    "LIVE_YAML_PATH",
}

# Paths that MAY be missing (runtime-created or optional features).
OPTIONAL_PATHS: set[str] = {
    "MICROSTRUCTURE_SCALER_PATH",
    "ONLINE_BRAIN_PATH",
    "ONLINE_WEIGHTS_PATH",
    "META_EXIT_MODEL_PATH",
    "GOVERNANCE_STATE_PATH",
    "BRAIN_PERFORMANCE_PATH",
    "BRAIN_PNL_LEDGER_PATH",
}


def resolve(path: str) -> Path:
    """Resolve a project-relative path to an absolute one."""
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def validate_defaults() -> dict[str, bool]:
    """Check every REQUIRED path exists.  Returns {name: exists} — only failing entries."""
    _checks: dict[str, bool] = {}
    for name in REQUIRED_PATHS:
        val = globals().get(name)
        if val is None or not isinstance(val, str):
            continue
        p = resolve(val)
        if not p.exists():
            _checks[name] = False
    return _checks

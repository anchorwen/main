"""Single source of truth for all feature schema dimensions and feature names.

Every other module that needs to know a schema's dimension or feature name list
MUST import from here.  There are no other copies of SCHEMA_DIMENSIONS anywhere.

Adding a new schema:
  1. Add the dimension entry to SCHEMA_DIMENSIONS below.
  2. Add feature name resolution to _get_schema_feature_names().
  3. Run `python scripts/verify.py --quick` to confirm no stale duplicates.
"""

from __future__ import annotations

from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES
from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.schemas.v9_micro_schema import V9_MICRO_49_FEATURES

# ── Schema name → expected feature count ──────────────────────────
SCHEMA_DIMENSIONS: dict[str, int] = {
    "v9_institutional_40": 40,
    "v9_micro_49": 49,
    "v4.5_microstructure_9": 9,
    "v2_microstructure_9": 9,
    "v2_microstructure_288": 288,
    "v4.3_microstructure_9": 9,
    "daily_swing_24": 24,
    "swing_24": 24,
    "v6_price_series_1": 1,
    "meta_stage2_runtime_47": 47,
    "meta_stage2_runtime_48": 48,
    "meta_stage2_runtime_56": 56,
    "meta_stage2_runtime_59": 59,
    "v9_40dim_ou3": 43,  # 40 V9 institutional + 3 OU physics (z_score, half_life, theta)
}

# Canonical name resolution (alias → canonical)
SCHEMA_ALIASES: dict[str, str] = {
    "swing_24": "daily_swing_24",
}

# ── Lazy cache for feature name resolution ─────────────────────────
_SCHEMA_FEATURE_NAMES_CACHE: dict[str, list[str]] = {}

# Meta-feature lists shared across meta_stage2_runtime_* schemas
_META_FEATURES_RUNTIME_7: list[str] = [
    "oof_pred",
    "oof_pred_zscore_20",
    "atr_percentile_100",
    "vol_zscore",
    "hurst_m5",
    "session_sin",
    "session_cos",
]

_META_FEATURES_RUNTIME_8: list[str] = _META_FEATURES_RUNTIME_7 + [
    "rolling_hit_rate_20",
]

_META_FEATURES_RUNTIME_10: list[str] = _META_FEATURES_RUNTIME_7 + [
    "spread_zscore",
    "oim_divergence",
    "toxicity_score",
]

_OU_FEATURES: list[str] = ["ou_z_score", "ou_half_life", "ou_theta"]


def get_schema_dimension(schema_name: str) -> int:
    """Return the expected feature count for *schema_name*.

    Raises ``KeyError`` if the schema is unknown — there is no silent default.
    """
    canonical = SCHEMA_ALIASES.get(schema_name, schema_name)
    return SCHEMA_DIMENSIONS[canonical]


def get_schema_feature_names(schema_name: str) -> list[str]:
    """Return the canonical feature name list for *schema_name*.

    Returns an empty list (not None) when the schema exists but has no
    feature-name mapping.  Raises ``KeyError`` when the schema itself is
    unknown.
    """
    canonical = SCHEMA_ALIASES.get(schema_name, schema_name)
    if canonical not in SCHEMA_DIMENSIONS:
        raise KeyError(f"Unknown schema: {schema_name!r} (canonical: {canonical!r})")

    if canonical in _SCHEMA_FEATURE_NAMES_CACHE:
        return _SCHEMA_FEATURE_NAMES_CACHE[canonical]

    names: list[str]

    if canonical == "v9_institutional_40":
        names = list(V9_INSTITUTIONAL_40_FEATURES)
    elif canonical == "v9_micro_49":
        names = list(V9_MICRO_49_FEATURES)
    elif canonical in ("daily_swing_24",):
        names = list(DAILY_SWING_24_FEATURES)
    elif canonical in ("v4.5_microstructure_9", "v2_microstructure_9", "v4.3_microstructure_9"):
        names = list(MICROSTRUCTURE_9_FEATURES)
    elif canonical == "v2_microstructure_288":
        names = list(MICROSTRUCTURE_9_FEATURES) * 32
    elif canonical == "v6_price_series_1":
        names = ["price_return"]
    elif canonical == "v9_40dim_ou3":
        names = list(V9_INSTITUTIONAL_40_FEATURES) + _OU_FEATURES
    elif canonical == "meta_stage2_runtime_47":
        names = list(V9_INSTITUTIONAL_40_FEATURES) + _META_FEATURES_RUNTIME_7
    elif canonical == "meta_stage2_runtime_48":
        names = list(V9_INSTITUTIONAL_40_FEATURES) + _META_FEATURES_RUNTIME_8
    elif canonical == "meta_stage2_runtime_56":
        names = list(V9_MICRO_49_FEATURES) + _META_FEATURES_RUNTIME_7
    elif canonical == "meta_stage2_runtime_59":
        names = list(V9_MICRO_49_FEATURES) + _META_FEATURES_RUNTIME_10
    else:
        # Schema is registered in SCHEMA_DIMENSIONS but has no feature-name
        # resolution yet — return empty list so callers can degrade gracefully.
        names = []

    _SCHEMA_FEATURE_NAMES_CACHE[canonical] = names
    return names

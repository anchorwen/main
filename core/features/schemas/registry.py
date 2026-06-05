"""Single source of truth for all feature schema dimensions and feature names.

Every other module that needs to know a schema's dimension or feature name list
MUST import from here.  There are no other copies of SCHEMA_DIMENSIONS anywhere.

Adding a new schema:
  1. Add the dimension entry to SCHEMA_DIMENSIONS below.
  2. Add feature name resolution to _get_schema_feature_names().
  3. Run `python scripts/verify.py --quick` to confirm no stale duplicates.
"""

from __future__ import annotations

from typing import Any

from core.features.schemas.btc_macro_enhanced_schema import BTC_MACRO_ENHANCED_37_FEATURES
from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES
from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES
from core.features.schemas.swing_enhanced_schema import (
    SWING_ENHANCED_21_FEATURES,
    SWING_ENHANCED_29_FEATURES,
    SWING_ENHANCED_35_FEATURES,
)
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
    "swing_enhanced_35": 35,  # 24 swing macro + 9 micro + 2 TF-specific (OU_Theta, Hurst)
    "swing_enhanced_29": 29,  # 21 swing macro + 6 micro + 2 TF (XAU cross-asset removed for BTC)
    "swing_enhanced_21": 21,  # 21 swing macro only — pure daily, no micro/TF
    # FIX-20260604-081: BTC-specific 37-dim macro-enhanced schema
    "btc_macro_enhanced_37": 37,  # 24 BTC macro + 9 BTC micro + 2 TF + 2 BTC/XAU ratio
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
    elif canonical == "swing_enhanced_35":
        names = list(SWING_ENHANCED_35_FEATURES)
    elif canonical == "swing_enhanced_29":
        names = list(SWING_ENHANCED_29_FEATURES)
    elif canonical == "swing_enhanced_21":
        names = list(SWING_ENHANCED_21_FEATURES)
    elif canonical == "btc_macro_enhanced_37":
        names = list(BTC_MACRO_ENHANCED_37_FEATURES)
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


# ── FIX-20260531-021: Data-driven swing feature assembly ──────────────────
# Eliminates hardcoded if/elif chains in live_cycle.py.
# Adding a new swing schema now only requires updating this file.

# Indices of XAU cross-asset features in the 35-dim concatenation
# FIX-20260531-026: XAU cross-asset feature indices derived from feature names.
# No more hardcoded {12,13,14,30,31,32} that silently break on reorder.
def _derive_xau_indices(feature_list: list[str]) -> set[int]:
    """Return indices of XAU cross-asset features in feature_list."""
    _XAU_PATTERNS = {
        "Cross_Gold", "Cross_DXY", "Cross_EUR", "Cross_Silver",
        "XAGUSD", "EURUSD", "USDJPY",
    }
    return {i for i, f in enumerate(feature_list)
            if any(p in f for p in _XAU_PATTERNS)}

_XAU_35_INDICES: set[int] = _derive_xau_indices(
    __import__("core.features.schemas.swing_enhanced_schema", fromlist=["SWING_ENHANCED_35_FEATURES"])
    .SWING_ENHANCED_35_FEATURES
)
_XAU_24_INDICES: set[int] = _derive_xau_indices(
    __import__("core.features.schemas.daily_swing_schema", fromlist=["DAILY_SWING_24_FEATURES"])
    .DAILY_SWING_24_FEATURES
)


def assemble_swing_features(
    schema_id: str,
    daily_features: Any,
    *,
    micro_features: Any = None,
    tf_ou: float = 0.0,
    tf_hurst: float = 0.5,
):
    """Build a swing feature vector from components using schema metadata.

    .. deprecated::
        Use ``core.features.feature_assembler.assemble_features_by_schema()``
        for new strategy code.  This function remains for backward compat with
        ``live_cycle.py`` management-phase re-evaluation (FIX-60531-021).

    Returns the assembled feature vector at the correct dimension.
    Raises ValueError if schema_id is unknown.
    """
    from core.features.feature_assembler import _build_swing_vector
    return _build_swing_vector(
        schema_id,
        daily_features,
        micro_features=micro_features,
        tf_ou=tf_ou,
        tf_hurst=tf_hurst,
    )

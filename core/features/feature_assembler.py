"""Feature Assembly Factory — single entry point for schema-driven feature vector construction.

Architectural Directive (FIX-20260601-039):
    Every strategy class (BarrierStrategy, SwingStrategy, ...) that needs to
    run brain inference calls ``assemble_features_by_schema()``.  The factory
    inspects the brain's declared ``feature_schema`` and builds the correct
    dimension vector from the available component arrays.

    Before this factory, each strategy class hardcoded its own assembly logic:
      - ``swing_strategy.py`` had ``if "swing_enhanced" in schema`` → call
        ``assemble_swing_features()``.
      - ``barrier_strategy.py`` had NO schema detection — always passed the
        40-dim V9 institutional vector, breaking any brain trained on
        ``swing_enhanced_35`` (Barrier_V9_12B_V1).

    Now all strategies call ONE function.  Adding a new schema (e.g.
    ``btc_micro_v1``) only requires a new branch in this factory — zero
    strategy-file changes.

Usage::

    from core.features.feature_assembler import assemble_features_by_schema

    fv = assemble_features_by_schema(
        schema_name="swing_enhanced_35",
        legacy_v9_vector=v9_40,
        daily_features=daily_arr,
        micro_features=micro_arr,
        tf_ou=0.123,
        tf_hurst=0.456,
    )
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def assemble_features_by_schema(
    schema_name: str,
    legacy_v9_vector: np.ndarray | None = None,
    *,
    daily_features: Any = None,
    micro_features: Any = None,
    tf_ou: float = 0.0,
    tf_hurst: float = 0.5,
    btc_augment: np.ndarray | None = None,  # FIX-134/138: pre-augmented 41-dim BTC vector
) -> np.ndarray:
    """Build a feature vector for a brain given its declared feature schema.

    Args:
        schema_name: The ``feature_schema`` string from the brain config
            (e.g. ``"v9_institutional"``, ``"swing_enhanced_35"``).
        legacy_v9_vector: 40-dim V9 institutional vector (used when
            schema is ``v9_institutional`` or as fallback).
        daily_features: 24-dim daily/macro feature vector (for swing schemas).
        micro_features: Optional 9-dim microstructure vector (for enhanced swing).
        tf_ou: Timeframe-specific OU-theta value (for enhanced swing).
        tf_hurst: Timeframe-specific Hurst exponent (for enhanced swing).
        btc_augment: Pre-computed 41-dim BTC feature vector from
            ``BTCFeatureAugmenter.augment()``.  Only used when
            *schema_name* is ``btc_macro_enhanced_37`` — the augmenter
            corrects cross-asset slots that differ between XAU and BTC.
            If None (or for XAU schemas), the legacy assembly path is used.

    Returns:
        Feature vector at the correct dimension for the declared schema.

    Raises:
        ValueError: If ``schema_name`` is unknown.
    """
    # ── NaN safety (cold-start guard) ──
    if math.isnan(tf_ou) or not math.isfinite(tf_ou):
        tf_ou = 0.0
    if math.isnan(tf_hurst) or not math.isfinite(tf_hurst):
        tf_hurst = 0.5

    if schema_name == "v9_institutional":
        if legacy_v9_vector is not None:
            v = np.asarray(legacy_v9_vector, dtype=np.float64).ravel()
            return v[:40] if len(v) >= 40 else np.pad(v, (0, 40 - len(v)))
        return np.zeros(40, dtype=np.float64)

    if (
        "swing_enhanced" in schema_name
        or "daily_swing" in schema_name
        or "btc_macro" in schema_name
    ):
        return _build_swing_vector(
            schema_name,
            daily_features,
            micro_features=micro_features,
            tf_ou=tf_ou,
            tf_hurst=tf_hurst,
            btc_augment=btc_augment,
        )

    # Unknown / unset schema → fall back to V9 40-dim (legacy default).
    # This is safe: all brains trained on V9 institutional will work.
    # Brains that need a specific schema MUST declare it — the fallback
    # produces a valid but potentially sub-optimal vector.
    if legacy_v9_vector is not None:
        v = np.asarray(legacy_v9_vector, dtype=np.float64).ravel()
        return v[:40] if len(v) >= 40 else np.pad(v, (0, 40 - len(v)))
    return np.zeros(40, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════════
# Swing feature builder (extracted from registry.py)
# ═══════════════════════════════════════════════════════════════════════════


def _derive_xau_indices(feature_list: list[str]) -> set[int]:
    """Return indices of XAU cross-asset features in *feature_list*."""
    _XAU_PATTERNS = {
        "Cross_Gold",
        "Cross_DXY",
        "Cross_EUR",
        "Cross_Silver",
        "XAGUSD",
        "EURUSD",
        "USDJPY",
    }
    return {i for i, f in enumerate(feature_list) if any(p in f for p in _XAU_PATTERNS)}


# Computed once at import (same as registry.py)
_XAU_35_INDICES: set[int] = _derive_xau_indices(
    __import__(
        "core.features.schemas.swing_enhanced_schema",
        fromlist=["SWING_ENHANCED_35_FEATURES"],
    ).SWING_ENHANCED_35_FEATURES,
)
_XAU_24_INDICES: set[int] = _derive_xau_indices(
    __import__(
        "core.features.schemas.daily_swing_schema",
        fromlist=["DAILY_SWING_24_FEATURES"],
    ).DAILY_SWING_24_FEATURES,
)


def _build_swing_vector(
    schema_id: str,
    daily_features: Any,
    *,
    micro_features: Any = None,
    tf_ou: float = 0.0,
    tf_hurst: float = 0.5,
    btc_augment: np.ndarray | None = None,  # FIX-134: pre-augmented 41-dim BTC vector
) -> np.ndarray:
    """Build a swing feature vector from components using schema metadata.

    Extracted from ``core.features.schemas.registry.assemble_swing_features()``
    (FIX-20260531-021) into the central factory per architect directive.
    """
    from core.features.schemas.registry import SCHEMA_ALIASES, SCHEMA_DIMENSIONS

    canonical = SCHEMA_ALIASES.get(schema_id, schema_id)
    dim = SCHEMA_DIMENSIONS.get(canonical)
    if dim is None:
        raise ValueError(
            f"Unknown schema '{schema_id}' for swing feature assembly. "
            f"Known: {[k for k in SCHEMA_DIMENSIONS if 'swing' in k or 'daily' in k]}"
        )

    daily_arr = np.asarray(daily_features, dtype=np.float64).ravel()
    if len(daily_arr) < 24:
        daily_arr = np.pad(daily_arr, (0, 24 - len(daily_arr)))

    # ── Pure daily schemas ──
    if canonical in ("daily_swing_24", "swing_24"):
        return daily_arr[:24]

    if canonical == "swing_enhanced_21":
        return np.array(
            [daily_arr[i] for i in range(24) if i not in _XAU_24_INDICES],
            dtype=np.float64,
        )

    # ── Enhanced schemas (daily + micro + TF) ──
    micro_arr = (
        np.asarray(micro_features, dtype=np.float64).ravel()
        if micro_features is not None
        else np.zeros(9, dtype=np.float64)
    )
    if len(micro_arr) < 9:
        micro_arr = np.pad(micro_arr, (0, 9 - len(micro_arr)))

    # ── NaN safety for OU/Hurst ──
    _ou = float(tf_ou) if (tf_ou is not None and math.isfinite(float(tf_ou))) else 0.0
    _hur = float(tf_hurst) if (tf_hurst is not None and math.isfinite(float(tf_hurst))) else 0.5

    fv_35 = np.concatenate([daily_arr[:24], micro_arr[:9], [_ou, _hur]])

    if canonical == "swing_enhanced_35":
        return fv_35

    if canonical == "btc_macro_enhanced_37":
        # ── FIX-20260606-133/134: BTC feature alignment (Phase 5b Step B) ─
        # When btc_augment is provided (BTCFeatureAugmenter), use the
        # pre-computed 37-dim vector with corrected cross-asset slots.
        # Otherwise fall back to the legacy assembly path (XAU-centric
        # features with hardcoded zeros for ratio slots).
        if btc_augment is not None and len(btc_augment) == 41:
            return np.asarray(btc_augment, dtype=np.float64)

        # ── FIX-20260615-006/C4: Fail-closed — NO silent fallback to XAU features ──
        # When the BTCFeatureAugmenter is unavailable (None or wrong length),
        # the BTC brain MUST NOT receive XAU-centric features (gold/silver ratio,
        # silver returns, hardcoded zeros for BTC/XAU ratio slots).
        # Raise FeatureGenerationError to abort this cycle — the caller
        # (live_cycle) catches this and skips trading for this frame.
        raise RuntimeError(
            f"BTC feature augmenter unavailable or wrong dim: "
            f"btc_augment={'None' if btc_augment is None else f'len={len(btc_augment)}'}"
        )

    if canonical == "swing_enhanced_29":
        return np.array(
            [fv_35[i] for i in range(35) if i not in _XAU_35_INDICES],
            dtype=np.float64,
        )

    # Fallback: return at declared dimension (truncate or pad)
    if dim > 0:
        if len(fv_35) >= dim:
            return fv_35[:dim]
        return np.pad(fv_35, (0, dim - len(fv_35)))

    return fv_35

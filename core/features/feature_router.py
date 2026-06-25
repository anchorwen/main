"""Feature Router — schema-contracted tensor dispatch.

FIX-20260616-092: Replaces array-concatenation (append/extend) with
dictionary-based feature lake + contract registry + Fail-Fast dispatch.
Eliminates silent zero-fill, dimension mismatch, and augmenter patches.

Architecture (IC Approved):
  1. Feature Lake   — compute ALL features, output key-value dict
  2. Contract Reg.  — per-schema recipe: ordered list of required keys
  3. Dispatcher     — extract exact tensor from lake by contract

Usage:
    router = FeatureRouter()
    tensor = router.dispatch(feature_lake, "btc_macro_enhanced_41")
    # → np.ndarray of shape (41,) with keys in contract order
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np


class FeatureMissingError(KeyError):
    """L3 physical circuit-breaker: contract demands a key the lake lacks."""


class SchemaNotFoundError(KeyError):
    """Unknown schema — no silent fallback."""


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Schema Contract Registry
# ═══════════════════════════════════════════════════════════════════════════

from core.features.schemas.registry import (
    get_schema_dimension,
    get_schema_feature_names,
)

# Load all feature name lists from the SSOT registry
SCHEMA_CONTRACTS: dict[str, list[str]] = {}

for _schema_id in [
    "btc_macro_enhanced_41",
    "btc_macro_enhanced_41_v2",  # FIX-20260625-137: clean contract
    "btc_macro_enhanced_37",  # legacy alias → 41 dims (kept for backward compat)
    "swing_enhanced_35",
    "swing_enhanced_29",
    "swing_enhanced_21",
    "v9_institutional_40",
    "v9_micro_49",
    "v9_40dim_ou3",
    "daily_swing_24",
]:
    try:
        _names = get_schema_feature_names(_schema_id)
        if _names:  # Only register schemas with explicit feature name lists
            SCHEMA_CONTRACTS[_schema_id] = _names
    except KeyError:
        pass  # Schema not yet registered with feature names


# ═══════════════════════════════════════════════════════════════════════════
# FIX-20260625-137: Legacy BTC 41-dim reorder shim
# ═══════════════════════════════════════════════════════════════════════════
# V4 was trained with the old training script (Order A) and has been trading
# live with corrupted feature values (Order C augmenter zipped with Order B
# schema names).  The augmenter now outputs in canonical Schema Order B.
# To keep V4's tensor BIT-IDENTICAL to pre-fix, this shim reverts the
# augmenter output back to Order C for the legacy "btc_macro_enhanced_41"
# schema only.  New models use "btc_macro_enhanced_41_v2" which skips the shim.
#
# Permutation: Order B → Order C (swap REGIME block with BTC_MACRO block)
#   Order B:  [35]=delta_ou [36]=delta_h [37]=ou*hurst [38]=ou/adx [39]=btc_au [40]=btc_au_roc
#   Order C:  [35]=btc_au   [36]=btc_au_roc [37]=delta_ou [38]=delta_h [39]=ou*hurst [40]=ou/adx
_LEGACY_BTC_41_PERMUTATION: tuple[tuple[int, int], ...] = (
    (35, 39),  # btc_xau_ratio     ← from pos 39
    (36, 40),  # btc_xau_ratio_roc ← from pos 40
    (37, 35),  # delta_ou          ← from pos 35
    (38, 36),  # delta_hurst       ← from pos 36
    (39, 37),  # ou_x_hurst        ← from pos 37
    (40, 38),  # ou_div_adx        ← from pos 38
)


def _apply_legacy_btc_41_shim(btc_vector: np.ndarray) -> np.ndarray:
    """Permute slots 35-40 from Schema Order B back to legacy Order C.

    This makes the tensor BIT-IDENTICAL to what V4 received before the
    FIX-20260625-137 augmenter refactor.  Only applied when the requested
    schema is ``"btc_macro_enhanced_41"`` (the legacy contract).
    """
    _v = np.copy(btc_vector)
    for _dst, _src in _LEGACY_BTC_41_PERMUTATION:
        _v[_dst] = btc_vector[_src]
    return _v


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Dispatcher Gateway
# ═══════════════════════════════════════════════════════════════════════════


class FeatureRouter:
    """Schema-contracted tensor dispatch from a feature lake dictionary."""

    def dispatch(
        self,
        feature_lake: dict[str, float],
        schema_name: str,
    ) -> np.ndarray:
        """Extract a tensor from *feature_lake* by *schema_name* contract.

        Args:
            feature_lake: Dict mapping feature keys to float values.
            schema_name: Contract identifier (e.g. "btc_macro_enhanced_41").

        Returns:
            np.ndarray of shape (N,) in contract-specified order.

        Raises:
            SchemaNotFoundError: *schema_name* is not a known contract.
            FeatureMissingError: A required key is missing from the lake.
        """
        required_keys = SCHEMA_CONTRACTS.get(schema_name)
        if required_keys is None:
            raise SchemaNotFoundError(
                f"Unknown schema: {schema_name!r}. " f"Known: {sorted(SCHEMA_CONTRACTS.keys())}"
            )

        try:
            tensor = np.asarray(
                [feature_lake[key] for key in required_keys],
                dtype=np.float64,
            )
        except KeyError as exc:
            missing = exc.args[0] if exc.args else str(exc)
            raise FeatureMissingError(
                f"Feature Lake missing required key {missing!r} "
                f"for schema {schema_name!r}. Lake has {len(feature_lake)} keys."
            ) from exc

        _expected = get_schema_dimension(schema_name)
        if len(tensor) != _expected:
            raise FeatureMissingError(
                f"Contract dimension mismatch: {schema_name!r} expects "
                f"{_expected} features, but contract list has {len(tensor)}"
            )

        return tensor

    def build_lake(
        self,
        legacy_v9_vector: Any = None,
        daily_features: Any = None,
        micro_features: Any = None,
        tf_ou: float = 0.0,
        tf_hurst: float = 0.5,
        btc_augment: Any = None,
        ou_z_score: float = 0.0,
        ou_half_life: float = 0.0,
        ou_theta: float = 0.0,
        extra_features: dict[str, float] | None = None,
        schema_name: str = "",  # FIX-20260625-137: controls legacy shim for BTC 41
    ) -> dict[str, float]:
        """Build a SUPERSET feature lake from ALL available sources.

        The lake is a flat key-value dict containing every feature the
        system can compute.  Future schemas can request any combination
        of keys — the dispatcher extracts only what the contract demands.

        Design principle: compute once, route many.  No feature is
        computed twice; no array is concatenated by position.
        """
        lake: dict[str, float] = {}

        # Source 1: V9 40-dim institutional vector (M5/M15/M30/H1 × 10 indicators)
        if legacy_v9_vector is not None:
            _v9 = np.asarray(legacy_v9_vector, dtype=np.float64).ravel()
            _v9_names = get_schema_feature_names("v9_institutional_40")
            if _v9_names and len(_v9) >= len(_v9_names):
                for name, val in zip(_v9_names, _v9, strict=False):
                    lake[name] = float(val)

        # Source 2: V9 Micro 49-dim (when available)
        _v9_micro_names = get_schema_feature_names("v9_micro_49")
        if _v9_micro_names and legacy_v9_vector is not None:
            _v9_full = np.asarray(legacy_v9_vector, dtype=np.float64).ravel()
            if len(_v9_full) >= 49:
                for name, val in zip(_v9_micro_names, _v9_full, strict=False):
                    lake[name] = float(val)

        # Source 3: Daily swing macro (24-dim: D1/H4/cross-market/calendar)
        if daily_features is not None:
            _daily = np.asarray(daily_features, dtype=np.float64).ravel()
            _daily_names = get_schema_feature_names("daily_swing_24")
            if _daily_names and len(_daily) >= len(_daily_names):
                for name, val in zip(_daily_names, _daily, strict=False):
                    lake[name] = float(val)

        # Source 4: Microstructure (9-dim: tick-level)
        if micro_features is not None:
            _micro = np.asarray(micro_features, dtype=np.float64).ravel()
            for _schema_name in ("v4.5_microstructure_9", "v2_microstructure_9"):
                _micro_names = get_schema_feature_names(_schema_name)
                if _micro_names and len(_micro) >= len(_micro_names):
                    for name, val in zip(_micro_names, _micro, strict=False):
                        lake[name] = float(val)
                    break  # Use the first schema that matches

        # Source 5: TF-specific scalars
        lake["TF_OU_Theta"] = (
            float(tf_ou) if (tf_ou is not None and np.isfinite(float(tf_ou))) else 0.0
        )
        lake["TF_Hurst"] = (
            float(tf_hurst) if (tf_hurst is not None and np.isfinite(float(tf_hurst))) else 0.5
        )

        # Source 6: OU physics (for v9_40dim_ou3 schema — MetaLabel/OU brains)
        lake["ou_z_score"] = (
            float(ou_z_score)
            if (ou_z_score is not None and np.isfinite(float(ou_z_score)))
            else 0.0
        )
        lake["ou_half_life"] = (
            float(ou_half_life)
            if (ou_half_life is not None and np.isfinite(float(ou_half_life)))
            else 0.0
        )
        lake["ou_theta"] = (
            float(ou_theta) if (ou_theta is not None and np.isfinite(float(ou_theta))) else 0.0
        )

        # Source 7: BTC augmented vector (41-dim pre-computed with regime derivatives)
        if btc_augment is not None:
            _btc = np.asarray(btc_augment, dtype=np.float64).ravel()
            # FIX-20260625-137: Select schema contract for name→position mapping.
            # Legacy "btc_macro_enhanced_41" → apply reorder shim so V4 receives
            # bit-identical tensor.  "btc_macro_enhanced_41_v2" → direct zip
            # (augmenter already outputs in Schema Order B).
            _btc_schema = schema_name if schema_name else "btc_macro_enhanced_41"
            if _btc_schema == "btc_macro_enhanced_41":
                _btc = _apply_legacy_btc_41_shim(_btc)
            _btc_names = get_schema_feature_names(_btc_schema)
            if _btc_names and len(_btc) >= len(_btc_names):
                for name, val in zip(_btc_names, _btc, strict=False):
                    lake[name] = float(val)

        # Source 8: OFI Lite — reads bridge IPC file (FIX-20260616-099)
        # Bridge worker writes ofi_snapshot.json atomically every ~30s.
        # Feature Lake reads it here.  Graceful degradation on any failure.
        try:
            from pathlib import Path as _Path

            _ofi_path = _Path("data_btc/reports/ofi_snapshot.json")
            if _ofi_path.exists():
                _ofi_data = json.loads(_ofi_path.read_text(encoding="utf-8"))
                if isinstance(_ofi_data, dict):
                    for k, v in _ofi_data.items():
                        lake[k] = float(v) if (v is not None and np.isfinite(float(v))) else 0.0
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass  # OFI file unavailable — lake just lacks these keys
        # Source 9: Caller-provided extras (future-proof injection point)
        if extra_features:
            for k, v in extra_features.items():
                lake[k] = float(v) if (v is not None and np.isfinite(float(v))) else 0.0

        return lake


# ── Legacy aliases (FIX-20260616-091: renamed 37→41) ──
if "btc_macro_enhanced_41" in SCHEMA_CONTRACTS:
    SCHEMA_CONTRACTS["btc_macro_enhanced_37"] = SCHEMA_CONTRACTS["btc_macro_enhanced_41"]

# ── Module-level singleton ──
_router: FeatureRouter | None = None


def get_router() -> FeatureRouter:
    global _router
    if _router is None:
        _router = FeatureRouter()
    return _router

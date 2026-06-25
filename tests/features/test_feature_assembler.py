"""Tests for core.features.feature_assembler — schema-driven feature assembly.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #6.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.features.feature_assembler import (
    _build_swing_vector,
    _derive_xau_indices,
    assemble_features_by_schema,
)

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def v9_40() -> np.ndarray:
    return np.arange(40, dtype=np.float64)


@pytest.fixture
def daily_24() -> np.ndarray:
    return np.arange(24, dtype=np.float64) + 100.0


@pytest.fixture
def micro_9() -> np.ndarray:
    return np.arange(9, dtype=np.float64) + 200.0


# ── _derive_xau_indices ───────────────────────────────────────────────────


class TestDeriveXAUIndices:
    def test_finds_cross_gold(self) -> None:
        features = ["D1_Ret_1", "Cross_Gold_Silver_Ratio", "D1_ATR_14"]
        indices = _derive_xau_indices(features)
        assert 1 in indices  # Cross_Gold_Silver_Ratio

    def test_finds_xagusd(self) -> None:
        features = ["XAGUSDc_return", "D1_Ret_1"]
        indices = _derive_xau_indices(features)
        assert 0 in indices

    def test_no_xau_features(self) -> None:
        features = ["D1_Ret_1", "D1_ATR_14", "BTC_Ret_1"]
        indices = _derive_xau_indices(features)
        assert len(indices) == 0


# ── assemble_features_by_schema ────────────────────────────────────────────


class TestAssembleFeaturesBySchema:
    def test_v9_institutional_with_vector(self, v9_40: np.ndarray) -> None:
        result = assemble_features_by_schema("v9_institutional", legacy_v9_vector=v9_40)
        assert result.shape == (40,)
        assert np.array_equal(result, v9_40[:40])

    def test_v9_institutional_short_vector_padded(self) -> None:
        short = np.array([1.0, 2.0, 3.0])
        result = assemble_features_by_schema("v9_institutional", legacy_v9_vector=short)
        assert result.shape == (40,)
        assert result[0] == 1.0
        assert result[3] == 0.0  # padded

    def test_v9_institutional_none_vector_returns_zeros(self) -> None:
        result = assemble_features_by_schema("v9_institutional", legacy_v9_vector=None)
        assert result.shape == (40,)
        assert np.all(result == 0.0)

    def test_nan_tf_ou_sanitized(self, v9_40: np.ndarray) -> None:
        result = assemble_features_by_schema(
            "v9_institutional", legacy_v9_vector=v9_40, tf_ou=float("nan")
        )
        assert result.shape == (40,)
        assert not np.isnan(result).any()

    def test_nan_tf_hurst_sanitized(self, v9_40: np.ndarray) -> None:
        result = assemble_features_by_schema(
            "v9_institutional", legacy_v9_vector=v9_40, tf_hurst=float("nan")
        )
        assert result.shape == (40,)
        assert not np.isnan(result).any()

    def test_inf_tf_ou_sanitized(self, v9_40: np.ndarray) -> None:
        result = assemble_features_by_schema(
            "v9_institutional", legacy_v9_vector=v9_40, tf_ou=float("inf")
        )
        assert result.shape == (40,)
        assert not np.isnan(result).any()

    def test_unknown_schema_fallback_to_v9(self, v9_40: np.ndarray) -> None:
        result = assemble_features_by_schema("nonexistent_schema_v99", legacy_v9_vector=v9_40)
        assert result.shape == (40,)

    def test_unknown_schema_no_vector_zeros(self) -> None:
        result = assemble_features_by_schema("nonexistent_schema_v99", legacy_v9_vector=None)
        assert result.shape == (40,)
        assert np.all(result == 0.0)


# ── Swing Schema Tests ─────────────────────────────────────────────────────


class TestSwingSchemas:
    def test_daily_swing_24(self, daily_24: np.ndarray) -> None:
        result = assemble_features_by_schema(
            "daily_swing_24",
            daily_features=daily_24,
            legacy_v9_vector=daily_24,
        )
        assert result.shape == (24,)

    def test_btc_macro_keyword_in_schema(self) -> None:
        """Schema containing 'btc_macro' routes to swing path."""
        result = assemble_features_by_schema(
            "btc_macro_enhanced_37",  # alias resolved to 41 inside _build_swing_vector
            daily_features=np.zeros(24),
            micro_features=np.zeros(9),
            tf_ou=0.0,
            tf_hurst=0.5,
            btc_augment=np.arange(41, dtype=np.float64),
            legacy_v9_vector=np.zeros(40),
        )
        assert result.shape == (41,)

    def test_swing_enhanced_35(self, daily_24: np.ndarray, micro_9: np.ndarray) -> None:
        result = assemble_features_by_schema(
            "swing_enhanced_35",
            daily_features=daily_24,
            micro_features=micro_9,
            tf_ou=0.1,
            tf_hurst=0.6,
            legacy_v9_vector=daily_24,
        )
        assert result.shape == (35,)

    def test_daily_swing_short_padded(self) -> None:
        short_daily = np.array([1.0, 2.0])
        result = assemble_features_by_schema(
            "daily_swing_24",
            daily_features=short_daily,
            legacy_v9_vector=short_daily,
        )
        assert result.shape == (24,)

    def test_swing_enhanced_short_micro_padded(self, daily_24: np.ndarray) -> None:
        short_micro = np.array([1.0, 2.0])
        result = assemble_features_by_schema(
            "swing_enhanced_35",
            daily_features=daily_24,
            micro_features=short_micro,
            tf_ou=0.1,
            tf_hurst=0.6,
            legacy_v9_vector=daily_24,
        )
        assert result.shape == (35,)

    def test_btc_macro_enhanced_41_with_augment(self) -> None:
        """BTC legacy schema: augmenter output gets legacy reorder shim applied.

        FIX-20260625-137: The legacy ``btc_macro_enhanced_41`` schema triggers
        a 3-cycle permutation on slots 35-40 (Order B → Order C) so V4 receives
        bit-identical tensor to pre-fix.  Slots 0-34 are unaffected.
        """
        btc_aug = np.arange(41, dtype=np.float64) + 500.0
        result = assemble_features_by_schema(
            "btc_macro_enhanced_41",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
            micro_features=np.arange(9, dtype=np.float64) + 200.0,
            tf_ou=0.2,
            tf_hurst=0.7,
            btc_augment=btc_aug,
            legacy_v9_vector=np.arange(40, dtype=np.float64),
        )
        assert result.shape == (41,)
        # Legacy shim permutes slots 35-40: (35←39, 36←40, 37←35, 38←36, 39←37, 40←38)
        expected = np.arange(41, dtype=np.float64) + 500.0
        expected[35], expected[36], expected[37], expected[38], expected[39], expected[40] = (
            expected[39],
            expected[40],
            expected[35],
            expected[36],
            expected[37],
            expected[38],
        )
        assert np.array_equal(result, expected)

    def test_btc_macro_enhanced_41_v2_with_augment(self) -> None:
        """BTC v2 schema: augmenter output passes through unchanged (no shim).

        FIX-20260625-137: The clean ``btc_macro_enhanced_41_v2`` schema skips
        the legacy reorder shim — augmenter already outputs in Schema Order B.
        """
        btc_aug = np.arange(41, dtype=np.float64) + 500.0
        result = assemble_features_by_schema(
            "btc_macro_enhanced_41_v2",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
            micro_features=np.arange(9, dtype=np.float64) + 200.0,
            tf_ou=0.2,
            tf_hurst=0.7,
            btc_augment=btc_aug,
            legacy_v9_vector=np.arange(40, dtype=np.float64),
        )
        assert result.shape == (41,)
        assert np.array_equal(result, btc_aug)

    def test_btc_macro_enhanced_41_no_augment_raises(self) -> None:
        """BTC schema without augmenter must raise RuntimeError."""
        with pytest.raises(RuntimeError, match="BTC feature augmenter unavailable"):
            assemble_features_by_schema(
                "btc_macro_enhanced_41",
                daily_features=np.arange(24, dtype=np.float64) + 100.0,
                micro_features=np.arange(9, dtype=np.float64) + 200.0,
                tf_ou=0.2,
                tf_hurst=0.7,
                btc_augment=None,
                legacy_v9_vector=np.arange(40, dtype=np.float64),
            )

    def test_btc_macro_enhanced_41_wrong_dim_raises(self) -> None:
        """BTC augment with wrong dimension must raise."""
        wrong_dim = np.arange(30, dtype=np.float64)
        with pytest.raises(RuntimeError, match="BTC feature augmenter unavailable"):
            assemble_features_by_schema(
                "btc_macro_enhanced_41",
                daily_features=np.arange(24, dtype=np.float64) + 100.0,
                btc_augment=wrong_dim,
                legacy_v9_vector=np.arange(40, dtype=np.float64),
            )


# ── _build_swing_vector ────────────────────────────────────────────────────


class TestBuildSwingVector:
    def test_unknown_schema_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown schema"):
            _build_swing_vector(
                "completely_unknown_schema_v99",
                daily_features=np.zeros(24),
            )

    def test_swing_enhanced_21_filters_xau(self) -> None:
        """swing_enhanced_21 removes XAU-related indices from the 24-dim vector."""
        # Without mocking the schema, just verify it runs and returns float array
        result = _build_swing_vector(
            "swing_enhanced_21",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
        )
        assert result.dtype == np.float64

    def test_fallback_padding(self) -> None:
        """Fallback dimension padding/truncation."""
        # Test with a schema that exists in SCHEMA_DIMENSIONS but not in the
        # specific branches — uses the fallback path
        result = _build_swing_vector(
            "daily_swing_24",
            daily_features=np.arange(30, dtype=np.float64),
        )
        assert result.shape == (24,)

    def test_nan_ou_hurst_in_build(self) -> None:
        result = _build_swing_vector(
            "swing_enhanced_35",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
            micro_features=np.zeros(9),
            tf_ou=float("nan"),
            tf_hurst=float("nan"),
        )
        assert not np.isnan(result[33]).any()
        assert not np.isnan(result[34]).any()


# ── Edge Cases ──────────────────────────────────────────────────────────────


class TestFeatureAssemblerEdgeCases:
    def test_swing_enhanced_29_filters_xau(self) -> None:
        """swing_enhanced_29 is a known schema with keyword match."""
        result = assemble_features_by_schema(
            "swing_enhanced_29",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
            micro_features=np.arange(9, dtype=np.float64) + 200.0,
            tf_ou=0.1,
            tf_hurst=0.6,
            legacy_v9_vector=np.arange(40, dtype=np.float64),
        )
        assert result is not None
        assert isinstance(result, np.ndarray)
        assert len(result) == 29

    def test_daily_swing_24_routing(self) -> None:
        """daily_swing_24 is routed through the swing path."""
        result = assemble_features_by_schema(
            "daily_swing_24",
            daily_features=np.arange(24, dtype=np.float64) + 100.0,
            micro_features=np.arange(9, dtype=np.float64) + 200.0,
            legacy_v9_vector=np.arange(40, dtype=np.float64),
        )
        assert result is not None
        assert isinstance(result, np.ndarray)
        assert len(result) == 24

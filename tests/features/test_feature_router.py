"""Tests for core.features.feature_router — schema-contracted dispatch.

FIX-20260619-050: Tier 2 zero-coverage breakout #5.
"""

from __future__ import annotations

import pytest

from core.features.feature_router import FeatureMissingError, FeatureRouter, SchemaNotFoundError


class TestFeatureRouter:
    def test_unknown_schema_raises(self) -> None:
        router = FeatureRouter()
        with pytest.raises((SchemaNotFoundError, FeatureMissingError)):
            router.dispatch({"feat_a": 1.0}, "nonexistent_schema_xyz")

    def test_get_router_returns_singleton(self) -> None:
        from core.features.feature_router import get_router

        r1 = get_router()
        r2 = get_router()
        assert r1 is r2

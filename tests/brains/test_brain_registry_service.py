"""Tests for core.brains.services.brain_registry_service.

FIX-20260619-062: Tier 2 zero-coverage breakout.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest
from core.brains.services.brain_registry_service import BrainRegistryService

class TestBrainRegistryService:
    def test_init_with_entries(self) -> None:
        svc = BrainRegistryService([{"path": "configs/brains/b1.json"}])
        assert svc._has_explicit_entries() is True

    def test_init_empty_entries(self) -> None:
        svc = BrainRegistryService([])
        assert svc._has_explicit_entries() is False

    def test_init_defaults(self) -> None:
        svc = BrainRegistryService()
        assert svc._has_explicit_entries() is False

    def test_list_active_entries_with_explicit(self) -> None:
        entries = [{"brain_id": "test_brain", "brain_type": "xgboost_v9"}]
        svc = BrainRegistryService(entries)
        result = svc.list_active_entries()
        assert len(result) == 1
        assert result[0]["brain_id"] == "test_brain"

    def test_auto_discovery_cached(self) -> None:
        svc = BrainRegistryService()
        with patch.object(svc, "_discover_from_disk", return_value=[{"brain_id": "auto1"}]):
            result1 = svc.list_active_entries()
            result2 = svc.list_active_entries()
        assert len(result1) == 1
        assert result1 == result2  # cached

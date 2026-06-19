"""Tests for core.feedback.param_optimizer.

FIX-20260619-061: Tier 2 zero-coverage breakout.
"""
from __future__ import annotations
import json, tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from core.feedback.param_optimizer import (
    SEARCH_SPACES, NO_SEARCH_TYPES, _load_brain_registry, suggest_parameters,
)

class TestSearchSpaces:
    def test_xgboost_v9_has_params(self) -> None:
        assert len(SEARCH_SPACES["xgboost_v9"]) == 7
    def test_ou_params_has_params(self) -> None:
        assert len(SEARCH_SPACES["ou_params_v6"]) == 4
    def test_no_search_types(self) -> None:
        assert "onnx_v9" in NO_SEARCH_TYPES

class TestSuggestParameters:
    def test_search_spaces_has_required_types(self) -> None:
        assert "xgboost_v9" in SEARCH_SPACES
        assert "lightgbm_v1" in SEARCH_SPACES
        assert "ou_params_v6" in SEARCH_SPACES

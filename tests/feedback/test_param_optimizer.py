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
    def test_returns_empty_for_unknown_brain(self) -> None:
        with patch("core.feedback.param_optimizer._load_brain_registry", return_value={}):
            result = suggest_parameters(["nonexistent_brain"])
        assert result == []

    def test_generates_suggestions_for_known_type(self) -> None:
        reg = {"brain_1": {"brain_type": "xgboost_v9"}}
        with patch("core.feedback.param_optimizer._load_brain_registry", return_value=reg):
            result = suggest_parameters(["brain_1"])
        assert len(result) == 1
        assert result[0]["brain_id"] == "brain_1"

    def test_skips_onnx_type(self) -> None:
        reg = {"onnx_brain": {"brain_type": "onnx_v9"}}
        with patch("core.feedback.param_optimizer._load_brain_registry", return_value=reg):
            result = suggest_parameters(["onnx_brain"])
        assert result == []

    def test_writes_to_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = {"brain_1": {"brain_type": "xgboost_v9"}}
            with patch("core.feedback.param_optimizer._load_brain_registry", return_value=reg):
                with patch("core.feedback.param_optimizer.Path") as mock_path:
                    mock_path.return_value.parent.mkdir = lambda **kw: None
                    result = suggest_parameters(["brain_1"], output_dir=tmpdir)
            assert len(result) >= 1

"""End-to-end smoke test for V9 feature → normalisation → ONNX inference pipeline.

Covers the full chain without any MetaTrader5 dependency.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
from core.features.feature_service import FeatureService

# ---------------------------------------------------------------------------
# Fixture: normalisation config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def norm_config() -> dict:
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "configs"
        / "brains"
        / "v9_institutional_01.normalization.json"
    )
    if not path.exists():
        pytest.skip("normalisation config not found")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fixture: brain entry
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def brain_entry() -> dict:
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "configs"
        / "brains"
        / "v9_institutional_01.json"
    )
    if not path.exists():
        pytest.skip("brain entry config not found")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_normalisation_config_smoke(norm_config: dict):
    """Normalisation config is well-formed with mean/std vectors of length 40."""
    assert "mean" in norm_config
    assert "std" in norm_config
    assert len(norm_config["mean"]) == 40
    assert len(norm_config["std"]) == 40


def test_v9_feature_adapter_normalizes(norm_config: dict):
    """V9FeatureAdapter uses normalisation config to produce zero-mean-ish output."""
    adapter = V9FeatureAdapter(normalization_config=norm_config)
    # Feed the mean vector itself → expected ~zero after normalization
    mean_vec = np.asarray(norm_config["mean"], dtype=np.float32)
    result = adapter.normalize(mean_vec)
    assert result.shape == (40,)
    assert np.allclose(result, 0.0, atol=1e-5), "mean vector should normalize to near-zero"


def test_feature_service_tier3_stub():
    """FeatureService falls back to Tier 3 zero-vector when no store or computer."""
    fs = FeatureService()
    vec = fs.build_feature_vector()
    assert vec.shape == (40,)
    assert vec.dtype == np.float32
    assert np.allclose(vec, 0.0)


def test_feature_service_to_brain_adapter(brain_entry: dict, norm_config: dict):
    """FeatureService → V9FeatureAdapter → V9OnnxBrainAdapter full chain.

    Tier 3 zero-vector feeds ONNX brain adapter deterministic fallback.
    """
    adapter = V9FeatureAdapter(normalization_config=norm_config)
    fs = FeatureService(feature_adapter=adapter, feature_computer=None, feature_store=None)
    feature_vector = fs.build_feature_vector()

    brain = V9OnnxBrainAdapter(brain_entry)
    brain.load()

    raw = brain.infer(feature_vector)
    proposal = brain.get_signal(raw)

    # Contract assertions
    assert proposal.brain_id == brain_entry.get("brain_id")
    assert proposal.prediction["direction_bias"] in {"long", "short", "neutral"}
    assert 0.0 <= proposal.prediction["confidence"] <= 1.0
    assert proposal.prediction["uncertainty"] == pytest.approx(
        1.0 - proposal.prediction["confidence"]
    )
    assert "raw_outputs" in proposal.extensions
    assert isinstance(proposal.extensions["raw_outputs"]["out_risk"], float)
    assert isinstance(proposal.extensions["raw_outputs"]["out_vol"], float)
    assert "v9_institutional_onnx" in proposal.rationale["reason_tags"]
    assert isinstance(proposal.health["runtime_ms"], float)
    assert isinstance(proposal.health["fallback_used"], bool)

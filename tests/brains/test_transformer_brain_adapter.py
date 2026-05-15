import numpy as np

from core.brains.adapters.transformer_brain_adapter import (
    MICROSTRUCTURE_9_FEATURES,
    NUM_FEATURES,
    TransformerBrainAdapter,
)
from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal


def _make_entry():
    return {
        "brain_id": "Microstructure_Transformer_V5.0_H4",
        "brain_type": "transformer_v5_h4",
        "brain_role": "alpha_brain",
        "status": "shadow",
        "model_version": "v5.0-barrier-h4",
        "vote_weight": 0.4,
        "artifact_path": "data/models/transformer_v5_micro_barrier_h4.onnx",
        "deployment_scope": {
            "regimes": ["trend"],
            "symbols": ["XAUUSDc"],
        },
    }


def test_transformer_brain_adapter_loads():
    """Adapter loads ONNX model + scaler and sets backend correctly."""
    adapter = TransformerBrainAdapter(brain_entry=_make_entry())
    adapter.load()

    info = adapter.describe()
    assert info["session_loaded"], f"ONNX session not loaded: backend={info['backend']}"
    assert "uses_feature_adapter" in info
    assert info["num_features"] == NUM_FEATURES
    assert info["seq_len"] > 0  # detected from ONNX input shape
    assert info["buffer_size"] == 0


def test_transformer_brain_adapter_fallback_when_buffer_not_full():
    """Returns fallback=True while buffer has fewer than 64 entries."""
    adapter = TransformerBrainAdapter(brain_entry=_make_entry())
    adapter.load()

    # First call — buffer has 1 entry
    vec = np.random.randn(NUM_FEATURES).astype(np.float32)
    raw = adapter.infer(vec)
    assert raw["fallback"] is True
    assert raw.get("fallback_reason") == "buffer_not_full"
    assert raw["buffer_size"] == 1


def test_transformer_brain_adapter_inference_after_buffer_full():
    """Produces a non-fallback inference once 64 entries have been accumulated."""
    adapter = TransformerBrainAdapter(brain_entry=_make_entry())
    adapter.load()

    # Feed 64 bars of synthetic features
    rng = np.random.RandomState(42)
    last_raw = None
    for _ in range(64):
        vec = rng.randn(NUM_FEATURES).astype(np.float32)
        last_raw = adapter.infer(vec)

    assert last_raw is not None
    assert last_raw["fallback"] is False, f"Expected non-fallback after 64 bars: {last_raw}"
    assert "raw_score" in last_raw
    assert isinstance(last_raw["runtime_ms"], float)


def test_transformer_brain_adapter_get_signal():
    """get_signal maps raw score to a valid BrainDecisionProposal."""
    adapter = TransformerBrainAdapter(brain_entry=_make_entry())
    adapter.load()

    # Feed 64 bars then get signal
    rng = np.random.RandomState(42)
    for _ in range(64):
        adapter.infer(rng.randn(NUM_FEATURES).astype(np.float32))

    # Extra call that triggers inference
    raw = adapter.infer(rng.randn(NUM_FEATURES).astype(np.float32))
    proposal = adapter.get_signal(raw)

    assert isinstance(proposal, BrainDecisionProposal)
    assert proposal.brain_id == "Microstructure_Transformer_V5.0_H4"
    assert "direction_bias" in proposal.prediction
    assert proposal.prediction["direction_bias"] in ("long", "short", "neutral")
    assert "v4_3_microstructure_transformer" in proposal.rationale["reason_tags"]
    assert "raw_outputs" in proposal.extensions
    assert "raw_score" in proposal.extensions["raw_outputs"]


def test_transformer_brain_adapter_score_to_direction():
    """Score-to-direction mapping handles long / short / neutral thresholds."""
    sd = TransformerBrainAdapter._score_to_direction

    direction, up, down = sd(1.0)
    assert direction == "long"
    assert up > 0.7

    direction, up, down = sd(-1.0)
    assert direction == "short"
    assert down > 0.7

    direction, up, down = sd(0.0)
    assert direction == "neutral"
    assert up == 0.5
    assert down == 0.5

    direction, up, down = sd(0.05)
    assert direction == "neutral"
    assert up == 0.5
    assert down == 0.5


def test_transformer_brain_adapter_stub_without_artifact():
    """Adapter reports backend=stub when no artifact_path is set."""
    entry = {
        "brain_id": "Transformer_No_Artifact",
        "brain_type": "transformer_v4.3",
        "brain_role": "alpha_brain",
        "status": "shadow",
        "model_version": "v4.3",
        "artifact_path": "",
        "deployment_scope": {"regimes": [], "symbols": []},
    }
    adapter = TransformerBrainAdapter(brain_entry=entry)
    adapter.load()
    assert adapter._backend == "stub:no_artifact_path"


def test_transformer_brain_adapter_feature_names():
    """Microstructure 9 feature names are correctly defined."""
    assert len(MICROSTRUCTURE_9_FEATURES) == NUM_FEATURES
    assert "tick_return" in MICROSTRUCTURE_9_FEATURES
    assert "OIM" in MICROSTRUCTURE_9_FEATURES
    assert "USDJPYc_return" in MICROSTRUCTURE_9_FEATURES


def test_transformer_with_feature_adapter_e2e():
    """End-to-end: MicrostructureFeatureAdapter → Transformer inference → proposal."""
    from core.features.adapters.microstructure_feature_adapter import (
        MicrostructureFeatureAdapter,
    )
    from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

    feat_adapter = MicrostructureFeatureAdapter(
        scaler_path=None,  # H4 model uses no separate scaler
    )

    entry = _make_entry()
    adapter = TransformerBrainAdapter(brain_entry=entry, feature_adapter=feat_adapter)
    adapter.load()

    # Simulate 64 bars of microstructure features
    rng = np.random.RandomState(42)
    for _ in range(64):
        feature_source = {name: float(rng.randn()) for name in MICROSTRUCTURE_9_FEATURES}
        scaled_vec = feat_adapter.build_model_input(feature_source).ravel()
        adapter.infer(scaled_vec)

    # One more call to trigger ONNX inference
    feature_source = {name: float(rng.randn()) for name in MICROSTRUCTURE_9_FEATURES}
    scaled_vec = feat_adapter.build_model_input(feature_source).ravel()
    raw = adapter.infer(scaled_vec)
    assert raw["fallback"] is False
    proposal = adapter.get_signal(raw)

    assert proposal.brain_id == "Microstructure_Transformer_V5.0_H4"
    assert proposal.prediction["direction_bias"] in ("long", "short", "neutral")
    assert proposal.health["fallback_used"] is False
    assert "v4_3_microstructure_transformer" in proposal.rationale["reason_tags"]
    assert float(raw["raw_score"]) != 0.0  # real inference happened


def test_xgboost_with_microstructure_feature_adapter():
    """XGBoost adapter gets correct 9-dim feature vector via MicrostructureFeatureAdapter."""
    from core.features.adapters.microstructure_feature_adapter import (
        MicrostructureFeatureAdapter,
    )
    from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

    feat_adapter = MicrostructureFeatureAdapter(
        scaler_path=None,  # H4 model uses no separate scaler
    )

    entry = {
        "brain_id": "XGBoost_V4.5_Microstructure",
        "brain_type": "xgboost_v4.5",
        "brain_role": "alpha_brain",
        "status": "shadow",
        "model_version": "v4.5",
        "vote_weight": 0.8,
        "artifact_path": "data/models/V4.X_XGBoost_Core.json",
        "deployment_scope": {
            "regimes": ["trend"],
            "symbols": ["XAUUSDc"],
        },
    }

    from core.brains.adapters.xgboost_brain_adapter import XGBoostBrainAdapter

    adapter = XGBoostBrainAdapter(brain_entry=entry, feature_adapter=feat_adapter)
    adapter.load()

    # Simulate a feature_source dict with 9 microstructure features
    feature_source = {name: 0.01 for name in MICROSTRUCTURE_9_FEATURES}
    proposal = adapter.run(None, feature_source)

    assert isinstance(proposal, BrainDecisionProposal)
    assert proposal.brain_id == "XGBoost_V4.5_Microstructure"
    assert proposal.prediction["direction_bias"] in ("long", "short", "neutral")

    # Verify the adapter actually got 9 features (not 40)
    info = adapter.describe()
    assert info["uses_feature_adapter"] is True

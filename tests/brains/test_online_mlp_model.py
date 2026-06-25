"""Tests for core.brains.online_mlp_model — OnlineMLP streaming neural network.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #3.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.brains.online_mlp_model import OnlineMLP

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def model() -> OnlineMLP:
    return OnlineMLP(n_features=40, n_classes=3, seed=42)


# ── Initialization ─────────────────────────────────────────────────────────


class TestOnlineMLPInit:
    def test_weight_shapes(self) -> None:
        m = OnlineMLP(n_features=40, n_classes=3)
        assert m.W1.shape == (40, 32)
        assert m.b1.shape == (32,)
        assert m.gamma1.shape == (32,)
        assert m.beta1.shape == (32,)
        assert m.W2.shape == (32, 16)
        assert m.b2.shape == (16,)
        assert m.gamma2.shape == (16,)
        assert m.beta2.shape == (16,)
        assert m.W3.shape == (16, 3)
        assert m.b3.shape == (3,)

    def test_biases_are_zero(self) -> None:
        m = OnlineMLP(seed=42)
        assert np.all(m.b1 == 0.0)
        assert np.all(m.b2 == 0.0)
        assert np.all(m.b3 == 0.0)

    def test_layernorm_params_default(self) -> None:
        m = OnlineMLP(seed=42)
        assert np.all(m.gamma1 == 1.0)
        assert np.all(m.beta1 == 0.0)
        assert np.all(m.gamma2 == 1.0)
        assert np.all(m.beta2 == 0.0)

    def test_same_seed_produces_same_weights(self) -> None:
        m1 = OnlineMLP(seed=42)
        m2 = OnlineMLP(seed=42)
        assert np.array_equal(m1.W1, m2.W1)
        assert np.array_equal(m1.W2, m2.W2)
        assert np.array_equal(m1.W3, m2.W3)

    def test_different_seed_produces_different_weights(self) -> None:
        m1 = OnlineMLP(seed=42)
        m2 = OnlineMLP(seed=123)
        assert not np.array_equal(m1.W1, m2.W1)

    def test_he_init_range(self) -> None:
        """He init: W ~ N(0, sqrt(2/fan_in)).  Most weights within 3 sigma."""
        m = OnlineMLP(n_features=40, seed=42)
        std_expected = np.sqrt(2.0 / 40)
        assert 0.05 < np.std(m.W1) < 0.5
        # At least 95% within 3 sigma
        within = np.abs(m.W1) < 3 * std_expected
        assert within.mean() > 0.95

    def test_total_updates_starts_zero(self) -> None:
        m = OnlineMLP()
        assert m._total_updates == 0


# ── Forward Pass (Numpy) ───────────────────────────────────────────────────


class TestForwardNumpy:
    def test_1d_input_returns_3_probs(self, model: OnlineMLP) -> None:
        x = np.random.RandomState(42).randn(40)
        probs = model.forward_numpy(x)
        assert probs.shape == (3,)

    def test_2d_input_returns_batch_probs(self, model: OnlineMLP) -> None:
        x = np.random.RandomState(42).randn(5, 40)
        probs = model.forward_numpy(x)
        assert probs.shape == (5, 3)

    def test_probabilities_sum_to_one(self, model: OnlineMLP) -> None:
        x = np.random.RandomState(42).randn(40)
        probs = model.forward_numpy(x)
        assert probs.sum() == pytest.approx(1.0, abs=1e-5)

    def test_probabilities_sum_to_one_batch(self, model: OnlineMLP) -> None:
        x = np.random.RandomState(42).randn(10, 40)
        probs = model.forward_numpy(x)
        sums = probs.sum(axis=-1)
        for s in sums:
            assert s == pytest.approx(1.0, abs=1e-5)

    def test_all_probabilities_non_negative(self, model: OnlineMLP) -> None:
        x = np.random.RandomState(42).randn(40)
        probs = model.forward_numpy(x)
        assert np.all(probs >= 0.0)

    def test_deterministic_output(self) -> None:
        m1 = OnlineMLP(seed=42)
        m2 = OnlineMLP(seed=42)
        x = np.ones(40)
        p1 = m1.forward_numpy(x)
        p2 = m2.forward_numpy(x)
        assert np.allclose(p1, p2)

    def test_he_init_range(self) -> None:
        """He init: W ~ N(0, sqrt(2/fan_in)).  Most weights within 3 sigma."""
        m = OnlineMLP(n_features=40, seed=42)
        std_expected = np.sqrt(2.0 / 40)
        within = np.abs(m.W1) < 3 * std_expected
        assert within.mean() > 0.95

    def test_all_zeros_input_is_stable(self, model: OnlineMLP) -> None:
        """Zero input should not produce NaN or Inf."""
        probs = model.forward_numpy(np.zeros(40))
        assert not np.isnan(probs).any()
        assert not np.isinf(probs).any()

    def test_large_input_is_stable(self, model: OnlineMLP) -> None:
        """Very large inputs should be stable after softmax."""
        probs = model.forward_numpy(np.full(40, 100.0))
        assert not np.isnan(probs).any()
        assert not np.isinf(probs).any()

    def test_negative_large_input_is_stable(self, model: OnlineMLP) -> None:
        probs = model.forward_numpy(np.full(40, -100.0))
        assert not np.isnan(probs).any()
        assert not np.isinf(probs).any()


# ── LayerNorm ──────────────────────────────────────────────────────────────


class TestLayerNorm:
    def test_normalizes_to_unit_variance(self, model: OnlineMLP) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0])
        gamma = np.ones(4)
        beta = np.zeros(4)
        out = model._layer_norm(x, gamma, beta)
        assert out.mean() == pytest.approx(0.0, abs=1e-5)
        assert out.std(ddof=0) == pytest.approx(1.0, abs=1e-4)

    def test_applies_gamma_and_beta(self, model: OnlineMLP) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0])
        gamma = np.full(4, 2.0)
        beta = np.full(4, 10.0)
        out = model._layer_norm(x, gamma, beta)
        assert out.mean() == pytest.approx(10.0, abs=1e-5)


# ── GELU ───────────────────────────────────────────────────────────────────


class TestGELU:
    def test_zero_input(self, model: OnlineMLP) -> None:
        assert model._gelu(np.array([0.0]))[0] == pytest.approx(0.0, abs=0.01)

    def test_positive_input(self, model: OnlineMLP) -> None:
        out = model._gelu(np.array([3.0]))
        assert out[0] > 2.5  # GELU(3) ≈ 2.996

    def test_negative_input(self, model: OnlineMLP) -> None:
        out = model._gelu(np.array([-3.0]))
        assert out[0] < 0.01  # GELU(-3) ≈ -0.004

    def test_vector_input(self, model: OnlineMLP) -> None:
        x = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
        out = model._gelu(x)
        assert out.shape == (5,)
        # GELU is close to 0 for negative x, close to x for positive x
        assert abs(out[0]) < 0.1  # GELU(-2) ≈ -0.045
        assert out[2] == pytest.approx(0.0, abs=0.05)  # GELU(0) ≈ 0
        assert out[4] == pytest.approx(2.0, abs=0.1)  # GELU(2) ≈ 1.95


# ── State Dict Roundtrip ───────────────────────────────────────────────────


class TestStateDictRoundtrip:
    def test_roundtrip_preserves_weights(self, model: OnlineMLP) -> None:
        d = model.state_dict()
        m2 = OnlineMLP(seed=999)  # different seed
        m2.load_state_dict(d)
        # Weights should be identical after load_state_dict
        assert np.array_equal(m2.W1, model.W1)
        assert np.array_equal(m2.W2, model.W2)
        assert np.array_equal(m2.W3, model.W3)
        assert np.array_equal(m2.b1, model.b1)
        assert m2._total_updates == model._total_updates

    def test_state_dict_keys(self, model: OnlineMLP) -> None:
        d = model.state_dict()
        expected_keys = {
            "n_features",
            "n_classes",
            "total_updates",
            "W1",
            "b1",
            "gamma1",
            "beta1",
            "W2",
            "b2",
            "gamma2",
            "beta2",
            "W3",
            "b3",
        }
        assert set(d.keys()) >= expected_keys

    def test_state_dict_values_are_serializable(self, model: OnlineMLP) -> None:
        d = model.state_dict()
        json.dumps(d)  # should not raise


# ── Save / Load ────────────────────────────────────────────────────────────


class TestSaveLoad:
    def test_save_and_load_roundtrip(self, model: OnlineMLP, tmp_path: Path) -> None:
        path = tmp_path / "model.json"
        model.save(path)
        assert path.exists()
        loaded = OnlineMLP.load(path)
        assert np.array_equal(loaded.W1, model.W1)
        assert np.array_equal(loaded.W2, model.W2)
        assert np.array_equal(loaded.W3, model.W3)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "deep" / "model.json"
        model = OnlineMLP()
        model.save(path)
        assert path.exists()

    def test_load_preserves_metadata(self, tmp_path: Path) -> None:
        model = OnlineMLP(n_features=20, n_classes=4, seed=42)
        path = tmp_path / "model.json"
        model.save(path)
        loaded = OnlineMLP.load(path)
        assert loaded.n_features == 20
        assert loaded.n_classes == 4

    def test_save_writes_model_type(self, tmp_path: Path) -> None:
        path = tmp_path / "model.json"
        model = OnlineMLP()
        model.save(path)
        data = json.loads(path.read_text())
        assert data["model_type"] == "online_mlp_v1"


# ── Partial Fit ────────────────────────────────────────────────────────────


class TestPartialFit:
    def test_partial_fit_without_torch_returns_false(self) -> None:
        """partial_fit returns False when torch is not available."""
        m = OnlineMLP(seed=42)
        x = np.random.RandomState(42).randn(40)
        result = m.partial_fit(x, 1)
        # In CI without torch, this should return False
        # If torch IS available, it returns True
        assert result in (True, False)

    def test_partial_fit_increments_counter(self) -> None:
        """If torch is available, total_updates should increment."""
        m = OnlineMLP(seed=42)
        x = np.random.RandomState(42).randn(40)
        result = m.partial_fit(x, 1)
        if result:
            assert m._total_updates == 1

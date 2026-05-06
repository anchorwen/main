"""Tests for LabelContract and TrainingRecipe — the training contract system."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core.contracts.training.label_contract import (
    SCHEMA_VERSION as LC_SCHEMA_VERSION,
)
from core.contracts.training.label_contract import (
    BarrierResult,
    LabelContract,
    _compute_atr,
)
from core.contracts.training.training_recipe import (
    SCHEMA_VERSION as TR_SCHEMA_VERSION,
)
from core.contracts.training.training_recipe import (
    TrainingRecipe,
)

# ── helpers ──


def _make_minimal_contract_dict(**overrides: object) -> dict:
    d: dict = {
        "schema_version": LC_SCHEMA_VERSION,
        "contract_id": "test-contract-1.0.0",
        "type": "survival_barrier",
        "horizon_bars": 12,
        "label_classes": {"1": "tp_hit_first", "0": "timeout", "-1": "sl_hit_first"},
        "barriers": {"sl_atr_mult": 2.0, "tp_atr_mult": 3.5},
    }
    d.update(overrides)  # type: ignore[arg-type]
    return d


def _make_ohlc(n: int = 200, seed: int = 42) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    base = 4500.0
    noise = rng.normal(0, 5, n)
    closes = base + np.cumsum(noise * 0.5)
    highs = closes + np.abs(rng.normal(0, 3, n))
    lows = closes - np.abs(rng.normal(0, 3, n))
    return highs, lows, closes


# ── _compute_atr ──


def test_compute_atr_basic():
    h, l, c = _make_ohlc()
    atr = _compute_atr(h, l, c, period=14)
    assert atr > 0
    assert atr < 100  # reasonable for gold


def test_compute_atr_insufficient_data():
    h, l, c = _make_ohlc(10)
    atr = _compute_atr(h, l, c, period=14)
    assert atr == 0.0


# ── LabelContract.from_dict ──


def test_from_dict_minimal():
    d = _make_minimal_contract_dict()
    c = LabelContract.from_dict(d)
    assert c.contract_id == "test-contract-1.0.0"
    assert c.type == "survival_barrier"
    assert c.horizon_bars == 12
    assert c.sl_atr_mult == 2.0
    assert c.tp_atr_mult == 3.5
    assert c.atr_period == 14
    assert c.bar_timeframe == "M5"


def test_from_dict_with_atr_config():
    d = _make_minimal_contract_dict()
    d["atr_config"] = {"period": 20, "timeframe": "M15"}
    c = LabelContract.from_dict(d)
    assert c.atr_period == 20
    assert c.atr_timeframe == "M15"


def test_from_dict_regression_type():
    d = _make_minimal_contract_dict(type="regression", regression_target="forward_return")
    # regression doesn't require barriers
    d.pop("barriers", None)
    c = LabelContract.from_dict(d)
    assert c.type == "regression"
    assert c.regression_target == "forward_return"


def test_from_dict_invalid_schema():
    d = _make_minimal_contract_dict(schema_version="wrong.version")
    with pytest.raises(ValueError, match="schema_version"):
        LabelContract.from_dict(d)


def test_from_dict_invalid_type():
    d = _make_minimal_contract_dict(type="unknown_type")
    with pytest.raises(ValueError, match="Unknown label contract type"):
        LabelContract.from_dict(d)


def test_from_dict_negative_horizon():
    d = _make_minimal_contract_dict(horizon_bars=0)
    with pytest.raises(ValueError, match="horizon_bars"):
        LabelContract.from_dict(d)


# ── LabelContract.from_file / to_dict ──


def test_from_file_real_contract():
    path = Path("configs/training/label_contracts/label-survival-barrier-1.0.0.json")
    c = LabelContract.from_file(path)
    assert c.contract_id == "label-survival-barrier-1.0.0"
    assert c.type == "survival_barrier"
    assert c.sl_atr_mult == 2.0
    assert c.tp_atr_mult == 3.5
    assert c.horizon_bars == 12


def test_to_dict_roundtrip():
    d = _make_minimal_contract_dict()
    c = LabelContract.from_dict(d)
    out = c.to_dict()
    assert out["schema_version"] == d["schema_version"]
    assert out["contract_id"] == d["contract_id"]
    assert out["type"] == d["type"]
    assert out["barriers"]["sl_atr_mult"] == d["barriers"]["sl_atr_mult"]


# ── LabelContract.build_barrier_labels ──


def test_build_barrier_labels_long_tp_hit():
    """Construct a scenario where price clearly rises to hit TP."""
    n = 100
    entry_idx = 30
    # flat then spike up
    h = np.full(n, 4500.0)
    l = np.full(n, 4500.0)
    c = np.full(n, 4500.0)
    # ATR depends on prior bars; add some noise for realistic ATR
    rng = np.random.default_rng(42)
    for i in range(14, entry_idx + 1):
        h[i] += rng.uniform(1, 3)
        l[i] -= rng.uniform(1, 3)
    # spike above TP
    h[entry_idx + 3] = 4600.0
    l[entry_idx + 3] = 4595.0

    contract = LabelContract(
        schema_version=LC_SCHEMA_VERSION,
        contract_id="test",
        type="survival_barrier",
        horizon_bars=10,
        label_classes={"1": "tp", "0": "timeout", "-1": "sl"},
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
    )
    result = contract.build_barrier_labels(h, l, c, entry_idx=entry_idx, side="long")
    assert result.label == "tp_hit_first"
    assert result.hit_bar_index is not None
    assert result.entry_price == c[entry_idx]


def test_build_barrier_labels_short_sl_hit():
    """Construct a scenario where short hits SL (price rises)."""
    n = 100
    entry_idx = 30
    h = np.full(n, 4500.0)
    l = np.full(n, 4500.0)
    c = np.full(n, 4500.0)
    rng = np.random.default_rng(42)
    for i in range(14, entry_idx + 1):
        h[i] += rng.uniform(1, 3)
        l[i] -= rng.uniform(1, 3)
    h[entry_idx + 3] = 4550.0
    l[entry_idx + 3] = 4545.0

    contract = LabelContract(
        schema_version=LC_SCHEMA_VERSION,
        contract_id="test",
        type="survival_barrier",
        horizon_bars=10,
        label_classes={"1": "tp", "0": "timeout", "-1": "sl"},
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
    )
    result = contract.build_barrier_labels(h, l, c, entry_idx=entry_idx, side="short")
    assert result.label == "sl_hit_first"


def test_build_barrier_labels_timeout():
    """Flat market → neither barrier hit."""
    n = 100
    c = np.full(n, 4500.0)
    h = np.full(n, 4500.5)
    l = np.full(n, 4499.5)
    contract = LabelContract(
        schema_version=LC_SCHEMA_VERSION,
        contract_id="test",
        type="survival_barrier",
        horizon_bars=10,
        label_classes={"1": "tp", "0": "timeout", "-1": "sl"},
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
    )
    result = contract.build_barrier_labels(h, l, c, entry_idx=50, side="long")
    assert result.label == "timeout"
    assert result.hit_bar_index is None
    assert result.hit_price is None


def test_build_barrier_labels_wrong_type_raises():
    contract = LabelContract(
        schema_version=LC_SCHEMA_VERSION,
        contract_id="test",
        type="regression",
        horizon_bars=10,
        label_classes={"1": "x"},
        regression_target="forward_return",
    )
    h, l, c = _make_ohlc()
    with pytest.raises(ValueError, match="survival_barrier"):
        contract.build_barrier_labels(h, l, c, entry_idx=50, side="long")


def test_build_barrier_labels_atr_fallback():
    """Very short price history → ATR fallback to 2.31 (training mean)."""
    h, l, c = _make_ohlc(10)  # not enough bars for ATR(14) + 1
    contract = LabelContract(
        schema_version=LC_SCHEMA_VERSION,
        contract_id="test",
        type="survival_barrier",
        horizon_bars=10,
        label_classes={"1": "tp", "0": "timeout", "-1": "sl"},
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
    )
    result = contract.build_barrier_labels(h, l, c, entry_idx=8, side="long")
    assert result.atr_at_entry == 2.31  # fallback


def test_barrier_result_fields():
    r = BarrierResult(
        label="tp_hit_first",
        hit_bar_index=5,
        hit_price=4550.0,
        entry_price=4500.0,
        sl_price=4480.0,
        tp_price=4555.0,
        atr_at_entry=5.0,
        horizon_bars=12,
    )
    assert r.label == "tp_hit_first"
    assert r.hit_bar_index == 5
    assert r.hit_price == 4550.0


# ── LabelContract.validate ──


def test_validate_valid_contract():
    c = LabelContract.from_dict(_make_minimal_contract_dict())
    assert c.validate() == []


def test_validate_missing_label_class():
    d = _make_minimal_contract_dict(
        label_classes={"1": "tp_hit_first"}  # missing sl_hit_first and timeout
    )
    c = LabelContract.from_dict(d)
    issues = c.validate()
    assert len(issues) > 0
    assert any("missing" in i.lower() for i in issues)


def test_validate_regression_needs_target():
    d = _make_minimal_contract_dict(type="regression")
    d.pop("barriers", None)
    c = LabelContract.from_dict(d)
    issues = c.validate()
    assert any("regression_target" in i for i in issues)


# ═══════════════════════════════════════════════════════════════════
# TrainingRecipe tests
# ═══════════════════════════════════════════════════════════════════


def _make_minimal_recipe_dict(**overrides: object) -> dict:
    d: dict = {
        "schema_version": TR_SCHEMA_VERSION,
        "recipe_id": "test-recipe-1.0.0",
        "model_identity": {
            "lane": "sur",
            "role": "chlg",
            "generation": "g2026.1",
            "feature_contract_id": "feat-v9-institutional-1.0.0",
        },
        "label_contract_ref": {
            "contract_id": "label-survival-barrier-1.0.0",
        },
        "data": {
            "dataset_slice_id": "xauusd-m5-2025",
        },
        "training": {
            "epochs": 100,
            "architecture": "mlp_multihead",
        },
        "evaluation": {
            "metrics": ["accuracy", "f1"],
        },
    }
    d.update(overrides)  # type: ignore[arg-type]
    return d


def test_recipe_from_dict_basic():
    d = _make_minimal_recipe_dict()
    r = TrainingRecipe.from_dict(d)
    assert r.recipe_id == "test-recipe-1.0.0"
    assert r.model_identity.lane == "sur"
    assert r.model_identity.role == "chlg"
    assert r.training.epochs == 100
    assert r.training.architecture == "mlp_multihead"
    assert r.data.normalization_strategy == "fixed"
    assert r.data.data_augmentation.enabled is False


def test_recipe_from_file():
    path = Path("configs/training/recipes/sur-g2026.1-recipe.json")
    r = TrainingRecipe.from_file(path)
    assert r.recipe_id == "CRT.sur.chlg.g2026.1"
    assert r.model_identity.lane == "sur"
    assert r.training.input_dim == 40
    assert r.data.data_augmentation.enabled is True
    assert len(r.training.seeds) == 5


def test_recipe_to_trainer_args():
    d = _make_minimal_recipe_dict()
    r = TrainingRecipe.from_dict(d)
    args = r.to_trainer_args()
    assert "--recipe-id" in args
    assert "test-recipe-1.0.0" in args
    assert "--label-contract" in args
    assert "--epochs" in args
    assert "100" in args
    assert "--lr" in args
    assert "0.001" in args
    assert "--architecture" in args
    assert "mlp_multihead" in args


def test_recipe_to_trainer_args_with_augmentation():
    d = _make_minimal_recipe_dict()
    d["data"]["data_augmentation"] = {
        "enabled": True,
        "volatility_scaling": [0.5, 1.0, 1.5],
        "noise_std": 0.02,
    }
    r = TrainingRecipe.from_dict(d)
    args = r.to_trainer_args()
    assert "--augment" in args
    assert "--augment-noise" in args
    assert "0.02" in args
    assert "--augment-vol-scales" in args


def test_recipe_validate_valid():
    d = _make_minimal_recipe_dict()
    r = TrainingRecipe.from_dict(d)
    assert r.validate() == []


def test_recipe_validate_invalid_arch():
    d = _make_minimal_recipe_dict()
    d["training"]["architecture"] = "resnet50"
    r = TrainingRecipe.from_dict(d)
    issues = r.validate()
    assert any("architecture" in i for i in issues)


def test_recipe_validate_invalid_optimizer():
    d = _make_minimal_recipe_dict()
    d["training"]["optimizer"] = "sgd_momentum"
    r = TrainingRecipe.from_dict(d)
    issues = r.validate()
    assert any("optimizer" in i for i in issues)


def test_recipe_validate_invalid_norm():
    d = _make_minimal_recipe_dict()
    d["data"]["normalization_strategy"] = "zscore_fancy"
    r = TrainingRecipe.from_dict(d)
    issues = r.validate()
    assert any("normalization" in i for i in issues)


def test_recipe_validate_invalid_metrics():
    d = _make_minimal_recipe_dict()
    d["evaluation"]["metrics"] = ["quantum_sharpe"]
    r = TrainingRecipe.from_dict(d)
    issues = r.validate()
    assert any("metrics" in i for i in issues)


def test_recipe_validate_lane_arch_mismatch():
    d = _make_minimal_recipe_dict()
    d["model_identity"]["lane"] = "sur"
    d["training"]["architecture"] = "xgboost"
    r = TrainingRecipe.from_dict(d)
    issues = r.validate()
    assert any("architecture" in i for i in issues)


def test_recipe_to_dict_roundtrip():
    d = _make_minimal_recipe_dict()
    r = TrainingRecipe.from_dict(d)
    out = r.to_dict()
    assert out["recipe_id"] == d["recipe_id"]
    assert out["model_identity"]["lane"] == d["model_identity"]["lane"]

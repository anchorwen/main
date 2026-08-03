"""Phase 3 / M2 hard assertions — 自动 OOS / 盈亏平衡门槛 (auto OOS & breakeven gate).

FIX-20260803-004 / IC 最高批准:
  1. A model whose blind-test Spearman rho underperforms, or whose expected
     win rate cannot cover the physical wear of spread & slippage, is
     HARD-VETOED — it dies in CI/CD, never entering the candidate pool.
  2. ``compute_breakeven`` (core/training/breakeven.py) is the ONLY breakeven
     implementation — friction accounting aligned with label_contract physics.
  3. ``run_blind_test`` (scripts/training/oos_blind_test.py) returns FAIL for
     near-breakeven models and PASS for skill-bearing models.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core.contracts.training.label_contract import LabelContract
from core.training.breakeven import (
    BreakevenResult,
    breakeven_from_contract,
    compute_breakeven,
    compute_breakeven_from_params,
    compute_friction_costs,
    compute_rr,
)
from core.training.utils import spearman_rho
from scripts.training.oos_blind_test import OOSBlindError, run_blind_test

# ── 1. Breakeven math (WR = 1/(1+RR)) ──────────────────────────────────────


class TestBreakevenMath:
    def test_rr_one(self) -> None:
        assert compute_breakeven(1.0) == pytest.approx(0.5)

    def test_rr_two(self) -> None:
        assert compute_breakeven(2.0) == pytest.approx(1.0 / 3.0)

    def test_rr_half(self) -> None:
        assert compute_breakeven(0.5) == pytest.approx(2.0 / 3.0)

    def test_rr_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_breakeven(-1.0)

    def test_rr_non_finite_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_breakeven(float("inf"))

    def test_friction_costs_mt5_native(self) -> None:
        spread_cost, slippage_cost = compute_friction_costs(200, 10, 0.01)
        assert spread_cost == 2.0
        assert slippage_cost == 0.1

    def test_friction_negative_tick_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_friction_costs(200, 10, 0.0)

    def test_rr_barrier_convention(self) -> None:
        # win = TP − spread, loss = SL + slippage
        rr = compute_rr(1.0, 2.0, spread_cost=0.5, slippage_cost=0.1)
        assert rr == pytest.approx((2.0 - 0.5) / (1.0 + 0.1))  # 1.5 / 1.1

    def test_rr_untradeable_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_rr(1.0, 0.4, spread_cost=0.5)  # win leg <= 0


# ── 2. compute_breakeven_from_params (friction models) ─────────────────────


class TestBreakevenFromParams:
    def test_barrier_friction_model(self) -> None:
        r = compute_breakeven_from_params(
            2.0,
            2.5,
            spread_points=200,
            slippage_points=10,
            tick_size=0.01,
            friction_model="barrier",
        )
        assert isinstance(r, BreakevenResult)
        # RR = (2.5 - 2.0) / (2.0 + 0.1) = 0.5 / 2.1 = 0.2381
        assert r.rr == pytest.approx(0.5 / 2.1)
        assert r.breakeven_win_rate == pytest.approx(1.0 / (1.0 + 0.5 / 2.1))
        assert r.friction_model == "barrier_net_spread_gross_slippage"

    def test_expected_r_entry_costed(self) -> None:
        r = compute_breakeven_from_params(
            1.5,
            2.5,
            spread_points=200,
            slippage_points=10,
            tick_size=0.01,
            friction_model="expected_r",
        )
        # costs baked into entry → RR = tp/sl = 2.5/1.5 = 1.6667
        assert r.rr == pytest.approx(2.5 / 1.5)
        assert r.breakeven_win_rate == pytest.approx(1.0 / (1.0 + 2.5 / 1.5))
        assert r.friction_model == "expected_r_entry_costed"

    def test_unknown_friction_model_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_breakeven_from_params(
                2.0,
                2.5,
                spread_points=200,
                slippage_points=10,
                tick_size=0.01,
                friction_model="nonsense",
            )

    def test_from_contract_delegation(self) -> None:
        contract = LabelContract(
            schema_version="label_contract.v1",
            contract_id="test-exp",
            type="expected_r",
            horizon_bars=12,
            label_classes={},
            sl_atr_mult=1.5,
            tp_atr_mult=2.5,
            bar_timeframe="M15",
            atr_period=14,
            spread_points=200,
            slippage_points=10,
            tick_size=0.01,
        )
        r = breakeven_from_contract(contract)
        assert r.rr == pytest.approx(2.5 / 1.5)
        assert r.friction_model == "expected_r_entry_costed"

    def test_from_contract_wrong_type_raises(self) -> None:
        # Param is typed Any (duck-typed LabelContract) — the isinstance guard
        # is the enforcement point; passing a str raises TypeError at runtime.
        with pytest.raises(TypeError):
            breakeven_from_contract("not a contract")


# ── 3. spearman_rho shared helper ──────────────────────────────────────────


class TestSpearmanRho:
    def test_perfect_monotone(self) -> None:
        x = np.arange(50, dtype=np.float64)
        assert spearman_rho(x, x) == pytest.approx(1.0)

    def test_inverse_monotone(self) -> None:
        x = np.arange(50, dtype=np.float64)
        assert spearman_rho(x, -x) == pytest.approx(-1.0)

    def test_constant_degenerate(self) -> None:
        x = np.ones(10)
        y = np.arange(10, dtype=np.float64)
        assert spearman_rho(x, y) == 0.0

    def test_too_small(self) -> None:
        assert spearman_rho(np.array([1.0]), np.array([2.0])) == 0.0


# ── 4. run_blind_test verdicts (mock model, real NPZ) ──────────────────────


class _MockModel:
    def __init__(self, preds: np.ndarray) -> None:
        self._preds = np.asarray(preds, dtype=np.float64)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self._preds[: len(X)]


def _write_npz(path: Path, X: np.ndarray, y: np.ndarray) -> Path:
    np.savez(path, X=X, y_long=y)
    return path


def _make_dataset(n: int = 200, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, 4))
    y = X[:, 0] * 1.0 + rng.standard_normal(n) * 0.5  # feature 0 drives label
    return X, y


class TestBlindTestVerdicts:
    def test_skill_model_passes(self, tmp_path: Path) -> None:
        X, y = _make_dataset(200)
        npz = _write_npz(tmp_path / "blind.npz", X, y)
        pred = y * 1.0  # perfect rank agreement
        res = run_blind_test(
            "model.txt",
            npz,
            y_key="y_long",
            min_rho=0.3,
            min_win_rate=0.0,
            model=_MockModel(pred),
        )
        assert res["verdict"] == "PASS"
        assert res["spearman_rho"] == pytest.approx(1.0)
        assert res["win_rate"] > 0.9  # direction always matches a positive-y sample

    def test_noise_model_fails_rho(self, tmp_path: Path) -> None:
        X, y = _make_dataset(200)
        npz = _write_npz(tmp_path / "blind.npz", X, y)
        rng = np.random.default_rng(99)
        pred = rng.standard_normal(200)  # uncorrelated noise
        res = run_blind_test(
            "model.txt",
            npz,
            y_key="y_long",
            min_rho=0.3,
            model=_MockModel(pred),
        )
        assert res["verdict"] == "FAIL"
        assert any("FAIL_RHO" in f for f in res["failures"])

    def test_near_breakeven_fails_breakeven(self, tmp_path: Path) -> None:
        """A model whose win rate <= breakeven is hard-vetoed (physical wear)."""
        X, y = _make_dataset(400, seed=3)
        npz = _write_npz(tmp_path / "blind.npz", X, y)
        # Model always goes long (pred > 0).  y has ~half positive → win_rate ~0.5.
        pred = np.full(400, 0.5)
        res = run_blind_with_breakeven(npz, pred)
        assert res["verdict"] == "FAIL"
        assert any("FAIL_BREAKEVEN" in f for f in res["failures"])
        assert res["win_rate"] <= res["breakeven_win_rate"]

    def test_insufficient_samples_is_warning(self, tmp_path: Path) -> None:
        # 5 valid rows but only 2 active trades (< min_samples) → INSUFFICIENT_OOS.
        X = np.ones((5, 4))
        y = np.array([0.5, -0.5, 0.3, -0.3, 0.2])
        npz = _write_npz(tmp_path / "blind.npz", X, y)
        res = run_blind_test(
            "model.txt",
            npz,
            y_key="y_long",
            min_rho=0.3,
            min_samples=100,
            model=_MockModel(np.array([1.0, -1.0, 0.0, 0.0, 0.0])),
        )
        # Plan semantics: < min_samples → INSUFFICIENT_OOS is a WARNING, not a
        # hard veto.  Stats are still reported when computable.
        assert res["verdict"] == "INSUFFICIENT_OOS"
        assert res["n_active"] == 2
        assert any("INSUFFICIENT_OOS" in f for f in res["failures"])

    def test_too_few_valid_rows_raises(self, tmp_path: Path) -> None:
        X = np.ones((1, 4))
        y = np.array([np.nan])  # all dropped
        npz = _write_npz(tmp_path / "blind.npz", X, y)
        with pytest.raises(OOSBlindError):
            run_blind_test("model.txt", npz, y_key="y_long", model=_MockModel(np.array([1.0])))

    def test_missing_y_key_raises(self, tmp_path: Path) -> None:
        np.savez(tmp_path / "blind.npz", X=np.ones((10, 4)))
        with pytest.raises(OOSBlindError):
            run_blind_test(
                "model.txt", tmp_path / "blind.npz", y_key="y_long", model=_MockModel(np.ones(10))
            )


def run_blind_with_breakeven(npz: Path, pred: np.ndarray) -> dict:
    """Helper: run blind test with breakeven enforcement enabled."""
    return run_blind_test(
        "model.txt",
        npz,
        y_key="y_long",
        min_rho=0.0,
        min_win_rate=0.0,
        breakeven_win_rate=0.55,
        model=_MockModel(pred),
    )


# ── 5. check_quality_gates train_spearman gate (via train.py) ──────────────


_GATE_TRAIN_METRICS = {
    "sharpe_ratio": 0.5,
    "sortino_ratio": 1.0,
    "calmar_ratio": 0.5,
    "max_vol_scaled_dd": 10.0,
}


def _make_gate_contract(min_rho: float):
    from core.contracts.training.training_contract import (
        SCHEMA_VERSION,
        ArchitectureSpec,
        DatasetSpec,
        LabelSpec,
        OutputSpec,
        QualityGateSpec,
        TrainingContract,
        ValidationSpec,
    )

    return TrainingContract(
        schema_version=SCHEMA_VERSION,
        contract_id="test",
        dataset=DatasetSpec(path=""),
        label=LabelSpec(),
        # Regression objective → win-rate gates are skipped, isolating the
        # spearman gate under test.
        architecture=ArchitectureSpec(objective_function="reg_squarederror"),
        validation=ValidationSpec(),
        quality_gates=QualityGateSpec(
            min_spearman_rho=min_rho,
            min_forward_sharpe=-0.5,
            min_train_sharpe=0.0,
            min_sortino_ratio=0.0,
            min_calmar_ratio=0.0,
            max_vol_scaled_dd_pct=100.0,
        ),
        output=OutputSpec(),
    )


class TestTrainSpearmanGate:
    def test_spearman_gate_blocks_weak_model(self) -> None:
        from scripts.training.train import check_quality_gates

        passed, results = check_quality_gates(
            {**_GATE_TRAIN_METRICS, "spearman_rho": 0.02},
            {"sharpe_ratio": 0.3},
            _make_gate_contract(min_rho=0.05),
        )
        assert not passed
        assert results["train_spearman"] is False

    def test_spearman_gate_passes_strong_model(self) -> None:
        from scripts.training.train import check_quality_gates

        passed, results = check_quality_gates(
            {**_GATE_TRAIN_METRICS, "spearman_rho": 0.08},
            {"sharpe_ratio": 0.3},
            _make_gate_contract(min_rho=0.05),
        )
        assert passed
        assert results["train_spearman"] is True

    def test_spearman_gate_disabled_by_default(self) -> None:
        from scripts.training.train import check_quality_gates

        passed, results = check_quality_gates(
            {**_GATE_TRAIN_METRICS, "spearman_rho": 0.0},
            {"sharpe_ratio": 0.3},
            _make_gate_contract(min_rho=0.0),  # legacy contract: gate off
        )
        assert results["train_spearman"] is True
        assert passed

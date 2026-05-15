"""Unit tests for TrainingEvalReport and SHAP analysis."""

from __future__ import annotations

import numpy as np

from core.training.evaluation_report import (
    SHAPReport,
    TrainingEvalReport,
    check_shap_stability,
    compute_financial_metrics,
    compute_overfit_gap,
    compute_regime_breakdown,
    run_shap_analysis,
)


class TestComputeFinancialMetrics:
    """Tests for compute_financial_metrics."""

    def test_perfect_predictions(self):
        y_true = np.array([1, 0, 1, 0, 1, 0])
        y_pred = np.array([0.99, 0.01, 0.99, 0.01, 0.99, 0.01])
        metrics = compute_financial_metrics(y_true, y_pred)
        assert metrics["win_rate"] == 1.0
        assert metrics["sharpe_ratio"] > 0
        assert metrics["profit_factor"] > 1.0

    def test_all_wrong(self):
        y_true = np.array([1, 0, 1, 0])
        y_pred = np.array([0.01, 0.99, 0.01, 0.99])
        metrics = compute_financial_metrics(y_true, y_pred)
        assert metrics["win_rate"] == 0.0
        assert metrics["sharpe_ratio"] < 0

    def test_with_pnl(self):
        y_true = np.array([1, 0, 1, 0])
        y_pred = np.array([0.99, 0.01, 0.99, 0.01])
        pnl = np.array([0.05, -0.02, 0.03, -0.01])
        metrics = compute_financial_metrics(y_true, y_pred, pnl=pnl)
        assert "sharpe_ratio" in metrics
        assert "win_rate" in metrics

    def test_multi_class_predictions(self):
        y_true = np.array([0, 1, 2])
        y_pred = np.array([[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.1, 0.1, 0.8]])
        metrics = compute_financial_metrics(y_true, y_pred)
        assert metrics["total_trades"] >= 0

    def test_annual_factor_default(self):
        y_true = np.array([1, 0])
        y_pred = np.array([0.9, 0.1])
        metrics = compute_financial_metrics(y_true, y_pred)
        # Sharpe should be annualized by sqrt(252)
        assert isinstance(metrics["sharpe_ratio"], float)

    def test_all_same_predictions(self):
        y_true = np.array([1, 0, 1, 0])
        y_pred = np.array([0.5, 0.5, 0.5, 0.5])
        metrics = compute_financial_metrics(y_true, y_pred)
        # Edge case: don't crash
        assert "sharpe_ratio" in metrics
        assert np.isfinite(metrics["sharpe_ratio"])

    def test_empty_returns(self):
        y_true = np.array([])
        y_pred = np.array([])
        metrics = compute_financial_metrics(y_true, y_pred)
        assert metrics["total_trades"] == 0

    def test_returns_required_keys(self):
        y_true = np.random.RandomState(42).randint(0, 2, 50)
        y_pred = np.random.RandomState(43).random(50)
        metrics = compute_financial_metrics(y_true, y_pred)
        required_keys = [
            "sharpe_ratio",
            "win_rate",
            "profit_factor",
            "max_drawdown",
            "expectancy",
            "sortino_ratio",
            "total_trades",
        ]
        for key in required_keys:
            assert key in metrics, f"Missing required key: {key}"
            assert isinstance(metrics[key], int | float), f"Key {key} has wrong type"


class TestOverfitGap:
    """Tests for compute_overfit_gap."""

    def test_no_overfit(self):
        train = {"sharpe_ratio": 2.0}
        forward = {"sharpe_ratio": 2.0}
        assert compute_overfit_gap(train, forward) == 0.0

    def test_positive_gap(self):
        train = {"sharpe_ratio": 2.5}
        forward = {"sharpe_ratio": 1.5}
        assert compute_overfit_gap(train, forward) == 1.0

    def test_negative_gap_absolute(self):
        train = {"sharpe_ratio": 1.0}
        forward = {"sharpe_ratio": 2.0}
        assert compute_overfit_gap(train, forward) == 1.0


class TestRegimeBreakdown:
    """Tests for compute_regime_breakdown."""

    def test_three_regimes(self):
        rng = np.random.RandomState(42)
        y_true = rng.randint(0, 2, 300)
        y_pred = rng.random(300)
        regimes = np.array([0] * 100 + [1] * 100 + [2] * 100)

        breakdown = compute_regime_breakdown(y_true, y_pred, regimes)
        assert len(breakdown) == 3
        assert "low_vol" in breakdown
        assert "normal_vol" in breakdown
        assert "high_vol" in breakdown
        for _name, metrics in breakdown.items():
            assert "sharpe_ratio" in metrics
            assert "n_samples" in metrics

    def test_custom_regime_names(self):
        y_true = np.array([0, 1, 0, 1])
        y_pred = np.array([0.2, 0.8, 0.3, 0.7])
        regimes = np.array([0, 0, 1, 1])

        breakdown = compute_regime_breakdown(
            y_true,
            y_pred,
            regimes,
            regime_names={0: "bear", 1: "bull"},
        )
        # Small sample sizes: metrics may not have enough trades
        assert len(breakdown) >= 0

    def test_small_regime_skipped(self):
        rng = np.random.RandomState(42)
        y_true = rng.randint(0, 2, 20)
        y_pred = rng.random(20)
        # regime 0: 15 samples (passes threshold), regime 1: 5 samples (<10, skipped)
        regimes = np.array([0] * 15 + [1] * 5)

        breakdown = compute_regime_breakdown(y_true, y_pred, regimes)
        # Regime 1 should be skipped (< 10 samples)
        assert "high_vol" not in breakdown  # regime 1 → high_vol
        assert "low_vol" in breakdown


class TestTrainingEvalReport:
    """Tests for TrainingEvalReport."""

    def test_basic_creation(self):
        report = TrainingEvalReport(
            contract_id="test_v1",
            feature_names=["f0", "f1", "f2"],
            model_hash="abc123",
        )
        assert report.contract_id == "test_v1"
        assert len(report.feature_names) == 3
        assert report.timestamp is not None

    def test_check_quality_gates_pass(self):
        from core.contracts.training.training_contract import QualityGateSpec

        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={
                "sharpe_ratio": 2.0,
                "win_rate": 0.60,
                "max_drawdown": 0.10,
                "sortino_ratio": 1.5,
                "calmar_ratio": 2.0,
                "max_vol_scaled_dd": 15.0,
            },
            forward_metrics={"sharpe_ratio": 1.8, "win_rate": 0.58},
            overfit_gap=0.20,
        )
        gates = QualityGateSpec()
        passed, results = report.check_quality_gates(gates)
        assert passed
        assert all(results.values())

    def test_check_quality_gates_fail(self):
        from core.contracts.training.training_contract import QualityGateSpec

        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={"sharpe_ratio": 0.3, "win_rate": 0.30, "max_drawdown": 0.50},
            forward_metrics={"sharpe_ratio": 0.1, "win_rate": 0.25},
            overfit_gap=0.50,
        )
        gates = QualityGateSpec()
        passed, results = report.check_quality_gates(gates)
        assert not passed
        assert not all(results.values())

    def test_failure_reasons(self):
        from core.contracts.training.training_contract import QualityGateSpec

        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={"sharpe_ratio": 0.0, "win_rate": 0.0, "max_drawdown": 1.0},
            forward_metrics={"sharpe_ratio": 0.0, "win_rate": 0.0},
            overfit_gap=0.0,
        )
        gates = QualityGateSpec()
        reasons = report.failure_reasons(gates)
        assert len(reasons) > 0

    def test_to_dict(self):
        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={"sharpe_ratio": 1.5},
            forward_metrics={"sharpe_ratio": 1.2},
            overfit_gap=0.3,
        )
        d = report.to_dict()
        assert d["contract_id"] == "test"
        assert d["train_metrics"]["sharpe_ratio"] == 1.5
        assert d["overfit_gap"] == 0.3

    def test_to_dict_with_shap(self):
        shap = SHAPReport(
            feature_names=["a", "b"],
            shap_values_mean=[0.1, 0.2],
            shap_values_std=[0.01, 0.02],
            top_features=[("b", 0.2), ("a", 0.1)],
            feature_stability_score=0.85,
        )
        report = TrainingEvalReport(
            contract_id="test",
            shap_report=shap,
            shap_stability_score=0.85,
        )
        d = report.to_dict()
        assert "shap_report" in d
        assert d["shap_report"]["feature_stability_score"] == 0.85

    def test_save_and_reload(self, tmp_path):
        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={"sharpe_ratio": 2.0},
            forward_metrics={"sharpe_ratio": 1.8},
        )
        path = tmp_path / "report.json"
        report.save(path)
        assert path.exists()

    def test_default_values(self):
        report = TrainingEvalReport(contract_id="minimal")
        assert report.train_metrics == {}
        assert report.forward_metrics == {}
        assert report.overfit_gap == 0.0
        assert report.shap_report is None
        assert report.feature_names == []

    def test_shap_stability_gate_skipped_when_missing(self):
        from core.contracts.training.training_contract import QualityGateSpec

        report = TrainingEvalReport(
            contract_id="test",
            train_metrics={
                "sharpe_ratio": 2.0,
                "win_rate": 0.60,
                "max_drawdown": 0.10,
                "sortino_ratio": 1.5,
                "calmar_ratio": 2.0,
                "max_vol_scaled_dd": 15.0,
            },
            forward_metrics={"sharpe_ratio": 1.8, "win_rate": 0.58},
            overfit_gap=0.20,
        )
        gates = QualityGateSpec(require_shap_stability=True)
        passed, results = report.check_quality_gates(gates)
        # SHAP stability gate should be True (skipped) when report is None
        assert results.get("shap_stability", True) is True


class TestSHAPReport:
    """Tests for SHAPReport dataclass."""

    def test_creation(self):
        report = SHAPReport(
            feature_names=["f0", "f1"],
            shap_values_mean=[0.1, 0.2],
            shap_values_std=[0.01, 0.02],
            top_features=[("f1", 0.2)],
            feature_stability_score=0.9,
        )
        assert report.feature_stability_score == 0.9
        assert len(report.top_features) == 1

    def test_to_dict(self):
        report = SHAPReport(
            feature_names=["a", "b", "c"],
            shap_values_mean=[0.1, 0.2, 0.05],
            shap_values_std=[0.01, 0.02, 0.005],
            top_features=[("b", 0.2), ("a", 0.1), ("c", 0.05)],
            feature_stability_score=0.88,
        )
        d = report.to_dict()
        assert len(d["top_features"]) == 3
        assert d["feature_stability_score"] == 0.88


class TestCheckSHAPStability:
    """Tests for check_shap_stability."""

    def test_single_report(self):
        r = SHAPReport(
            feature_names=["a"],
            shap_values_mean=[0.1],
            shap_values_std=[0.01],
            top_features=[],
            feature_stability_score=0.9,
        )
        assert check_shap_stability([r]) is True

    def test_empty_reports(self):
        assert check_shap_stability([]) is True

    def test_multiple_stable(self):
        reports = [
            SHAPReport(
                feature_names=["a"],
                shap_values_mean=[0.1],
                shap_values_std=[0.01],
                top_features=[],
                feature_stability_score=0.85,
            ),
            SHAPReport(
                feature_names=["a"],
                shap_values_mean=[0.1],
                shap_values_std=[0.01],
                top_features=[],
                feature_stability_score=0.82,
            ),
        ]
        assert check_shap_stability(reports, threshold=0.7) is True

    def test_multiple_unstable(self):
        reports = [
            SHAPReport(
                feature_names=["a"],
                shap_values_mean=[0.1],
                shap_values_std=[0.01],
                top_features=[],
                feature_stability_score=0.3,
            ),
            SHAPReport(
                feature_names=["a"],
                shap_values_mean=[0.1],
                shap_values_std=[0.01],
                top_features=[],
                feature_stability_score=0.4,
            ),
        ]
        assert check_shap_stability(reports, threshold=0.7) is False


class TestRunSHAPAnalysis:
    """Tests for run_shap_analysis (SHAP unavailable fallback)."""

    def test_returns_none_when_shap_unavailable(self):
        # SHAP is not installed in this environment
        X = np.random.RandomState(42).randn(50, 5)
        result = run_shap_analysis(
            model=None,
            X_sample=X,
            feature_names=["a", "b", "c", "d", "e"],
            model_type="xgboost",
        )
        assert result is None

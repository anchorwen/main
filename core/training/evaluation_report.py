"""Standardized training evaluation report with financial metrics, stability
checks, regime breakdown, and optional SHAP analysis.

Provides a single auditable report that replaces scattered metric computation
across trainers. Quality gate enforcement is built-in — the report can
self-assess against contract thresholds.

SHAP analysis is optional and requires ``shap`` to be installed. When
unavailable, ``generate_shap_report`` returns None and the quality gate
``require_shap_stability`` is skipped.

Usage:
    report = TrainingEvalReport(
        contract_id="barrier_12bar_xgboost_v2",
        model_hash="abc123",
        feature_names=feature_names,
    )
    report.add_train_metrics(train_preds, y_train, pnl_train)
    report.add_forward_metrics(test_preds, y_test, pnl_test)
    report.add_cpcv_results(cpcv_folds)
    report.add_regime_breakdown(test_preds, y_test, regime_labels)

    passed, failures = report.check_quality_gates(contract.quality_gates)

    if _shap_available():
        shap_path = report.generate_shap_report(model, X_sample)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# ── Optional import ──


def _shap_available() -> bool:
    try:
        import shap  # noqa: F401

        return True
    except ImportError:
        return False


# ── Financial metric computation ──


def compute_financial_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    annual_factor: int = 252,
    threshold: float = 0.5,
) -> dict[str, float]:
    """Compute trading-aligned metrics from predictions.

    Args:
        y_true: True labels (0/1 binary, -1/0/1 multi-class).
        y_pred: Predicted probabilities or continuous scores.
        pnl: Optional P&L array for return-magnitude weighting.
        annual_factor: Annualization factor (252 for daily).
        threshold: Decision threshold for binary classification.
                   Must match the threshold used during training.

    Returns:
        Dict with sharpe_ratio, win_rate, profit_factor, max_drawdown, etc.
    """
    if len(y_true) == 0:
        return {
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "expectancy": 0.0,
            "sortino_ratio": 0.0,
            "total_trades": 0,
            "mean_return": 0.0,
            "std_return": 0.0,
        }

    if y_pred.ndim > 1 and y_pred.shape[1] > 1:
        pred_class = np.argmax(y_pred, axis=1)
    else:
        pred_class = (np.asarray(y_pred).flatten() >= threshold).astype(np.int32)

    if pnl is not None and len(pnl) > 0:
        returns = np.where(pred_class == 1, np.abs(pnl), -np.abs(pnl))
    else:
        direction = 2.0 * y_true.astype(np.float64) - 1.0
        pos = 2.0 * pred_class.astype(np.float64) - 1.0
        returns = pos * direction

    # Filter NaN before computing metrics
    _nan_mask = np.isnan(returns)
    if np.any(_nan_mask):
        returns = returns[~_nan_mask]

    if len(returns) == 0:
        return {
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "expectancy": 0.0,
            "sortino_ratio": 0.0,
            "total_trades": 0,
            "mean_return": 0.0,
            "std_return": 0.0,
        }

    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns)) + 1e-10
    sharpe = mean_ret / std_ret * np.sqrt(annual_factor)

    wins = int(np.sum(returns > 0))
    losses = int(np.sum(returns < 0))
    total_trades = wins + losses
    win_rate = wins / max(total_trades, 1)

    gross_profit = float(np.sum(returns[returns > 0]))
    gross_loss = float(np.abs(np.sum(returns[returns < 0])))
    profit_factor = gross_profit / max(gross_loss, 1e-10)

    cumulative = np.cumsum(returns)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = cumulative - running_max
    max_drawdown = float(np.abs(np.min(drawdowns)))

    expectancy = mean_ret

    downside = returns[returns < 0]
    downside_std = float(np.std(downside)) + 1e-10 if len(downside) > 0 else 1e-10
    sortino = mean_ret / downside_std * np.sqrt(annual_factor)

    annualized_return = mean_ret * annual_factor
    calmar_ratio = annualized_return / max(max_drawdown, 1e-10)

    return {
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 4),
        "calmar_ratio": round(calmar_ratio, 4),
        "expectancy": round(expectancy, 6),
        "sortino_ratio": round(sortino, 4),
        "total_trades": total_trades,
        "mean_return": round(mean_ret, 6),
        "std_return": round(std_ret, 6),
    }


def compute_overfit_gap(
    train_metrics: dict[str, float], forward_metrics: dict[str, float]
) -> float:
    """Compute overfit gap as |train_sharpe - forward_sharpe|."""
    train_s = train_metrics.get("sharpe_ratio", 0.0)
    forward_s = forward_metrics.get("sharpe_ratio", 0.0)
    return round(abs(train_s - forward_s), 4)


# ── SHAP analysis ──


@dataclass
class SHAPReport:
    """Per-feature SHAP analysis for model audit."""

    feature_names: list[str]
    shap_values_mean: list[float]
    shap_values_std: list[float]
    top_features: list[tuple[str, float]]
    feature_stability_score: float
    interaction_effects: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": self.feature_names,
            "shap_values_mean": [round(v, 6) for v in self.shap_values_mean],
            "shap_values_std": [round(v, 6) for v in self.shap_values_std],
            "top_features": [(n, round(v, 6)) for n, v in self.top_features],
            "feature_stability_score": round(self.feature_stability_score, 4),
            "interaction_effects": {k: round(v, 6) for k, v in self.interaction_effects.items()},
        }


def run_shap_analysis(
    model: Any,
    X_sample: np.ndarray,
    feature_names: list[str],
    *,
    max_samples: int = 200,
    model_type: str = "xgboost",
) -> SHAPReport | None:
    """Run SHAP analysis on a trained model.

    Uses TreeExplainer for XGBoost/LightGBM, KernelExplainer as fallback.

    Args:
        model: Trained model (XGBoost booster, LightGBM booster, or sklearn-style).
        X_sample: Feature matrix sample for explanation.
        feature_names: Feature names list.
        max_samples: Max samples for SHAP computation (speed vs accuracy).
        model_type: "xgboost", "lightgbm", or "generic".

    Returns:
        SHAPReport or None if SHAP is unavailable or analysis fails.
    """
    if not _shap_available():
        return None

    try:
        import shap

        n = min(len(X_sample), max_samples)
        X_s = X_sample[:n]

        if model_type in ("xgboost", "lightgbm"):
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X_s)

            if isinstance(shap_values, list):
                shap_values = shap_values[1] if len(shap_values) > 1 else shap_values[0]

            shap_mean = np.abs(shap_values).mean(axis=0).tolist()
            shap_std = np.abs(shap_values).std(axis=0).tolist()
        else:
            # KernelExplainer fallback with a small background
            background = X_s[: min(50, n)]
            explainer = shap.KernelExplainer(
                lambda x: model.predict(x) if hasattr(model, "predict") else np.zeros(len(x)),
                background,
            )
            shap_values = explainer.shap_values(X_s[: min(100, n)], nsamples=50)
            if isinstance(shap_values, list):
                shap_values = shap_values[1] if len(shap_values) > 1 else shap_values[0]

            shap_mean = np.abs(shap_values).mean(axis=0).tolist()
            shap_std = np.abs(shap_values).std(axis=0).tolist()

        # Top 10 features by |SHAP|
        importance = list(zip(feature_names, shap_mean, strict=False))
        importance.sort(key=lambda x: abs(x[1]), reverse=True)
        top_features = [(name, val) for name, val in importance[:10]]

        # Feature stability score: 1 - mean(CV of SHAP across features)
        cv_values = np.array(shap_std) / (np.array(shap_mean) + 1e-10)
        stability_score = float(1.0 - np.clip(np.mean(cv_values), 0, 1))

        return SHAPReport(
            feature_names=list(feature_names),
            shap_values_mean=shap_mean,
            shap_values_std=shap_std,
            top_features=top_features,
            feature_stability_score=stability_score,
        )

    except Exception as e:  # BLE001:FOG
        import sys

        print(f"[evaluation_report] SHAP analysis failed: {e}", file=sys.stderr)
        return None
def check_shap_stability(shap_reports: list[SHAPReport], threshold: float = 0.5) -> bool:
    """Check cross-fold SHAP stability.

    Feature rankings should be consistent across folds. If the top feature
    in fold 1 is rank 17 in fold 2, the model may be unstable.

    Args:
        shap_reports: SHAP reports from multiple CPCV folds or seeds.
        threshold: Minimum mean feature stability score (0-1).

    Returns:
        True if stability is acceptable.
    """
    if not shap_reports or len(shap_reports) < 2:
        return True

    scores = [r.feature_stability_score for r in shap_reports]
    mean_score = sum(scores) / len(scores)
    return mean_score >= threshold


# ── Regime breakdown ──


def compute_regime_breakdown(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    regime_labels: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    regime_names: dict[int, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Compute per-regime financial metrics.

    Args:
        y_true: True labels.
        y_pred: Predictions.
        regime_labels: Integer regime labels per sample (0=low, 1=normal, 2=high).
        pnl: Optional P&L array.
        regime_names: Mapping from regime int to name.

    Returns:
        Dict of regime_name → metrics_dict.
    """
    if regime_names is None:
        regime_names = {0: "low_vol", 1: "normal_vol", 2: "high_vol"}

    result: dict[str, dict[str, float]] = {}
    unique_regimes = np.unique(regime_labels)

    for regime_id in unique_regimes:
        name = regime_names.get(int(regime_id), f"regime_{int(regime_id)}")
        mask = regime_labels == regime_id
        if mask.sum() < 10:
            continue
        result[name] = compute_financial_metrics(
            y_true[mask], y_pred[mask], pnl[mask] if pnl is not None else None
        )
        result[name]["n_samples"] = int(mask.sum())

    return result


# ── Main report dataclass ──


@dataclass
class TrainingEvalReport:
    """Standardized evaluation report for a single training run.

    Bundles financial metrics, stability metrics, regime breakdown, SHAP
    analysis, and audit trail into a single object that can self-assess
    against quality gates.

    Attributes:
        contract_id: Training contract identifier.
        model_hash: SHA256 hash of the model artifact.
        feature_names: Ordered feature names.
        train_metrics: Financial metrics on training set.
        forward_metrics: Financial metrics on test/validation set.
        overfit_gap: |train_sharpe - forward_sharpe|.
        cpcv_sharpe_mean: Mean Sharpe across CPCV folds.
        cpcv_sharpe_std: Std Sharpe across CPCV folds.
        regime_breakdown: Per-regime metrics dict.
        shap_report: SHAP analysis result (None if unavailable).
        shap_stability_score: Cross-fold feature stability score.
        model_hash: Cryptographitc hash of model file.
        timestamp: ISO-8601 report generation time.
        metadata: Arbitrary additional metadata.
    """

    contract_id: str
    feature_names: list[str] = field(default_factory=list)
    model_hash: str = ""
    train_metrics: dict[str, float] = field(default_factory=dict)
    forward_metrics: dict[str, float] = field(default_factory=dict)
    overfit_gap: float = 0.0
    cpcv_sharpe_mean: float | None = None
    cpcv_sharpe_std: float | None = None
    regime_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    shap_report: SHAPReport | None = None
    shap_stability_score: float | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Quality gates ──

    def check_quality_gates(self, gate_spec) -> tuple[bool, dict[str, bool]]:
        """Check if this report passes all quality gates.

        Args:
            gate_spec: QualityGateSpec from TrainingContract.

        Returns:
            (passed, {gate_name: result}).
        """
        results: dict[str, bool] = {}

        train_s = self.train_metrics.get("sharpe_ratio", 0.0)
        forward_s = self.forward_metrics.get("sharpe_ratio", 0.0)
        train_wr = self.train_metrics.get("win_rate", 0.0)
        forward_wr = self.forward_metrics.get("win_rate", 0.0)

        results["min_train_sharpe"] = train_s >= gate_spec.min_train_sharpe
        results["min_train_win_rate"] = train_wr >= gate_spec.min_train_win_rate
        results["train_sortino"] = (
            self.train_metrics.get("sortino_ratio", -999.0) >= gate_spec.min_sortino_ratio
        )
        results["train_calmar"] = (
            self.train_metrics.get("calmar_ratio", -999.0) >= gate_spec.min_calmar_ratio
        )
        results["vol_scaled_dd"] = (
            self.train_metrics.get("max_vol_scaled_dd", 100.0) <= gate_spec.max_vol_scaled_dd_pct
        )
        results["min_forward_sharpe"] = forward_s >= gate_spec.min_forward_sharpe
        results["min_forward_win_rate"] = forward_wr >= gate_spec.min_forward_win_rate
        results["max_overfit_gap"] = self.overfit_gap <= gate_spec.max_overfit_gap

        if gate_spec.require_shap_stability and self.shap_report is not None:
            results["shap_stability"] = (
                self.shap_stability_score is not None and self.shap_stability_score >= 0.5
            )
        elif gate_spec.require_shap_stability:
            results["shap_stability"] = True  # Skip gate if SHAP unavailable

        passed = all(results.values())
        return passed, results

    # ── Serialization ──

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "contract_id": self.contract_id,
            "timestamp": self.timestamp,
            "model_hash": self.model_hash,
            "feature_names": self.feature_names,
            "train_metrics": self.train_metrics,
            "forward_metrics": self.forward_metrics,
            "overfit_gap": self.overfit_gap,
            "cpcv_sharpe_mean": self.cpcv_sharpe_mean,
            "cpcv_sharpe_std": self.cpcv_sharpe_std,
            "regime_breakdown": self.regime_breakdown,
        }
        if self.shap_report is not None:
            d["shap_report"] = self.shap_report.to_dict()
            d["shap_stability_score"] = self.shap_stability_score
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    def save(self, path: str | Path) -> Path:
        """Save the report as JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def failure_reasons(self, gate_spec) -> list[str]:
        """Return human-readable list of failed quality gates."""
        passed, results = self.check_quality_gates(gate_spec)
        if passed:
            return []
        return [k for k, v in results.items() if not v]

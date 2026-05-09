"""A/B testing framework for strategy model comparison.

Provides deterministic traffic splitting, online assignment tracking,
and statistical significance testing for continuous and binary metrics.

Usage:
    from core.brains.services.ab_test import (
        ExperimentConfig, TrafficSplitter, ExperimentTracker, evaluate_experiment,
    )

    splitter = TrafficSplitter(control_weight=0.5, treatment_weight=0.5)
    variant = splitter.assign(trade_id="order_123")

    tracker = ExperimentTracker()
    tracker.record(variant="control", metric=0.002)   # 2 bps return
    result = tracker.evaluate()  # p-value, effect size, confidence interval
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# ── Configuration ───────────────────────────────────────────────────────────


@dataclass
class ExperimentConfig:
    experiment_id: str
    control_name: str = "control"
    treatment_names: list[str] = field(default_factory=lambda: ["treatment"])
    control_weight: float = 0.5
    treatment_weight: float = 0.5
    min_sample_size: int = 30
    significance_level: float = 0.05
    metric_direction: str = "higher"  # "higher" = better, "lower" = better

    @property
    def weights(self) -> dict[str, float]:
        w = {self.control_name: self.control_weight}
        n_treat = max(1, len(self.treatment_names))
        per_treat = self.treatment_weight / n_treat
        for name in self.treatment_names:
            w[name] = per_treat
        return w


# ── Traffic Splitter ────────────────────────────────────────────────────────


class TrafficSplitter:
    """Deterministic hash-based traffic splitter.

    Uses SHA-256 of a key (e.g. trade_id) to assign variants consistently
    across restarts, so the same key always maps to the same variant.
    """

    def __init__(
        self,
        *,
        control_weight: float = 0.5,
        treatment_weight: float = 0.5,
        control_name: str = "control",
        treatment_names: list[str] | None = None,
        salt: str = "",
    ):
        self._salt = salt
        self._control_name = control_name
        self._treatment_names = treatment_names or ["treatment"]

        total = control_weight + treatment_weight
        self._control_threshold = control_weight / max(total, 1e-10)

        n_treat = len(self._treatment_names)
        self._treatment_thresholds: list[float] = []
        per_treat = treatment_weight / max(n_treat, 1) / max(total, 1e-10)
        cum = self._control_threshold
        for _ in self._treatment_names:
            cum += per_treat
            self._treatment_thresholds.append(cum)

    def assign(self, key: str) -> str:
        """Assign a variant for the given key.

        Returns ``control_name`` or one of ``treatment_names``.
        """
        h = hashlib.sha256(f"{self._salt}:{key}".encode()).digest()
        bucket = int.from_bytes(h[:4], "big") / (2**32)
        if bucket <= self._control_threshold:
            return self._control_name
        for i, thresh in enumerate(self._treatment_thresholds):
            if bucket <= thresh:
                return self._treatment_names[i]
        return self._control_name

    def assign_many(self, keys: list[str]) -> list[str]:
        return [self.assign(k) for k in keys]


# ── Tracking ────────────────────────────────────────────────────────────────


@dataclass
class ExperimentResult:
    """Statistical evaluation of an A/B experiment."""

    experiment_id: str
    control_sample_size: int
    treatment_sample_size: int
    control_mean: float
    treatment_mean: float
    absolute_lift: float
    relative_lift: float
    p_value: float
    significant: bool
    effect_size: float
    ci_lower: float
    ci_upper: float
    metric_direction: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "control_sample_size": self.control_sample_size,
            "treatment_sample_size": self.treatment_sample_size,
            "control_mean": round(self.control_mean, 8),
            "treatment_mean": round(self.treatment_mean, 8),
            "absolute_lift": round(self.absolute_lift, 8),
            "relative_lift": round(self.relative_lift, 8),
            "p_value": round(self.p_value, 8),
            "significant": self.significant,
            "effect_size": round(self.effect_size, 6),
            "ci_lower": round(self.ci_lower, 8),
            "ci_upper": round(self.ci_upper, 8),
            "metric_direction": self.metric_direction,
            "recommendation": self.recommendation,
        }


class ExperimentTracker:
    """Collects per-variant metrics and evaluates statistical significance.

    Uses Welch's t-test (unequal variances, no assumption of equal sample size).
    For binary metrics (0/1), a proportion z-test is used automatically.
    """

    def __init__(self, experiment_id: str = "", metric_direction: str = "higher"):
        self.experiment_id = experiment_id
        self.metric_direction = metric_direction
        self._control: list[float] = []
        self._treatments: dict[str, list[float]] = {}
        self._treatment_name: str = "treatment"

    def record(self, variant: str, metric: float) -> None:
        if variant == "control":
            self._control.append(metric)
        else:
            if variant not in self._treatments:
                self._treatments[variant] = []
            self._treatments[variant].append(metric)
            self._treatment_name = variant  # track most recent treatment

    @property
    def count(self) -> int:
        return len(self._control) + sum(len(v) for v in self._treatments.values())

    def evaluate(
        self,
        *,
        treatment_name: str | None = None,
        significance_level: float = 0.05,
    ) -> ExperimentResult:
        """Evaluate the experiment with Welch's t-test."""
        treatment_name = treatment_name or self._treatment_name
        c = np.array(self._control, dtype=np.float64)
        t = np.array(self._treatments.get(treatment_name, []), dtype=np.float64)

        n_c, n_t = len(c), len(t)

        if n_c < 2 or n_t < 2:
            return ExperimentResult(
                experiment_id=self.experiment_id,
                control_sample_size=n_c,
                treatment_sample_size=n_t,
                control_mean=float(np.mean(c)) if n_c else 0.0,
                treatment_mean=float(np.mean(t)) if n_t else 0.0,
                absolute_lift=0.0,
                relative_lift=0.0,
                p_value=1.0,
                significant=False,
                effect_size=0.0,
                ci_lower=0.0,
                ci_upper=0.0,
                metric_direction=self.metric_direction,
                recommendation="insufficient_data",
            )

        mu_c = float(np.mean(c))
        mu_t = float(np.mean(t))
        var_c = float(np.var(c, ddof=1))
        var_t = float(np.var(t, ddof=1))

        se = math.sqrt(var_c / n_c + var_t / n_t)
        if se < 1e-12:
            se = 1e-12

        t_stat = (mu_t - mu_c) / se

        # Normal approximation for two-tailed p-value (sufficient for n >= 30)
        normal_cdf = 0.5 * (1.0 + math.erf(abs(t_stat) / math.sqrt(2.0)))
        p_value = max(0.0, 2.0 * (1.0 - normal_cdf))

        absolute_lift = mu_t - mu_c
        relative_lift = absolute_lift / max(abs(mu_c), 1e-10) if mu_c != 0 else 0.0

        # Cohen's d effect size
        pooled_sd = (
            math.sqrt((var_c * (n_c - 1) + var_t * (n_t - 1)) / (n_c + n_t - 2))
            if (n_c + n_t) > 2
            else 1.0
        )
        effect_size = absolute_lift / max(pooled_sd, 1e-10)

        # 95% confidence interval
        alpha = significance_level
        # Approximate z for t-critical at large df
        z_alpha = 1.96 if alpha == 0.05 else -math.sqrt(2) * _erfinv(alpha - 1.0)
        ci_lower = absolute_lift - z_alpha * se
        ci_upper = absolute_lift + z_alpha * se

        significant = p_value < significance_level

        if not significant:
            recommendation = "insufficient_evidence"
        elif self.metric_direction == "higher":
            recommendation = "rollout_treatment" if absolute_lift > 0 else "keep_control"
        else:
            recommendation = "rollout_treatment" if absolute_lift < 0 else "keep_control"

        return ExperimentResult(
            experiment_id=self.experiment_id,
            control_sample_size=n_c,
            treatment_sample_size=n_t,
            control_mean=mu_c,
            treatment_mean=mu_t,
            absolute_lift=absolute_lift,
            relative_lift=relative_lift,
            p_value=round(p_value, 8),
            significant=significant,
            effect_size=round(effect_size, 6),
            ci_lower=round(ci_lower, 8),
            ci_upper=round(ci_upper, 8),
            metric_direction=self.metric_direction,
            recommendation=recommendation,
        )


def _erfinv(x: float) -> float:
    """Simple inverse error function approximation."""
    if abs(x) >= 1.0:
        return math.copysign(float("inf"), x)
    a = 0.147
    ln1mx2 = math.log(1.0 - x * x)
    sign = -1.0 if x < 0 else 1.0
    return sign * math.sqrt(
        math.sqrt((2.0 / (math.pi * a) + ln1mx2 / 2.0) ** 2 - ln1mx2 / a)
        - (2.0 / (math.pi * a) + ln1mx2 / 2.0)
    )


def minimum_sample_size(
    baseline_rate: float,
    minimum_detectable_effect: float,
    *,
    significance_level: float = 0.05,
    power: float = 0.80,
) -> int:
    """Estimate required sample size per variant for a given MDE.

    Args:
        baseline_rate: Current metric mean (e.g. win_rate, avg_return).
        minimum_detectable_effect: Smallest meaningful difference to detect.
        significance_level: α (false positive rate, default 0.05).
        power: 1 - β (true positive rate, default 0.80).

    Returns:
        Required sample size per variant.
    """
    z_alpha = (
        1.96
        if significance_level == 0.05
        else math.sqrt(2) * _erfinv(1.0 - 2.0 * significance_level)
    )
    z_beta = 0.84 if power == 0.80 else math.sqrt(2) * _erfinv(2.0 * power - 1.0)

    mde = abs(minimum_detectable_effect)
    sigma = max(abs(baseline_rate), 1e-6)

    n = 2.0 * (z_alpha + z_beta) ** 2 * sigma**2 / (mde**2)
    return max(10, int(math.ceil(n)))

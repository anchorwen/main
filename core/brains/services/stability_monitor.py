"""Population Stability Index (PSI) and Characteristic Stability Index (CSI).

Monitors feature and prediction drift between reference and production
distributions.  High PSI/CSI triggers retraining warnings.

Usage:
    from core.brains.services.stability_monitor import compute_psi, compute_csi, StabilityReport

    psi = compute_psi(ref_probs, prod_probs)
    csi = compute_csi(ref_features, prod_features)  # per-feature
    report = StabilityReport(psi=psi, csi_per_feature=csi, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ── Threshold constants ───────────────────────────────────────────────────

PSI_WARNING = 0.10  # elevated — monitor
PSI_CRITICAL = 0.25  # high — retrain recommended
CSI_WARNING = 0.10
CSI_CRITICAL = 0.25


# ── Core computation ──────────────────────────────────────────────────────


def compute_psi(
    expected: np.ndarray,
    actual: np.ndarray,
    *,
    bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """Population Stability Index between two 1-D distributions.

    .. deprecated::
        Prefer ``scripts/monitor_feature_drift._compute_psi()`` (DQAF-060)
        which uses **fixed baseline bin edges** (equal-frequency deciles).
        This function uses equal-width binning over the combined range,
        which is less robust for drift detection with heavy-tailed features.

    PSI = Σ (actual_i - expected_i) * ln(actual_i / expected_i)

    Args:
        expected: Reference distribution (e.g., training predictions).
        actual: Production distribution (e.g., live predictions).
        bins: Number of equal-width bins for discretisation.
        epsilon: Small value to avoid log(0).

    Returns:
        PSI value.  < 0.10 is stable, > 0.25 warrants retraining.
    """
    e = np.asarray(expected, dtype=np.float64).ravel()
    a = np.asarray(actual, dtype=np.float64).ravel()

    if len(e) == 0 or len(a) == 0:
        return 0.0

    # Equal-width binning over the combined range
    combined = np.concatenate([e, a])
    bin_min = combined.min()
    bin_max = combined.max()

    if bin_max - bin_min < epsilon:
        return 0.0

    bin_edges = np.linspace(bin_min, bin_max, bins + 1)

    e_hist, _ = np.histogram(e, bins=bin_edges)
    a_hist, _ = np.histogram(a, bins=bin_edges)

    # Convert to proportions
    e_total = e_hist.sum()
    a_total = a_hist.sum()

    if e_total == 0 or a_total == 0:
        return 0.0

    e_prop = e_hist.astype(np.float64) / e_total + epsilon
    a_prop = a_hist.astype(np.float64) / a_total + epsilon

    psi = float(np.sum((a_prop - e_prop) * np.log(a_prop / e_prop)))
    return round(psi, 6)


def compute_csi(
    expected: np.ndarray,
    actual: np.ndarray,
    *,
    bins: int = 10,
    epsilon: float = 1e-6,
) -> np.ndarray:
    """Characteristic Stability Index — per-feature PSI.

    Args:
        expected: Reference feature matrix (n_samples, n_features).
        actual: Production feature matrix (n_samples, n_features).
        bins: Number of equal-width bins.

    Returns:
        1-D array of CSI values, one per feature column.
    """
    e = np.asarray(expected, dtype=np.float64)
    a = np.asarray(actual, dtype=np.float64)

    if e.ndim != 2 or a.ndim != 2:
        raise ValueError("Expected 2-D arrays (n_samples, n_features) for CSI")

    if e.shape[1] != a.shape[1]:
        raise ValueError(f"Feature count mismatch: expected={e.shape[1]}, actual={a.shape[1]}")

    n_features = e.shape[1]
    csi_values = np.empty(n_features, dtype=np.float64)

    for j in range(n_features):
        csi_values[j] = compute_psi(e[:, j], a[:, j], bins=bins, epsilon=epsilon)

    return csi_values


# ── Report ────────────────────────────────────────────────────────────────


@dataclass
class StabilityReport:
    """Aggregated stability check result."""

    psi: float
    csi_per_feature: np.ndarray
    feature_names: list[str] = field(default_factory=list)
    psi_status: str = "stable"  # stable | warning | critical
    csi_alerts: list[str] = field(default_factory=list)
    retrain_recommended: bool = False

    def to_dict(self) -> dict:
        feature_alerts = {}
        for j, name in enumerate(
            self.feature_names
            if self.feature_names
            else [f"f_{i}" for i in range(len(self.csi_per_feature))]
        ):
            csi_val = float(self.csi_per_feature[j])
            if csi_val >= CSI_CRITICAL:
                feature_alerts[name] = {"csi": csi_val, "level": "critical"}
            elif csi_val >= CSI_WARNING:
                feature_alerts[name] = {"csi": csi_val, "level": "warning"}

        return {
            "psi": self.psi,
            "psi_status": self.psi_status,
            "csi_alerts": feature_alerts,
            "retrain_recommended": self.retrain_recommended,
        }


def build_stability_report(
    expected_preds: np.ndarray,
    actual_preds: np.ndarray,
    expected_features: np.ndarray,
    actual_features: np.ndarray,
    *,
    feature_names: list[str] | None = None,
    psi_warning: float = PSI_WARNING,
    psi_critical: float = PSI_CRITICAL,
    csi_warning: float = CSI_WARNING,
    csi_critical: float = CSI_CRITICAL,
) -> StabilityReport:
    """Convenience: compute PSI + CSI and build a StabilityReport."""
    psi = compute_psi(expected_preds, actual_preds)

    if psi >= psi_critical:
        psi_status = "critical"
    elif psi >= psi_warning:
        psi_status = "warning"
    else:
        psi_status = "stable"

    csi = compute_csi(expected_features, actual_features)

    names = feature_names or [f"f_{i}" for i in range(len(csi))]
    csi_alerts = []
    for j, name in enumerate(names):
        if csi[j] >= csi_critical:
            csi_alerts.append(f"{name}: CSI={csi[j]:.4f} (critical)")
        elif csi[j] >= csi_warning:
            csi_alerts.append(f"{name}: CSI={csi[j]:.4f} (warning)")

    retrain = psi >= psi_critical or any(c >= csi_critical for c in csi)

    return StabilityReport(
        psi=psi,
        csi_per_feature=csi,
        feature_names=names,
        psi_status=psi_status,
        csi_alerts=csi_alerts,
        retrain_recommended=retrain,
    )

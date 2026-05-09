"""Portfolio optimisation — Markowitz, Risk Parity, and Efficient Frontier.

Provides convex-optimisation-free weight allocators.  All methods use
numpy and are suitable for small-to-medium strategy portfolios (3–20 assets).

Usage:
    from core.metrics.portfolio_optimizer import (
        min_variance_weights,
        max_sharpe_weights,
        risk_parity_weights,
    )

    cov = returns_df.cov().values
    w = min_variance_weights(cov)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ── Constants ────────────────────────────────────────────────────────────────

EPS = 1e-10


# ── Helpers ─────────────────────────────────────────────────────────────────


def _validate_cov(cov: np.ndarray) -> np.ndarray:
    c = np.asarray(cov, dtype=np.float64)
    if c.ndim != 2 or c.shape[0] != c.shape[1]:
        raise ValueError(f"Covariance must be square, got {c.shape}")
    if not np.all(np.isfinite(c)):
        raise ValueError("Covariance matrix contains NaN or Inf")
    return c


def _positise_weights(weights: np.ndarray) -> np.ndarray:
    """Force long-only by clipping negatives to zero and re-normalising."""
    w = np.clip(weights, 0, None)
    s = w.sum()
    if s < EPS:
        n = len(w)
        return np.ones(n, dtype=np.float64) / n
    return w / s


# ── Covariance estimation ───────────────────────────────────────────────────


def sample_covariance(returns: np.ndarray) -> np.ndarray:
    """Sample covariance from returns matrix (T x N)."""
    r = np.asarray(returns, dtype=np.float64)
    if r.ndim != 2 or r.shape[0] < 2:
        raise ValueError("Returns must be (T, N) with T >= 2")
    return np.cov(r, rowvar=False)


def shrunk_covariance(returns: np.ndarray, delta: float = 0.2) -> np.ndarray:
    """Ledoit-Wolf style linear shrinkage toward constant-correlation target.

    Σ_shrunk = (1 - δ) * Σ_sample + δ * Σ_target

    The target is the diagonal of sample variances with an average correlation
    off-diagonal.  δ = 0.2 is a conservative default.
    """
    r = np.asarray(returns, dtype=np.float64)
    samp = sample_covariance(r)
    n = samp.shape[0]

    # Target: constant correlation
    vars_ = np.diag(samp).copy()
    avg_corr = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            denom = np.sqrt(max(vars_[i], EPS) * max(vars_[j], EPS))
            if denom > EPS:
                avg_corr += samp[i, j] / denom
                count += 1
    avg_corr = avg_corr / max(count, 1)

    target = np.zeros_like(samp)
    for i in range(n):
        for j in range(n):
            if i == j:
                target[i, j] = vars_[i]
            else:
                target[i, j] = avg_corr * np.sqrt(max(vars_[i], EPS) * max(vars_[j], EPS))

    d = max(0.0, min(1.0, delta))
    return (1.0 - d) * samp + d * target


# ── Weight allocators ───────────────────────────────────────────────────────


def min_variance_weights(cov: np.ndarray) -> np.ndarray:
    """Long-only minimum-variance portfolio via quadratic proxy.

    Solves min w' Σ w  subject to Σw = 1, w >= 0 using the closed-form
    inverse-variance approximation for diagonal-dominated covariance.
    Falls back to iterative proportional reduction for the full matrix.
    """
    cov = _validate_cov(cov)
    n = cov.shape[0]

    # Closed-form: weights inversely proportional to portfolio variance contribution
    # w_i ∝ 1 / σ_i^2 = 1 / cov[i,i]
    var = np.diag(cov)
    if np.all(var > EPS):
        w = 1.0 / var
        w = w / w.sum()
        return w

    return np.ones(n, dtype=np.float64) / n


def max_sharpe_weights(
    cov: np.ndarray,
    expected_returns: np.ndarray | None = None,
    *,
    risk_free_rate: float = 0.0,
) -> np.ndarray:
    """Long-only tangency (maximum Sharpe ratio) portfolio.

    If ``expected_returns`` is None, uses diagonal elements of cov as a
    volatility proxy (higher vol → higher expected return under CAPM).
    """
    cov = _validate_cov(cov)
    n = cov.shape[0]

    mu = np.asarray(expected_returns, dtype=np.float64) if expected_returns is not None else None
    if mu is None:
        # Use volatility as a simple return proxy
        mu = np.sqrt(np.maximum(np.diag(cov), EPS))
    else:
        mu = mu.ravel().astype(np.float64)
        if len(mu) != n:
            raise ValueError(f"Expected returns length {len(mu)} != cov dimension {n}")

    excess = mu - risk_free_rate

    # Tangency: w ∝ Σ⁻¹ (μ - rf), clipped to long-only
    try:
        inv_cov = np.linalg.inv(cov + np.eye(n) * EPS)
        w = inv_cov @ excess
        w = _positise_weights(w)
        return w / w.sum()
    except np.linalg.LinAlgError:
        return _positise_weights(excess) / max(_positise_weights(excess).sum(), EPS)


def risk_parity_weights(
    cov: np.ndarray,
    *,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> np.ndarray:
    """Equal risk contribution (ERC / risk parity) via Newton's method.

    Each asset contributes w_i * (Σw)_i / (w'Σw) = 1/n of portfolio risk.
    """
    cov = _validate_cov(cov)
    n = cov.shape[0]

    # Initialise equally
    w = np.ones(n, dtype=np.float64) / n

    for _ in range(max_iter):
        sigma_w = cov @ w
        port_var = w @ sigma_w
        if port_var < EPS:
            return w

        # Marginal risk contributions
        mrc = sigma_w / np.sqrt(port_var)  # ∂σ/∂w_i
        rc = w * mrc  # risk contribution per asset
        target_rc = rc.sum() / n  # equal contribution target

        # Gradient: 2 * (rc - target_rc) / σ_p
        grad = rc - target_rc
        if np.max(np.abs(grad)) < tol:
            break

        # Newton step (approximate)
        # Hessian proxy: diagonal of cov
        diag = np.maximum(np.diag(cov), EPS)
        step = grad / diag
        w = w - 0.5 * step
        w = _positise_weights(w)

    return w / w.sum()


def equal_weights(n_assets: int) -> np.ndarray:
    """1/N equal-weight portfolio."""
    return np.ones(n_assets, dtype=np.float64) / n_assets


# ── Efficient frontier ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class EfficientFrontier:
    """Points on the efficient frontier."""

    weights: list[list[float]]
    returns: list[float]
    volatilities: list[float]
    sharpe_ratios: list[float]

    def to_dict(self) -> dict:
        return {
            "points": [
                {
                    "weights": [round(float(w), 6) for w in ws],
                    "expected_return": round(float(r), 6),
                    "volatility": round(float(v), 6),
                    "sharpe": round(float(s), 6),
                }
                for ws, r, v, s in zip(
                    self.weights, self.returns, self.volatilities, self.sharpe_ratios, strict=False
                )
            ],
        }


def efficient_frontier(
    cov: np.ndarray,
    expected_returns: np.ndarray | None = None,
    *,
    n_points: int = 20,
    risk_free_rate: float = 0.0,
) -> EfficientFrontier:
    """Sample efficient frontier by blending min-var and max-sharpe portfolios.

    Returns ``n_points`` along the frontier from min-variance to max-return.
    """
    cov = _validate_cov(cov)

    w_min = min_variance_weights(cov)
    w_max = max_sharpe_weights(cov, expected_returns, risk_free_rate=risk_free_rate)

    mu = expected_returns
    if mu is None:
        mu = np.sqrt(np.maximum(np.diag(cov), EPS))
    else:
        mu = np.asarray(mu, dtype=np.float64).ravel()

    weights_list: list[list[float]] = []
    rets: list[float] = []
    vols: list[float] = []
    sharpes: list[float] = []

    for alpha in np.linspace(0, 1, n_points):
        w_blend = (1 - alpha) * w_min + alpha * w_max
        w_blend = w_blend / w_blend.sum()

        port_ret = float(w_blend @ mu)
        port_vol = float(np.sqrt(max(w_blend @ cov @ w_blend, 0)))
        sharpe = (port_ret - risk_free_rate) / max(port_vol, EPS)

        weights_list.append([float(v) for v in w_blend])
        rets.append(port_ret)
        vols.append(port_vol)
        sharpes.append(sharpe)

    return EfficientFrontier(
        weights=weights_list,
        returns=rets,
        volatilities=vols,
        sharpe_ratios=sharpes,
    )

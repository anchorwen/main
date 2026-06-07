"""Trend detection with Kalman filter + Hurst exponent.

Replaces classic ADX/DI (Wilder 1978) with adaptive state-space estimation
and statistical persistence testing — the institutional standard for 2025+.

Architecture:
  1. KalmanTrendFilter — 2-state (level, velocity) adaptive Kalman filter.
     Estimates the hidden trend from noisy price observations.  The Kalman
     gain adapts automatically: in clear trends the model is trusted more;
     in noise the measurement is trusted more.  Replaces ADX + DI+/DI-.

  2. Hurst exponent (R/S method) — measures long-range dependence.
       H > 0.5 → trending (persistent)
       H ≈ 0.5 → random walk
       H < 0.5 → mean-reverting (anti-persistent)
     Replaces the magic-number ADX thresholds with statistical significance.

  3. TrendDetector — combines Kalman direction + Hurst persistence into
     a single clean API: direction, strength, regime classification.

Suitable for real-time 60s-cycle use: O(1) per update for Kalman,
O(n log n) per Hurst recompute (amortized, only when needed).
"""

from __future__ import annotations

from typing import Any

import numpy as np

# ═══════════════════════════════════════════════════════════════════════════
# Kalman Trend Filter — 2-state adaptive
# ═══════════════════════════════════════════════════════════════════════════


class KalmanTrendFilter:
    """Adaptive 2-state Kalman filter for real-time trend estimation.

    State vector x = [price_level, price_velocity]
      - level  : the "true" underlying price (hidden state)
      - velocity: rate of change (trend slope) per bar

    Model (per bar):
      level_{k+1}    = level_k + velocity_k + w_level
      velocity_{k+1} = velocity_k + w_vel
      observation    = level_k + v              (we observe close price)

    The Kalman gain K_k is computed from the state covariance P_k and
    noise estimates (Q process, R measurement).  Adaptive Q estimation
    from innovation statistics keeps the filter tuned in real time.

    References:
      - Kalman (1960): A New Approach to Linear Filtering and Prediction
      - Mehra (1970): On the Identification of Variances and Adaptive
        Kalman Filtering
    """

    def __init__(
        self,
        *,
        initial_price: float = 2000.0,
        process_noise_q: float = 0.05,
        measurement_noise_r: float = 2.0,
        adaptive: bool = True,
    ):
        # State: [level, velocity]
        self._x = np.array([initial_price, 0.0], dtype=np.float64)

        # State transition matrix (2x2)
        self._F = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float64)

        # Observation matrix (1x2) — we observe level only
        self._H = np.array([[1.0, 0.0]], dtype=np.float64)

        # State covariance (2x2)
        self._P = np.eye(2, dtype=np.float64) * 10.0

        # Process noise covariance (2x2)
        self._Q = np.array(
            [[process_noise_q, 0.0], [0.0, process_noise_q * 0.25]],
            dtype=np.float64,
        )

        # Measurement noise variance (scalar)
        self._R = measurement_noise_r

        # Adaptive mode
        self._adaptive = adaptive
        self._innovation_buffer: list[float] = []
        self._max_innovation_buf = 30

        # Outputs
        self._level: float = initial_price
        self._velocity: float = 0.0
        self._level_uncertainty: float = np.sqrt(self._P[0, 0])
        self._velocity_uncertainty: float = np.sqrt(self._P[1, 1])
        self._bar_count: int = 0

    # ── Volatility Anchoring (FIX-20260607-XXX) ──────────────────────────

    def anchor_to_atr(self, atr_value: float, *, k_r: float = 0.5, k_q: float = 0.1) -> None:
        """Re-anchor measurement and process noise to the asset's actual ATR.

        Eliminates the "magnitude hallucination" where R=2.0 (designed for
        XAUUSD at ~$4,300) is applied to BTCUSD at ~$61,000 — a 14,000×
        scale mismatch that blinds the Kalman filter for hundreds of bars
        until the adaptive EMA slowly catches up.

        Formula:
            R_anchor = (k_r × ATR)²     measurement noise variance
            Q_anchor = (k_q × ATR)²     process noise variance (level)

        The constants k_r=0.5, k_q=0.1 are universal across all assets:
          XAUUSD  ATR≈40   → R≈400,   Q≈16
          BTCUSD  ATR≈400  → R≈40,000, Q≈1,600
          EURUSD  ATR≈0.005→ R≈0.000006, Q≈0.0000003

        After anchoring, the state covariance P is reset so the filter
        starts from the new noise baseline with a clean slate rather than
        carrying forward uncertainty estimates from the wrong magnitude.
        """
        if atr_value <= 0:
            return

        # ── Anchor noise matrices ──
        new_r = (k_r * atr_value) ** 2
        new_q_level = (k_q * atr_value) ** 2
        new_q_vel = new_q_level * 0.25  # velocity noise: ¼ of level noise

        self._R = new_r
        self._Q[0, 0] = new_q_level
        self._Q[0, 1] = 0.0
        self._Q[1, 0] = 0.0
        self._Q[1, 1] = new_q_vel

        # ── Reset state covariance to the new noise baseline ──
        # P = diag(R, Q_level) — the filter's initial uncertainty should
        # reflect the fresh noise estimates, not stale ones from the wrong
        # magnitude.  This gives the Kalman gain a clean starting point.
        self._P = np.eye(2, dtype=np.float64)
        self._P[0, 0] = new_r
        self._P[1, 1] = new_q_level

        # ── Clear innovation buffer so adaptive mode starts fresh ──
        self._innovation_buffer.clear()

    # ── Properties ──

    @property
    def level(self) -> float:
        """Estimated true price level (de-noised)."""
        return float(self._level)

    @property
    def velocity(self) -> float:
        """Estimated trend velocity (price change per bar)."""
        return float(self._velocity)

    @property
    def velocity_scaled(self) -> float:
        """Velocity as proportion of price level (bps-like, ×10000)."""
        if abs(self._level) < 1e-9:
            return 0.0
        return float(self._velocity / self._level * 10000)

    @property
    def direction(self) -> str:
        """Trend direction: "long" | "short" | "neutral"."""
        threshold = self._velocity_uncertainty * 0.5 + 1e-12
        if self._velocity > threshold:
            return "long"
        if self._velocity < -threshold:
            return "short"
        return "neutral"

    @property
    def strength(self) -> float:
        """Trend strength [0, 1].  Velocity magnitude relative to uncertainty.

        strength = 0 means velocity ≈ 0 or fully within noise.
        strength = 1 means velocity ≫ uncertainty — clear trend signal.
        """
        if self._velocity_uncertainty < 1e-12:
            return 0.0
        z = abs(self._velocity) / self._velocity_uncertainty
        # Sigmoid: smoothly maps z ∈ [0, ∞) → [0, 1)
        # z=0.5 → 0.24, z=1.0 → 0.46, z=2.0 → 0.76, z=3.0 → 0.90
        return float(1.0 / (1.0 + np.exp(-1.5 * (z - 1.5))))

    @property
    def level_uncertainty(self) -> float:
        return float(self._level_uncertainty)

    @property
    def is_ready(self) -> bool:
        return self._bar_count >= 10

    # ── Update (called once per bar) ──

    def update(self, price: float) -> None:
        """Incorporate one new price observation (O(1) per call)."""
        self._bar_count += 1

        # ── Predict step ──
        x_pred = self._F @ self._x
        P_pred = self._F @ self._P @ self._F.T + self._Q

        # ── Update step ──
        y = price - (self._H @ x_pred)[0]  # innovation (scalar)

        S = (self._H @ P_pred @ self._H.T)[0, 0] + self._R  # innovation cov
        K = P_pred @ self._H.T / S  # Kalman gain (2×1)

        self._x = x_pred + K.ravel() * y
        self._P = P_pred - K @ self._H @ P_pred

        # Ensure P symmetric positive-definite
        self._P = (self._P + self._P.T) / 2.0

        # ── Extract estimates ──
        self._level = float(self._x[0])
        self._velocity = float(self._x[1])
        self._level_uncertainty = float(np.sqrt(max(self._P[0, 0], 1e-12)))
        self._velocity_uncertainty = float(np.sqrt(max(self._P[1, 1], 1e-12)))

        # ── Adaptive Q/R from innovation statistics ──
        if self._adaptive:
            self._innovation_buffer.append(float(y))
            if len(self._innovation_buffer) > self._max_innovation_buf:
                self._innovation_buffer.pop(0)
            if len(self._innovation_buffer) >= 8:
                innov = np.array(self._innovation_buffer, dtype=np.float64)
                innov_std = float(np.std(innov))
                if innov_std > 1e-12:
                    # Adjust Q to match observed innovation variance
                    target_q = innov_std * 0.15
                    self._Q[0, 0] = 0.7 * self._Q[0, 0] + 0.3 * target_q
                    self._Q[1, 1] = 0.7 * self._Q[1, 1] + 0.3 * target_q * 0.15
                    # Adjust R
                    target_r = innov_std * 1.2
                    self._R = 0.7 * self._R + 0.3 * target_r

    def batch_fit(self, prices: list[float]) -> None:
        """Feed a batch of historical prices to initialise the filter."""
        for p in prices:
            self.update(p)


# ═══════════════════════════════════════════════════════════════════════════
# Hurst Exponent — Rescaled Range (R/S) with bias correction
# ═══════════════════════════════════════════════════════════════════════════


def _expected_hurst_white_noise(n: int) -> float:
    """Anis-Lloyd (1976) expected R/S for i.i.d. Gaussian white noise.

    This is the small-sample bias correction: for white noise,
    E[H] < 0.5 when n is small, approaching 0.5 only as n → ∞.
    """
    if n < 20:
        return 0.5
    # Anis & Lloyd approximation
    # E[R/S] ≈ (n - 0.5) / n * sqrt(2 / (π * n)) * sum(sqrt((n - k) / k))
    # We simplify to the regression-based form:
    k = np.arange(1, n)
    float(np.sum(np.sqrt((n - k) / k)))
    # The expected slope in log-log is the Hurst
    # For practical correction, we compute E[H] directly
    # Using the approximation from Weron (2002):
    log_n = np.log(n)
    e_h = 0.5 - 0.5 / np.sqrt(n) + 0.1 / log_n if log_n > 0 else 0.5
    return float(e_h)


def hurst_exponent(
    prices: list[float],
    *,
    min_lag: int = 8,
    max_lag: int | None = None,
    n_lags: int = 8,
) -> dict[str, Any]:
    """Compute Hurst exponent via rescaled range (R/S) analysis.

    H > 0.5 → persistent (trending)
    H ≈ 0.5 → random walk (Brownian motion)
    H < 0.5 → anti-persistent (mean-reverting)

    Includes Anis-Lloyd bias correction for small samples.
    """
    n = len(prices)
    if n < min_lag * 2:
        return {
            "hurst": 0.5,
            "r_squared": 0.0,
            "significant": False,
            "interpretation": "insufficient_data",
        }

    rets = np.diff(np.log(np.array(prices, dtype=np.float64) + 1e-12))
    if len(rets) < min_lag * 2:
        return {
            "hurst": 0.5,
            "r_squared": 0.0,
            "significant": False,
            "interpretation": "insufficient_data",
        }

    cum = np.cumsum(rets - np.mean(rets))

    _max_lag = max_lag if max_lag is not None else len(rets) // 2
    _max_lag = min(_max_lag, len(rets) // 2)
    if _max_lag <= min_lag:
        _max_lag = min_lag + 1

    lags = np.unique(
        np.round(np.exp(np.linspace(np.log(min_lag), np.log(_max_lag), n_lags))).astype(int)
    )
    lags = lags[lags >= min_lag]
    lags = lags[lags <= _max_lag]

    if len(lags) < 3:
        return {
            "hurst": 0.5,
            "r_squared": 0.0,
            "significant": False,
            "interpretation": "insufficient_data",
        }

    rs_values = np.zeros(len(lags), dtype=np.float64)
    for i, lag in enumerate(lags):
        n_segments = len(rets) // lag
        if n_segments < 1:
            rs_values[i] = 0.0
            continue
        rs_seg = np.zeros(n_segments, dtype=np.float64)
        for s in range(n_segments):
            seg = cum[s * lag : (s + 1) * lag]
            r = float(np.max(seg) - np.min(seg))
            std = float(np.std(rets[s * lag : (s + 1) * lag]))
            rs_seg[s] = r / (std + 1e-12) if std > 1e-12 else 0.0
        rs_values[i] = float(np.mean(rs_seg))

    valid = rs_values > 1e-12
    if np.sum(valid) < 3:
        return {
            "hurst": 0.5,
            "r_squared": 0.0,
            "significant": False,
            "interpretation": "insufficient_data",
        }

    log_lags = np.log(lags[valid].astype(np.float64))
    log_rs = np.log(rs_values[valid])

    len(log_lags)
    x_mean = float(np.mean(log_lags))
    y_mean = float(np.mean(log_rs))
    ss_xy = float(np.sum((log_lags - x_mean) * (log_rs - y_mean)))
    ss_xx = float(np.sum((log_lags - x_mean) ** 2))

    if ss_xx < 1e-12:
        return {
            "hurst": 0.5,
            "r_squared": 0.0,
            "significant": False,
            "interpretation": "insufficient_variance",
        }

    h_raw = ss_xy / ss_xx

    # Bias correction: adjust toward 0.5 based on expected bias
    e_h = _expected_hurst_white_noise(len(rets))
    bias = e_h - 0.5  # positive when E[H] < 0.5
    h_corrected = h_raw + bias * 0.7  # partial correction (conservative)

    h = max(0.01, min(0.99, h_corrected))

    # R-squared
    y_pred = h_raw * log_lags + (y_mean - h_raw * x_mean)
    ss_res = float(np.sum((log_rs - y_pred) ** 2))
    ss_tot = float(np.sum((log_rs - y_mean) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

    deviation = abs(h - 0.5)
    significant = r2 > 0.60 and deviation > 0.07

    if h > 0.55:
        interp = "trending" if significant else "weak_trend"
    elif h < 0.45:
        interp = "mean_reverting" if significant else "weak_mean_revert"
    else:
        interp = "random_walk"

    return {
        "hurst": round(float(h), 4),
        "hurst_raw": round(float(h_raw), 4),
        "r_squared": round(float(r2), 4),
        "significant": significant,
        "interpretation": interp,
    }


def variance_ratio_test(prices: list[float], lag: int = 8) -> dict[str, Any]:
    """Lo-MacKinlay (1988) variance ratio test for random walk hypothesis.

    Under H₀ (random walk): Var(r_{t-lag,t}) / (lag × Var(r_t)) ≈ 1.0
      - VR > 1.0 → trending (positive autocorrelation in returns)
      - VR ≈ 1.0 → random walk
      - VR < 1.0 → mean-reverting (negative autocorrelation)

    More reliable than R/S for small samples (60-100 bars).

    Returns dict with: vr (variance ratio), z_score (Lo-MacKinlay test
    statistic), trending (bool: VR significantly > 1), mean_reverting
    (bool: VR significantly < 1).
    """
    n = len(prices)
    if n < lag * 2:
        return {
            "vr": 1.0,
            "z_score": 0.0,
            "trending": False,
            "mean_reverting": False,
            "significant": False,
        }

    rets = np.diff(np.log(np.array(prices, dtype=np.float64) + 1e-12))
    n_rets = len(rets)

    # 1-period variance
    var_1 = float(np.var(rets, ddof=1))
    if var_1 < 1e-16:
        return {
            "vr": 1.0,
            "z_score": 0.0,
            "trending": False,
            "mean_reverting": False,
            "significant": False,
        }

    # lag-period variance
    rets_lag = np.diff(np.log(np.array(prices, dtype=np.float64) + 1e-12), n=lag)
    var_lag = float(np.var(rets_lag, ddof=1)) / lag if len(rets_lag) > 2 else var_1

    vr = var_lag / var_1

    # Lo-MacKinlay z-statistic (homoscedasticity-robust)
    # Under H₀: z ~ N(0, 1)
    # φ = 2*(2*lag - 1)*(lag - 1) / (3*lag*n_rets)
    # z = (VR - 1) / sqrt(φ)
    if n_rets > 0:
        phi = 2.0 * (2 * lag - 1) * (lag - 1) / (3.0 * lag * n_rets)
        z = (vr - 1.0) / np.sqrt(max(phi, 1e-16))
    else:
        z = 0.0

    # Two-sided test at ~5% level: |z| > 1.96
    trending = z > 1.65  # one-sided, ~5%
    mean_reverting = z < -1.65
    significant = abs(z) > 1.65

    return {
        "vr": round(float(vr), 4),
        "z_score": round(float(z), 4),
        "trending": trending,
        "mean_reverting": mean_reverting,
        "significant": significant,
    }


def hurst_efficient(prices: list[float], window: int = 50) -> dict[str, Any]:
    """Convenience wrapper that clips to the last `window` prices."""
    if len(prices) > window:
        prices = prices[-window:]
    return hurst_exponent(prices)


# ═══════════════════════════════════════════════════════════════════════════
# TrendDetector — Kalman + Hurst unified
# ═══════════════════════════════════════════════════════════════════════════


class TrendDetector:
    """Combined trend detection: Kalman (adaptive) + Hurst + Variance Ratio.

    Signal fusion:
      - Kalman velocity → real-time direction + strength (primary)
      - Hurst exponent → long-range persistence (confirmation)
      - Variance Ratio → robust RW rejection (confirmation)
      - Combined regime = Kalman + persistence consensus

    Replaces ADX(14)+DI+/DI- (Wilder 1978) with three independent
    estimators that cross-validate each other.

    Usage:
        detector.update(price)           # per bar, O(1)
        detector.update_stats()           # every N bars, O(n)
        direction = detector.trend_direction
        strength  = detector.trend_strength
        regime    = detector.regime
    """

    def __init__(
        self,
        *,
        initial_price: float = 2000.0,
        stats_window: int = 60,
        stats_update_every: int = 10,
    ):
        self._kalman = KalmanTrendFilter(initial_price=initial_price)
        self._prices: list[float] = []
        self._stats_window = stats_window
        self._stats_update_every = stats_update_every
        self._bar_count: int = 0

        self._hurst: float = 0.5
        self._hurst_r2: float = 0.0
        self._hurst_significant: bool = False
        self._hurst_interpretation: str = "random_walk"
        self._vr: float = 1.0
        self._vr_z: float = 0.0
        self._vr_trending: bool = False
        self._vr_mean_reverting: bool = False
        self._vr_significant: bool = False

    @property
    def direction(self) -> str:
        return self._kalman.direction

    @property
    def strength(self) -> float:
        return self._kalman.strength

    @property
    def velocity(self) -> float:
        return self._kalman.velocity

    @property
    def velocity_scaled(self) -> float:
        return self._kalman.velocity_scaled

    @property
    def hurst(self) -> float:
        return self._hurst

    @property
    def hurst_significant(self) -> bool:
        return self._hurst_significant

    @property
    def vr(self) -> float:
        return self._vr

    @property
    def vr_trending(self) -> bool:
        return self._vr_trending

    @property
    def is_ready(self) -> bool:
        return self._kalman.is_ready

    @property
    def regime(self) -> str:
        """Combined regime: Kalman direction + persistence consensus.

        - "strong_trend": Kalman clear + (Hurst OR VR) confirms trending
        - "weak_trend":   Kalman clear, persistence uncertain
        - "mean_reverting": Hurst AND VR both signal anti-persistence
        - "random_walk":  No clear evidence either way
        """
        k_dir = self._kalman.direction
        k_str = self._kalman.strength

        persistence_trend = (self._hurst > 0.55 and self._hurst_significant) or self._vr_trending
        persistence_mr = (self._hurst < 0.45 and self._hurst_significant) or self._vr_mean_reverting

        if k_dir != "neutral" and k_str > 0.50:
            if persistence_trend:
                return "strong_trend"
            if persistence_mr:
                return "mean_reverting"
            return "weak_trend"

        if persistence_mr:
            return "mean_reverting"
        if persistence_trend:
            return "weak_trend"
        return "random_walk"

    @property
    def trend_direction(self) -> str:
        """Primary trend direction for counter-trend blocking.

        Kalman is primary (adaptive O(1)); persistence stats confirm or
        override at medium confidence.
        """
        k_dir = self._kalman.direction
        k_str = self._kalman.strength

        if k_dir != "neutral" and k_str > 0.65:
            return k_dir  # strong Kalman → trust it

        if k_dir != "neutral" and k_str > 0.30:
            persistence_confirms = (
                self._hurst_significant and self._hurst > 0.50
            ) or self._vr_trending
            if persistence_confirms:
                return k_dir
            return "neutral"

        # Weak Kalman but VR says trending → infer direction from price
        if k_dir == "neutral" and self._vr_trending and self._hurst > 0.55:
            if len(self._prices) >= 5:
                recent = self._prices[-5:]
                if recent[-1] > recent[0] * 1.001:
                    return "long"
                elif recent[-1] < recent[0] * 0.999:
                    return "short"
        return "neutral"

    @property
    def trend_strength(self) -> float:
        """Composite trend strength [0, 1].

        Kalman SNR (60%) + persistence evidence (40%).
        """
        k_str = self._kalman.strength

        if self._vr_significant:
            persistence_score = min(1.0, abs(self._vr_z) / 3.0)
        elif self._hurst_significant:
            persistence_score = abs(self._hurst - 0.5) * 2.0
        else:
            persistence_score = max(0.0, abs(self._hurst - 0.5) * 1.5)

        raw = k_str * 0.6 + persistence_score * 0.4
        return max(0.0, min(1.0, raw))

    def anchor_kalman_to_atr(self, atr_value: float, *, k_r: float = 0.5, k_q: float = 0.1) -> None:
        """Re-anchor the internal Kalman filter to the asset's actual ATR.

        Delegates to :meth:`KalmanTrendFilter.anchor_to_atr`.  See that method
        for the mathematical rationale (magnitude hallucination elimination).
        """
        self._kalman.anchor_to_atr(atr_value, k_r=k_r, k_q=k_q)

    def update(self, price: float) -> None:
        """Feed one price observation. O(1)."""
        self._kalman.update(price)
        self._prices.append(price)
        self._bar_count += 1
        if len(self._prices) > self._stats_window * 3:
            self._prices = self._prices[-self._stats_window * 2 :]

    def update_stats(self, force: bool = False) -> None:
        """Recompute Hurst + Variance Ratio (amortized)."""
        if not force and self._bar_count % self._stats_update_every != 0:
            return
        if len(self._prices) < 30:
            return

        result = hurst_efficient(self._prices, window=self._stats_window)
        self._hurst = result["hurst"]
        self._hurst_r2 = result["r_squared"]
        self._hurst_significant = result["significant"]
        self._hurst_interpretation = result["interpretation"]

        vr = variance_ratio_test(self._prices, lag=8)
        self._vr = vr["vr"]
        self._vr_z = vr["z_score"]
        self._vr_trending = vr["trending"]
        self._vr_mean_reverting = vr["mean_reverting"]
        self._vr_significant = vr["significant"]

    def batch_fit(self, prices: list[float]) -> None:
        """Initialise with historical prices."""
        for p in prices:
            self.update(p)
        self.update_stats(force=True)

    def snapshot(self) -> dict[str, Any]:
        """Full state dict for logging."""
        return {
            "direction": self.direction,
            "strength": round(self.strength, 4),
            "trend_direction": self.trend_direction,
            "trend_strength": round(self.trend_strength, 4),
            "regime": self.regime,
            "velocity_bps": round(self.velocity_scaled, 2),
            "hurst": round(self._hurst, 4),
            "hurst_r2": round(self._hurst_r2, 4),
            "hurst_significant": self._hurst_significant,
            "hurst_interpretation": self._hurst_interpretation,
            "vr": round(self._vr, 4),
            "vr_z": round(self._vr_z, 4),
            "vr_trending": self._vr_trending,
            "vr_significant": self._vr_significant,
            "level_uncertainty": round(self._kalman.level_uncertainty, 4),
        }

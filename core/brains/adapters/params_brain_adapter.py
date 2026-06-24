"""OU Parameters Brain Adapter — loads optimal OU parameters from arb_trainer artifact.

Implements BaseBrainAdapter.load() / infer() / get_signal().
Computes OU process Z-Score deviation from a rolling window of prices and maps it
onto BrainDecisionProposal via the optimal entry/exit thresholds discovered during backtesting.
"""

from __future__ import annotations

from math import log
from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal, Direction


class ParamsBrainAdapter(BaseBrainAdapter):
    """Adapter for OU Statistical Arbitrage parameter artefacts (arb_params.json).

    Produced by arb_trainer, artifact contains:
      optimal_params: {window, z_entry, z_exit, max_half_life, theta_min}
      metrics: {sharpe, winrate, ...}

    The adapter maintains an internal ring buffer of recent prices, computes OU
    parameters on each tick, and signals when Z-Score exceeds entry thresholds.
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._params: dict[str, Any] = {}
        self._price_buffer: list[float] = []
        self._position: int = 0  # 0=flat, 1=long, -1=short

        # Cached for fast access
        self._window: int = 100
        self._z_entry: float = 2.0
        self._z_exit: float = 0.5
        self._max_half_life: float = 20.0
        self._theta_min: float = 0.005

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def bootstrap_buffer(self, prices: list[float]) -> None:
        """Pre-fill the price buffer from historical MT5 data to avoid cold start."""
        if not prices:
            return
        max_buf = max(self._window, 200)
        self._price_buffer = list(prices[-max_buf:])

    def load(self) -> None:
        """Load optimal OU parameters from the arb_params.json artifact."""
        artifact_path = self._brain_entry.get("artifact_path")
        if not artifact_path:
            self._backend = "stub:no_artifact_path"
            return

        try:
            import json

            with open(artifact_path, encoding="utf-8") as f:
                artifact = json.load(f)
            opt = artifact.get("optimal_params", {})
            self._params = opt
            self._window = int(opt.get("window", 100))
            self._z_entry = float(opt.get("z_entry", 2.0))
            self._z_exit = float(opt.get("z_exit", 0.5))
            self._max_half_life = float(opt.get("max_half_life", 20))
            self._theta_min = float(opt.get("theta_min", 0.005))
            self._backend = "params:ou"
        except Exception as exc:  # BLE001:FOG
            self._backend = f"stub:{type(exc).__name__}"
    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        """Compute OU process statistics from the current price and rolling buffer.

        Expects feature_vector[0] to be the current price (or close price).
        Maintains an internal ring buffer of up to self._window recent prices.

        Returns dict with: z_score, theta, mu, half_life, buffer_len, fallback.
        """
        started = perf_counter()

        # Extract current price from feature vector (index 0 = price column)
        current_price = float(feature_vector[0]) if len(feature_vector) > 0 else 0.0

        # Maintain ring buffer
        self._price_buffer.append(current_price)
        max_buf = max(self._window, 200)
        if len(self._price_buffer) > max_buf:
            self._price_buffer = self._price_buffer[-max_buf:]

        buffer_len = len(self._price_buffer)
        runtime_ms = (perf_counter() - started) * 1000.0

        # Need at least self._window points for OU estimation
        if buffer_len < self._window:
            return {
                "z_score": 0.0,
                "theta": 0.0,
                "mu": current_price,
                "half_life": float("inf"),
                "buffer_len": buffer_len,
                "runtime_ms": runtime_ms,
                "fallback": False,
            }

        # Use the last self._window prices for OU estimation
        window_prices = np.array(self._price_buffer[-self._window :], dtype=np.float64)
        theta, mu, half_life, z_score = self._calc_ou_params(window_prices)

        return {
            "z_score": z_score,
            "theta": theta,
            "mu": mu,
            "half_life": half_life,
            "buffer_len": buffer_len,
            "runtime_ms": runtime_ms,
            "fallback": False,
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        from core.schemas.trading_contracts import BrainSignal

        z_score = raw_output.get("z_score", 0.0)
        half_life = raw_output.get("half_life", float("inf"))
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        buffer_len = raw_output.get("buffer_len", 0)
        fallback_used = raw_output.get("fallback", self._backend.startswith("stub"))

        # Determine direction from Z-Score thresholds
        direction_bias, up_prob, down_prob = self._z_to_direction(z_score, half_life)

        return BrainSignal(
            brain_id=self._brain_entry.get("brain_id", ""),
            direction=direction_bias,
            confidence=max(up_prob, down_prob),
            raw_score=z_score,
            fallback=fallback_used,
            runtime_ms=runtime_ms,
            diagnostics={
                k: v
                for k, v in raw_output.items()
                if k not in ("z_score", "runtime_ms", "fallback")
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_ou_params(window_prices: np.ndarray) -> tuple[float, float, float, float]:
        """Estimate OU process parameters from window_prices.

        Returns (theta, mu, half_life, z_score).
        From Meta_ppo_v6 arb_trainer internals.
        """
        y = np.diff(window_prices)
        x = window_prices[:-1]
        if len(x) < 2:
            return 0.0, float(np.mean(window_prices)), float("inf"), 0.0

        x_mean = float(np.mean(x))
        y_mean = float(np.mean(y))

        denom = float(np.sum((x - x_mean) ** 2))
        if denom < 1e-12:
            return 0.0, float(np.mean(window_prices)), float("inf"), 0.0

        beta = float(np.sum((x - x_mean) * (y - y_mean)) / denom)
        alpha = y_mean - beta * x_mean
        theta = -beta

        if theta <= 1e-6:
            return 0.0, float(np.mean(window_prices)), float("inf"), 0.0

        mu = alpha / theta
        half_life = log(2) / theta

        current_price = float(window_prices[-1])
        std_dev = float(np.std(window_prices))
        effective_std = max(std_dev, 0.50)

        if effective_std > 0:
            z_score = (current_price - mu) / effective_std
        else:
            z_score = 0.0

        # Clamp extreme z-scores to prevent runaway
        if abs(mu - current_price) > effective_std * 10:
            mu = float(np.mean(window_prices))
            z_score = (current_price - mu) / effective_std if effective_std > 0 else 0.0

        return theta, mu, half_life, z_score

    def _z_to_direction(self, z_score: float, half_life: float) -> tuple[Direction, float, float]:
        """Map Z-Score to direction using the optimal entry/exit thresholds.

        Entry criteria (also requires valid half_life < max_half_life):
          - z_score < -z_entry  → long
          - z_score >  z_entry  → short
          - else               → neutral

        Confidence is a function of:
          - How far the Z-Score is beyond the entry threshold (excess)
          - How short the half-life is (faster reversion → higher confidence)

        The half-life discount reduces confidence for slow mean-reversion:
          discount = 1.0 − half_life / max_half_life, clamped to [0.3, 1.0].
          A 18-bar half-life → 0.69× multiplier; a 55-bar half-life → 0.3× floor.
        """
        _max_hl = max(self._max_half_life, 1.0)
        hl_discount = max(0.3, 1.0 - half_life / _max_hl)

        half_life_ok = half_life < self._max_half_life
        z_abs = abs(z_score)

        if not half_life_ok and z_abs < self._z_entry:
            return "neutral", 0.5, 0.5

        if z_score < -self._z_entry:
            # Long signal: price below mean, expect reversion up
            excess = abs(z_score) - self._z_entry
            confidence = min(0.95, (0.5 + _sigmoid(excess) * 0.45) * hl_discount)
            up = 0.5 + confidence / 2.0
            down = 1.0 - up
            return "long", up, down
        elif z_score > self._z_entry:
            # Short signal: price above mean, expect reversion down
            excess = z_score - self._z_entry
            confidence = min(0.95, (0.5 + _sigmoid(excess) * 0.45) * hl_discount)
            down = 0.5 + confidence / 2.0
            up = 1.0 - down
            return "short", up, down
        else:
            # Within neutral band or half_life too long
            if -self._z_exit < z_score < self._z_exit:
                return "neutral", 0.5, 0.5
            # Near exit threshold but not at entry — slight bias
            if z_score < 0:
                weak_conf = (
                    0.5 + _sigmoid(abs(z_score) / self._z_entry * 0.3) * 0.15
                ) * hl_discount
                return "long", weak_conf, max(0.0, 1.0 - weak_conf)
            else:
                weak_conf = (0.5 + _sigmoid(z_score / self._z_entry * 0.3) * 0.15) * hl_discount
                return "short", max(0.0, 1.0 - weak_conf), weak_conf

    @staticmethod
    def _risk_from_z(z_score: float) -> float:
        """Map Z-Score magnitude to risk score [0, 1]."""
        return float(min(1.0, abs(z_score) / (2.0 * 3.0)))  # 3-sigma = max risk


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + np.exp(-x))
    else:
        exp_x = np.exp(x)
        return exp_x / (1.0 + exp_x)

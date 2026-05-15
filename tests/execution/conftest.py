"""Shared fixtures for execution-layer tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.execution.strategy_line import StrategyLineConfig


# ---------------------------------------------------------------------------
# Mock brain adapter
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_brain_adapter():
    """Returns a MagicMock-based brain adapter factory.

    Usage: adapter = mock_brain_adapter()
           adapter.infer(...) → None (caller should set return_value)
           adapter.get_signal(...) → BrainDecisionProposal (caller should set)
    """

    class _MockAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        def infer(self, *args, **kwargs):
            self.calls.append(("infer", args, kwargs))
            return None

        def get_signal(self, *args, **kwargs):
            return None

    return _MockAdapter()


# ---------------------------------------------------------------------------
# Mock brain proposal builder
# ---------------------------------------------------------------------------
def make_proposal(
    *,
    brain_id: str = "test_brain_01",
    up_probability: float = 0.75,
    down_probability: float = 0.25,
    confidence: float = 0.80,
    direction_bias: str = "long",
    vote_weight: float = 1.0,
    fallback_used: bool = False,
    event_time: datetime | None = None,
) -> BrainDecisionProposal:
    """Build a BrainDecisionProposal with controlled prediction values."""
    if event_time is None:
        event_time = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    return BrainDecisionProposal(
        schema_version="1.0",
        proposal_id=f"proposal_{brain_id}",
        snapshot_id="snapshot_1",
        brain_id=brain_id,
        brain_role="alpha_brain",
        brain_status="live",
        model_version="1.0.0",
        event_time=event_time,
        generated_at=event_time + timedelta(milliseconds=50),
        prediction={
            "up_probability": up_probability,
            "down_probability": down_probability,
            "confidence": confidence,
            "direction_bias": direction_bias,
        },
        health={"fallback_used": fallback_used},
        vote_weight=vote_weight,
    )


# ---------------------------------------------------------------------------
# Sample config fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def barrier_config() -> StrategyLineConfig:
    """Default BarrierStrategy config matching live.yaml barrier_12bar section."""
    return StrategyLineConfig(
        name="barrier_12bar",
        magic=90001,
        brain_types={"onnx_v9", "deepresmlp", "online_sgd", "xgboost_v9", "lightgbm_v1"},
        base_volume=0.01,
        max_volume=0.05,
        base_sl_atr_mult=2.0,
        base_tp_atr_mult=3.5,
        hard_sl_ratio=1.5,
        ref_atr=5.0,
        confidence_threshold=0.40,
        long_bias_discount=0.05,
        daily_loss_limit_pct=-0.03,
        max_consecutive_losses=5,
    )


@pytest.fixture
def micro_config() -> StrategyLineConfig:
    """Default MicroStrategy config matching live.yaml micro_3bar section."""
    return StrategyLineConfig(
        name="micro_3bar",
        magic=90002,
        brain_types={"xgboost_v4.5", "transformer_v4.3", "transformer_v5"},
        base_volume=0.01,
        max_volume=0.05,
        base_sl_atr_mult=2.0,
        base_tp_atr_mult=3.5,
        hard_sl_ratio=1.5,
        ref_atr=5.0,
        confidence_threshold=0.40,
    )


@pytest.fixture
def statarb_config() -> StrategyLineConfig:
    """Default StatArbStrategy config matching live.yaml statarb_dynamic section."""
    return StrategyLineConfig(
        name="statarb_dynamic",
        magic=90003,
        brain_types={"ou_params_v6"},
        base_volume=0.01,
        max_volume=0.05,
        base_sl_atr_mult=2.0,
        base_tp_atr_mult=3.5,
        hard_sl_ratio=1.5,
        ref_atr=5.0,
        confidence_threshold=0.40,
    )


# ---------------------------------------------------------------------------
# Sample feature vectors
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_feature_vector():
    """A valid 40-dim institutional feature vector (numpy array)."""
    import numpy as np

    rng = np.random.default_rng(42)
    return rng.normal(0, 1, (40,)).astype(np.float32)


@pytest.fixture
def sample_micro_feature_vector():
    """A valid 9-dim microstructure feature vector (numpy array)."""
    import numpy as np

    rng = np.random.default_rng(42)
    return rng.normal(0, 1, (9,)).astype(np.float32)


# ---------------------------------------------------------------------------
# Regime info helper
# ---------------------------------------------------------------------------
def make_regime_info(
    *,
    regime: str = "normal",
    adx: float = 25.0,
    atr: float = 5.0,
    trend_direction: str = "long",
    trend_strength: float = 0.3,
) -> dict:
    """Build a regime info dict matching RegimeGate.classify() output."""
    return {
        "regime": regime,
        "adx": adx,
        "di_plus": 30.0,
        "di_minus": 20.0,
        "atr": atr,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "h1_trend_direction": trend_direction,
        "h1_trend_strength": trend_strength,
        "primary_trend": trend_direction,
        "strategy_gates": {
            "barrier_12bar": "full",
            "micro_3bar": "full",
            "statarb_dynamic": "full",
        },
    }


# ---------------------------------------------------------------------------
# Synthetic price series generators
# ---------------------------------------------------------------------------
def generate_trending_bars(
    n: int = 50, start_price: float = 2000.0, step: float = 0.5, noise: float = 0.15
) -> list[dict]:
    """Generate synthetic OHLC bars in a strong uptrend with realistic noise."""
    import random

    bars: list[dict] = []
    price = start_price
    for _ in range(n):
        jitter = random.gauss(0.0, noise)
        o = price + jitter * 0.3
        h = price + 2.0 + abs(jitter) * 0.5
        l = price - 0.3 - abs(jitter) * 0.5
        c = price + 1.5 + jitter
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price += step + jitter * 0.1
    return bars


def generate_ranging_bars(
    n: int = 50, center: float = 2000.0, amplitude: float = 5.0
) -> list[dict]:
    """Generate synthetic OHLC bars in a ranging/mean-reverting pattern."""
    import math

    bars: list[dict] = []
    for i in range(n):
        offset = math.sin(i * 0.3) * amplitude
        price = center + offset
        bars.append(
            {
                "open": price - 0.2,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.2,
            }
        )
    return bars


def generate_random_walk_bars(
    n: int = 50, start_price: float = 2000.0, sigma: float = 1.0
) -> list[dict]:
    """Generate synthetic OHLC bars following a random walk."""
    import random as _random

    _random.seed(42)
    bars: list[dict] = []
    price = start_price
    for _ in range(n):
        o = price
        change = _random.gauss(0, sigma)
        c = price + change
        h = max(o, c) + abs(change) * 0.5
        l = min(o, c) - abs(change) * 0.5
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price = c
    return bars

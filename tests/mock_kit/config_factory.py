"""Strategy and live config factories for testing.

Provides canned StrategyLineConfig objects (matching live.yaml sections)
and a create_minimal_live_config() helper for integration tests.

Extracted from tests/execution/conftest.py StrategyLineConfig fixtures.
"""

from __future__ import annotations

from typing import Any

from core.execution.strategy_line import StrategyLineConfig


# ---------------------------------------------------------------------------
# StrategyLineConfig presets
# ---------------------------------------------------------------------------
def create_strategy_line_config(
    *,
    name: str = "barrier_12bar",
    magic: int = 90001,
    brain_types: set[str] | None = None,
    base_volume: float = 0.01,
    max_volume: float = 0.05,
    base_sl_atr_mult: float = 2.0,
    base_tp_atr_mult: float = 3.5,
    hard_sl_ratio: float = 1.5,
    ref_atr: float = 5.0,
    confidence_threshold: float = 0.40,
    long_bias_discount: float = 0.05,
    daily_loss_limit_pct: float = -0.03,
    max_consecutive_losses: int = 5,
) -> StrategyLineConfig:
    """Create a StrategyLineConfig with controlled values.

    Defaults match the barrier_12bar section in live.yaml.
    """
    if brain_types is None:
        brain_types = {"xgboost_v9", "lightgbm_v1"}

    return StrategyLineConfig(
        name=name,
        magic=magic,
        brain_types=brain_types,
        base_volume=base_volume,
        max_volume=max_volume,
        base_sl_atr_mult=base_sl_atr_mult,
        base_tp_atr_mult=base_tp_atr_mult,
        hard_sl_ratio=hard_sl_ratio,
        ref_atr=ref_atr,
        confidence_threshold=confidence_threshold,
        long_bias_discount=long_bias_discount,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_consecutive_losses=max_consecutive_losses,
    )


# ---------------------------------------------------------------------------
# Strategy magic map
# ---------------------------------------------------------------------------
def create_strategy_magic_map() -> dict[str, int]:
    """Return the standard strategy-name → magic mapping used in live configs."""
    return {
        "barrier_12bar": 90001,
        "micro_3bar": 90002,
        "statarb_dynamic": 90003,
        "trend_ensemble": 90004,
        "swing_m15": 90005,
        "swing_m30": 90006,
        "swing_h1": 90007,
        "swing_h4": 90008,
    }


# ---------------------------------------------------------------------------
# Minimal live config
# ---------------------------------------------------------------------------
def create_minimal_live_config(
    *,
    symbol: str = "XAUUSDc",
    strategies: list[str] | None = None,
    default_volume: float = 0.01,
    circuit_breaker_enabled: bool = True,
) -> dict[str, Any]:
    """Create a minimal live config dict for integration tests.

    Does NOT read any YAML file. Returns a controlled, in-memory dict
    with just enough structure to satisfy LiveCycle.__init__().

    Args:
        symbol: Trading symbol.
        strategies: List of strategy names to enable.
        default_volume: Default lot size.
        circuit_breaker_enabled: Whether circuit breakers start active.

    Returns:
        Dict with minimal live config structure.
    """
    if strategies is None:
        strategies = ["barrier_12bar"]

    return {
        "symbol": symbol,
        "strategies": strategies,
        "default_volume": default_volume,
        "circuit_breaker_enabled": circuit_breaker_enabled,
        "mt5": {
            "path": "mock_mt5_path",
            "port": 0,
            "timeout_ms": 100,
        },
        "risk": {
            "max_positions": 1,
            "daily_loss_limit_r": -0.05,
            "max_consecutive_losses": 5,
        },
    }

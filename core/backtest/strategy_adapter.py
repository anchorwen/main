"""Bridge between strategies and the BacktestEngine callback.

Provides ``StrategyLineAdapter`` that wraps strategy lines or rule-based
strategies into a signal-producing callback the engine understands.

Usage:
    from core.backtest.strategy_adapter import rule_based_strategies

    # Quick rule-based backtest (no ML dependencies):
    strategy_fn = rule_based_strategies(["barrier", "micro", "statarb"])
    engine = BacktestEngine(feed, strategy_fn)
    result = engine.run()
"""

from __future__ import annotations

from typing import Any

import numpy as np

from core.backtest.data_feed import Bar
from core.backtest.portfolio import VirtualPortfolio

# ── Rule-based strategy generators ──────────────────────────────────────────


def _barrier_rule(
    bar: Bar, portfolio: VirtualPortfolio, context: dict[str, Any], history: list[Bar]
) -> dict[str, Any] | None:
    """Barrier-style: trend-following with breakout logic.

    Enters long when price breaks above recent high, short when below recent low.
    """
    if len(history) < 20:
        return None
    closes = np.array([b.close for b in history[-20:]])
    highs = np.array([b.high for b in history[-12:]])
    lows = np.array([b.low for b in history[-12:]])

    ma = float(np.mean(closes))
    resistance = float(np.max(highs))
    support = float(np.min(lows))
    atr = float(context.get("current_atr", 5.0))

    if bar.close > resistance and bar.close > ma:
        return {
            "direction": "long",
            "confidence": min(0.9, 0.5 + (bar.close - resistance) / max(atr, 1e-6) * 0.1),
            "volume": 0.02,
            "sl_atr_mult": 2.0,
            "tp_atr_mult": 3.5,
        }
    if bar.close < support and bar.close < ma:
        return {
            "direction": "short",
            "confidence": min(0.9, 0.5 + (support - bar.close) / max(atr, 1e-6) * 0.1),
            "volume": 0.02,
            "sl_atr_mult": 2.0,
            "tp_atr_mult": 3.5,
        }
    return None


def _micro_rule(
    bar: Bar, portfolio: VirtualPortfolio, context: dict[str, Any], history: list[Bar]
) -> dict[str, Any] | None:
    """Micro-structure: short-term mean-reversion on small pullbacks.

    Enters on pullback within a trending regime, targeting quick reversion.
    """
    if len(history) < 8:
        return None
    closes = np.array([b.close for b in history[-8:]])
    atr = float(context.get("current_atr", 3.0))

    recent_high = float(np.max(closes))
    recent_low = float(np.min(closes))
    mid = (recent_high + recent_low) / 2
    range_pct = (recent_high - recent_low) / max(bar.close, 1e-6)

    # Only trade meaningful ranges (> 0.1% of price)
    if range_pct < 0.001:
        return None

    if bar.close < mid - 0.3 * atr:
        return {
            "direction": "long",
            "confidence": 0.65,
            "volume": 0.03,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
        }
    if bar.close > mid + 0.3 * atr:
        return {
            "direction": "short",
            "confidence": 0.65,
            "volume": 0.03,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
        }
    return None


def _statarb_rule(
    bar: Bar, portfolio: VirtualPortfolio, context: dict[str, Any], history: list[Bar]
) -> dict[str, Any] | None:
    """StatArb-style: Ornstein-Uhlenbeck mean-reversion on price deviations.

    Uses z-score of price relative to rolling mean as entry signal.
    """
    if len(history) < 50:
        return None
    closes = np.array([b.close for b in history[-50:]])

    rolling_mean = float(np.mean(closes))
    rolling_std = float(np.std(closes))
    if rolling_std < 1e-6:
        return None

    z_score = (bar.close - rolling_mean) / rolling_std

    if z_score < -2.0:
        return {
            "direction": "long",
            "confidence": min(0.95, 0.5 + abs(z_score) * 0.15),
            "volume": 0.01,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
        }
    if z_score > 2.0:
        return {
            "direction": "short",
            "confidence": min(0.95, 0.5 + abs(z_score) * 0.15),
            "volume": 0.01,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
        }
    return None


# Registry
_RULE_REGISTRY: dict[str, Any] = {
    "barrier": _barrier_rule,
    "barrier_12bar": _barrier_rule,
    "micro": _micro_rule,
    "micro_3bar": _micro_rule,
    "statarb": _statarb_rule,
    "statarb_dynamic": _statarb_rule,
}


def rule_based_strategies(
    strategies: list[str] | None = None,
) -> Any:
    """Create a StrategyFn that cycles through named rule-based strategies.

    Args:
        strategies: List of strategy names. Default: all three.
                    Valid: ``"barrier"``, ``"micro"``, ``"statarb"``.

    Returns:
        A StrategyFn callable suitable for ``BacktestEngine(strategy=...)``.
    """
    names = strategies or ["barrier_12bar", "micro_3bar", "statarb_dynamic"]
    rules = [_RULE_REGISTRY[n] for n in names if n in _RULE_REGISTRY]
    history: list[Bar] = []
    idx = [0]  # mutable counter for round-robin

    def _strategy_fn(
        bar: Bar, portfolio: VirtualPortfolio, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        history.append(bar)
        # Try each rule in round-robin order from last used
        for offset in range(len(rules)):
            rule_idx = (idx[0] + offset) % len(rules)
            signal = rules[rule_idx](bar, portfolio, context, history)
            if signal is not None:
                idx[0] = (rule_idx + 1) % len(rules)  # next starts after this one
                return signal
        idx[0] = (idx[0] + 1) % len(rules)
        return None

    return _strategy_fn


# ── Brain-backed adapter for real strategy lines ────────────────────────────


class StrategyLineAdapter:
    """Wraps StrategyLine instances for use with BacktestEngine.

    Use when brain adapters (ONNX models) are loaded and configured.
    """

    def __init__(self, lines: list[Any]):
        self._lines = lines
        self._bar_history: list[Bar] = []
        self._current_line_idx = 0

    @classmethod
    def from_configs(
        cls,
        configs: list[dict[str, Any]],
        brain_adapters_map: dict[str, list[Any]] | None = None,
    ) -> StrategyLineAdapter:
        """Factory from strategy config dicts with pre-loaded brain adapters."""
        from core.execution.strategy_line import (
            BarrierStrategy,
            MicroStrategy,
            StatArbStrategy,
            StrategyLineConfig,
        )

        adapters_map = brain_adapters_map or {}
        lines: list[Any] = []

        for cfg in configs:
            name = cfg["name"]
            magic = cfg["magic"]
            brain_types = set(cfg["brain_types"])

            config = StrategyLineConfig(
                name=name,
                magic=magic,
                brain_types=brain_types,
                base_volume=cfg.get("base_volume", 0.01),
                max_volume=cfg.get("max_volume", 0.05),
                confidence_threshold=cfg.get("confidence_threshold", 0.5),
            )

            brains = adapters_map.get(name, [])

            if name in ("barrier_12bar", "barrier"):
                lines.append(BarrierStrategy(config=config, brains=brains))
            elif name in ("micro_3bar", "micro"):
                lines.append(MicroStrategy(config=config, brains=brains))
            elif name in ("statarb_dynamic", "statarb"):
                lines.append(StatArbStrategy(config=config, brains=brains))
            else:
                raise ValueError(f"Unknown strategy: {name}")

        return cls(lines)

    def to_strategy_fn(self):
        adapter = self

        def _fn(
            bar: Bar, portfolio: VirtualPortfolio, context: dict[str, Any]
        ) -> dict[str, Any] | None:
            adapter._bar_history.append(bar)
            atr = float(context.get("current_atr", 5.0))

            for line in adapter._lines:
                try:
                    decision = line.evaluate(
                        feature_vector=None,
                        micro_feature_vector=None,
                        mid_price=bar.close,
                        current_atr=atr,
                    )
                except Exception:
                    continue
                if not decision.should_trade:
                    continue
                return {
                    "direction": decision.direction,
                    "confidence": decision.confidence,
                    "volume": decision.volume,
                    "sl_atr_mult": 2.0,
                    "tp_atr_mult": 3.5,
                }
            return None

        return _fn

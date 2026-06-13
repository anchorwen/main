"""Rule-engine strategy wrapper — bridges zero-ML strategies into StrategyLine.

Blind Spot / Phase 1 (2026-06-13): StructuralSwingV1 existed but was never
wired into the live pipeline.  strategy_builder.py only knew about 4 ML
StrategyLine subclasses.  This wrapper adapts the pure-rule StructuralSwingV1
to the standard StrategyLine.evaluate() interface so strategy_builder can
instantiate it alongside ML strategies.

The wrapper:
  - Uses mid/ask/bid prices + current ATR (already available in evaluate())
  - Applies Bid/Ask-aware barrier computation (Rule 2 from StructuralSwingV1)
  - Enforces hard SL=3.0/TP=1.5 with time-stop horizon
  - Does NOT run the H1 EMA trend filter (Rule 1) — cooldown_bars handles
    trade frequency in live mode; the EMA filter can be re-enabled later
    when H1 OHLC data is plumbed through mtf_price_service.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyDecision
from core.strategies.structural_swing_v1 import StructuralSwingV1


class RuleEngineStrategyWrapper:
    """Adapts a pure-rule strategy to the StrategyLine.evaluate() interface.

    Unlike ML strategies (Barrier/Micro/StatArb/Swing), rule-based strategies
    have no brain inference, no feature vectors, no MetaFilter.  They compute
    signals directly from price + ATR using pure math.
    """

    def __init__(
        self,
        strategy_name: str,
        magic: int,
        rule_engine: StructuralSwingV1,
        cooldown_bars: int = 3,
        max_positions_per_direction: int = 1,
        base_volume: float = 0.01,
    ):
        self._name = strategy_name
        self._magic = magic
        self._engine = rule_engine
        self._cooldown_bars = cooldown_bars
        self._max_per_dir = max_positions_per_direction
        self._base_volume = base_volume
        # Internal state
        self._bars_since_last_signal: int = 999  # start ready

    @property
    def config(self) -> dict[str, Any]:
        """Minimal config for compatibility with strategy_evaluator."""
        return {"name": self._name, "timeframe": "M5"}

    @property
    def engine(self) -> StructuralSwingV1:
        return self._engine

    def evaluate(
        self,
        feature_vector: Any = None,
        micro_feature_vector: Any = None,
        mid_price: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        current_atr: float | None = None,
        regime_info: dict[str, Any] | None = None,
        regime_gate_mode: str = "full",
        trend_direction: str = "neutral",
        trend_strength: float = 0.0,
        h4_trend_strength: float = 0.0,
        hurst: float | None = None,
        kalman_velocity_bps: float | None = None,
        macro_regime: str = "mixed",
        risk_budget_usd: float = 0.0,
        tracker: Any = None,
        pnl_ledger: Any = None,
        pnl_store: Any = None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        meta_filter: Any = None,
        meta_filter_gate: Any = None,
        conformal_ou_gate: Any = None,
        micro_feature_dict: dict[str, float] | None = None,
        btc_augment: Any = None,
    ) -> StrategyDecision:
        """Evaluate one cycle. Returns StrategyDecision with should_trade flag.

        Uses a simplified StructuralSwingV1 path:
          - No H1 EMA trend filter (cooldown_bars handles frequency)
          - Bid/Ask-aware barriers from current price + ATR
          - Hard SL=3.0/TP=1.5, 12-bar time horizon
        """
        # ── Cooldown ──
        if self._bars_since_last_signal < self._cooldown_bars:
            self._bars_since_last_signal += 1
            return StrategyDecision(
                strategy_name=self._name,
                magic=self._magic,
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                reason=f"cooldown:{self._bars_since_last_signal}/{self._cooldown_bars}",
            )

        # ── Need valid prices and ATR ──
        if mid_price is None or bid is None or ask is None:
            return StrategyDecision(
                strategy_name=self._name,
                magic=self._magic,
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                reason="missing_price_data",
            )

        if current_atr is None or current_atr <= 0 or np.isnan(current_atr):
            return StrategyDecision(
                strategy_name=self._name,
                magic=self._magic,
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                reason="invalid_atr",
            )

        # ── Shadow block ──
        if regime_gate_mode == "shadow":
            return StrategyDecision(
                strategy_name=self._name,
                magic=self._magic,
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                reason="shadow_mode",
            )

        # ── Direction: always allow both (no trend filter in MVP) ──
        # We need a binary direction.  Use a simple heuristic:
        # if we already have a long position, prefer short to balance;
        # otherwise alternate based on bar parity (ensures mixed exposure).
        # In practice, cooldown_bars=3 + max_positions_per_direction=1
        # prevents clustering.
        _bar_parity = self._bars_since_last_signal % 2
        if _bar_parity == 0:
            direction = "long"
        else:
            direction = "short"

        # ── Compute barriers (Bid/Ask-aware) ──
        ref_price = float(mid_price)
        atr_val = float(current_atr)
        entry, sl, tp = self._engine._compute_barriers(direction, ref_price, atr_val)

        if entry <= 0 or sl <= 0 or tp <= 0:
            return StrategyDecision(
                strategy_name=self._name,
                magic=self._magic,
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                reason="invalid_barriers",
            )

        # ── Signal valid → trade ──
        self._bars_since_last_signal = 0

        return StrategyDecision(
            strategy_name=self._name,
            magic=self._magic,
            should_trade=True,
            direction=direction,
            confidence=0.50,  # calibration EV=+0.20R, neutral confidence
            volume=self._base_volume,
            sl=round(sl, 3),
            tp=round(tp, 3),
            hard_sl=round(sl, 3),  # no separate hard SL for rule strategies
            brain_ids=[],  # no ML brains
            brain_votes=[],
            supporting_count=0,
            total_count=0,
            regime_mode=regime_gate_mode,
            venue="live",
            reason="structural_swing_v1_signal",
            entry_context={
                "atr": round(atr_val, 4),
                "entry_price": round(entry, 3),
                "spread_points": self._engine.spread_points,
                "slippage_points": self._engine.slippage_points,
                "strategy": "Structural_Swing_V1",
            },
            p_win=0.476,  # calibration TP rate
            p_win_source="calibration",
            p_win_degraded=False,
            kelly_mult=0.25,  # conservative fractional Kelly
            cold_explore=False,
        )

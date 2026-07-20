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

from core.execution.strategy_context import StrategyEvaluationContext
from core.execution.strategy_decision import StrategyDecision
from core.execution.strategy_protocol import StrategyEvaluateProtocol
from core.strategies.structural_swing_v1 import StructuralSwingV1


class RuleEngineStrategyWrapper(StrategyEvaluateProtocol):
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

    @property
    def budget(self) -> None:
        """Rule-based strategies have no budget tracking (no brain PnL ledger).

        FIX-20260619-002: Previously missing — AttributeError crashed the
        cycle when live_cycle checked _strat.budget is not None.
        Returning None is correct: budget recording is simply skipped
        for zero-ML strategies (no brain to track PnL for).
        """
        return None

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> StrategyDecision:
        """Evaluate one cycle. Returns StrategyDecision with should_trade flag.

        Uses a simplified StructuralSwingV1 path:
          - No H1 EMA trend filter (cooldown_bars handles frequency)
          - Bid/Ask-aware barriers from current price + ATR
          - Hard SL=3.0/TP=1.5, 12-bar time horizon

        Args:
            context: StrategyEvaluationContext — all input state for this cycle.
                     Frozen (immutable).  Rule engines ignore ML-specific fields
                     (feature_vector, brains, gates) and use only price/ATR/regime.
        """
        # ── L3 Interface Consolidation: local unpacking ──────────────────────
        # Extract context fields into local variables for backward-compatible
        # internal references.  Context is frozen — local shadowing is safe.
        feature_vector = context.feature_vector  # unused — rule engines have no ML
        micro_feature_vector = context.micro_feature_vector
        mid_price = context.mid_price
        bid = context.bid
        ask = context.ask
        current_atr = context.current_atr
        strategy_atr = context.strategy_atr
        regime_info = context.regime_info
        regime_gate_mode = context.regime_gate_mode
        trend_direction = context.trend_direction
        trend_strength = context.trend_strength
        h4_trend_strength = context.h4_trend_strength
        hurst = context.hurst
        kalman_velocity_bps = context.kalman_velocity_bps
        macro_regime = context.macro_regime
        risk_budget_usd = context.risk_budget_usd
        tracker = context.tracker
        pnl_ledger = context.pnl_ledger
        pnl_store = context.pnl_store
        micro_sequences = context.micro_sequences
        daily_feature_vector = context.daily_feature_vector
        meta_filter = context.meta_filter
        meta_filter_gate = context.meta_filter_gate
        conformal_ou_gate = context.conformal_ou_gate
        microstructure_gate = context.microstructure_gate
        micro_feature_dict = context.micro_feature_dict
        btc_augment = context.btc_augment
        governance_state = context.governance_state

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

        # ── Direction: use regime gate trend_direction ──────────────────
        # DQAF-20260616-003: previously used _bars_since_last_signal % 2
        # (bar parity) as a direction selector.  With cooldown_bars=3,
        # the counter is always 3 when the cooldown check passes →
        # 3 % 2 = 1 → always SHORT → LONG branch mathematically unreachable.
        #
        # The regime gate already computes trend_direction from ADX, which
        # is a superior signal to bar parity.  Use it directly.
        # Fall back to "neutral" (no trade) when trend is indeterminate.
        if trend_direction == "long":
            direction = "long"
        elif trend_direction == "short":
            direction = "short"
        else:
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
                reason="neutral_trend_no_direction",
            )

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

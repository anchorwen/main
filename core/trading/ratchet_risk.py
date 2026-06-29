"""V6 Layer B3: Ratchet Risk — profit protection with discrete state escalation.

Ports God's Eye V6.0 exit_manager.py:343-388 (check_ratchet_risk_exit) into
d:\future as a brain-agnostic shared module.  Operates purely on PnL and ATR
— no z_score, no half_life, no regime_prob, no brain signals.

Two discrete contracts per position:
  1. BreakevenDefense: Arm when PnL > eta × ATR_cost.
     Once armed, close when PnL fades to ≤ cost_buffer.  IRREVERSIBLE.
  2. DrawdownLock: Arm when peak PnL > activation × ATR_cost.
     Once armed, close on N% giveback from peak.  Peak is monotonic.

Design (v6_integration_blueprint.pdf §4 P6):
  - Zero MODIFY_SL — never touches stop-loss.  Close-only.
  - Nonlinear DrawdownLock: as peak profit expands, the allowed % giveback
    compresses exponentially (more aggressive than V6's linear 35%).
  - State lives on ActivePosition fields (ratchet_breakeven_armed,
    ratchet_drawdown_armed, ratchet_peak_pnl).

Reference parameters (from V6 ScoutConfig, tuned on XAUUSD M5 live):
  breakeven_atr_mult  = 1.2   (was 0.4 in blueprint — corrected to V6 empirical)
  cost_buffer         = 5.0   (USD, covers spread+commission on 0.05 lot)
  drawdown_activation_atr = 2.0
  drawdown_giveback_pct   = 35.0
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class RatchetConfig:
    """Per-strategy ratchet risk parameters.

    All ATR multipliers are expressed in USD-equivalent terms:
    ATR_cost = atr × point_value × base_lot.
    """

    breakeven_enabled: bool = True
    breakeven_atr_mult: float = 1.2
    """Arm breakeven defense when Net PnL > N × ATR_cost."""

    cost_buffer: float = 5.0
    """Close when Net PnL ≤ $N after breakeven is armed."""

    drawdown_enabled: bool = True
    drawdown_activation_atr: float = 2.0
    """Arm drawdown lock when peak PnL > N × ATR_cost."""

    drawdown_giveback_pct: float = 35.0
    """Close when Net PnL < peak × (1 - N/100)."""

    # ── Nonlinear drawdown lock (blueprint enhancement) ──
    nonlinear_drawdown: bool = True
    """When True, the allowed giveback % compresses as peak profit expands.
    Linear: always 35%.  Nonlinear: 35% at 2×ATR, 25% at 5×ATR, 15% at 10×ATR."""


@dataclass
class RatchetVerdict:
    """Output of RatchetRisk.evaluate()."""

    should_close: bool
    reason: str  # "BREAKEVEN_DEFENSE" | "DRAWDOWN_LOCK" | ""
    details: dict[str, Any]

    @property
    def is_triggered(self) -> bool:
        return self.should_close


class RatchetRisk:
    """Profit protection engine — pure physical, no model dependency.

    Usage per management cycle:
        verdict = ratchet.evaluate(
            net_pnl=pos.current_pnl,
            atr=current_atr,
            point_value=point_value,
            base_lot=pos.volume,
            ratchet_state=pos,  # reads/writes ActivePosition ratchet fields
            config=ratchet_config,
        )
        if verdict.should_close:
            dispatch_close(pos, reason=verdict.reason)
    """

    def evaluate(
        self,
        net_pnl: float,
        atr: float,
        point_value: float,
        base_lot: float,
        breakeven_armed: bool,
        drawdown_armed: bool,
        peak_pnl: float,
        config: RatchetConfig,
    ) -> RatchetVerdict:
        """Evaluate both ratchet contracts.  Returns new state + verdict.

        Args:
            net_pnl: Current net PnL in USD for this position.
            atr: Current ATR in price units.
            point_value: USD per 1.0 price move per 1 lot.
            base_lot: Position volume in lots.
            breakeven_armed: Current breakeven defense state.
            drawdown_armed: Current drawdown lock state.
            peak_pnl: Highest net PnL seen for this position.
            config: Ratchet parameters.
        """
        atr_dollar = atr * point_value * base_lot
        if atr_dollar <= 0:
            return RatchetVerdict(False, "", {})

        new_breakeven_armed = breakeven_armed
        new_drawdown_armed = drawdown_armed
        new_peak_pnl = max(peak_pnl, net_pnl)

        # ── Breakeven Defense ─────────────────────────────────
        if config.breakeven_enabled:
            if not breakeven_armed:
                if net_pnl > config.breakeven_atr_mult * atr_dollar:
                    new_breakeven_armed = True
            else:
                if net_pnl <= config.cost_buffer:
                    return RatchetVerdict(
                        True,
                        "BREAKEVEN_DEFENSE",
                        {
                            "net_pnl": round(net_pnl, 2),
                            "atr_dollar": round(atr_dollar, 2),
                            "cost_buffer": config.cost_buffer,
                            "armed_at_pnl": round(config.breakeven_atr_mult * atr_dollar, 2),
                        },
                    )

        # ── Drawdown Lock ─────────────────────────────────────
        if config.drawdown_enabled:
            if not drawdown_armed:
                if new_peak_pnl > config.drawdown_activation_atr * atr_dollar:
                    new_drawdown_armed = True
            if new_drawdown_armed and new_peak_pnl > 0:
                # Nonlinear giveback: compress as peak expands
                if config.nonlinear_drawdown:
                    effective_pct = self._nonlinear_giveback_pct(new_peak_pnl, atr_dollar, config)
                else:
                    effective_pct = config.drawdown_giveback_pct

                threshold = new_peak_pnl * (1.0 - effective_pct / 100.0)
                if net_pnl < threshold:
                    giveback = (
                        (new_peak_pnl - net_pnl) / new_peak_pnl * 100.0 if new_peak_pnl > 0 else 0.0
                    )
                    return RatchetVerdict(
                        True,
                        "DRAWDOWN_LOCK",
                        {
                            "peak_pnl": round(new_peak_pnl, 2),
                            "current_pnl": round(net_pnl, 2),
                            "threshold": round(threshold, 2),
                            "giveback_pct": round(giveback, 1),
                            "effective_pct": round(effective_pct, 1),
                            "nonlinear": config.nonlinear_drawdown,
                        },
                    )

        # Return updated state flags for caller to persist
        return RatchetVerdict(
            False,
            "",
            {
                "_breakeven_armed": new_breakeven_armed,
                "_drawdown_armed": new_drawdown_armed,
                "_peak_pnl": new_peak_pnl,
            },
        )

    @staticmethod
    def _nonlinear_giveback_pct(peak_pnl: float, atr_dollar: float, config: RatchetConfig) -> float:
        """Compute nonlinear giveback % based on peak PnL in ATR multiples.

        Compression curve:
          peak ≤ 2×ATR   → 35.0% (base)
          peak = 5×ATR   → 25.0%
          peak = 10×ATR  → 15.0%
          peak ≥ 20×ATR  → 10.0% (floor)

        Uses logarithmic interpolation between anchor points.
        """
        if atr_dollar <= 0:
            return config.drawdown_giveback_pct

        peak_in_atr = peak_pnl / atr_dollar

        # Anchor points: (ATR_multiple, giveback_pct)
        anchors = [
            (2.0, 35.0),
            (5.0, 25.0),
            (10.0, 15.0),
            (20.0, 10.0),
        ]

        if peak_in_atr <= anchors[0][0]:
            return anchors[0][1]
        if peak_in_atr >= anchors[-1][0]:
            return anchors[-1][1]

        for i in range(len(anchors) - 1):
            lo_atr, lo_pct = anchors[i]
            hi_atr, hi_pct = anchors[i + 1]
            if lo_atr <= peak_in_atr <= hi_atr:
                # Log-linear interpolation
                log_frac = math.log(peak_in_atr / lo_atr) / math.log(hi_atr / lo_atr)
                return lo_pct + (hi_pct - lo_pct) * log_frac

        return config.drawdown_giveback_pct


# ── Module-level convenience ────────────────────────────────────────────


def create_default_ratchet_config() -> RatchetConfig:
    """Return RatchetConfig with V6-empirical defaults for XAUUSD M5."""
    return RatchetConfig(
        breakeven_enabled=True,
        breakeven_atr_mult=1.2,
        cost_buffer=5.0,
        drawdown_enabled=True,
        drawdown_activation_atr=2.0,
        drawdown_giveback_pct=35.0,
        nonlinear_drawdown=True,
    )

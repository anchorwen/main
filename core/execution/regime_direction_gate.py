"""RegimeDirectionGate — trend-conditional brain signal filter.

FIX-20260613-079: Blocks brain signals that predict against the prevailing
trend direction, reducing noise from regime-locked brains.  Ranging markets
(ADX < 25) passthrough ALL signals to preserve Parliament diversity.

FIX-20260613-090 (Step 1): Physics-based mean-reversion override.  OU Theta
and Hurst exponent are injected into regime_info by live_cycle.  When both
confirm strong mean-reversion (Theta > 0.5, Hurst < 0.48), the gate returns
"ranging" regardless of ADX — counter-trend signals are physically justified.

TODO (FIX-20260613-090-Phase2): Phase out ADX gating entirely once ML Brain
is retrained with V9_Micro features.  Relegate RegimeGate to purely anomaly
detection (e.g., flash crash block).

Design (IC Review approved):
    - Trend definition: ADX >= 25, +DI > -DI → uptrend, -DI > +DI → downtrend
    - Ranging (ADX < 25): FULL passthrough, no blocking
    - Stale-shield: WARN when any direction blocked > N consecutive cycles

Usage:
    gate = RegimeDirectionGate(adx_threshold=25, stale_warn_cycles=20)
    filtered = gate.filter(brain_signals, regime_info)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RegimeDirectionGate:
    """Trend-conditional filter for brain signal proposals.

    In a confirmed uptrend (ADX >= 25, +DI > -DI), brains predicting
    "short" are blocked.  In a confirmed downtrend, "long" brains are
    blocked.  In ranging markets (ADX < 25), all brains pass through.
    """

    def __init__(
        self,
        adx_threshold: float = 25.0,
        stale_warn_cycles: int = 20,
    ) -> None:
        self._adx_threshold = adx_threshold
        self._stale_warn_cycles = stale_warn_cycles
        self._long_blocked_streak: int = 0
        self._short_blocked_streak: int = 0
        self._total_cycles: int = 0
        # ── FIX-20260613-090: rolling physics buffers for self-calibration ──
        # Hardcoded thresholds (Theta > 0.5, Hurst < 0.48) have no empirical
        # basis in our system's data.  Instead, the gate self-calibrates:
        # it collects recent (Theta, Hurst) pairs and only triggers the
        # physics override when BOTH values are in the extreme tail of their
        # own rolling distribution — Theta > P75 AND Hurst < P25.
        # This adapts to whatever volatility regime the market is in.
        self._ou_history: list[float] = []  # rolling window of OU Theta
        self._hurst_history: list[float] = []  # rolling window of Hurst
        self._physics_window: int = 288  # ~24h of M5 bars
        self._physics_min_samples: int = 50  # need enough data before trusting

    def _resolve_trend(self, regime_info: dict[str, Any]) -> str:
        """Determine trend direction — physics-first, ADX as fallback.

        FIX-20260613-090 Step 1: OU Theta + Hurst mean-reversion override.
        When both indicators confirm strong mean-reversion (Theta > 0.5,
        Hurst < 0.48), the market is treated as "ranging" regardless of
        ADX or explicit trend_direction.  This is the first step toward
        replacing ADX-based gating with physics-based regime detection.

        Returns: "up", "down", or "ranging"
        """
        import math

        # ═══ Priority 0: Self-calibrating physics-based override ═══
        # No hardcoded thresholds — the gate maintains rolling distributions
        # of OU Theta and Hurst, and triggers only when BOTH values are in
        # the extreme tail of their own history.
        # Condition: Theta > its own P75 AND Hurst < its own P25
        # This means: "mean-reversion is unusually strong AND persistence
        # is unusually low, relative to recent market conditions."
        ou_theta = float(regime_info.get("ou_theta_m5", float("nan")))
        hurst_m5 = float(regime_info.get("hurst_m5", float("nan")))

        if not (math.isnan(ou_theta) or math.isnan(hurst_m5)):
            # Guardrail: only trust values in physically plausible ranges
            if 0.0 < ou_theta < 2.0 and 0.0 < hurst_m5 < 1.0:
                # Skip default/value from cold start (ou=0.0, hurst=0.5)
                if not (abs(ou_theta) < 0.0001 and abs(hurst_m5 - 0.5) < 0.0001):
                    # Accumulate rolling history
                    self._ou_history.append(ou_theta)
                    self._hurst_history.append(hurst_m5)
                    if len(self._ou_history) > self._physics_window:
                        self._ou_history.pop(0)
                    if len(self._hurst_history) > self._physics_window:
                        self._hurst_history.pop(0)

                    # Self-calibrate: only trigger with sufficient history
                    if len(self._ou_history) >= self._physics_min_samples:
                        _ou_sorted = sorted(self._ou_history)
                        _hurst_sorted = sorted(self._hurst_history)
                        _ou_p75 = _ou_sorted[int(len(_ou_sorted) * 0.75)]
                        _hurst_p25 = _hurst_sorted[int(len(_hurst_sorted) * 0.25)]
                        # Override: strong pull AND weak persistence vs own history
                        if ou_theta > _ou_p75 and hurst_m5 < _hurst_p25:
                            return "ranging"

        # ═══ Priority 1: DI-based with ADX confirmation ═══
        adx = float(regime_info.get("adx", 0) or 0)
        plus_di = float(regime_info.get("plus_di", 0) or 0)
        minus_di = float(regime_info.get("minus_di", 0) or 0)

        if adx >= self._adx_threshold and plus_di > 0 and minus_di > 0:
            if plus_di > minus_di:
                return "up"
            else:
                return "down"

        # ═══ Priority 2: explicit trend_direction gated by ADX ═══
        trend_str = str(regime_info.get("trend_direction", "")).lower()
        if adx >= self._adx_threshold:
            if trend_str in ("long", "up", "bullish"):
                return "up"
            if trend_str in ("short", "down", "bearish"):
                return "down"

        # ═══ Priority 3: regime string ═══
        detected_regime = str(regime_info.get("detected_regime", regime_info.get("primary_regime", ""))).lower()
        if "trending_up" in detected_regime:
            return "up"
        if "trending_down" in detected_regime:
            return "down"

        # Default: ranging — passthrough all signals
        return "ranging"

    def filter(
        self,
        brain_signals: list[dict[str, Any]],
        regime_info: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Filter brain signals based on trend alignment.

        Args:
            brain_signals: List of {brain_id, direction, ...} dicts
            regime_info: Current regime snapshot {adx, plus_di, minus_di,
                         trend_direction, detected_regime, ...}

        Returns:
            (filtered_signals, gate_audit) where gate_audit contains
            blocking decisions and streak counters for diagnostics.
        """
        trend = self._resolve_trend(regime_info)
        self._total_cycles += 1

        blocked_long: list[str] = []
        blocked_short: list[str] = []
        passed: list[dict[str, Any]] = []

        for sig in brain_signals:
            direction = str(sig.get("direction", "")).lower()
            brain_id = str(sig.get("brain_id", "?"))

            if trend == "up" and direction == "short":
                blocked_short.append(brain_id)
                continue
            elif trend == "down" and direction == "long":
                blocked_long.append(brain_id)
                continue

            passed.append(sig)

        # Update streak counters
        if blocked_long:
            self._long_blocked_streak += 1
        else:
            self._long_blocked_streak = 0

        if blocked_short:
            self._short_blocked_streak += 1
        else:
            self._short_blocked_streak = 0

        # Stale-shield warnings
        stale_warnings: list[str] = []
        if self._long_blocked_streak >= self._stale_warn_cycles:
            msg = (
                f"RegimeDirectionGate: LONG brains blocked for "
                f"{self._long_blocked_streak} consecutive cycles "
                f"(trend={trend}).  Verify trend signal is not stale."
            )
            logger.warning(msg)
            stale_warnings.append(msg)
        if self._short_blocked_streak >= self._stale_warn_cycles:
            msg = (
                f"RegimeDirectionGate: SHORT brains blocked for "
                f"{self._short_blocked_streak} consecutive cycles "
                f"(trend={trend}).  Verify trend signal is not stale."
            )
            logger.warning(msg)
            stale_warnings.append(msg)

        # ── Self-calibration diagnostics ──
        _phys_state: dict[str, Any] = {"active": False, "samples": 0}
        if len(self._ou_history) >= self._physics_min_samples:
            _ou_s = sorted(self._ou_history)
            _hu_s = sorted(self._hurst_history)
            _phys_state = {
                "active": True,
                "samples": len(self._ou_history),
                "ou_p75": round(_ou_s[int(len(_ou_s) * 0.75)], 4),
                "hurst_p25": round(_hu_s[int(len(_hu_s) * 0.25)], 4),
                "ou_current": round(float(regime_info.get("ou_theta_m5", 0)), 4),
                "hurst_current": round(float(regime_info.get("hurst_m5", 0.5)), 4),
            }

        audit = {
            "gate": "RegimeDirectionGate",
            "trend": trend,
            "adx_threshold": self._adx_threshold,
            "total_signals_in": len(brain_signals),
            "passed": len(passed),
            "blocked_long": blocked_long,
            "blocked_short": blocked_short,
            "long_blocked_streak": self._long_blocked_streak,
            "short_blocked_streak": self._short_blocked_streak,
            "stale_warnings": stale_warnings,
            "cycle": self._total_cycles,
            "physics_calibration": _phys_state,
        }
        return passed, audit

    def reset_streaks(self) -> None:
        """Reset blocking streaks (e.g. after manual trend review)."""
        self._long_blocked_streak = 0
        self._short_blocked_streak = 0

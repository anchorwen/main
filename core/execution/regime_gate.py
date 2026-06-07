"""Regime gate — continuous manifold modulation for strategy activation.

Replaces discrete binary gating with a 4D continuous feature space:
  trend_strength, vol_percentile, hurst, vr_z_score

The output is a smooth RegimeModulation that adjusts position sizing,
minimum brain requirements, and confidence thresholds — without ever
fully silencing a strategy (``"off"`` is dead; ``"shadow"`` replaces it).

v3.0 (2026-05-11): Removed vol_regime hard-override.  Replaced 1D discrete
matrix with 4D continuous manifold.  Trend × Volatility are orthogonal
dimensions with explicit interaction terms.  All "off" gating → "shadow".

v3.1 (2026-05-15): Added 2D regime matrix for OU strategies:
  X-axis = H1 Hurst (structure), Y-axis = M5 RV percentile (energy).
  Schmitt trigger hysteresis prevents flapping at threshold edges —
  FORCE-OFF at RV ≥ 95%, restore only after 3 consecutive bars < 80%.

Key architectural invariants:
  1. Brain primacy — regime NEVER silences brains
  2. Continuous modulation — sigmoid replaces if/else
  3. Macro-first hierarchy — D1/H4 set strategic bias, M5 provides tactical tuning
  4. TrendDetector.regime (Kalman + Hurst + VR fusion) is the trend ground truth
  5. OU regime: Hurst × RV 2D plane with Schmitt trigger hysteresis
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from core.execution.trend_detector import TrendDetector

# ═══════════════════════════════════════════════════════════════════════════
# Continuous Regime Modulation
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RegimeModulation:
    """Continuous modulation parameters computed from the 4D feature space.

    All values are unitless multipliers applied to strategy defaults.
    """

    position_size_mult: float  # [0.25, 1.0]  applied to base volume
    min_brains_mult: float  # [1.0, 2.0]   applied to min_valid_brains
    confidence_threshold: float  # [0.30, 0.55] absolute threshold override
    sl_tightness: float  # [0.55, 1.0]  applied to SL distance
    strategy_activation: str  # "full" | "reduced" | "shadow"
    trend_conviction: float  # [0, 1] combined Kalman × Hurst conviction
    vol_stress: float  # [0, 1] smooth vol stress signal
    mr_risk: float  # [0, 1] mean-reversion risk


# ═══════════════════════════════════════════════════════════════════════════════
# Strictness ordering for minimum-privilege gate fusion
# ═══════════════════════════════════════════════════════════════════════════════

_STRICTNESS: dict[str, int] = {"full": 0, "reduced": 1, "shadow": 2, "false": 3, "off": 3}


def get_stricter_mode(base_mode: str, global_mode: str) -> str:
    """Return the stricter of two strategy-activation modes.

    Minimum-privilege principle: the discrete per-strategy ``regime_map``
    (physics-derived: trend-following vs. mean-reversion hardware guard)
    sets a floor that the continuous modulation can only tighten, never relax.

    If the base (discrete) mode is ``"false"`` or ``"shadow"`` — the strategy
    has no business trading in this regime — the continuous global modulation
    is ignored entirely and the base mode is returned.
    """
    base_strict = _STRICTNESS.get(base_mode, 1)  # unknown → "reduced"
    global_strict = _STRICTNESS.get(global_mode, 1)
    if base_strict >= _STRICTNESS["shadow"]:
        return base_mode  # hardware lock — continuous modulation cannot override
    if global_strict > base_strict:
        return global_mode
    return base_mode


def compute_continuous_regime_modulation(
    trend_strength: float,  # Kalman |velocity| / uncertainty  [0, 1]
    vol_pct: float,  # ATR rolling percentile           [0, 1]
    hurst: float,  # Hurst exponent                   [0, 1]
    vr_z: float,  # Lo-MacKinlay VR Z-score          ℝ
    *,
    macro_regime: str = "mixed",  # "risk_on" | "risk_off" | "mixed"
) -> RegimeModulation:
    """Compute continuous regime modulation from 4D feature inputs.

    No discrete thresholds.  No if/else branching on regime labels.
    Every output is a smooth function of the input features.

    Design rationale:
      - sigmoid(k=15) creates a ~0.05-wide transition zone around percentile
        thresholds, eliminating boundary flicker
      - trend_conviction = Kalman_strength × (1 − Hurst_ambiguity)^0.5
        so Hurst confirming trend → conviction high, Hurst contradicting → low
      - interaction term: trend × vol cross-effect explicitly modeled
        (strong trend + high vol = trend confirmed, not double penalised)
      - mean-reversion risk: Hurst < 0.5 + VR_z < -1.65 → OU is dangerous
    """
    # ── Clamp inputs to safe ranges ──
    trend_strength = max(0.0, min(1.0, trend_strength))
    vol_pct = max(0.0, min(1.0, vol_pct))
    hurst = max(0.01, min(0.99, hurst))
    vr_z = max(-5.0, min(5.0, vr_z))

    # ── Trend conviction: Kalman × Hurst cross-validation ──
    # Hurst near 0.5 → high ambiguity → penalise trend conviction
    # Hurst > 0.55 (trending) or < 0.45 (mean-reverting) → low ambiguity
    hurst_ambiguity = 1.0 - abs(hurst - 0.5) * 2.0  # [0, 1], 0=clear, 1=ambiguous
    hurst_ambiguity = max(0.0, min(1.0, hurst_ambiguity))
    trend_conviction = trend_strength * (1.0 - hurst_ambiguity) ** 0.5
    trend_conviction = max(0.0, min(1.0, trend_conviction))

    # ── Vol stress: smooth sigmoid around high-vol percentile ──
    # k=15 gives transition from ~0.05 at pct=0.70 to ~0.95 at pct=0.90
    vol_stress = 1.0 / (1.0 + math.exp(-15 * (vol_pct - 0.80)))
    vol_stress = max(0.0, min(1.0, vol_stress))

    # ── Mean-reversion risk: Hurst + VR both signal anti-persistence ──
    # Higher when BOTH Hurst < 0.5 (anti-persistent) AND VR_z < -1.65 (significant MR)
    hurst_mr_signal = max(0.0, (0.5 - hurst) * 2.0)  # [0, 1] when hurst < 0.5
    vr_mr_signal = 1.0 / (1.0 + math.exp(3 * (vr_z + 1.65)))  # sigmoid around -1.65
    mr_risk = hurst_mr_signal * vr_mr_signal
    mr_risk = max(0.0, min(1.0, mr_risk))

    # ── Position size: interaction of trend conviction and vol stress ──
    # Independent term: high vol stress → reduce size, high trend conviction → restore
    independent = 1.0 - 0.75 * vol_stress * (1.0 - trend_conviction)
    # Interaction term: trend × vol synergy
    interaction = (
        trend_conviction * (1.0 - vol_stress) * 0.6  # high trend, low vol = best
        + (1.0 - trend_conviction) * vol_stress * 0.3  # low trend, high vol = bad
        + trend_conviction * vol_stress * 0.1  # high trend, high vol = OK (trend confirmed)
    )
    pos_size = 0.55 * independent + 0.45 * interaction
    pos_size = max(0.25, min(1.0, pos_size))

    # ── min_brains_mult: more vol stress → need more brains agreeing ──
    # Reduced when macro regime is clear (risk_on/risk_off → fewer brains needed)
    macro_clarity = 0.0 if macro_regime in ("risk_on", "risk_off") else 0.3
    min_brains = (
        1.0 + 1.0 * vol_stress * (1.0 - trend_conviction * 0.5) - macro_clarity * vol_stress
    )
    min_brains = max(1.0, min(2.0, min_brains))

    # ── Confidence threshold: harder when vol high and trend unclear ──
    conf_thresh = 0.30 + 0.25 * vol_stress * (1.0 - trend_conviction)
    conf_thresh = max(0.30, min(0.55, conf_thresh))

    # ── SL tightness: tighten in high vol, relax when trend is clear ──
    sl_tight = 1.0 - 0.45 * vol_stress * (1.0 - trend_conviction * 0.6)
    sl_tight = max(0.55, min(1.0, sl_tight))

    # ── Strategy activation: continuous mapping ──────────────────────────
    # FIX-20260606-129: Global "shadow" (absolute trading ban) removed per
    # architect directive.  The continuous modulation must NOT issue
    # universal kill orders — vol-based trade restrictions belong in
    # per-strategy gates (e.g. ou_high_vol_blocked for OU strategies).
    # shadow_score is retained as a diagnostic for future per-gate consumption.
    #
    # Historical context: FIX-20260602-053 lowered trend_conviction floor
    # 0.30→0.15 for BTC compatibility, exposing cold-start shadow lock
    # (Kalman/Hurst not converged → trend_conviction≈0 → shadow_score>0.60
    # for 20-70 min post-restart).  See AUDIT_20260605_XAU_BTC_DIVERGENCE #2.
    shadow_score = vol_stress * (1.0 - trend_conviction)  # diagnostic only
    if vol_stress > 0.35 or trend_conviction < 0.15:
        activation = "reduced"
    else:
        activation = "full"

    return RegimeModulation(
        position_size_mult=round(pos_size, 4),
        min_brains_mult=round(min_brains, 4),
        confidence_threshold=round(conf_thresh, 4),
        sl_tightness=round(sl_tight, 4),
        strategy_activation=activation,
        trend_conviction=round(trend_conviction, 4),
        vol_stress=round(vol_stress, 4),
        mr_risk=round(mr_risk, 4),
    )


# ═══════════════════════════════════════════════════════════════════════════
# OU 2D Regime — Hurst × RV plane with Schmitt trigger
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class OURegime2D:
    """Result of 2D regime matrix lookup for OU strategies.

    X-axis (structure): H1 Hurst exponent
    Y-axis (energy):    M5 12-bar realized volatility percentile (vs 500-bar history)
    """

    regime_factor: float  # [0.0, 1.0] position size multiplier for OU
    strategy_activation: str  # "full" | "reduced" | "off"
    force_off: bool  # Schmitt trigger FORCE-OFF active
    cooldown_counter: int  # bars remaining before FORCE-OFF can lift
    hurst_zone: str  # "ranging" | "mild" | "trending"
    rv_zone: str  # "normal" | "elevated" | "extreme"


# 2D lookup table: rows = RV percentile bands, cols = Hurst zones
# regime_factor (upper-left is best: Ranging + Normal RV = full power)
_OU_REGIME_MATRIX: dict[tuple[str, str], tuple[float, str]] = {
    # ── RV < 80% (Normal) ──
    ("ranging", "normal"): (1.0, "full"),
    ("mild", "normal"): (0.5, "reduced"),
    ("trending", "normal"): (0.35, "reduced"),
    # ── RV 80-95% (Elevated) ──
    ("ranging", "elevated"): (0.5, "reduced"),
    ("mild", "elevated"): (0.5, "reduced"),
    ("trending", "elevated"): (0.25, "reduced"),
    # ── RV ≥ 95% (Extreme) — absolute firewall, all cells FORCE-OFF ──
    ("ranging", "extreme"): (0.0, "off"),
    ("mild", "extreme"): (0.0, "off"),
    ("trending", "extreme"): (0.0, "off"),
}


def _classify_hurst_zone(hurst: float) -> str:
    if hurst < 0.4:
        return "ranging"
    elif hurst > 0.6:
        return "trending"
    return "mild"


def _classify_rv_zone(rv_pct: float) -> str:
    if rv_pct >= 0.95:
        return "extreme"
    elif rv_pct >= 0.80:
        return "elevated"
    return "normal"


# ═══════════════════════════════════════════════════════════════════════════
# RegimeGate — multi-TF trend classifier + strategy gating
# ═══════════════════════════════════════════════════════════════════════════


class RegimeGate:
    """M5 + H1 + H4 + D1 trend classifier for strategy gating.

    Uses KalmanTrendFilter for real-time adaptive trend tracking,
    Hurst + Variance Ratio for statistical persistence confirmation.

    v3.0: classify() no longer uses vol_regime for regime classification.
    Strategy activation is computed by compute_continuous_regime_modulation()
    from the 4D feature space.  The regime_map is retained as a fallback
    for backward-compatible get_strategy_mode(), with all "off" → "shadow".
    """

    def __init__(
        self,
        *,
        adx_trending_threshold: float = 30.0,
        adx_mild_threshold: float = 20.0,
        atr_high_vol_pct: float = 0.80,
        atr_low_vol_pct: float = 0.20,
        regime_map: dict[str, dict[str, Any]] | None = None,
        adx_period: int = 14,
        di_period: int = 14,
    ):
        self.adx_trending = adx_trending_threshold
        self.adx_mild = adx_mild_threshold
        self.atr_high_pct = atr_high_vol_pct
        self.atr_low_pct = atr_low_vol_pct

        # v3.0: "off" replaced with "shadow" — no strategy is ever fully silenced
        self.regime_map = regime_map or {
            "trending": {
                "barrier_12bar": "full",
                "micro_3bar": "reduced",
                "statarb_dynamic": "reduced",
            },
            "mild_trend": {
                "barrier_12bar": "full",
                "micro_3bar": "full",
                "statarb_dynamic": "reduced",
            },
            "ranging": {
                "barrier_12bar": "reduced",
                "micro_3bar": "reduced",
                "statarb_dynamic": "full",
            },
            "high_vol": {
                "barrier_12bar": "shadow",
                "micro_3bar": "full",
                "statarb_dynamic": "reduced",
            },
            "normal": {"barrier_12bar": "full", "micro_3bar": "full", "statarb_dynamic": "full"},
        }

        # Four independent TrendDetectors — M5/H1 for regime + counter-trend,
        # H4/D1 for macro regime classification and barrier alignment
        self._m5 = TrendDetector(initial_price=2000.0, stats_window=50)
        self._h1 = TrendDetector(initial_price=2000.0, stats_window=40)
        self._h4 = TrendDetector(initial_price=2000.0, stats_window=30)
        self._d1 = TrendDetector(initial_price=2000.0, stats_window=20)

        # Bar aggregation state: 48×M5 → 1×H4, 6×H4 → 1×D1
        self._m5_bar_count: int = 0
        self._h4_bar_count: int = 0
        self._h4_accum: list[float] = []

        # ── FIX-20260607-XXX: ATR anchoring for Kalman noise matrices ──
        # Eliminates magnitude hallucination: R=2.0 is designed for XAU at
        # $4,300 but applied to BTC at $61,000 — a 14,000× mismatch.  After
        # the first ATR_PERIOD M5 bars, compute ATR and re-anchor Q and R
        # to the asset's actual volatility.  One-shot, never repeats.
        self._kalman_anchored: bool = False
        self._atr_tr_buffer: list[float] = []  # rolling True Range values
        self._atr_prev_close: float = 0.0
        self._d1_accum: list[float] = []

        self._current_regime: str = "normal"
        self._macro_regime: str = "mixed"

        # OU 2D regime: Schmitt trigger state
        self._force_off: bool = False
        self._cooldown_counter: int = 0

        # RV percentile tracking: 500-bar rolling buffer of 12-bar realized vol
        self._rv_buffer: list[float] = []  # recent realized vol values (max 500)
        self._rv_buffer_max: int = 500
        self._m5_close_window: list[float] = []  # recent 12 M5 closes for RV calc

    @staticmethod
    def default_fail_closed() -> RegimeGate:
        """Return a RegimeGate with all strategies locked to "shadow".

        Blocks all new position entries while allowing Exit Manager to
        continue managing existing positions (stop-loss movement, take-profit,
        trailing stops).  Used when regime computation fails beyond the stale
        tolerance — fail-closed for entries, fail-open for exits.
        """
        all_shadow: dict[str, dict[str, str]] = {}
        for regime in ("trending", "mild_trend", "ranging", "high_vol", "normal"):
            all_shadow[regime] = {
                "barrier_12bar": "shadow",
                "micro_3bar": "shadow",
                "statarb_dynamic": "shadow",
            }
        return RegimeGate(regime_map=all_shadow)

    # ── Properties ──

    @property
    def adx(self) -> float:
        return round(self._m5.trend_strength * 100, 1)

    @property
    def di_plus(self) -> float:
        v = self._m5.velocity_scaled
        return round(max(0.0, v * 25 + 25), 1)

    @property
    def di_minus(self) -> float:
        v = self._m5.velocity_scaled
        return round(max(0.0, -v * 25 + 25), 1)

    @property
    def current_regime(self) -> str:
        return self._current_regime

    @property
    def h1_trend_direction(self) -> str:
        return self._h1.trend_direction

    @property
    def h1_trend_strength(self) -> float:
        return self._h1.trend_strength

    @property
    def h1_adx(self) -> float:
        return round(self._h1.trend_strength * 100, 1)

    @property
    def h4_trend_direction(self) -> str:
        return self._h4.trend_direction

    @property
    def h4_trend_strength(self) -> float:
        return self._h4.trend_strength

    @property
    def h4_is_ready(self) -> bool:
        return self._h4.is_ready

    @property
    def d1_trend_direction(self) -> str:
        return self._d1.trend_direction

    @property
    def d1_trend_strength(self) -> float:
        return self._d1.trend_strength

    @property
    def d1_is_ready(self) -> bool:
        return self._d1.is_ready

    @property
    def macro_regime(self) -> str:
        return self._macro_regime

    @property
    def m5_trend_strength(self) -> float:
        return self._m5.trend_strength

    @property
    def m5_hurst(self) -> float:
        return self._m5.hurst

    @property
    def m5_vr_z(self) -> float:
        return self._m5._vr_z

    @property
    def m5_regime(self) -> str:
        """TrendDetector fused regime: strong_trend/weak_trend/mean_reverting/random_walk."""
        return self._m5.regime

    # ── Bar ingestion ──

    def feed_m5_bar(self, high: float, low: float, close: float) -> None:
        self._m5.update(close)
        self._h4_accum.append(close)
        self._m5_bar_count += 1

        # ── FIX-20260607-XXX: ATR anchoring trigger ──
        # Accumulate True Range values on every M5 bar.  On the ATR_PERIOD-th
        # bar (14), compute the initial ATR and anchor all four Kalman filters
        # to the asset's actual volatility.  One-shot — _kalman_anchored
        # prevents re-anchoring on subsequent bars.
        if not self._kalman_anchored:
            if self._atr_prev_close > 0:
                tr = max(
                    high - low, abs(high - self._atr_prev_close), abs(low - self._atr_prev_close)
                )
                self._atr_tr_buffer.append(tr)
            self._atr_prev_close = close
            if len(self._atr_tr_buffer) >= 14:
                atr_val = sum(self._atr_tr_buffer[-14:]) / 14.0
                self._m5.anchor_kalman_to_atr(atr_val)
                self._h1.anchor_kalman_to_atr(atr_val)
                self._h4.anchor_kalman_to_atr(atr_val)
                self._d1.anchor_kalman_to_atr(atr_val)
                self._kalman_anchored = True
                import logging

                _log = logging.getLogger(__name__)
                _log.info(
                    "[RegimeGate] Kalman Anchored: ATR=%.2f, " "New_R=%.2f, New_Q_level=%.2f",
                    atr_val,
                    (0.5 * atr_val) ** 2,
                    (0.1 * atr_val) ** 2,
                )

        # RV tracking: maintain 12-bar close window for realized vol
        self._m5_close_window.append(close)
        if len(self._m5_close_window) > 12:
            self._m5_close_window.pop(0)
        if len(self._m5_close_window) == 12:
            rv = self._compute_12bar_rv()
            self._rv_buffer.append(rv)
            if len(self._rv_buffer) > self._rv_buffer_max:
                self._rv_buffer.pop(0)

        if self._m5_bar_count >= 48:
            h4_close = self._h4_accum[-1]
            self._h4.update(h4_close)
            self._d1_accum.append(h4_close)
            self._h4_bar_count += 1
            self._m5_bar_count = 0
            self._h4_accum.clear()

            if self._h4_bar_count >= 6:
                d1_close = self._d1_accum[-1]
                self._d1.update(d1_close)
                self._h4_bar_count = 0
                self._d1_accum.clear()

    def feed_h1_bar(self, high: float, low: float, close: float) -> None:
        self._h1.update(close)

    @staticmethod
    def _get_field(bar: Any, field: str, fallback_field: str) -> float:
        """Extract a field from a bar, supporting both dict and numpy.void."""
        try:
            return float(bar[field])
        except (TypeError, KeyError, ValueError, IndexError):
            try:
                return float(bar[fallback_field])
            except (TypeError, KeyError, ValueError, IndexError):
                return 0.0

    def feed_m5_bars_batch(self, bars: list[Any]) -> None:
        for b in bars:
            try:
                high = self._get_field(b, "high", "close")
                low = self._get_field(b, "low", "close")
                close = float(b["close"]) if not hasattr(b, "get") else float(b["close"])
            except (TypeError, KeyError, ValueError):
                continue
            self.feed_m5_bar(high=high, low=low, close=close)

    def feed_h1_bars_batch(self, bars: list[Any]) -> None:
        for b in bars:
            try:
                close = float(b["close"])
            except (TypeError, KeyError, ValueError):
                continue
            self._h1.update(close)

    def feed_h4_bars_batch(self, bars: list[Any]) -> None:
        """FIX-20260603-063: bootstrap H4 TrendDetector with historical bars."""
        for b in bars:
            try:
                close = float(b["close"])
            except (TypeError, KeyError, ValueError):
                continue
            self._h4.update(close)

    def feed_d1_bars_batch(self, bars: list[Any]) -> None:
        """FIX-20260603-063: bootstrap D1 TrendDetector with historical bars."""
        for b in bars:
            try:
                close = float(b["close"])
            except (TypeError, KeyError, ValueError):
                continue
            self._d1.update(close)

    @property
    def is_ready(self) -> bool:
        return self._m5.is_ready

    @property
    def h1_is_ready(self) -> bool:
        return self._h1.is_ready

    @property
    def h1_hurst(self) -> float:
        return self._h1.hurst

    @property
    def rv_percentile(self) -> float:
        """Current M5 12-bar RV percentile rank (requires ≥ 500 bars warmed up)."""
        return self._compute_rv_percentile()

    # ── OU 2D regime ──

    def check_ou_regime(
        self,
        h1_hurst: float | None = None,
        m5_rv_pct: float | None = None,
    ) -> OURegime2D:
        """Evaluate OU strategy activation from the 2D regime matrix.

        X-axis (structure): H1 Hurst exponent
        Y-axis (energy):    M5 12-bar RV percentile

        Schmitt trigger hysteresis: FORCE-OFF at RV ≥ 95%, restore only
        after 3 consecutive bars with RV < 80%.  This prevents flapping
        at the threshold edge during volatility spike-decay cycles.

        Returns OURegime2D with regime_factor and strategy_activation.
        """
        hurst = h1_hurst if h1_hurst is not None else self._h1.hurst
        rv_pct = m5_rv_pct if m5_rv_pct is not None else self._compute_rv_percentile()

        # ── Schmitt trigger state machine ──
        if rv_pct >= 0.95:
            # Immediate FORCE-OFF — volatility spike detected
            self._force_off = True
            self._cooldown_counter = 0
        elif self._force_off:
            if rv_pct < 0.80:
                self._cooldown_counter += 1
                if self._cooldown_counter >= 3:
                    self._force_off = False  # volatility genuinely receded
                    self._cooldown_counter = 0
            else:
                self._cooldown_counter = 0  # bounced back above 80% → reset cooldown

        hurst_zone = _classify_hurst_zone(hurst)
        rv_zone = _classify_rv_zone(rv_pct)

        # ── 2D matrix lookup ──
        if self._force_off:
            # RV ≥ 95% at some point → absolute firewall across all cells
            regime_factor = 0.0
            strategy_activation = "off"
        else:
            regime_factor, strategy_activation = _OU_REGIME_MATRIX.get(
                (hurst_zone, rv_zone), (0.5, "reduced")
            )

        return OURegime2D(
            regime_factor=round(regime_factor, 4),
            strategy_activation=strategy_activation,
            force_off=self._force_off,
            cooldown_counter=self._cooldown_counter,
            hurst_zone=hurst_zone,
            rv_zone=rv_zone,
        )

    def get_ou_regime_factor(
        self,
        h1_hurst: float | None = None,
        m5_rv_pct: float | None = None,
    ) -> float:
        """Convenience: return just the OU regime factor [0, 1]."""
        return self.check_ou_regime(h1_hurst, m5_rv_pct).regime_factor

    def _compute_12bar_rv(self) -> float:
        """Compute 12-bar M5 realized volatility (annualized std of log returns)."""
        if len(self._m5_close_window) < 2:
            return 0.0
        closes = self._m5_close_window
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if not log_returns:
            return 0.0
        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r) ** 2 for r in log_returns) / len(log_returns)
        return math.sqrt(variance) if variance > 0 else 0.0

    def _compute_rv_percentile(self) -> float:
        """Compute current 12-bar RV's percentile rank in the 500-bar buffer.

        Uses causal rolling window — only past values, no future leak.
        Returns 0.5 when buffer is not yet full (< 500 values).
        """
        if len(self._rv_buffer) < 12:
            return 0.5
        current_rv = self._rv_buffer[-1]
        sorted_rv = sorted(self._rv_buffer)
        rank = sum(1.0 for v in sorted_rv if v < current_rv)
        ties = sum(1.0 for v in sorted_rv if v == current_rv) - 1.0
        return float((rank + 0.5 * ties) / len(self._rv_buffer))

    # ── Regime classification ──

    def classify(
        self,
        atr_value: float,
        atr_percentile: float | None = None,
        *,
        vol_pct: float = 0.5,  # from RegimeDetector (rolling percentile)
        vol_regime: str = "normal",  # kept for backward compat + SL/TP
    ) -> dict[str, Any]:
        """Classify current market regime across M5/H1/H4/D1.

        v3.0: vol_regime does NOT override classification.  Trend regime is
        determined by Kalman + Hurst + VR fusion (TrendDetector.regime).
        The continuous modulation output is included in the result dict
        so callers can use it for strategy-level adjustments.

        Returns dict with:
          - regime: market regime label (backward-compatible)
          - modulation: RegimeModulation dataclass (new)
          - trend features, primary trend, macro regime, strategy gates
        """
        self._m5.update_stats()
        self._h1.update_stats()
        self._h4.update_stats()
        self._d1.update_stats()

        m5_dir = self._m5.direction
        m5_strength = self._m5.trend_strength
        m5_hurst = self._m5.hurst
        m5_vr_z = self._m5._vr_z
        m5_fused_regime = self._m5.regime  # Kalman + Hurst + VR fusion

        # ── Trend regime from fused TrendDetector output ──
        # v3.0: no vol_regime override.  Trend classification is pure.
        if m5_fused_regime == "strong_trend":
            market_regime = "trending"
        elif m5_fused_regime == "weak_trend":
            market_regime = "mild_trend"
        elif m5_fused_regime == "mean_reverting":
            market_regime = "ranging"
        else:
            # random_walk or unclassified → default to normal
            market_regime = "normal"

        self._current_regime = market_regime

        # M5 trend direction
        m5_trend = m5_dir

        # H1 trend
        h1_dir = self._h1.trend_direction
        h1_strength = self._h1.trend_strength

        # H4 trend
        h4_dir = self._h4.trend_direction
        h4_strength = self._h4.trend_strength

        # D1 trend
        d1_dir = self._d1.trend_direction
        d1_strength = self._d1.trend_strength

        # Primary trend: H4 > H1 > M5
        if h4_dir != "neutral" and self._h4.is_ready:
            primary_trend = h4_dir
            primary_trend_source = "h4"
        elif h1_dir != "neutral":
            primary_trend = h1_dir
            primary_trend_source = "h1"
        else:
            primary_trend = m5_trend
            primary_trend_source = "m5"

        # Macro regime: D1 × H4 direction agreement
        if self._d1.is_ready and self._h4.is_ready:
            if d1_dir == "long" and h4_dir == "long":
                self._macro_regime = "risk_on"
            elif d1_dir == "short" and h4_dir == "short":
                self._macro_regime = "risk_off"
            else:
                self._macro_regime = "mixed"
        elif self._h4.is_ready:
            self._macro_regime = (
                "risk_on" if h4_dir == "long" else ("risk_off" if h4_dir == "short" else "mixed")
            )
        else:
            self._macro_regime = "mixed"

        # ── Continuous regime modulation (new) ──
        _vpct = (
            vol_pct
            if vol_pct is not None
            else atr_percentile
            if atr_percentile is not None
            else 0.5
        )
        # FIX-20260604-086: Macro-Anchor Fusion — strip M5 micro-noise from
        # global activation.  M5 trend_conviction → 0 in normal choppy markets
        # → shadow_score > 0.60 → permanent global shadow deadlock.
        # Max-pool H1 (primary swing anchor) and H4 (macro anchor at 0.7x)
        # so the system activates when ANY higher timeframe shows conviction.
        _macro_trend_conviction = max(h1_strength, h4_strength * 0.7)
        modulation = compute_continuous_regime_modulation(
            trend_strength=_macro_trend_conviction,
            vol_pct=_vpct,
            hurst=m5_hurst,
            vr_z=m5_vr_z,
            macro_regime=self._macro_regime,
        )

        # ── OU 2D regime (new v3.1) ──
        ou_regime = self.check_ou_regime()

        # ── Strategy gates: discrete regime_map provides per-strategy differentiation;
        #     continuous modulation (position_size_mult, confidence_threshold, etc.)
        #     is applied on top via strategy.evaluate() ──
        gates = {}
        _all_strategies: set[str] = set()
        for _rmap in self.regime_map.values():
            _all_strategies.update(_rmap.keys())
        for sname in sorted(_all_strategies):
            gates[sname] = self.get_strategy_mode(sname)

        return {
            "regime": market_regime,
            "adx": self.adx,
            "di_plus": self.di_plus,
            "di_minus": self.di_minus,
            "trend_direction": m5_trend,
            "h1_trend_direction": h1_dir,
            "h1_trend_strength": round(h1_strength, 4),
            "h1_adx": self.h1_adx,
            "h1_ema_slope": round(self._h1.velocity_scaled / 10000, 6),
            "h4_trend_direction": h4_dir,
            "h4_trend_strength": round(h4_strength, 4),
            "h4_is_ready": self._h4.is_ready,
            "d1_trend_direction": d1_dir,
            "d1_trend_strength": round(d1_strength, 4),
            "d1_is_ready": self._d1.is_ready,
            "macro_regime": self._macro_regime,
            "primary_trend": primary_trend,
            "primary_trend_source": primary_trend_source,
            "vol_regime": vol_regime,
            "vol_pct": _vpct,
            "atr": round(atr_value, 4),
            "strategy_gates": gates,
            # v3.0: continuous modulation for strategy-line consumption
            "modulation": modulation,
            "m5_fused_regime": m5_fused_regime,
            "m5_hurst": round(m5_hurst, 4),
            "m5_vr_z": round(m5_vr_z, 4),
            # v3.1: OU 2D regime
            "ou_regime_factor": ou_regime.regime_factor,
            "ou_force_off": ou_regime.force_off,
            "ou_hurst_zone": ou_regime.hurst_zone,
            "ou_rv_zone": ou_regime.rv_zone,
        }

    def get_strategy_mode(self, strategy_name: str) -> str:
        """Return active mode: "full" | "reduced" | "shadow" (never "off").

        Handles YAML booleans: ``false`` (Python ``False``) → ``"shadow"``
        (hard lock — strategy has no business in this regime), ``true``
        (Python ``True``) → ``"full"``.
        """
        gates = self.regime_map.get(self._current_regime, {})
        mode = gates.get(strategy_name, "reduced")
        if isinstance(mode, bool):
            return "full" if mode else "shadow"
        if mode == "off" or mode == "false":
            return "shadow"
        if isinstance(mode, str):
            return mode
        return "reduced"

    def is_counter_trend(self, trade_direction: str) -> bool:
        if trade_direction == "neutral":
            return False

        if self._h4.is_ready:
            h4_dir = self._h4.trend_direction
            if h4_dir != "neutral":
                return trade_direction != h4_dir

        h1_dir = self._h1.trend_direction
        if h1_dir != "neutral":
            return trade_direction != h1_dir

        m5_dir = self._m5.direction
        if m5_dir != "neutral":
            return trade_direction != m5_dir

        return False

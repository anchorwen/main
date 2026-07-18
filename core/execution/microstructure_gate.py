"""Microstructure quality gate — tick-level liquidity defence.

FIX-20260718-004 (DQAF-20260718-004): New pre-trade gate consuming gate-only
microstructure features (quote_intensity_zscore, buy_pressure_20,
arrival_rate_5s, spread_toxicity) computed by MicrostructureFeatureComputer.

Architecture (投委会修正):
  - Strict Domain Isolation: gate features flow through micro_feature_dict
    only — NEVER assembled into the 49-dim ML feature vector.
  - Stateful Rolling Buffer: features are computed from memory-resident
    deques, NOT from historical MT5 tick queries (I/O death trap avoidance).
  - Fail-Open: empty dict or missing keys → gate passes (no block).
    Micro features are best-effort — if MT5 tick query fails, trading
    continues with other gates still active.

Pure function contract: zero I/O, zero global state, same input → same output.
Pattern: follows ``trend_volume_guard.py`` (Strangler Fig #16).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MicrostructureResult:
    """Output of :meth:`MicrostructureGate.evaluate`.

    Attributes:
        blocked: If True, the trade must be rejected (hard block).
        conf_mult: Confidence multiplier to apply (1.0 = no change).
        reason: Human-readable reason string for logging/golden_master.
            Empty string when gate passes without modification.
    """

    blocked: bool = False
    conf_mult: float = 1.0
    reason: str = ""


class MicrostructureGate:
    """Pre-trade microstructure quality gate.

    Consumes gate-only microstructure features from the live feature pipeline
    and applies configurable thresholds to block or penalise trades during
    adverse microstructure conditions.

    Usage::

        gate = MicrostructureGate()
        result = gate.evaluate(micro_feature_dict, direction="short", confidence=0.65)
        if result.blocked:
            return reject_decision  # extreme liquidity shock
        confidence *= result.conf_mult  # apply penalise multiplier

    Thresholds (calibrated from 42-day XAU tick dataset, Jul 2026):

    ======= ===== =================================================== ===========
    Feature  Tier  Condition                                            Action
    ======= ===== =================================================== ===========
    q_i_z    1    abs(quote_intensity_zscore) > 3.5                   BLOCK
    q_i_z    2    abs(quote_intensity_zscore) > 2.5                   conf × 0.75
    bp_20    3    direction=LONG & buy_pressure_20 < 0.35             conf × 0.85
    bp_20    3    direction=SHORT & buy_pressure_20 > 0.65            conf × 0.85
    s_tox    4    spread_toxicity > 1.05                              conf × 0.90
    ======= ===== =================================================== ===========
    """

    # ── Default thresholds (calibrated from 42-day XAU tick dataset) ──────
    # Overridable per config; these are the production defaults.

    QZ_BLOCK: float = 3.5  # |z| > 3.5 → extreme liquidity shock → BLOCK
    QZ_PENALISE: float = 2.5  # |z| > 2.5 → elevated intensity → penalise
    QZ_CONF_MULT: float = 0.75  # confidence multiplier for QZ penalise

    BP_LONG_FLOOR: float = 0.35  # buy_pressure < 0.35 → sell pressure vs LONG
    BP_SHORT_CEIL: float = 0.65  # buy_pressure > 0.65 → buy pressure vs SHORT
    BP_CONF_MULT: float = 0.85  # confidence multiplier for BP penalise

    SPREAD_TOX_THRESHOLD: float = 1.05  # spread_toxicity > 1.05 → widening spread
    SPREAD_TOX_CONF_MULT: float = 0.90  # confidence multiplier for spread toxicity

    def __init__(
        self,
        *,
        qz_block: float | None = None,
        qz_penalise: float | None = None,
        qz_conf_mult: float | None = None,
        bp_long_floor: float | None = None,
        bp_short_ceil: float | None = None,
        bp_conf_mult: float | None = None,
        spread_tox_threshold: float | None = None,
        spread_tox_conf_mult: float | None = None,
    ):
        """Initialise with optional threshold overrides.

        All parameters are keyword-only.  Omitted values use calibrated defaults.
        """
        self.qz_block = qz_block if qz_block is not None else self.QZ_BLOCK
        self.qz_penalise = qz_penalise if qz_penalise is not None else self.QZ_PENALISE
        self.qz_conf_mult = qz_conf_mult if qz_conf_mult is not None else self.QZ_CONF_MULT
        self.bp_long_floor = bp_long_floor if bp_long_floor is not None else self.BP_LONG_FLOOR
        self.bp_short_ceil = bp_short_ceil if bp_short_ceil is not None else self.BP_SHORT_CEIL
        self.bp_conf_mult = bp_conf_mult if bp_conf_mult is not None else self.BP_CONF_MULT
        self.spread_tox_threshold = (
            spread_tox_threshold if spread_tox_threshold is not None else self.SPREAD_TOX_THRESHOLD
        )
        self.spread_tox_conf_mult = (
            spread_tox_conf_mult if spread_tox_conf_mult is not None else self.SPREAD_TOX_CONF_MULT
        )

    def evaluate(
        self,
        micro_feature_dict: dict[str, float] | None,
        direction: str,
        confidence: float,
    ) -> MicrostructureResult:
        """Evaluate microstructure quality for a trade signal.

        Args:
            micro_feature_dict: Dict of 13 micro features (9 ML + 4 gate-only).
                May be None or empty — gate passes (fail-open).
            direction: Trade direction ("long", "short", "neutral").
            confidence: Current trade confidence (for context, not modified here;
                the caller multiplies by ``conf_mult``).

        Returns:
            :class:`MicrostructureResult` with block/conf_mult/reason.
        """
        # ── Fail-open: no micro features → gate passes ──
        if not micro_feature_dict:
            return MicrostructureResult()

        # ── Extract gate-only features with safe defaults ──
        qz = float(_safe_get(micro_feature_dict, "quote_intensity_zscore", 0.0))
        bp = float(_safe_get(micro_feature_dict, "buy_pressure_20", 0.5))
        st = float(_safe_get(micro_feature_dict, "spread_toxicity", 1.0))

        # ── Tier 1: Extreme liquidity shock → HARD BLOCK ──
        if abs(qz) > self.qz_block:
            return MicrostructureResult(
                blocked=True,
                reason=f"micro_block:qz_abs={abs(qz):.1f}>{self.qz_block}",
            )

        # ── Tier 2-4: progressive confidence penalty ──
        conf_mult = 1.0

        # Tier 2: Elevated quote intensity → penalise
        if abs(qz) > self.qz_penalise:
            conf_mult *= self.qz_conf_mult

        # Tier 3: Directional buy pressure mismatch → penalise
        if direction == "long" and bp < self.bp_long_floor:
            conf_mult *= self.bp_conf_mult
        elif direction == "short" and bp > self.bp_short_ceil:
            conf_mult *= self.bp_conf_mult

        # Tier 4: Spread toxicity (widening spread vs baseline) → soft penalise
        if st > self.spread_tox_threshold:
            conf_mult *= self.spread_tox_conf_mult

        reason = ""
        if conf_mult < 1.0:
            reason = (
                f"micro_penalise:conf_mult={conf_mult:.2f}" f"_qz={qz:.1f}_bp={bp:.2f}_st={st:.2f}"
            )

        return MicrostructureResult(conf_mult=conf_mult, reason=reason)


def _safe_get(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    """Extract a float from dict, returning default for missing/None/NaN keys."""
    try:
        val = d.get(key)
        if val is None:
            return default
        f = float(val)
        if f != f:  # NaN check (NaN != NaN is True)
            return default
        return f
    except (ValueError, TypeError):
        return default

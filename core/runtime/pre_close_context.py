"""Pre-close context dataclass — immutable snapshot threaded through exit pipeline.

Architecture (Institutional Risk Override Pattern):
  - Pre-computed multiplier matrix built ONCE at cycle top (_check_pre_close)
  - Downstream functions apply pure multiplication — zero branching in hot path
  - Normal trading: all multipliers = 1.0 → zero overhead, zero side effects

Phase mapping:
  Phase 1 (tighten):   T-60 → T-30  — exit tightening only, new positions still allowed
  Phase 2 (aggressive): T-30 → T-5   — no new positions, tightest exits, TP disabled
  Phase 3 (flatten):   T-5  → T-0   — hard flatten (handled by live_cycle.py, not this ctx)

CRITICAL CORRECTION 1 (DQAF-20260727-001):
  tp_mult=0.0 replaced with disable_dynamic_tp: bool.  Setting tp_mult=0.0 causes
  tp_price = entry_price + 0 → MT5 Error 10016 (Invalid Stops) storm.
  Boolean truncation: compute_trail_tp returns None immediately, keeping existing TP
  unchanged.  Position termination is delegated to the rapidly-tightening trail SL.

CRITICAL CORRECTION 2 (DQAF-20260727-001):
  Absolute Time Cap overrides all timeframe multipliers.  H4 hesitation=240 bars
  (20h) ÷ 2.0 = 120 bars (10h) > 6 bars remaining → placebo.  max_bars_left from
  minutes_to_close / 5 enforces an absolute ceiling at 50% of remaining bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PreCloseContext:
    """Immutable snapshot of pre-close state for one cycle.

    All adjustment fields are pre-computed by from_calendar_result().
    Downstream code applies them via simple multiplication or boolean guard —
    no scattered if-minutes_to_close checks.
    """

    in_pre_close: bool = False
    phase: str = ""  # "tighten" | "aggressive" | "flatten" | ""
    minutes_to_close: float | None = None
    close_label: str = ""

    # ── Pre-computed adjustment multipliers ──
    trail_atr_mult: float = 1.0  # 1.0 → 0.75 → 0.50 (smaller = tighter trail)
    breakeven_mult: float = 1.0  # 1.0 → 0.67 → 0.50 (smaller = earlier BE trigger)
    hesitation_divisor: float = 1.0  # 1.0 → 1.5 → 2.0 (larger = faster time exit)
    bleed_bars_reduction: int = 0  # 0 → 1 → 2 (subtract from bleed window)

    # ── Boolean truncation (replaces tp_mult=0.0 — Error 10016 prevention) ──
    disable_dynamic_tp: bool = False  # True → compute_trail_tp returns None

    @classmethod
    def from_calendar_result(cls, result: dict[str, Any]) -> PreCloseContext:
        """Build from evaluate_pre_close() return dict.  Pure factory — no I/O.

        Returns a default (all-neutral) context when not in pre-close, so
        downstream code can unconditionally apply multipliers.
        """
        if not result.get("in_pre_close"):
            return cls()

        phase = result.get("phase", "")
        return cls(
            in_pre_close=True,
            phase=phase,
            minutes_to_close=_parse_minutes(result.get("minutes_to_close")),
            close_label=result.get("close_label", ""),
            trail_atr_mult=cls._trail_mult_for(phase),
            breakeven_mult=cls._breakeven_mult_for(phase),
            hesitation_divisor=cls._hesitation_divisor_for(phase),
            bleed_bars_reduction=cls._bleed_reduction_for(phase),
            disable_dynamic_tp=(phase == "aggressive"),
        )

    # ── Phase → multiplier mapping (pure functions, testable) ──

    @staticmethod
    def _trail_mult_for(phase: str) -> float:
        return {"tighten": 0.75, "aggressive": 0.50}.get(phase, 1.0)

    @staticmethod
    def _breakeven_mult_for(phase: str) -> float:
        return {"tighten": 0.67, "aggressive": 0.50}.get(phase, 1.0)

    @staticmethod
    def _hesitation_divisor_for(phase: str) -> float:
        return {"tighten": 1.5, "aggressive": 2.0}.get(phase, 1.0)

    @staticmethod
    def _bleed_reduction_for(phase: str) -> int:
        return {"tighten": 1, "aggressive": 2}.get(phase, 0)

    # ── Absolute Time Cap (CRITICAL CORRECTION 2) ──

    def compute_effective_hesitation(self, normal_cycles: int) -> int:
        """Return the effective hesitation cycle limit for this pre-close window.

        Step 1: Apply divisor (works well for M5/M15 strategies).
        Step 2: Apply absolute time cap — no hesitation can exceed 50% of
                remaining bars before flatten.  This is the ONLY mechanism
                that forces H1/H4 strategies (with 12×/48× scaled hesitation)
                to accelerate exit timing as the close approaches.
        """
        if normal_cycles <= 0:
            return 0

        # Step 1: divisor-based acceleration
        effective = max(4, int(normal_cycles / self.hesitation_divisor))

        # Step 2: absolute physical cap (overrides ALL timeframe multipliers)
        if self.minutes_to_close is not None and self.minutes_to_close > 0:
            max_bars_left = int(self.minutes_to_close / 5)  # M5 bars remaining
            absolute_cap = max(2, int(max_bars_left * 0.5))  # ≤50% of remaining time
            effective = min(effective, absolute_cap)

        return effective

    def compute_effective_bleed_bars(self, normal_bars: int) -> int:
        """Return the effective bleed bar window for this pre-close window.

        Bleed is typically 3-N bars.  Subtracting 1-2 bars from a 12-bar
        H4 bleed is a placebo — the absolute time cap fixes this.
        """
        if normal_bars <= 0:
            return 0

        effective = max(2, normal_bars - self.bleed_bars_reduction)

        # Absolute time cap: bleed window cannot exceed remaining bars
        if self.minutes_to_close is not None and self.minutes_to_close > 0:
            max_bars_left = int(self.minutes_to_close / 5)
            effective = min(effective, max(2, max_bars_left))

        return effective


def _parse_minutes(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None

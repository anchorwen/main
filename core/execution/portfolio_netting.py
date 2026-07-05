"""Portfolio Netting Gate — same-symbol exposure netting before MT5 dispatch.

Institutional mandate (DQAF-20260705-064): The portfolio is ONE. Multiple
brains vote independently, but the order gateway computes Net_Exposure =
Sum(LONG_power) - Sum(SHORT_power).  If |Net| / Gross falls below the
netting threshold, ALL trades on that symbol are physically SWALLOWED —
zero order reaches the broker.

Zero Exposure is a position.

Architecture
------------
Sits BETWEEN execution_queue.enqueue() and exec_queue.flush() in
live_cycle.py.  Operates on queued decisions grouped by symbol.

Algorithm (per symbol group)
----------------------------
1. Separate LONG / SHORT / NEUTRAL decisions
2. Compute power = vote_weight × confidence × volume per side
   (conviction-weighted exposure, not raw lot count)
3. Net = LONG_power - SHORT_power;  Gross = LONG_power + SHORT_power
4. If Gross == 0 → no directional signals → pass-through
5. If only one side → unanimous → pass-through
6. If |Net| / Gross < netting_threshold:
     swallow:  should_trade=False for ALL (institutional default)
     reduce:   swallow minority side only
     warn:     log only, dispatch all (probation telemetry)
7. Otherwise → swallow minority side, allow net-dominant

Golden Master Observability
---------------------------
The netting result is emitted as a JSON event (event="portfolio_netting")
in live_cycle.py.  The pre-netting golden_master record already captures
each strategy's raw decision.  Together they provide full audit trail:

  - golden_master.jsonl → what each strategy wanted
  - portfolio_netting event → what the netting gate decided
  - dispatch result → what actually reached MT5
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NettedDecision:
    """Immutable result of portfolio netting for one cycle."""

    action: str  # "dispatch" | "swallow" | "reduce" | "warn"
    direction: str  # "long" | "short" | "neutral"
    net_exposure_power: float  # conviction-weighted net exposure
    gross_exposure_power: float  # sum of |LONG_power| + |SHORT_power|
    net_ratio: float  # |net| / gross  (1.0 = unanimous, 0.0 = perfectly hedged)
    original_decisions: dict[str, str]  # strategy_name → direction
    swallowed: list[str]  # strategy names whose orders were cancelled
    survivors: list[str]  # strategy names allowed through
    reason: str


@dataclass
class PortfolioNettingConfig:
    """Configuration for the netting gate.

    Attributes:
        enabled: Master kill-switch for the netting gate.
        netting_threshold: If |net|/gross < this value, internal disagreement
            is high enough that both sides cancel → net PnL ≈ 0 minus costs.
            0.20 means: LONG=0.55, SHORT=0.45 → net=0.10 < 0.20 → swallow.
        mode:
            - "swallow": Cancel ALL orders when netted (institutional default).
            - "reduce":  Swallow minority side, dispatch net-dominant only.
            - "warn":    Log warning only, dispatch all (probation telemetry).
    """

    enabled: bool = True
    netting_threshold: float = 0.20
    mode: str = "swallow"

    def __post_init__(self) -> None:
        if self.mode not in ("swallow", "reduce", "warn"):
            raise ValueError(f"Invalid netting mode: {self.mode!r}")


class PortfolioNettingGate:
    """Cross-strategy netting gate for same-symbol trade decisions.

    Operates on queued decisions BEFORE MT5 dispatch.  Groups by symbol,
    computes net exposure, and swallows internal disagreements.

    Usage::

        gate = PortfolioNettingGate(PortfolioNettingConfig(
            enabled=True, netting_threshold=0.20, mode="swallow",
        ))
        netted, result = gate.net(
            queued_decisions=[
                ("btc_swing", short_decision),
                ("btc_swing_h1", long_decision),
            ],
            symbol="BTCUSDc",
        )
        # If both sides ~equal conviction → both decisions.should_trade = False
        # result.action == "swallow"
        # result.reason == "net_ratio_0.07_below_threshold_0.20"
    """

    def __init__(self, config: PortfolioNettingConfig | None = None) -> None:
        self.config = config or PortfolioNettingConfig()
        self._netting_count: int = 0
        self._swallow_count: int = 0
        self._reduce_count: int = 0

    # ── Public API ────────────────────────────────────────────────────────

    def net(
        self,
        queued_decisions: list[tuple[str, Any]],  # (strategy_name, decision)
        symbol: str = "",
    ) -> tuple[list[tuple[str, Any]], NettedDecision]:
        """Apply portfolio netting to queued trade decisions.

        Args:
            queued_decisions: List of (strategy_name, decision) where each
                decision has .direction, .confidence, .volume, .should_trade,
                and optionally .vote_weight.
            symbol: Trading symbol (for logging context, not grouping — caller
                should group by symbol before calling).

        Returns:
            (decisions, netted_decision).  On swallow/reduce, the affected
            decisions have should_trade=False and reason set to a netting
            tag.  The NettedDecision carries the full audit record.
        """
        if not self.config.enabled or not queued_decisions:
            return queued_decisions, self._passthrough(
                queued_decisions, "netting_disabled_or_empty"
            )

        # ── Phase 1: Classify by direction ──
        long_entries: list[tuple[str, Any, float]] = []  # (name, decision, power)
        short_entries: list[tuple[str, Any, float]] = []
        neutral_entries: list[tuple[str, Any]] = []

        for sname, decision in queued_decisions:
            if not getattr(decision, "should_trade", False):
                neutral_entries.append((sname, decision))
                continue

            direction = getattr(decision, "direction", "neutral")
            if direction not in ("long", "short"):
                neutral_entries.append((sname, decision))
                continue

            power = self._compute_power(decision)
            # ── Power=0 → effectively neutral (muted brain or zero confidence) ──
            if power <= 0.0:
                neutral_entries.append((sname, decision))
                continue
            if direction == "long":
                long_entries.append((sname, decision, power))
            else:
                short_entries.append((sname, decision, power))

        long_power = sum(p for _, _, p in long_entries)
        short_power = sum(p for _, _, p in short_entries)
        gross_power = long_power + short_power
        original = {s: getattr(d, "direction", "neutral") for s, d in queued_decisions}

        # ── Phase 2: No conflict → pass-through ──
        if gross_power == 0:
            # All neutral or should_trade=False — nothing to net
            return queued_decisions, NettedDecision(
                action="dispatch",
                direction="neutral",
                net_exposure_power=0.0,
                gross_exposure_power=0.0,
                net_ratio=0.0,
                original_decisions=original,
                swallowed=[],
                survivors=[s for s, _ in queued_decisions],
                reason="no_directional_signals",
            )

        if long_power == 0 or short_power == 0:
            # Unanimous direction — every active voter agrees
            dir_str = "long" if long_power > 0 else "short"
            return queued_decisions, NettedDecision(
                action="dispatch",
                direction=dir_str,
                net_exposure_power=gross_power,
                gross_exposure_power=gross_power,
                net_ratio=1.0,
                original_decisions=original,
                swallowed=[],
                survivors=[s for s, _ in queued_decisions],
                reason="unanimous_direction",
            )

        # ── Phase 3: Opposing directions exist — compute net ratio ──
        net_power = long_power - short_power
        net_ratio = abs(net_power) / gross_power
        self._netting_count += 1

        if net_ratio < self.config.netting_threshold:
            # ── Net near zero: internal disagreement ≈ broker profit ──
            return self._apply_netting(
                long_entries,
                short_entries,
                neutral_entries,
                queued_decisions,
                original,
                net_power,
                gross_power,
                net_ratio,
            )
        else:
            # ── Net one-sided: allow dominant, swallow minority ──
            return self._reduce_minority(
                long_entries,
                short_entries,
                neutral_entries,
                queued_decisions,
                original,
                net_power,
                gross_power,
                net_ratio,
            )

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def netting_count(self) -> int:
        """Total cycles where opposing directions were detected."""
        return self._netting_count

    @property
    def swallow_count(self) -> int:
        """Cycles where ALL orders were physically swallowed."""
        return self._swallow_count

    @property
    def reduce_count(self) -> int:
        """Cycles where minority side was swallowed, majority dispatched."""
        return self._reduce_count

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _compute_power(decision: Any) -> float:
        """Compute conviction-weighted exposure power for one decision.

        Power = vote_weight × confidence × volume

        This is NOT raw volume — it weights decisions by conviction.
        A high-confidence 0.01 lot carries more weight than a
        low-confidence 0.05 lot.
        """
        # Use sentinel pattern — 0.0 is a valid value (muted brain)
        _vw = getattr(decision, "vote_weight", None)
        vote_weight = float(_vw) if _vw is not None else 1.0
        _conf = getattr(decision, "confidence", None)
        confidence = float(_conf) if _conf is not None else 0.5
        _vol = getattr(decision, "volume", None)
        volume = float(_vol) if _vol is not None else 0.01
        return vote_weight * confidence * volume

    def _passthrough(
        self,
        queued_decisions: list[tuple[str, Any]],
        reason: str,
    ) -> NettedDecision:
        """Build a no-op NettedDecision."""
        return NettedDecision(
            action="dispatch",
            direction="neutral",
            net_exposure_power=0.0,
            gross_exposure_power=0.0,
            net_ratio=0.0,
            original_decisions={s: getattr(d, "direction", "neutral") for s, d in queued_decisions},
            swallowed=[],
            survivors=[s for s, _ in queued_decisions],
            reason=reason,
        )

    def _apply_netting(
        self,
        long_entries: list[tuple[str, Any, float]],
        short_entries: list[tuple[str, Any, float]],
        neutral_entries: list[tuple[str, Any]],
        queued_decisions: list[tuple[str, Any]],
        original: dict[str, str],
        net_power: float,
        gross_power: float,
        net_ratio: float,
    ) -> tuple[list[tuple[str, Any]], NettedDecision]:
        """Apply netting when net ratio is below threshold."""
        mode = self.config.mode

        if mode == "swallow":
            self._swallow_count += 1
            tag = (
                f"portfolio_netted_swallow:"
                f"net_ratio={net_ratio:.3f}_lt_{self.config.netting_threshold}"
            )
            for _sname, decision, _ in long_entries + short_entries:
                decision.should_trade = False
                decision.reason = tag
            return (
                [(s, d) for s, d in queued_decisions],
                NettedDecision(
                    action="swallow",
                    direction="neutral",
                    net_exposure_power=abs(net_power),
                    gross_exposure_power=gross_power,
                    net_ratio=net_ratio,
                    original_decisions=original,
                    swallowed=[s for s, _, _ in long_entries + short_entries],
                    survivors=[s for s, _ in neutral_entries],
                    reason=f"net_ratio_{net_ratio:.3f}_below_threshold_{self.config.netting_threshold}",
                ),
            )

        elif mode == "reduce":
            return self._reduce_minority(
                long_entries,
                short_entries,
                neutral_entries,
                queued_decisions,
                original,
                net_power,
                gross_power,
                net_ratio,
            )

        else:  # "warn"
            return (
                [(s, d) for s, d in queued_decisions],
                NettedDecision(
                    action="warn",
                    direction="neutral",
                    net_exposure_power=abs(net_power),
                    gross_exposure_power=gross_power,
                    net_ratio=net_ratio,
                    original_decisions=original,
                    swallowed=[],
                    survivors=[s for s, _ in queued_decisions],
                    reason=f"net_ratio_{net_ratio:.3f}_warn_only",
                ),
            )

    def _reduce_minority(
        self,
        long_entries: list[tuple[str, Any, float]],
        short_entries: list[tuple[str, Any, float]],
        neutral_entries: list[tuple[str, Any]],
        queued_decisions: list[tuple[str, Any]],
        original: dict[str, str],
        net_power: float,
        gross_power: float,
        net_ratio: float,
    ) -> tuple[list[tuple[str, Any]], NettedDecision]:
        """Swallow minority side, allow net-dominant side through."""
        self._reduce_count += 1

        if net_power > 0:
            # LONG dominant — swallow SHORT entries
            dominant = "long"
            swallowed_entries = short_entries
            surviving_entries = long_entries
        else:
            # SHORT dominant — swallow LONG entries
            dominant = "short"
            swallowed_entries = long_entries
            surviving_entries = short_entries

        tag = "portfolio_netted_reduce:minority_side_swallowed"
        for _sname, decision, _ in swallowed_entries:
            decision.should_trade = False
            decision.reason = tag

        return (
            [(s, d) for s, d in queued_decisions],
            NettedDecision(
                action="reduce",
                direction=dominant,
                net_exposure_power=abs(net_power),
                gross_exposure_power=gross_power,
                net_ratio=net_ratio,
                original_decisions=original,
                swallowed=[s for s, _, _ in swallowed_entries],
                survivors=[s for s, _, _ in surviving_entries],
                reason=f"{dominant}_dominant_minority_swallowed",
            ),
        )


__all__ = [
    "NettedDecision",
    "PortfolioNettingConfig",
    "PortfolioNettingGate",
]

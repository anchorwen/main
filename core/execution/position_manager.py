"""Active position management — dynamic exit orchestration.

Replaces the static SL/TP set-and-forget model with a three-layer adaptive exit
system that runs every cycle while a position is open:

    Layer 1 — Chandelier trailing stop (ATR-adaptive, never moves backward,
              capped at original_SL + max_lock_atr × entry_atr to respect
              the model training contract)
    Layer 2 — Brain ensemble flip exit (re-evaluates brains every N cycles,
              requires 2 consecutive confirmations to avoid noise)
    Layer 3 — Model-aware time decay exit (phased pressure based on each
              model's training horizon)

All exit actions flow through the existing ``dispatch_live_order()`` →
mt5_bridge_worker pipeline; the bridge already supports ``modify_sltp`` and
``close`` with partial volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# ── Data model ────────────────────────────────────────────────────────────


@dataclass
class ActivePosition:
    """Mutable snapshot of one open position tracked cycle-by-cycle."""

    ticket: int
    side: str  # "long" or "short"
    entry_price: float
    volume: float

    # SL / TP levels (evolve over time)
    initial_sl: float
    initial_tp: float
    current_sl: float
    current_tp: float

    # Price extremes since entry (for Chandelier trailing)
    highest_high: float
    lowest_low: float

    # Context captured at entry
    entry_atr: float
    entry_cycle: int
    entry_consensus: dict[str, Any] = field(default_factory=dict)
    supporting_brain_ids: list[str] = field(default_factory=list)

    # Per-model training horizons (brain_id → cycles)
    # e.g. {"v9_institutional_01": 12, "xgboost_v4.5": 3, "ou_params_v6": 0}
    # horizon=0 means dynamic (e.g. ARB OU uses half-life)
    model_horizons: dict[str, int] = field(default_factory=dict)

    # State flags
    breakeven_triggered: bool = False
    trail_multiplier: float = 2.0
    r_milestones_hit: list[str] = field(default_factory=list)
    cycles_held: int = 0
    highest_r: float = 0.0  # peak R-multiple achieved


# ── Manager ────────────────────────────────────────────────────────────────


class ActivePositionManager:
    """Orchestrates dynamic exit logic for one position at a time.

    Designed for ``max_positions=1``.  If you later raise the limit you create
    one manager per position.

    All numeric parameters are defaults; they can be overridden per instance
    from CLI flags or ``live.yaml``.
    """

    def __init__(
        self,
        *,
        trail_atr_mult: float = 2.0,
        trail_atr_mult_low: float = 1.5,
        trail_atr_mult_high: float = 3.0,
        breakeven_threshold_atr: float = 1.0,
        brain_reeval_interval: int = 5,
        flip_exit_threshold: float = 0.5,
        confidence_drop_threshold: float = 0.10,
        max_hold_cycles: int = 60,
        require_min_r: float = 0.3,
        min_step: float = 0.005,  # minimum SL change to fire modify (~0.5 pip XAUUSD)
        max_lock_atr: float = 1.0,  # max R to lock in via trailing (capped at original_SL + max_lock_atr × entry_atr)
        flip_confirm_count: int = 2,  # consecutive flips required before brain-flip exit
        pnl_store: Any = None,  # BrainPnLStore for brain-specific trail tuning
        meta_exit_engine: Any = None,  # MetaExitEngine for multi-factor exit scoring
    ):
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_mult_low = trail_atr_mult_low
        self.trail_atr_mult_high = trail_atr_mult_high
        self.breakeven_threshold_atr = breakeven_threshold_atr
        self.brain_reeval_interval = brain_reeval_interval
        self.flip_exit_threshold = flip_exit_threshold
        self.confidence_drop_threshold = confidence_drop_threshold
        self.max_hold_cycles = max_hold_cycles
        self.require_min_r = require_min_r
        self.min_step = min_step
        self.max_lock_atr = max_lock_atr
        self.flip_confirm_count = flip_confirm_count
        self.pnl_store = pnl_store
        self.meta_exit_engine = meta_exit_engine

        self._position: ActivePosition | None = None
        self._last_brain_reeval_cycle: int = -1
        self._entry_consensus_score: float = 0.0
        self._consecutive_flips: int = 0  # for 2-confirmation flip exit

    # ── Public API ──────────────────────────────────────────────────────

    def has_position(self) -> bool:
        return self._position is not None

    def get_position(self) -> ActivePosition | None:
        return self._position

    def clear_position(self) -> None:
        self._position = None
        self._last_brain_reeval_cycle = -1
        self._entry_consensus_score = 0.0

    def register_position(
        self,
        *,
        ticket: int,
        side: str,
        entry_price: float,
        volume: float,
        initial_sl: float,
        initial_tp: float,
        entry_atr: float,
        entry_cycle: int,
        entry_consensus: dict[str, Any] | None = None,
        supporting_brain_ids: list[str] | None = None,
        model_horizons: dict[str, int] | None = None,
        current_high: float | None = None,
    ) -> ActivePosition:
        """Record a newly-opened position (or recover one after restart)."""
        high = current_high if current_high is not None else entry_price
        low = entry_price  # worst-case for a long; for short we'd swap

        self._position = ActivePosition(
            ticket=ticket,
            side=side,
            entry_price=entry_price,
            volume=volume,
            initial_sl=initial_sl,
            initial_tp=initial_tp,
            current_sl=initial_sl,
            current_tp=initial_tp,
            highest_high=max(high, entry_price),
            lowest_low=min(low, entry_price),
            entry_atr=entry_atr,
            entry_cycle=entry_cycle,
            entry_consensus=dict(entry_consensus or {}),
            supporting_brain_ids=list(supporting_brain_ids or []),
            model_horizons=dict(model_horizons or {}),
            trail_multiplier=self.trail_atr_mult,
        )
        self._entry_consensus_score = float(
            self._position.entry_consensus.get("consensus_score", 0)
        )
        self._consecutive_flips = 0
        return self._position

    def update_prices(
        self,
        mid: float,
        bid: float,
        ask: float,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        cycle_count: int = 0,
    ) -> dict[str, Any]:
        """Per-cycle update.  Returns an action dict (may be empty)."""
        pos = self._position
        if pos is None:
            return {}

        pos.cycles_held += 1

        # Track extremes
        pos.highest_high = max(pos.highest_high, bid if pos.side == "long" else ask)
        pos.lowest_low = min(pos.lowest_low, ask if pos.side == "long" else bid)

        # Track peak R
        r_now = self._compute_r_multiple(mid)
        pos.highest_r = max(pos.highest_r, r_now)

        # Adjust trail multiplier for regime
        self._adjust_trail_for_regime(current_atr, regime_info)

        return {
            "mid": mid,
            "current_atr": current_atr,
            "r_multiple": r_now,
            "highest_r": pos.highest_r,
        }

    # ── Layer 1: Chandelier trailing stop ───────────────────────────────

    def compute_trail_stop(self, current_atr: float) -> float | None:
        """Return new SL if the trail has advanced, else None.

        Long:  max(current_sl, highest_high - trail_mult × atr)
        Short: min(current_sl, lowest_low + trail_mult × atr)

        **Label-consistent constraint**: trail SL cannot exceed
        original_SL + max_lock_atr × entry_atr.  This preserves the
        model's original SL as a hard floor while allowing up to
        max_lock_atr R of profit to be locked in by the trailing stop.
        The original TP remains as the hard ceiling — never cancelled.
        """
        pos = self._position
        if pos is None:
            return None

        max_lock_level: float
        if pos.side == "long":
            candidate = pos.highest_high - pos.trail_multiplier * current_atr
            # Breakeven floor: never go below entry after breakeven
            if pos.breakeven_triggered:
                candidate = max(candidate, pos.entry_price)
            # Never go below original SL (respect model training contract)
            candidate = max(candidate, pos.initial_sl)
            # Lock-in cap: trail cannot lock more than max_lock_atr × entry_atr profit
            max_lock_level = pos.entry_price + self.max_lock_atr * pos.entry_atr
            candidate = min(candidate, max_lock_level)
            if candidate <= pos.current_sl:
                return None
            return round(candidate, 3)
        else:
            candidate = pos.lowest_low + pos.trail_multiplier * current_atr
            if pos.breakeven_triggered:
                candidate = min(candidate, pos.entry_price)
            # Never go above original SL
            candidate = min(candidate, pos.initial_sl)
            # Lock-in cap
            max_lock_level = pos.entry_price - self.max_lock_atr * pos.entry_atr
            candidate = max(candidate, max_lock_level)
            if candidate >= pos.current_sl:
                return None
            return round(candidate, 3)

    def should_breakeven(self, mid: float, current_atr: float) -> bool:
        """Return True when the favorable move exceeds the breakeven threshold."""
        pos = self._position
        if pos is None or pos.breakeven_triggered:
            return False

        threshold = self.breakeven_threshold_atr * current_atr
        if pos.side == "long":
            return (pos.highest_high - pos.entry_price) >= threshold
        else:
            return (pos.entry_price - pos.lowest_low) >= threshold

    def _compute_r_multiple(self, mid: float) -> float:
        """Current R-multiple (fraction of initial risk)."""
        pos = self._position
        if pos is None:
            return 0.0
        risk = abs(pos.entry_price - pos.initial_sl)
        if risk < 1e-8:
            return 0.0
        if pos.side == "long":
            return (mid - pos.entry_price) / risk
        else:
            return (pos.entry_price - mid) / risk

    def check_r_milestones(self, mid: float) -> str | None:
        """Return '1R', '2R', or '3R' if newly crossed, else None."""
        pos = self._position
        if pos is None:
            return None
        r = self._compute_r_multiple(mid)
        for milestone_r, tag in [(3.0, "3R"), (2.0, "2R"), (1.0, "1R")]:
            if r >= milestone_r and tag not in pos.r_milestones_hit:
                pos.r_milestones_hit.append(tag)
                return tag
        return None

    def _adjust_trail_for_regime(
        self, current_atr: float, regime_info: dict[str, Any] | None = None
    ) -> None:
        """Dynamically adjust trail multiplier based on volatility regime
        and brain-specific P&L performance."""
        pos = self._position
        if pos is None:
            return

        regime = (regime_info or {}).get("regime", "normal")
        if regime == "low":
            base = self.trail_atr_mult_low
        elif regime == "high":
            base = self.trail_atr_mult_high
        else:
            base = self.trail_atr_mult

        # Apply R-milestone tightening on top
        if "3R" in pos.r_milestones_hit:
            base *= 0.5
        elif "2R" in pos.r_milestones_hit:
            base *= 0.7

        # Apply brain-specific adjustment based on live P&L performance
        base *= self._compute_brain_specific_trail_scale()

        pos.trail_multiplier = round(base, 3)

    def _compute_brain_specific_trail_scale(self) -> float:
        """Scale trail multiplier by supporting brains' live Sharpe ratios.

        High Sharpe → wider trail (let profits run).
        Negative Sharpe → tighter trail (cut faster).
        Returns a multiplier in [0.6, 1.5].
        """
        pos = self._position
        if pos is None or not pos.supporting_brain_ids:
            return 1.0
        if self.pnl_store is None:
            return 1.0

        try:
            sharpe_values: list[float] = []
            for bid in pos.supporting_brain_ids:
                metrics = self.pnl_store.get_metrics(bid)
                if metrics and metrics.sample_count >= 5:
                    s = float(metrics.sharpe_ratio)
                    sharpe_values.append(s)

            if not sharpe_values:
                return 1.0

            avg_sharpe = float(np.mean(sharpe_values))
            # Map Sharpe to scale: Sharpe -2 → 0.6, Sharpe 0 → 1.0, Sharpe 2 → 1.5
            # tanh((s + 0.2) * 0.8) maps nicely into [~0.6, ~1.4] for s ∈ [-2, 3]
            scale = 1.0 + 0.25 * float(np.tanh(avg_sharpe * 1.2))
            return float(np.clip(scale, 0.6, 1.5))
        except Exception:
            return 1.0

    # ── Layer 2: Brain ensemble exit ────────────────────────────────────

    def evaluate_brain_exit(
        self,
        current_consensus: dict[str, Any],
        current_supporting: list[str],
    ) -> tuple[bool, str]:
        """Check if brain consensus has flipped against the entry direction.

        Requires ``flip_confirm_count`` consecutive flip detections to avoid
        exiting on single-cycle noise.  An immediate exit is triggered only
        when the flip ratio is extreme (≥0.70).

        Returns (should_exit, reason).
        """
        pos = self._position
        if pos is None:
            return False, ""

        entry_ids = set(pos.supporting_brain_ids)

        # 1. Flip check: how many previously-supporting brains flipped?
        flip_detected = False
        flip_ratio = 0.0
        if entry_ids:
            current_support_set = set(current_supporting)
            flipped = entry_ids - current_support_set
            flip_ratio = len(flipped) / len(entry_ids)
            if flip_ratio >= self.flip_exit_threshold:
                flip_detected = True

        # 2. Confidence drop check
        current_score = float(current_consensus.get("consensus_score", 0))
        drop = self._entry_consensus_score - current_score
        confidence_dropped = drop > self.confidence_drop_threshold

        # ── Consecutive confirmation logic ──
        if flip_detected:
            self._consecutive_flips += 1
            # Immediate exit on extreme flip
            if flip_ratio >= 0.70:
                self._consecutive_flips = 0
                return True, f"brain_flip_extreme_{int(flip_ratio*100)}pct"
            # Exit after confirm_count consecutive flips
            if self._consecutive_flips >= self.flip_confirm_count:
                self._consecutive_flips = 0
                return True, f"brain_flip_{int(flip_ratio*100)}pct_c{self.flip_confirm_count}"
        else:
            self._consecutive_flips = 0

        if confidence_dropped:
            return True, f"confidence_drop_{drop:.3f}"

        return False, ""

    def should_reeval_brains(self, cycle_count: int) -> bool:
        """Return True when it's time to re-evaluate brain signals."""
        return cycle_count - self._last_brain_reeval_cycle >= self.brain_reeval_interval

    def mark_brains_reevaluated(self, cycle_count: int) -> None:
        self._last_brain_reeval_cycle = cycle_count

    def should_exit_ou_based(self, z_score: float, z_exit: float = 0.3) -> tuple[bool, str]:
        """OU mean-reversion exit: exit when Z-score reverts to near zero.

        This is the natural exit for the ARB OU brain — it enters when
        |z| > z_entry and exits when |z| < z_exit (mean reversion complete).

        Returns (should_exit, reason).
        """
        pos = self._position
        if pos is None:
            return False, ""

        if abs(z_score) < z_exit:
            return True, f"ou_reversion_z{abs(z_score):.2f}"
        return False, ""

    # ── Layer 2.5: Meta-model multi-factor exit ────────────────────────

    def evaluate_meta_exit(
        self,
        mid: float,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        current_consensus: dict[str, Any] | None = None,
        current_supporting: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Multi-factor exit evaluation using MetaExitEngine.

        When a trained model is available, uses ML inference for P(win).
        Otherwise falls back to heuristic scoring (PnL + time + regime +
        consensus + volatility).

        Returns (should_exit, reason).
        """
        if self.meta_exit_engine is None:
            return False, ""

        pos = self._position
        if pos is None:
            return False, ""

        from core.execution.meta_exit_engine import ExitFeatureSnapshot

        # ── Build snapshot ──
        r_now = self._compute_r_multiple(mid)
        reg = regime_info or {}
        cons = current_consensus or {}

        entry_score = self._entry_consensus_score
        current_score = float(cons.get("consensus_score", entry_score))
        drift = entry_score - current_score

        snap = ExitFeatureSnapshot(
            # PnL state
            current_r=round(r_now, 4),
            peak_r=round(pos.highest_r, 4),
            drawdown_r=round(max(0.0, pos.highest_r - r_now), 4),
            # Time state
            cycles_held=pos.cycles_held,
            expected_horizon=self._get_effective_horizon(),
            time_ratio=round(pos.cycles_held / max(self._get_effective_horizon(), 1), 4),
            # Regime state
            regime=reg.get("regime", "normal"),
            regime_confidence=float(reg.get("regime_confidence", 0.0)),
            trend_aligned=self._is_trend_aligned(reg),
            atr_current=current_atr,
            atr_entry=pos.entry_atr,
            atr_expansion=round((current_atr - pos.entry_atr) / max(pos.entry_atr, 0.001), 4),
            # Brain consensus state
            entry_consensus_score=round(entry_score, 4),
            entry_supporting_count=len(pos.supporting_brain_ids),
            current_supporting_count=len(current_supporting or []),
            consensus_drift=round(drift, 4),
            # Context
            side=pos.side,
            symbol="",
        )

        evaluation = self.meta_exit_engine.evaluate(snap)

        if evaluation.should_exit:
            return True, f"meta_exit_u{evaluation.exit_urgency:.2f}_{evaluation.exit_reason}"

        return False, ""

    @staticmethod
    def _is_trend_aligned(regime_info: dict[str, Any]) -> bool:
        """Check if the current regime's trend direction matches position side."""
        pos_side = regime_info.get("_position_side", "")
        trend_dir = regime_info.get("trend_direction", "")
        if not pos_side or not trend_dir:
            return True  # unknown → assume aligned
        return pos_side == trend_dir

    # ── Layer 3: Time / regime-based exit ───────────────────────────────

    def _get_effective_horizon(self) -> int:
        """Return the effective time horizon (cycles) for this position.

        Uses the shortest model horizon among supporting brains.
        Horizon=0 (dynamic/OU) is excluded from the min calculation.
        Falls back to ``max_hold_cycles`` if no model horizons are recorded.
        """
        pos = self._position
        if pos is None:
            return self.max_hold_cycles
        horizons = [h for h in pos.model_horizons.values() if h > 0]
        if not horizons:
            return self.max_hold_cycles
        return min(horizons)

    def should_exit_time_based(self, mid: float) -> tuple[bool, str]:
        """Phased time-decay exit based on model training horizon.

        Phase 1 (0-50% of horizon): model prediction is still valid, no time pressure.
        Phase 2 (50-80% of horizon): need ≥0.3R to avoid time exit.
        Phase 3 (80-100% of horizon): need ≥0.5R to avoid time exit.
        Phase 4 (>100% of horizon): model prediction expired, exit unless ≥1.0R.
        """
        pos = self._position
        if pos is None:
            return False, ""

        r_now = self._compute_r_multiple(mid)
        effective_horizon = self._get_effective_horizon()
        ratio = pos.cycles_held / max(effective_horizon, 1)

        if ratio < 0.50:
            return False, ""
        elif ratio < 0.80:
            if r_now < self.require_min_r:  # 0.3R default
                return True, f"time_phase2_{pos.cycles_held}c_h{effective_horizon}_r{r_now:.2f}"
        elif ratio < 1.00:
            if r_now < self.require_min_r * 1.67:  # ~0.5R
                return True, f"time_phase3_{pos.cycles_held}c_h{effective_horizon}_r{r_now:.2f}"
        else:
            if r_now < 1.0:
                return (
                    True,
                    f"time_phase4_expired_{pos.cycles_held}c_h{effective_horizon}_r{r_now:.2f}",
                )
        return False, ""

    # ── Payload builders ─────────────────────────────────────────────────

    def build_modify_payload(
        self, new_sl: float, new_tp: float, reason: str = ""
    ) -> dict[str, Any]:
        """Return execution_payload for a modify_sltp dispatch."""
        pos = self._position
        return {
            "action": "modify_sltp",
            "side": pos.side if pos else "long",
            "position_ticket": pos.ticket if pos else 0,
            "sl": new_sl,
            "tp": new_tp,
            "comment": reason,
        }

    def build_close_payload(self, reason: str = "") -> dict[str, Any]:
        """Return execution_payload for a close dispatch."""
        pos = self._position
        return {
            "action": "close",
            "side": pos.side if pos else "long",
            "position_ticket": pos.ticket if pos else 0,
            "volume": pos.volume if pos else 0.01,
            "comment": reason,
        }

    # ── Persistence ──────────────────────────────────────────────────────

    _SAVE_INTERVAL_CYCLES = 5  # persist every N cycles to limit disk I/O

    def save_state(self, save_path: str | Path) -> None:
        """Persist current position + manager state to JSON.

        Called from the main loop every N cycles.  Skipped if no position.
        """
        import json as _json
        from pathlib import Path as _Path

        pos = self._position
        if pos is None:
            return

        p = _Path(save_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "ticket": pos.ticket,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "volume": pos.volume,
                "initial_sl": pos.initial_sl,
                "initial_tp": pos.initial_tp,
                "current_sl": pos.current_sl,
                "current_tp": pos.current_tp,
                "highest_high": pos.highest_high,
                "lowest_low": pos.lowest_low,
                "entry_atr": pos.entry_atr,
                "entry_cycle": pos.entry_cycle,
                "entry_consensus": pos.entry_consensus,
                "supporting_brain_ids": pos.supporting_brain_ids,
                "model_horizons": pos.model_horizons,
                "breakeven_triggered": pos.breakeven_triggered,
                "trail_multiplier": pos.trail_multiplier,
                "r_milestones_hit": pos.r_milestones_hit,
                "cycles_held": pos.cycles_held,
                "highest_r": pos.highest_r,
                "_last_brain_reeval_cycle": self._last_brain_reeval_cycle,
                "_entry_consensus_score": self._entry_consensus_score,
                "_consecutive_flips": self._consecutive_flips,
                "saved_at_utc": (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").UTC)
                    .replace(tzinfo=None)
                    .isoformat()
                ),
            }
            p.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # Disk write failure is non-fatal

    def load_state(self, save_path: str | Path) -> ActivePosition | None:
        """Restore position + manager state from JSON, if fresh enough.

        Returns the restored ActivePosition, or None if no valid state exists.
        """
        import json as _json
        from pathlib import Path as _Path

        p = _Path(save_path)
        if not p.exists():
            return None

        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            return None

        # Require minimum fields
        if not all(k in data for k in ("ticket", "side", "entry_price", "volume")):
            return None

        pos = ActivePosition(
            ticket=int(data["ticket"]),
            side=str(data["side"]),
            entry_price=float(data["entry_price"]),
            volume=float(data["volume"]),
            initial_sl=float(data.get("initial_sl", data["entry_price"])),
            initial_tp=float(data.get("initial_tp", 0)),
            current_sl=float(data.get("current_sl", data["initial_sl"])),
            current_tp=float(data.get("current_tp", data.get("initial_tp", 0))),
            highest_high=float(data.get("highest_high", data["entry_price"])),
            lowest_low=float(data.get("lowest_low", data["entry_price"])),
            entry_atr=float(data.get("entry_atr", 2.0)),
            entry_cycle=int(data.get("entry_cycle", 0)),
            entry_consensus=data.get("entry_consensus", {}),
            supporting_brain_ids=data.get("supporting_brain_ids", []),
            model_horizons=data.get("model_horizons", {}),
            breakeven_triggered=bool(data.get("breakeven_triggered", False)),
            trail_multiplier=float(data.get("trail_multiplier", self.trail_atr_mult)),
            r_milestones_hit=data.get("r_milestones_hit", []),
            cycles_held=int(data.get("cycles_held", 0)),
            highest_r=float(data.get("highest_r", 0.0)),
        )
        self._position = pos
        self._last_brain_reeval_cycle = int(data.get("_last_brain_reeval_cycle", -1))
        self._entry_consensus_score = float(data.get("_entry_consensus_score", 0.0))
        self._consecutive_flips = int(data.get("_consecutive_flips", 0))
        return pos

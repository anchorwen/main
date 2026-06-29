"""V6 Layer B: Position Lifecycle Manager.

B1 — 5-Stage Gate (StageGate): Finite state machine for position lifecycle.
    IDLE → ENTRY_CONFIRMED → MANAGED → AT_RISK → CLOSING → IDLE.

B2 — 7-Level Exit Priority Queue (ExitPriorityQueue): First-match-wins
    exit pipeline.  Higher priority exits preempt lower ones.  Unifies
    existing scattered exit checks (bleed, OU revert, brain flip, hesitation,
    time expiry) with new V6 exits (basket TP, regime collapse, circuit
    breaker, premise invalidation, ratchet risk).

Reference:
  - God's Eye V6.0: multi_tf_stage_gate.py (5-stage FSM), exit_manager.py (7-level queue)
  - v6_integration_blueprint.pdf §3-4
  - d:\future\core\execution\position_manager.py (ActivePosition, exit methods)
  - d:\future\core\runtime\management_phase.py L632-1686 (existing exit checks)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.trading.contracts import ExitVerdict, StageInfo

if TYPE_CHECKING:
    from core.execution.position_manager import ActivePosition

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# B1: 5-Stage Gate
# ═══════════════════════════════════════════════════════════════════════


class StageGate:
    """5-stage position lifecycle finite state machine.

    One instance per symbol.  Each ActivePosition carries its current
    lifecycle_stage and stage_entered_at_cycle as mutable fields.

    State transitions follow the V6 God's Eye architecture:
      IDLE → ENTRY_CONFIRMED  (position opened, waiting for M15 confirm)
      ENTRY_CONFIRMED → MANAGED  (M15 confirms direction)
      ENTRY_CONFIRMED → AT_RISK  (M15 contradicts or timeout)
      MANAGED → AT_RISK  (M30 trend against, regime deterioration)
      AT_RISK → MANAGED  (Schmitt trigger recovery: M15 re-aligns)
      {MANAGED, AT_RISK} → CLOSING  (exit triggered by priority queue)
      CLOSING → IDLE  (MT5 confirms close, ledger reconciled)

    Any reverse or skip transition triggers a warning log but does NOT
    raise — the system treats unexpected transitions as MANAGED for safety.
    """

    def __init__(self) -> None:
        self._transition_log: list[StageInfo] = []

    # ── Transition engine ──────────────────────────────────────

    def transition(
        self,
        pos: ActivePosition,
        new_stage: str,
        reason: str,
        cycle: int,
        bar: int = 0,
    ) -> StageInfo:
        """Execute a stage transition, validating against the state matrix.

        Returns StageInfo for audit.  Mutates pos.lifecycle_stage in place.
        """
        old_stage = pos.lifecycle_stage

        # Validate transition is allowed
        if not self._is_valid_transition(old_stage, new_stage):
            logger.warning(
                "StageGate: invalid transition %s→%s (reason=%s, ticket=%s). "
                "Forcing MANAGED for safety.",
                old_stage,
                new_stage,
                reason,
                pos.ticket,
            )
            new_stage = "MANAGED"
            reason = f"forced_managed_from_invalid_{old_stage}_to_invalid_original"

        pos.lifecycle_stage = new_stage
        pos.stage_entered_at_cycle = cycle

        info = StageInfo(
            from_stage=old_stage,
            to_stage=new_stage,
            reason=reason,
            cycle=cycle,
            bar=bar,
        )
        self._transition_log.append(info)

        if info.is_escalation:
            logger.info(
                "StageGate: %s → %s (reason=%s, ticket=%s, cycle=%s)",
                old_stage,
                new_stage,
                reason,
                pos.ticket,
                cycle,
            )
        return info

    def reset(self, pos: ActivePosition, cycle: int) -> StageInfo:
        """Reset position to IDLE after close confirmed."""
        return self.transition(pos, "IDLE", "close_confirmed", cycle)

    def on_entry(self, pos: ActivePosition, cycle: int, bar: int = 0) -> StageInfo:
        """Called when a new position is registered."""
        return self.transition(pos, "ENTRY_CONFIRMED", "position_opened", cycle, bar)

    def on_m15_confirmed(self, pos: ActivePosition, cycle: int) -> StageInfo:
        """M15 TF confirms entry direction → promote to MANAGED."""
        pos.m15_confirmed = True
        return self.transition(pos, "MANAGED", "m15_confirmed", cycle)

    def on_m15_contradiction(self, pos: ActivePosition, cycle: int) -> StageInfo:
        """M15 contradicts or timeout → degrade to AT_RISK."""
        return self.transition(pos, "AT_RISK", "m15_contradiction", cycle)

    def on_regime_deterioration(self, pos: ActivePosition, cycle: int) -> StageInfo:
        """M30 trend against or regime collapse → AT_RISK."""
        return self.transition(pos, "AT_RISK", "regime_deterioration", cycle)

    def on_recovery(self, pos: ActivePosition, cycle: int) -> StageInfo:
        """AT_RISK → MANAGED via Schmitt trigger (M15 re-aligns + PnL improving)."""
        return self.transition(pos, "MANAGED", "schmitt_recovery", cycle)

    def on_exit_triggered(self, pos: ActivePosition, cycle: int, reason: str = "") -> StageInfo:
        """Exit priority queue fired → CLOSING."""
        return self.transition(pos, "CLOSING", reason or "exit_triggered", cycle)

    # ── Valid transition matrix ───────────────────────────────

    @staticmethod
    def _is_valid_transition(old: str, new: str) -> bool:
        _valid = {
            ("IDLE", "ENTRY_CONFIRMED"): True,
            ("ENTRY_CONFIRMED", "MANAGED"): True,
            ("ENTRY_CONFIRMED", "AT_RISK"): True,
            ("MANAGED", "AT_RISK"): True,
            ("MANAGED", "CLOSING"): True,
            ("AT_RISK", "MANAGED"): True,  # Schmitt recovery
            ("AT_RISK", "CLOSING"): True,
            ("CLOSING", "IDLE"): True,  # Reset after close
            # Allow re-entry from IDLE (position closed, new position opened)
            ("IDLE", "IDLE"): True,  # No-op (evaluating entry)
        }
        return _valid.get((old, new), False)

    # ── Diagnostic ────────────────────────────────────────────

    @property
    def transition_count(self) -> int:
        return len(self._transition_log)

    def recent_transitions(self, n: int = 20) -> list[StageInfo]:
        return self._transition_log[-n:]


# ═══════════════════════════════════════════════════════════════════════
# B2: 7-Level Exit Priority Queue
# ═══════════════════════════════════════════════════════════════════════


class ExitPriorityQueue:
    """Unified 7-level exit pipeline.  First match wins.

    Priority levels (P1 highest, P7 lowest):
      P1: Basket TP        — dynamic ATR-based portfolio TP
      P2: Z-score Flip     — OU reversion complete
      P3: Regime Collapse  — higher-TF regime no longer supports position
      P4: Circuit Breaker  — PnL trajectory model failure (Emergency + Slow Burn)
      P5: Premise Invalid  — entry thesis broken (T1 z-divergence + T2 PnL-critical)
      P6: Ratchet Risk     — BreakevenDefense + DrawdownLock
      P7: Time Stop        — max hold time exceeded

    All levels are config-gated.  When the master `enabled` flag is False,
    evaluate() returns ExitVerdict(is_triggered=False) immediately.

    Usage:
        queue = ExitPriorityQueue(config)
        verdict = queue.evaluate(pos, pm, mid, current_atr, regime_info,
                                  current_z, entry_z, entry_hl, current_bar,
                                  ratchet_verdict)
        if verdict.is_triggered:
            dispatch_close(pos, reason=verdict.exit_code)
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg = config or {}
        self._global = self._cfg.get("global", {})
        self._exit_queue = self._cfg.get("exit_queue", {})

    @property
    def enabled(self) -> bool:
        return bool(self._global.get("enabled", False))

    @property
    def shadow_mode(self) -> bool:
        return bool(self._global.get("shadow_mode", False))

    def evaluate(
        self,
        *,
        pos: ActivePosition,
        pm: Any,  # ActivePositionManager
        mid: dict[str, float],
        current_atr: float,
        regime_info: dict[str, Any],
        current_z_score: float,
        prev_z_score: float | None,
        entry_z_score: float,
        entry_half_life: float,
        current_bar: int,
        point_value: float,
        ratchet_verdict: Any | None = None,  # RatchetVerdict from RatchetRisk
        ou_params: dict[str, Any] | None = None,
        # Existing exit check results (from management_phase.py)
        # These feed context into the priority queue without re-computing.
        trail_would_exit: bool = False,
        trail_exit_price: float = 0.0,
    ) -> ExitVerdict:
        """Evaluate all 7 priority levels.  First match wins.

        Returns ExitVerdict with is_triggered=True and the winning priority
        level, or is_triggered=False if no level fires.
        """
        if not self.enabled:
            return ExitVerdict(False, 0, "")

        # ── P1: Basket TP ─────────────────────────────────
        if self._level_enabled("P1_basket_tp"):
            verdict = self._check_basket_tp(pos, mid, current_atr, point_value)
            if verdict.is_triggered:
                return verdict

        # ── P2: Z-score Flip ───────────────────────────────
        if self._level_enabled("P2_zscore_flip"):
            verdict = self._check_zscore_flip(pos, current_z_score, prev_z_score, ou_params)
            if verdict.is_triggered:
                return verdict

        # ── P3: Regime Collapse ────────────────────────────
        if self._level_enabled("P3_regime_collapse"):
            verdict = self._check_regime_collapse(pos, regime_info)
            if verdict.is_triggered:
                return verdict

        # ── P4: Circuit Breaker ────────────────────────────
        if self._level_enabled("P4_circuit_breaker"):
            verdict = self._check_circuit_breaker(
                pos,
                pm,
                current_bar,
                entry_half_life,
                current_z_score,
                point_value,
            )
            if verdict.is_triggered:
                return verdict

        # ── P5: Premise Invalidation ───────────────────────
        if self._level_enabled("P5_premise_invalidation"):
            verdict = self._check_premise_invalidation(
                pos,
                current_z_score,
                entry_z_score,
                entry_half_life,
                current_bar,
                point_value,
            )
            if verdict.is_triggered:
                return verdict

        # ── P6: Ratchet Risk ───────────────────────────────
        if self._level_enabled("P6_ratchet_risk") and ratchet_verdict is not None:
            if ratchet_verdict.is_triggered:
                return ExitVerdict(
                    True,
                    6,
                    ratchet_verdict.reason.lower(),
                    details=ratchet_verdict.details,
                )

        # ── P7: Time Stop ──────────────────────────────────
        if self._level_enabled("P7_time_stop"):
            verdict = self._check_time_stop(pos, current_bar, entry_half_life)
            if verdict.is_triggered:
                return verdict

        return ExitVerdict(False, 0, "")

    # ── P1: Basket TP ───────────────────────────────────────────

    def _check_basket_tp(
        self,
        pos: ActivePosition,
        mid: dict[str, float],
        current_atr: float,
        point_value: float,
    ) -> ExitVerdict:
        """P1: Basket TP — dynamic ATR-based portfolio take-profit.

        Formula (V6): TP = α × ATR × PointValue × total_lots × sqrt(q) / q
        where q = number of concurrent positions in the same contract group.
        """
        cfg = self._exit_queue.get("P1_basket_tp", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        # Determine current PnL for this position
        current_pnl = getattr(pos, "current_pnl", 0.0)
        if current_pnl <= 0:
            return ExitVerdict(False, 0, "")

        total_lots = pos.volume
        q = max(getattr(pos, "_concurrent_count", 1), 1)  # positions in same group

        atr_mult = cfg.get("atr_mult", 1.2)

        # Early-stage multiplier: let winners run before M15 confirms
        if pos.lifecycle_stage == "ENTRY_CONFIRMED":
            atr_mult *= cfg.get("early_stage_mult", 2.0)

        import math

        basket_tp = atr_mult * current_atr * point_value * total_lots * math.sqrt(q) / q

        if basket_tp <= 0:
            return ExitVerdict(False, 0, "")

        if current_pnl >= basket_tp:
            return ExitVerdict(
                True,
                1,
                "basket_tp",
                details={
                    "tp_target": round(basket_tp, 2),
                    "pnl": round(current_pnl, 2),
                    "atr_mult": round(atr_mult, 3),
                    "q": q,
                },
            )
        return ExitVerdict(False, 0, "")

    # ── P2: Z-score Flip ────────────────────────────────────────

    def _check_zscore_flip(
        self,
        pos: ActivePosition,
        current_z: float,
        prev_z: float | None,
        ou_params: dict[str, Any] | None,
    ) -> ExitVerdict:
        """P2: Z-score sign flip — OU reversion complete.

        BUY (z<0 at entry): exit when z >= 0 (price crossed above mu).
        SELL (z>0 at entry): exit when z <= 0 (price crossed below mu).

        Requires 2-bar confirmation to avoid false flips from estimator noise.
        Asymmetric transition shield: block profitable exits during KF→OLS
        estimator handoff to prevent false flips costing winners.
        """
        cfg = self._exit_queue.get("P2_zscore_flip", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        if pos.side not in ("long", "short"):
            return ExitVerdict(False, 0, "")

        confirm_bars = cfg.get("confirm_bars", 2)

        # Determine if z-score has flipped
        if pos.side == "long":
            # Long entered on z<0 (oversold).  Exit when z>=0 (reversion done).
            flipped = current_z >= 0
        else:
            # Short entered on z>0 (overbought).  Exit when z<=0.
            flipped = current_z <= 0

        if not flipped:
            return ExitVerdict(False, 0, "")

        # 2-bar confirmation: prev_z must also be on the flipped side
        if prev_z is not None and confirm_bars > 1:
            if pos.side == "long":
                confirmed = prev_z >= 0
            else:
                confirmed = prev_z <= 0
            if not confirmed:
                return ExitVerdict(False, 0, "")

        # Asymmetric transition shield:
        # During KF→OLS estimator transition, z-score is unreliable.
        # Block profitable exits (likely false flips), allow loss exits.
        if cfg.get("transition_shield", True) and ou_params:
            kf_failed_bars = ou_params.get("kf_failed_bars", 0)
            freeze_bars = ou_params.get("kf_freeze_bars", 3)
            crossfade_bars = ou_params.get("kf_crossfade_bars", 3)
            in_transition = 0 < kf_failed_bars <= freeze_bars + crossfade_bars

            current_pnl = getattr(pos, "current_pnl", 0.0)
            if in_transition and current_pnl > 0:
                # Profitable + estimator transition → block false flip
                logger.info(
                    "P2 Z-score flip BLOCKED (transition shield): "
                    "ticket=%s pnl=%.2f kf_failed_bars=%s",
                    pos.ticket,
                    current_pnl,
                    kf_failed_bars,
                )
                return ExitVerdict(False, 0, "")

        return ExitVerdict(
            True,
            2,
            "zscore_flip",
            details={
                "current_z": round(current_z, 3),
                "prev_z": round(prev_z, 3) if prev_z is not None else None,
                "side": pos.side,
            },
        )

    # ── P3: Regime Collapse ─────────────────────────────────────

    def _check_regime_collapse(
        self,
        pos: ActivePosition,
        regime_info: dict[str, Any],
    ) -> ExitVerdict:
        """P3: Higher-TF regime no longer supports the position's premise.

        When M15 regime probability drops below N% of the minimum threshold,
        the market structure has changed — exit immediately.
        """
        cfg = self._exit_queue.get("P3_regime_collapse", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        m15_regime = regime_info.get("m15_regime_prob")
        if m15_regime is None:
            return ExitVerdict(False, 0, "")

        # Minimum regime threshold per strategy config
        m15_regime_min = regime_info.get(
            "m15_confirm_regime_min",
            self._cfg.get("stage_gate", {}).get("m15_confirm_regime_min", 0.60),
        )
        collapse_pct = cfg.get("m15_regime_pct_of_min", 0.50)
        collapse_threshold = m15_regime_min * collapse_pct

        if m15_regime < collapse_threshold:
            return ExitVerdict(
                True,
                3,
                "regime_collapse",
                details={
                    "m15_regime": round(m15_regime, 4),
                    "m15_regime_min": round(m15_regime_min, 4),
                    "collapse_threshold": round(collapse_threshold, 4),
                },
            )
        return ExitVerdict(False, 0, "")

    # ── P4: Circuit Breaker ─────────────────────────────────────

    def _check_circuit_breaker(
        self,
        pos: ActivePosition,
        pm: Any,
        current_bar: int,
        entry_half_life: float,
        current_z_score: float,
        point_value: float,
    ) -> ExitVerdict:
        """P4: PnL trajectory circuit breaker — model failure detection.

        Rule A (Emergency): PnL < -75% of total SL distance.
        Rule B (Slow Burn): bars_held > half_life AND PnL < -50% SL
                           AND 4+ consecutive declining PnL bars.
        """
        cfg = self._exit_queue.get("P4_circuit_breaker", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        bar_pnls = getattr(pos, "bar_pnls", [])
        min_bars = cfg.get("min_bars_for_check", 2)
        if len(bar_pnls) < min_bars:
            return ExitVerdict(False, 0, "")

        current_pnl = bar_pnls[-1] if bar_pnls else 0.0
        if current_pnl >= 0:
            return ExitVerdict(False, 0, "")

        # Compute total SL distance in dollars
        total_sl_dollar = 0.0
        entry = pos.entry_price
        sl = pos.current_sl
        vol = pos.volume
        if entry and sl and vol:
            total_sl_dollar += abs(entry - sl) * vol * point_value

        if total_sl_dollar <= 0:
            return ExitVerdict(False, 0, "")

        # Rule A: Emergency — PnL cratered through most of SL
        emergency_pct = cfg.get("rule_a_sl_emergency_pct", 75.0)
        emergency_threshold = -total_sl_dollar * emergency_pct / 100.0
        if current_pnl <= emergency_threshold:
            oldest_bar = min(
                p.get("entry_bar", current_bar) if isinstance(p, dict) else current_bar
                for p in ([pos] if hasattr(pos, "ticket") else [])
            )
            return ExitVerdict(
                True,
                4,
                "cb_emergency",
                details={
                    "pnl": round(current_pnl, 2),
                    "threshold": round(emergency_threshold, 2),
                    "sl_total_dollar": round(total_sl_dollar, 2),
                    "rule": "A_EMERGENCY",
                },
            )

        # Rule B: Slow burn
        slow_burn_pct = cfg.get("rule_b_slow_burn_sl_pct", 50.0)
        slow_burn_threshold = -total_sl_dollar * slow_burn_pct / 100.0

        if current_pnl <= slow_burn_threshold:
            # Half-life gate
            if cfg.get("require_beyond_hl", True):
                bars_held = getattr(pos, "cycles_held", 0)
                if cfg.get("use_entry_hl", True) and entry_half_life > 0:
                    current_hl = (
                        pm.get_current_half_life(pos)
                        if hasattr(pm, "get_current_half_life")
                        else 999
                    )
                    effective_hl = min(current_hl, entry_half_life)
                else:
                    effective_hl = max(
                        getattr(pos, "entry_half_life", 999) or 999,
                        entry_half_life or 0,
                    )
                if bars_held <= effective_hl:
                    # Z-trajectory unlock: bypass hl gate when OU process is demonstrably dead
                    if cfg.get("z_trajectory_unlock", True):
                        if not self._z_trajectory_breach(pos, current_z_score, bar_pnls):
                            return ExitVerdict(False, 0, "")
                    else:
                        return ExitVerdict(False, 0, "")

            # Consecutive decline check
            decline_bars = cfg.get("rule_b_consecutive_decline_bars", 4)
            consecutive = self._count_consecutive_decline(bar_pnls)
            if consecutive >= decline_bars:
                return ExitVerdict(
                    True,
                    4,
                    "cb_slow_burn",
                    details={
                        "pnl": round(current_pnl, 2),
                        "threshold": round(slow_burn_threshold, 2),
                        "sl_total_dollar": round(total_sl_dollar, 2),
                        "decline_bars": consecutive,
                        "rule": "B_SLOW_BURN",
                    },
                )

        return ExitVerdict(False, 0, "")

    @staticmethod
    def _count_consecutive_decline(bar_pnls: list[float]) -> int:
        """Count consecutive declining PnL bars from most recent backward."""
        if len(bar_pnls) < 2:
            return 0
        count = 0
        for i in range(len(bar_pnls) - 1, 0, -1):
            if bar_pnls[i] < bar_pnls[i - 1]:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _z_trajectory_breach(
        pos: ActivePosition,
        current_z: float,
        bar_pnls: list[float],
    ) -> bool:
        """Check if |z| is expanding and mu is drifting against position.

        Returns True when the OU process is demonstrably dead:
        - |z| expanding (deviation worsening)
        - Position is losing money
        """
        current_pnl = bar_pnls[-1] if bar_pnls else 0.0
        if current_pnl >= 0:
            return False

        entry_z = getattr(pos, "entry_z_score", 0.0)
        entry_mu = getattr(pos, "entry_mu", 0.0)

        if entry_z == 0.0:
            return False

        # |z| expanding: deviation is worsening, not healing
        if abs(current_z) <= abs(entry_z):
            return False

        return True

    # ── P5: Premise Invalidation ────────────────────────────────

    def _check_premise_invalidation(
        self,
        pos: ActivePosition,
        current_z: float,
        entry_z: float,
        entry_hl: float,
        current_bar: int,
        point_value: float,
    ) -> ExitVerdict:
        """P5: Entry thesis broken — two-tier convergence gate.

        T1 (Z-Divergence): |z| >= |entry_z| AND PnL < -max(3% SL, $10).
            OU process dead — z never converged, real friction hit.
        T2 (PnL-Critical): PnL < -max(20% SL, $30).
            Critical loss — exit regardless of z dynamics.
        """
        cfg = self._exit_queue.get("P5_premise_invalidation", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        if not entry_hl or entry_hl <= 0:
            return ExitVerdict(False, 0, "")

        bars_held = getattr(pos, "cycles_held", 0)
        max_bars = int(cfg.get("hl_mult", 3.0) * entry_hl)
        if bars_held <= max_bars:
            return ExitVerdict(False, 0, "")

        bar_pnls = getattr(pos, "bar_pnls", [])
        current_pnl = bar_pnls[-1] if bar_pnls else 0.0
        if current_pnl >= 0:
            return ExitVerdict(False, 0, "")

        # Compute SL dollar distance
        sl_dollar = 0.0
        entry = pos.entry_price
        sl = pos.current_sl
        vol = pos.volume
        if entry and sl and vol:
            sl_dollar = abs(entry - sl) * vol * point_value
        if sl_dollar <= 0:
            return ExitVerdict(False, 0, "")

        t1_threshold = max(
            sl_dollar * cfg.get("t1_sl_pct", 3.0) / 100.0, cfg.get("t1_dollar_floor", 10.0)
        )
        t2_threshold = max(
            sl_dollar * cfg.get("t2_sl_pct", 20.0) / 100.0, cfg.get("t2_dollar_floor", 30.0)
        )

        # T1: Z-divergence — OU process dead
        if entry_z != 0.0 and abs(current_z) >= abs(entry_z) and current_pnl < -t1_threshold:
            return ExitVerdict(
                True,
                5,
                "premise_invalid",
                details={
                    "bars_held": bars_held,
                    "max_bars": max_bars,
                    "entry_hl": round(entry_hl, 1),
                    "pnl": round(current_pnl, 2),
                    "tier": "T1_Z_DIVERGENCE",
                    "z_current": round(current_z, 3),
                    "z_entry": round(entry_z, 3),
                    "sl_dollar": round(sl_dollar, 2),
                    "threshold": round(t1_threshold, 2),
                },
            )

        # T2: Critical loss — deep in the red
        if current_pnl < -t2_threshold:
            return ExitVerdict(
                True,
                5,
                "premise_invalid",
                details={
                    "bars_held": bars_held,
                    "max_bars": max_bars,
                    "entry_hl": round(entry_hl, 1),
                    "pnl": round(current_pnl, 2),
                    "tier": "T2_PNL_CRITICAL",
                    "sl_dollar": round(sl_dollar, 2),
                    "threshold": round(t2_threshold, 2),
                },
            )

        return ExitVerdict(False, 0, "")

    # ── P7: Time Stop ───────────────────────────────────────────

    def _check_time_stop(
        self,
        pos: ActivePosition,
        current_bar: int,
        entry_half_life: float,
    ) -> ExitVerdict:
        """P7: Time stop — max hold time exceeded.

        Uses entry-clock anchoring: effective_hl = min(max(current_hl, entry_hl), 999).
        dynamic_max = hold_mult × effective_hl, capped at [min_bars, max_bars].
        """
        cfg = self._exit_queue.get("P7_time_stop", {})
        if not cfg.get("enabled", False):
            return ExitVerdict(False, 0, "")

        bars_held = getattr(pos, "cycles_held", 0)
        if bars_held <= 0:
            return ExitVerdict(False, 0, "")

        min_bars = cfg.get("min_bars", 15)
        if bars_held < min_bars:
            return ExitVerdict(False, 0, "")

        effective_hl = min(
            max(
                getattr(pos, "entry_half_life", entry_half_life) or entry_half_life,
                entry_half_life or 0,
            ),
            999.0,
        )
        if effective_hl <= 0:
            return ExitVerdict(False, 0, "")

        hold_mult = cfg.get("hold_mult", 3.0)
        dynamic_max = int(hold_mult * effective_hl)
        max_bars = min(cfg.get("max_bars", 60), max(min_bars, dynamic_max))

        if bars_held > max_bars:
            return ExitVerdict(
                True,
                7,
                "time_expired",
                details={
                    "bars_held": bars_held,
                    "max_bars": max_bars,
                    "effective_hl": round(effective_hl, 1),
                    "hold_mult": hold_mult,
                },
            )
        return ExitVerdict(False, 0, "")

    # ── Helpers ─────────────────────────────────────────────────

    def _level_enabled(self, level: str) -> bool:
        """Check if a specific priority level is enabled in config."""
        level_cfg = self._exit_queue.get(level, {})
        if isinstance(level_cfg, dict):
            return bool(level_cfg.get("enabled", False))
        return False

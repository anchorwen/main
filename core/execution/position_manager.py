"""Active position management — dynamic exit orchestration.

Replaces the static SL/TP set-and-forget model with a three-layer adaptive exit
system that runs every cycle while a position is open:

    Layer 1 — Chandelier trailing stop (ATR-adaptive, never moves backward,
              capped at original_SL + max_lock_atr × entry_atr to respect
              the model training contract)
    Layer 2 — Brain ensemble flip exit (re-evaluates brains every N cycles,
              requires 2 consecutive confirmations to avoid noise)
    Layer 3 — EV trajectory time exit (sqrt-based Alpha decay envelope,
              replaces the old linear-four-phase time decay)

All exit actions flow through the existing ``dispatch_live_order()`` →
mt5_bridge_worker pipeline; the bridge already supports ``modify_sltp`` and
``close`` with partial volume.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from core.execution.trail_stop_engine import (
    TrailPolicy,
    TrailStopEngine,
    compute_rr_floor_price,
)

if TYPE_CHECKING:
    from core.execution.meta_exit_engine import ExitEvaluation

# ── Data model ────────────────────────────────────────────────────────────

# TECH_DEBT-019 §3: elastic-expansion hysteresis band upper edge.
# Legacy trigger for TP tightening is atr_ratio <= 0.80.  With an RR contract
# (tp_min_rr_ratio > 0), ATR recovery re-expands TP once ratio >= 0.85 — the
# 0.80–0.85 dead band prevents TP oscillating bidirectionally when ATR hovers
# near the trigger (second debounce layer beyond tp_min_step).
_EXPAND_THRESHOLD = 0.85


def _policy_from_dict(d: dict[str, Any] | None) -> TrailPolicy | None:
    """Reconstruct a TrailPolicy from a JSON-serialized dict (TECH_DEBT-019).

    JSON turns the ``graduated_lock_levels`` tuple-of-tuples into a list of
    lists — restore the tuple shape before constructing the frozen dataclass.
    None/empty → None (legacy positions fall back to the engine default).
    """
    if not d:
        return None
    restored = dict(d)
    if isinstance(restored.get("graduated_lock_levels"), list):
        restored["graduated_lock_levels"] = [
            tuple(level) for level in restored["graduated_lock_levels"]
        ]
    return TrailPolicy(**restored)


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
    entry_z_score: float = 0.0  # OU z-score at entry (0 = unknown / recovered position)
    entry_half_life: float = 0.0  # OU half-life at entry (0 = unknown / not OU)
    # FIX-20260709-004 (L3): per-TF ATR that sized the SL/TP bracket
    # (dynamic_sl_tp used the strategy's own-timeframe ATR — FIX-20260706-027).
    # entry_atr stays the M5 reference for R-metric/ratchet/MetaExit features;
    # bracket_atr lets trail-TP geometry scale to the bracket's timeframe instead
    # of collapsing it to M5.  0.0 = unknown (pre-fix / M5 strategy) → callers
    # fall back to the entry_atr scale (identical legacy behaviour).
    bracket_atr: float = 0.0
    entry_consensus: dict[str, Any] = field(default_factory=dict)
    supporting_brain_ids: list[str] = field(default_factory=list)

    # Per-model training horizons (brain_id → cycles)
    # e.g. {"v9_institutional_01": 12, "xgboost_v4.5": 3, "ou_params_v6": 0}
    # horizon=0 means dynamic (e.g. ARB OU uses half-life)
    model_horizons: dict[str, int] = field(default_factory=dict)

    # State flags
    breakeven_triggered: bool = False
    trail_multiplier: float = 2.0
    trail_advances: int = 0  # FIX-20260610-006: count of trail SL tightenings
    # DQAF-064 §2: Track consecutive MT5 rejections of trail modify_sltp requests
    trail_rejection_streak: int = 0
    trail_last_rejection_code: int = 0
    # DQAF-20260721-001: Skip trail dispatch for N cycles after broker rate-limit
    # rejection (retcode 10022/10024). Prevents hammering the broker and accumulating
    # 10024 errors across multiple positions on the same account.
    trail_rate_limit_cooldown: int = 0
    r_milestones_hit: list[str] = field(default_factory=list)
    cycles_held: int = 0
    # DQAF-064 §3: Fractional accumulator for M5-bar normalized cycle counting.
    # Main loop runs at ~30s; one M5 bar = 300s.  We accumulate fractions
    # and only increment cycles_held when a full M5-equivalent has elapsed.
    _cycles_held_fractional: float = 0.0
    _last_bar_cycle: int = 0  # last cycles_held value at which bar_pnls was appended
    highest_r: float = 0.0  # peak R-multiple achieved
    prev_r: float = 0.0  # previous cycle's R (for trajectory scoring)
    partial_tp_triggered: bool = False  # partial take-profit already executed
    partial_tp_r: float = 0.0  # R-multiple at which to trigger partial TP (0=disabled)
    partial_tp_ratio: float = 0.5  # fraction of volume to close at partial TP
    # Phase C: Microstructure-aware partial TP thresholds
    ofi_partial_tp_threshold: float = 0.0  # |OFI_Z| threshold (0=disabled, e.g. 2.5)
    ofi_partial_tp_r_mult: float = 0.5  # R-multiplier when OFI triggered (0.5 = half normal)
    confidence_ema: float = 0.0  # EMA-smoothed confidence for noise-immune exit
    confidence_alpha: float = 0.4  # EMA smoothing factor (0.4 ≈ 3 cycles to stabilise)
    consecutive_flips: int = 0  # per-position flip confirmation counter

    # v3.2: Opt3 bleed-stop tracking — bar-level PnL since entry
    bar_pnls: list[float] = field(default_factory=list)
    bleed_triggered: bool = False  # set when bleed stop fires (prevents duplicate exit)

    # v3.2: Alpha Handoff — OU exit bypassed in favor of trailing stop
    ou_handoff_active: bool = False  # True → use trail stop, NOT ou_exit
    ou_handoff_r: float = 0.0  # R-multiple at handoff time (for journal)

    # v3.3: Strategy attribution for gamma-based EV trajectory dispatch
    strategy_name: str = ""  # e.g. "statarb_dynamic", "barrier_12bar"

    # v3.4: Expected remaining volume after legitimate reductions (partial_tp, net_out).
    # Synced on every confirmed volume change; used for ghost-volume audit at close.
    expected_remaining_volume: float = 0.0

    # v3.5: Per-strategy exit parameters (FIX-20260520-026 — Dynamic Exit Manager)
    # These override the ActivePositionManager globals so that a statarb mean-reversion
    # position can use a tight 1.5 ATR trail while an H4 swing position uses 2.5 ATR.
    trail_atr_mult: float = 2.0
    trail_atr_mult_low: float = 1.5
    trail_atr_mult_high: float = 3.0
    breakeven_threshold_atr: float = 1.0

    # Per-strategy immutable trail policy (Phase B: physical isolation of Risk Exit)
    # When set, TrailStopEngine reads exclusively from this policy.
    # When None, the engine falls back to its default policy.
    trail_policy: TrailPolicy | None = None
    # TECH_DEBT-019: RR hard-floor TP price bound by the most recent cycle (0.0 = none).
    # Telemetry analogue to ratchet_floor_r — lets offline audit scripts reconstruct
    # when the RR floor bound the trailing TP.
    rr_floor_tp: float = 0.0

    # COLD exploration flag — bypass trailing stop to collect uncensored labels
    # for ConformalOU online calibration (FIX-20260527-004 architect directive)
    cold_explore: bool = False

    # v3 SSOT: consensus hash from persisted intent-state for reconciliation
    _v3_consensus_hash: str = ""

    # ── V6 Layer B1: Position Lifecycle Stage Gate (FIX-20260629-195) ──
    # 5-stage finite state machine: IDLE → ENTRY_CONFIRMED → MANAGED →
    # AT_RISK → CLOSING → IDLE.  Each stage mounts different risk gates.
    lifecycle_stage: str = "IDLE"  # IDLE|ENTRY_CONFIRMED|MANAGED|AT_RISK|CLOSING
    stage_entered_at_cycle: int = 0  # loop_iteration when entered current stage
    m15_confirmed: bool = False  # M15 TF confirmed entry direction
    m30_aligned: bool = False  # M30 TF aligned with entry direction
    h1_managing: bool = False  # H1 TF used for active basket management

    # ── V6 Layer B3: Ratchet Risk state (FIX-20260629-195) ──
    ratchet_breakeven_armed: bool = False  # PnL > 1.2×ATR_cost → defense active
    ratchet_drawdown_armed: bool = False  # peak PnL > 2×ATR_cost → lock active
    ratchet_peak_pnl: float = 0.0  # highest net PnL achieved this position

    # ── FIX-20260708-004: Profit Ratchet Floor telemetry (Risk Exit side) ──
    # Last positive SL lock (in entry_atr R-units) demanded by the Chandelier
    # profit ratchet.  Distinct from the V6 RatchetRisk fields above (which are
    # a shadow-mode close-only subsystem): this one drives the broker-bound
    # trailing SL floor and is surfaced in management_phase_diag for give-back
    # verification.  0.0 = not yet armed.
    ratchet_floor_r: float = 0.0

    # ── V6 P2/P5 context: anchor values at entry for z-trajectory analysis ──
    entry_mu: float = 0.0  # OU long-term mean at entry (0 = unknown / not OU)

    # Immutability guard — entry_price is the absolute baseline for all risk
    # calculations (breakeven, trail activation, PnL).  Any post-construction
    # mutation corrupts the risk origin and causes phantom breakeven/trail
    # decisions (DQAF-20260621-034 IC Addendum).
    _frozen: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Lock immutable fields after dataclass construction."""
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        """Block writes to ``entry_price`` after construction.

        The ONE exception is V3 cold-start recovery where entry_price starts as
        0.0 and must be backfilled from MT5 — use ``_recover_entry_price()``.
        """
        if name == "entry_price" and getattr(self, "_frozen", False):
            raise AttributeError(
                f"entry_price is immutable after construction "
                f"(current={self.entry_price}, attempted={value}). "
                f"Use _recover_entry_price() ONLY for V3 cold-start recovery."
            )
        object.__setattr__(self, name, value)

    def _recover_entry_price(self, value: float) -> None:
        """One-time V3 cold-start recovery: set entry_price from MT5 ground truth.

        ONLY permitted when the current entry_price is 0.0 (uninitialized V3
        intent-state restore).  Once a non-zero value is established, any
        further attempt is a hard error — entry_price is the absolute risk
        origin and must never drift (DQAF-20260621-034 IC Addendum).
        """
        if self.entry_price > 0:
            raise ValueError(
                f"Cannot overwrite existing entry_price={self.entry_price} "
                f"with {value}. entry_price is immutable once set."
            )
        object.__setattr__(self, "entry_price", float(value))


# ── Manager ────────────────────────────────────────────────────────────────


class ActivePositionManager:
    """Orchestrates dynamic exit logic for multiple concurrent positions.

    .. rubric:: Architectural Roadmap (1,720 lines — high essential complexity)

    This class is large but cohesive.  State is encapsulated in
    ``ActivePosition`` dataclass instances — no cross-boundary pollution.
    Big-bang refactoring is NOT recommended.

    **Domain boundaries**::

        ── CRUD ──
        register_position        (105L)  Entry registration + PnL mapping
        update_prices             (44L)  Price/trail refresh per cycle
        _update_single_position   (60L)  Single-position state machine
        clear_position            (23L)  Close + cleanup

        ── Trail Stop (delegated to TrailStopEngine) ──
        compute_trail_stop, should_breakeven, should_partial_tp  (thin wrappers)

        ── Exit Evaluation (called from _execute_management_phase) ──
        Actual evaluation order in live_cycle.py:
          1. should_exit_bleed          (L1471)  Bleed-stop — hard SL
          2. should_exit_ou_based       (L1525)  OU mean-reversion signal
          3. evaluate_brain_exit        (L1564)  Brain consensus flip + EMA decay
             └─ _toxicity_veto          (gate)   Blocks non-toxic exits in protected period
          4. evaluate_meta_exit         (L1642)  Meta-model multi-factor gate
             └─ _toxicity_veto          (gate)
          5. should_exit_hesitation     (L1697)  No breakeven within N cycles
             └─ _toxicity_veto          (gate)
          6. should_exit_time_based     (L1736)  Max hold time exceeded
             └─ _toxicity_veto          (gate)

        ── Other exit methods (called from specific strategy paths) ──
        should_exit_zscore_dynamic, should_handoff_ou_to_trail,
        check_volume_climax, should_enter_inflection

        ── Persistence ──
        save_state   (62L)  Serialize all positions to JSON
        load_state  (166L)  Deserialize + hydrate — Strangler Fig candidate

    **Strangler Fig candidates** (extract when next modified):
      - ``load_state()`` → ``PositionHydrator`` class
      - ``register_position()`` → ``EntryOperator`` class
    """

    def __init__(
        self,
        *,
        trail_atr_mult: float = 2.0,
        trail_atr_mult_low: float = 1.5,
        trail_atr_mult_high: float = 3.0,
        breakeven_threshold_atr: float = 1.0,
        trail_activation_atr: float = 1.0,  # FIX-064: trail only activates after this many ATRs of profit
        brain_reeval_interval: int = 5,
        flip_exit_threshold: float = 0.5,
        confidence_drop_threshold: float = 0.10,
        confidence_floor: float = 0.45,  # P1-2: absolute confidence floor for p_win trajectory exit
        max_hold_cycles: int = 60,
        require_min_r: float = 0.3,
        min_step: float = 0.15,  # minimum SL change to fire modify (~15 pips XAUUSD)
        min_trail_mult: float = 1.2,  # absolute floor on effective trail multiplier (prevents stop-hunting)
        max_lock_atr: float = 4.0,  # max R to lock in via trailing (capped at original_SL + max_lock_atr × entry_atr)
        graduated_lock_enabled: bool = True,  # graduated profit locking: SL floor rises with R milestones
        graduated_lock_levels: tuple[tuple[float, float], ...] = (
            (3.0, 1.5),  # at +3R peak, SL floor ≥ +1.5R (protect 50% of peak)
            (5.0, 3.5),  # at +5R peak, SL floor ≥ +3.5R (protect 70% of peak)
        ),
        confidence_decay_enabled: bool = True,  # per-strategy confidence_decay_exit toggle
        hesitation_cycles: int = 0,  # exit if breakeven not reached within N cycles (0=disabled)
        flip_confirm_count: int = 2,  # consecutive flips required before brain-flip exit
        min_hold_cycles: int = 3,  # minimum cycles before non-SL exit (toxicity veto can override)
        toxicity_velocity_mult: float = 3.0,  # tick-velocity multiplier for toxicity veto
        kalman_velocity_threshold_bps: float = 3.0,  # DQAF-20260731-002: min |velocity| in bps for Kalman exit trigger
        pnl_store: Any = None,  # BrainPnLStore for brain-specific trail tuning
        meta_exit_engine: Any = None,  # MetaExitEngine for multi-factor exit scoring
        trail_policy: TrailPolicy | None = None,  # Phase C: default TrailPolicy for all positions
        loop_interval_seconds: float = 30.0,  # DQAF-064 §3: main loop tick interval for cycle normalization
    ):
        # Backward-compat: store individual trail params for direct access
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_mult_low = trail_atr_mult_low
        self.trail_atr_mult_high = trail_atr_mult_high
        self.breakeven_threshold_atr = breakeven_threshold_atr
        self.min_step = min_step
        self.min_trail_mult = min_trail_mult
        self.max_lock_atr = max_lock_atr
        self.graduated_lock_enabled = graduated_lock_enabled
        self.graduated_lock_levels = graduated_lock_levels

        # Model exit params
        self.brain_reeval_interval = brain_reeval_interval
        self.flip_exit_threshold = flip_exit_threshold
        self.confidence_drop_threshold = confidence_drop_threshold
        self.confidence_floor = confidence_floor  # P1-2: absolute floor for p_win trajectory exit
        self.max_hold_cycles = max_hold_cycles
        self.require_min_r = require_min_r
        self.confidence_decay_enabled = confidence_decay_enabled
        self.hesitation_cycles = hesitation_cycles
        self.flip_confirm_count = flip_confirm_count
        self.min_hold_cycles = min_hold_cycles
        self.toxicity_velocity_mult = toxicity_velocity_mult
        self.kalman_velocity_threshold_bps = kalman_velocity_threshold_bps  # DQAF-20260731-002

        self.pnl_store = pnl_store
        self.meta_exit_engine = meta_exit_engine
        # DQAF-064 §3: stored for fractional cycle accumulator in _update_single_position
        self._loop_interval_seconds = loop_interval_seconds

        # Phase C: Risk Exit is a physically isolated subsystem
        if trail_policy is None:
            trail_policy = TrailPolicy(
                trail_atr_mult=trail_atr_mult,
                trail_atr_mult_low=trail_atr_mult_low,
                trail_atr_mult_high=trail_atr_mult_high,
                breakeven_threshold_atr=breakeven_threshold_atr,
                trail_activation_atr=trail_activation_atr,  # FIX-064
                min_step=min_step,
                min_trail_mult=min_trail_mult,
                max_lock_atr=max_lock_atr,
                graduated_lock_enabled=graduated_lock_enabled,
                graduated_lock_levels=graduated_lock_levels,
            )
        self._trail_engine = TrailStopEngine(
            default_policy=trail_policy,
            pnl_store=pnl_store,
        )

        self._positions: dict[int, ActivePosition] = {}  # ticket → position
        self._primary_ticket: int | None = None  # "primary" for backward compat
        self._last_brain_reeval_cycle: int = -1
        self._entry_consensus_score: float = 0.0
        self._last_state_path: str | None = None
        self._recovery_cycle: int = -1  # -1=normal, >=0=in grace period (increments each cycle)

        # v3.2 Knife 2: Drift Lock — per-direction spatial lock after mean-drift exit
        # Key: "long" or "short", Value: z-score threshold to unlock
        # When OU exit triggers with PnL < 0 (mean drifted, not price reverted),
        # same-direction re-entry is blocked until z crosses to the OPPOSITE side.
        self._drift_lock: dict[str, float] = {}

        # FIX-20260613-047: Pending Close Lock — prevent cross-cycle retry avalanche.
        # When ExitWatchdog fires for a ticket, subsequent management cycles must NOT
        # spawn fresh watchdog batches until the previous one has resolved (success
        # or timeout).  Maps ticket → cycle_count of first dispatch.
        # After PENDING_CLOSE_MAX_CYCLES, the lock auto-expires to allow retry.
        self._pending_close: dict[int, int] = {}

        # FIX-20260612-003: Close attempt counter — tracks how many close
        # dispatches have been issued for each ticket.  When the count exceeds
        # PENDING_CLOSE_FLOOD_THRESHOLD, the lock becomes permanent (phantom
        # flood guard).  Cleared when position is confirmed closed.
        self._close_attempt_count: dict[int, int] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get_pos(self, ticket: int | None = None) -> ActivePosition | None:
        """Return a specific position or the primary (backward compat)."""
        if ticket is not None:
            return self._positions.get(ticket)
        if self._primary_ticket is not None:
            return self._positions.get(self._primary_ticket)
        # Fallback: return any position
        for pos in self._positions.values():
            return pos
        return None

    @property
    def _position(self) -> ActivePosition | None:
        """Backward-compat property: returns primary position."""
        return self._get_pos()

    @_position.setter
    def _position(self, pos: ActivePosition) -> None:
        """Backward-compat setter: registers a position by its ticket."""
        self._positions[pos.ticket] = pos
        self._primary_ticket = pos.ticket

    # ── Public API ──────────────────────────────────────────────────────

    def has_position(self, ticket: int | None = None) -> bool:
        """True if any position exists (ticket=None) or if *ticket* is tracked."""
        if ticket is not None:
            return ticket in self._positions
        return len(self._positions) > 0

    def get_position(self, ticket: int | None = None) -> ActivePosition | None:
        """Return a specific position, the primary, or any (backward compat)."""
        return self._get_pos(ticket)

    def get_all_positions(self) -> list[ActivePosition]:
        """Return all tracked positions (for exit evaluation loops)."""
        return list(self._positions.values())

    def is_in_grace_period(self, max_cycles: int = 5) -> bool:
        """True during the first N cycles after a position is recovered from disk.

        During grace period the system skips Layer 2 (brain flip), Layer 2.5
        (Meta Exit), and Layer 3 (time decay) to give restarted features time
        to stabilize.  Layer 1 (trailing stop + hard SL) still runs normally.
        """
        return 0 <= self._recovery_cycle < max_cycles

    def clear_position(self, ticket: int | None = None) -> None:
        """Clear a specific position or all positions (ticket=None)."""
        if ticket is not None:
            self._positions.pop(ticket, None)
            self._pending_close.pop(ticket, None)
            self._close_attempt_count.pop(ticket, None)
            if self._primary_ticket == ticket:
                self._primary_ticket = (
                    next(iter(self._positions), None) if self._positions else None
                )
        else:
            self._positions.clear()
            self._pending_close.clear()
            self._close_attempt_count.clear()
            self._primary_ticket = None
            self._last_brain_reeval_cycle = -1
            self._entry_consensus_score = 0.0
        # ── FIX-20260615-Contract: Post-condition — atomic removal ──
        if ticket is not None:
            assert (
                ticket not in self._positions
            ), f"FATAL: ticket {ticket} still in _positions after clear"
            assert (
                ticket not in self._pending_close
            ), f"FATAL: ticket {ticket} still in _pending_close after clear"
        else:
            assert len(self._positions) == 0, "FATAL: _positions not empty after clear-all"
            assert len(self._pending_close) == 0, "FATAL: _pending_close not empty after clear-all"

        if not self._positions:
            self._recovery_cycle = -1
            if self._last_state_path is not None:
                try:
                    _p = Path(self._last_state_path)
                    if _p.exists():
                        _p.unlink()
                except OSError:
                    pass

    # ── FIX-20260613-047: Pending Close Lock ──────────────────────────────
    # Prevents the cross-cycle retry avalanche where each management cycle
    # spawns a fresh ExitWatchdog batch for the same ticket.  Once a close
    # has been dispatched, subsequent cycles must wait for it to resolve.
    #
    # FIX-20260612-003: Phantom flood guard — close_attempt_count tracks
    # cumulative dispatches per ticket.  When it exceeds the flood threshold,
    # the lock becomes permanent (prevents the 76-close/80min phantom flood
    # pattern observed on ticket 3807506009).

    PENDING_CLOSE_MAX_CYCLES: int = 10  # cycles before lock auto-expires for retry (was 3)
    PENDING_CLOSE_FLOOD_THRESHOLD: int = 3  # close attempts before permanent lock

    def mark_pending_close(self, ticket: int, cycle: int) -> None:
        """Record that a close has been dispatched for this ticket."""
        self._pending_close[ticket] = cycle
        self._close_attempt_count[ticket] = self._close_attempt_count.get(ticket, 0) + 1

    def is_pending_close(self, ticket: int, current_cycle: int) -> bool:
        """True if a close is in-flight and hasn't timed out yet.

        Returns False (allow retry) when:
        - Ticket is not in the pending set (never dispatched)
        - Lock has expired (> PENDING_CLOSE_MAX_CYCLES since dispatch)
          AND flood threshold hasn't been exceeded

        Returns True (permanent lock) when:
        - Close attempt count >= PENDING_CLOSE_FLOOD_THRESHOLD
          (phantom flood guard — prevents the 76-close/80min pattern)
        """
        dispatched_cycle = self._pending_close.get(ticket)
        if dispatched_cycle is None:
            return False
        # ── Phantom flood guard ──
        attempts = self._close_attempt_count.get(ticket, 0)
        if attempts >= self.PENDING_CLOSE_FLOOD_THRESHOLD:
            return True  # permanent lock — flood detected
        if current_cycle - dispatched_cycle >= self.PENDING_CLOSE_MAX_CYCLES:
            self._pending_close.pop(ticket, None)
            return False
        return True

    def clear_pending_close(self, ticket: int) -> None:
        """Explicitly release the pending lock (called on watchdog success)."""
        self._pending_close.pop(ticket, None)
        self._close_attempt_count.pop(ticket, None)

    def sync_position_volume(self, ticket: int, volume: float) -> None:
        """IC 2026-08-07 裁决 2b (Partial Fill State Machine): sync a tracked
        position's DISPATCH volume to the MT5 ground-truth residual after a
        partial fill.

        Only ``pos.volume`` is lowered — ``expected_remaining_volume`` stays at
        the FULL-close target so the management loop's partial-fill detection
        (residual < expected) can still see that a residual re-close is owed.
        """
        if volume <= 0:
            return
        p = self._positions.get(ticket)
        if p is not None:
            p.volume = float(volume)

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
        entry_z_score: float = 0.0,
        entry_half_life: float = 0.0,
        bracket_atr: float = 0.0,  # FIX-20260709-004: per-TF ATR that sized the bracket
        entry_consensus: dict[str, Any] | None = None,
        supporting_brain_ids: list[str] | None = None,
        model_horizons: dict[str, int] | None = None,
        current_high: float | None = None,
        partial_tp_r: float = 0.0,
        partial_tp_ratio: float = 0.5,
        ofi_partial_tp_threshold: float = 0.0,
        ofi_partial_tp_r_mult: float = 0.5,
        strategy_name: str = "",
        trail_atr_mult: float | None = None,
        trail_atr_mult_low: float | None = None,
        trail_atr_mult_high: float | None = None,
        breakeven_threshold_atr: float | None = None,
        trail_policy: TrailPolicy | None = None,  # Phase B: preferred over scattered attrs
        trail_activation_atr: float
        | None = None,  # DQAF-005: per-strategy override (None=use PM default)
        cold_explore: bool = False,  # bypass trailing for uncensored calibration labels
    ) -> ActivePosition:
        """Record a newly-opened position (or recover one after restart).

        Multi-position safe: each ticket gets its own slot.  If *ticket*
        already exists, its entry data is refreshed (idempotent).

        Phase B: trail_policy, when provided, is stored on the ActivePosition
        and becomes the single source of truth for all trail-related parameters.
        Individual trail_* arguments are still accepted for backward compat
        but are superseded by trail_policy when both are present.
        """
        # FIX-20260611-017: Initialize extremes from entry_price, not 0.0.
        # SHORT positions: lowest_low=entry_price ensures breakeven doesn't
        # fire prematurely (unrealized_r = (entry - lowest_low)/ATR = 0,
        # not (entry - 0)/ATR = 729 ATR!).  Updated each cycle by mgmt phase.
        high = current_high if current_high is not None else entry_price
        low = entry_price  # both directions start at entry — updated per cycle

        # Phase B: TrailPolicy is the preferred path
        if trail_policy is not None:
            _trail = trail_policy.trail_atr_mult
            _trail_low = trail_policy.trail_atr_mult_low
            _trail_high = trail_policy.trail_atr_mult_high
            _breakeven = trail_policy.breakeven_threshold_atr
        else:
            _trail = trail_atr_mult if trail_atr_mult is not None else self.trail_atr_mult
            _trail_low = (
                trail_atr_mult_low if trail_atr_mult_low is not None else self.trail_atr_mult_low
            )
            _trail_high = (
                trail_atr_mult_high if trail_atr_mult_high is not None else self.trail_atr_mult_high
            )
            _breakeven = (
                breakeven_threshold_atr
                if breakeven_threshold_atr is not None
                else self.breakeven_threshold_atr
            )
        # DQAF-005: Per-strategy trail_activation_atr override.
        # When provided without an explicit trail_policy, construct a
        # position-level TrailPolicy carrying the strategy-specific value.
        # Other trail params (decay, graduated lock, etc.) use TrailPolicy
        # defaults — they are not strategy-specific.
        if trail_policy is None and trail_activation_atr is not None:
            trail_policy = TrailPolicy(
                trail_atr_mult=_trail,
                trail_atr_mult_low=_trail_low,
                trail_atr_mult_high=_trail_high,
                breakeven_threshold_atr=_breakeven,
                trail_activation_atr=trail_activation_atr,
            )
        pos = ActivePosition(
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
            entry_z_score=entry_z_score,
            entry_half_life=entry_half_life,
            bracket_atr=bracket_atr,  # FIX-20260709-004: per-TF bracket sizing ATR
            entry_consensus=dict(entry_consensus or {}),
            supporting_brain_ids=list(supporting_brain_ids or []),
            model_horizons=dict(model_horizons or {}),
            trail_multiplier=_trail,
            partial_tp_r=partial_tp_r,
            partial_tp_ratio=partial_tp_ratio,
            ofi_partial_tp_threshold=ofi_partial_tp_threshold,
            ofi_partial_tp_r_mult=ofi_partial_tp_r_mult,
            strategy_name=strategy_name,
            expected_remaining_volume=volume,
            trail_atr_mult=_trail,
            trail_atr_mult_low=_trail_low,
            trail_atr_mult_high=_trail_high,
            breakeven_threshold_atr=_breakeven,
            trail_policy=trail_policy,  # Phase B: stored for compute_trail_stop / should_breakeven
            cold_explore=cold_explore,
        )
        self._entry_consensus_score = float(pos.entry_consensus.get("consensus_score", 0))
        # Seed EMA with entry confidence so first-cycle drop is measured
        # against a warm start, not zero
        pos.confidence_ema = self._entry_consensus_score
        pos.consecutive_flips = 0
        self._recovery_cycle = -1  # normal entry, no grace period

        self._positions[ticket] = pos
        if self._primary_ticket is None:
            self._primary_ticket = ticket
        return pos

    # ── DQAF-20260621-034 L3 Architecture Fix: utility, not hot-path ──
    # live_intent_loop.py recovery (L1164-1166) now unconditionally syncs
    # current_sl/current_tp from MT5 for ALL restored positions — this
    # method is no longer called in the management hot path.  Kept as a
    # utility for recovery scripts and manual debugging.

    def sync_position_from_mt5(self, ticket: int, mt5_worker: Any) -> bool:
        """Utility: sync position SL/TP/side/volume from MT5.

        Not called in the management hot path — ``live_intent_loop.py``
        recovery handles the one-time SL sync unconditionally for all
        restored positions (L1164-1166).

        Useful for recovery scripts, manual debugging, or future cold-start
        scenarios where a position enters management without going through
        the standard ``live_intent_loop`` recovery pipeline.

        Returns True if sync succeeded (SL now > 0).
        """
        if mt5_worker is None:
            return False
        try:
            positions = mt5_worker.positions_get(ticket=ticket)
            if not positions:
                return False
            _mt5_pos = positions[0]
            _sl = float(getattr(_mt5_pos, "sl", 0) or 0)
            _tp = float(getattr(_mt5_pos, "tp", 0) or 0)
            _type = int(getattr(_mt5_pos, "type", -1))
            _side = "long" if _type == 0 else ("short" if _type == 1 else "unknown")
            _vol = float(getattr(_mt5_pos, "volume", 0) or 0)

            # ── DQAF-034: XAU tick_size=0.01 → 3dp; BTC tick_size=1.0 → 1dp ──
            _sl = round(_sl, 3)
            _tp = round(_tp, 3)

            if ticket in self._positions:
                p = self._positions[ticket]
                if p.current_sl <= 0 and _sl > 0:
                    p.current_sl = _sl
                    if p.initial_sl <= 0:
                        p.initial_sl = _sl
                if p.current_tp <= 0 and _tp > 0:
                    p.current_tp = _tp
                    if p.initial_tp <= 0:
                        p.initial_tp = _tp
                if p.side == "unknown" and _side != "unknown":
                    p.side = _side
                if p.volume <= 0 and _vol > 0:
                    p.volume = _vol
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def update_prices(
        self,
        mid: float,
        bid: float,
        ask: float,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        cycle_count: int = 0,
        ticket: int | None = None,
        m5_high: float | None = None,
        m5_low: float | None = None,
        m5_spread_points: int = 0,
    ) -> dict[str, Any]:
        """Per-cycle update for one or all positions.  Returns aggregate info."""
        _spread = m5_spread_points * 0.001 if m5_spread_points > 0 else (ask - bid)
        if ticket is not None:
            return self._update_single_position(
                ticket,
                mid,
                bid,
                ask,
                current_atr,
                regime_info,
                m5_high=m5_high,
                m5_low=m5_low,
                spread=_spread,
            )

        # Update all positions
        result: dict[str, Any] = {}
        for t in list(self._positions):
            result[str(t)] = self._update_single_position(
                t,
                mid,
                bid,
                ask,
                current_atr,
                regime_info,
                m5_high=m5_high,
                m5_low=m5_low,
                spread=_spread,
            )
        return result

    def _update_single_position(
        self,
        ticket: int,
        mid: float,
        bid: float,
        ask: float,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        m5_high: float | None = None,
        m5_low: float | None = None,
        spread: float = 0.0,
    ) -> dict[str, Any]:
        """Update one position's cycle trackers and extremes.

        When M5 bar OHLC is available (m5_high/m5_low not None), uses
        bar extremes with bid/ask spread calibration to capture intra-cycle
        price extremes that instantaneous bid/ask would miss.  Otherwise
        falls back to the legacy instantaneous path (graceful degradation).
        """
        pos = self._positions.get(ticket)
        if pos is None:
            return {}

        # ── FIX-20260615-Contract: Runtime pre-conditions ──
        # These guards catch corrupted state BEFORE it poisons downstream logic.
        # Unit tests cover the evaluators (51.4%); these asserts defend the
        # orchestration layer (48.6%) at runtime — Fail-Fast, never trade blind.
        assert pos.ticket > 0, f"FATAL: invalid ticket {pos.ticket}"
        assert (
            pos.entry_price > 0
        ), f"FATAL: zero/negative entry_price {pos.entry_price} on ticket {pos.ticket}"
        assert (
            pos.current_sl > 0
        ), f"FATAL: zero/negative current_sl {pos.current_sl} on ticket {pos.ticket}"
        assert (
            pos.entry_atr > 0
        ), f"FATAL: zero/negative entry_atr {pos.entry_atr} on ticket {pos.ticket}"

        # ── DQAF-064 §3: Fractional cycle accumulator ─────────────────
        # The main loop runs at ~30s intervals; one M5 bar = 300s.
        # Previously cycles_held counted main-loop iterations (~10 per M5 bar),
        # causing watchdog hesitation/bleed_stop/time_exit to trigger ~10x too fast.
        # Now we accumulate wall-clock fractions and only increment cycles_held
        # when a full M5-bar-equivalent has elapsed.
        _M5_BAR_SECONDS = 300.0
        _loop_interval = float(getattr(self, "_loop_interval_seconds", 30.0) or 30.0)
        _tick = _loop_interval / _M5_BAR_SECONDS  # e.g. 30/300 = 0.1

        if not hasattr(pos, "_cycles_held_fractional"):
            pos._cycles_held_fractional = 0.0
        pos._cycles_held_fractional += _tick

        _new_cycles = int(pos._cycles_held_fractional)
        if _new_cycles > pos.cycles_held:
            pos.cycles_held = _new_cycles

        if self._recovery_cycle >= 0:
            self._recovery_cycle += 1

        # Track extremes — Pillar 1: OHLC-calibrated with graceful degradation
        if m5_high is not None and m5_low is not None:
            _eff_spread = spread if spread > 0 else (ask - bid)
            if pos.side == "long":
                pos.highest_high = max(pos.highest_high, m5_high)
                pos.lowest_low = min(pos.lowest_low, m5_low)
            else:  # short: add spread for Ask-equivalent (actual close price)
                pos.highest_high = max(pos.highest_high, m5_high + _eff_spread)
                pos.lowest_low = min(pos.lowest_low, m5_low + _eff_spread)
            # Update highest_r from OHLC-calibrated extremes
            _risk = abs(pos.entry_price - pos.initial_sl)
            if _risk > 1e-8:
                if pos.side == "long":
                    _extreme_r = (pos.highest_high - pos.entry_price) / _risk
                else:
                    _extreme_r = (pos.entry_price - pos.lowest_low) / _risk
                pos.highest_r = max(pos.highest_r, _extreme_r)
            r_now = self._compute_r_multiple(mid, ticket=ticket)
        else:
            # Graceful degradation: legacy instantaneous bid/ask
            pos.highest_high = max(pos.highest_high, bid if pos.side == "long" else ask)
            pos.lowest_low = min(pos.lowest_low, ask if pos.side == "long" else bid)
            r_now = self._compute_r_multiple(mid, ticket=ticket)
            pos.highest_r = max(pos.highest_r, r_now)

        # Adjust trail multiplier for regime
        self._adjust_trail_for_regime(current_atr, regime_info, ticket=ticket)

        return {
            "mid": mid,
            "current_atr": current_atr,
            "r_multiple": r_now,
            "highest_r": pos.highest_r,
        }

    # ── Layer 1: Chandelier trailing stop (delegated to TrailStopEngine) ──

    def compute_trail_stop(
        self,
        current_atr: float,
        ticket: int | None = None,
        pre_close_atr_mult_override: float | None = None,
    ) -> float | None:
        """Return new SL if the trail has advanced, else None.  Delegates to TrailStopEngine."""
        pos = self._get_pos(ticket)
        if pos is None:
            return None
        return self._trail_engine.compute_trail_stop(
            pos, current_atr, pre_close_atr_mult_override=pre_close_atr_mult_override
        )

    def compute_volatility_trail_sl(
        self,
        ticket: int | None = None,
        atr_ratio: float = 0.0,
    ) -> float | None:
        """TECH_DEBT-019 §2: symmetric volatility SL tightening.  Delegates to TrailStopEngine."""
        pos = self._get_pos(ticket)
        if pos is None:
            return None
        return self._trail_engine.compute_volatility_trail_sl(pos, atr_ratio)

    def get_effective_trail_policy(self, ticket: int | None = None) -> TrailPolicy:
        """Return the position's per-strategy TrailPolicy or the engine default.

        Mirrors the resolution inside compute_trail_tp — single accessor so
        trail_dispatch and the RR guard read the exact same policy the TP
        trail used (no drift between the two consumption points).
        """
        pos = self._get_pos(ticket)
        if pos is not None:
            _pol = pos.trail_policy
            if _pol is not None:
                return _pol
        return self._trail_engine.default_policy

    def should_breakeven(
        self,
        mid: float,
        current_atr: float,
        ticket: int | None = None,
        breakeven_threshold_mult_override: float | None = None,
    ) -> bool:
        """Return True when the favorable move exceeds the breakeven threshold.  Delegates to TrailStopEngine."""
        pos = self._get_pos(ticket)
        if pos is None or pos.breakeven_triggered:
            return False
        return self._trail_engine.should_breakeven(
            pos,
            current_atr,
            breakeven_threshold_mult_override=breakeven_threshold_mult_override,
        )

    def should_partial_tp(self, mid: float, ticket: int | None = None) -> tuple[bool, float, float]:
        """Return (trigger, close_volume, remaining_volume) if partial TP should fire."""
        pos = self._get_pos(ticket)
        if pos is None or pos.partial_tp_triggered or pos.partial_tp_r <= 0:
            return False, 0.0, 0.0

        r = self._compute_r_multiple(mid)
        if r >= pos.partial_tp_r:
            close_vol = round(pos.volume * pos.partial_tp_ratio, 2)
            remain_vol = round(pos.volume - close_vol, 2)
            return True, max(0.01, close_vol), max(0.01, remain_vol)
        return False, 0.0, 0.0

    def should_micro_partial_tp(
        self,
        mid: float,
        ofi_z: float,
        ticket: int | None = None,
    ) -> tuple[bool, float, float]:
        """Check if microstructure (OFI) signals justify early partial TP.

        Phase C Gate #3: When OFI z-score is extreme (signalling liquidity
        crunch / order flow toxicity), trigger partial TP at a lower
        R-multiple threshold than the normal static threshold.

        Args:
            mid: Current mid price.
            ofi_z: OFI z-score from microstructure computer (>2.0 = bearish
                   toxicity, <-2.0 = bullish toxicity).
            ticket: Position ticket (uses primary if None).

        Returns:
            (should_trigger, close_volume, remaining_volume)
        """
        pos = self._get_pos(ticket)
        if pos is None or pos.partial_tp_triggered or pos.partial_tp_r <= 0:
            return False, 0.0, 0.0

        # Read microstructure thresholds from position state
        ofi_threshold: float = float(getattr(pos, "ofi_partial_tp_threshold", 0.0) or 0.0)
        ofi_r_mult: float = float(getattr(pos, "ofi_partial_tp_r_mult", 0.5) or 0.5)
        if ofi_threshold <= 0:
            return False, 0.0, 0.0

        # Only trigger if OFI exceeds threshold AND R-multiple is above
        # reduced floor (e.g. 0.5x normal partial_tp_r when OFI > 2.5)
        if abs(ofi_z) < ofi_threshold:
            return False, 0.0, 0.0

        r = self._compute_r_multiple(mid)
        reduced_r = pos.partial_tp_r * ofi_r_mult
        if r < reduced_r:
            return False, 0.0, 0.0

        close_vol = round(pos.volume * pos.partial_tp_ratio, 2)
        remain_vol = round(pos.volume - close_vol, 2)
        return True, max(0.01, close_vol), max(0.01, remain_vol)

    def _compute_r_multiple(self, mid: float, ticket: int | None = None) -> float:
        """Current R-multiple (fraction of initial risk)."""
        pos = self._get_pos(ticket)
        if pos is None:
            return 0.0
        risk = abs(pos.entry_price - pos.initial_sl)
        if risk < 1e-8:
            return 0.0
        if pos.side == "long":
            return (mid - pos.entry_price) / risk
        else:
            return (pos.entry_price - mid) / risk

    def check_r_milestones(self, mid: float, ticket: int | None = None) -> str | None:
        """Return '1R', '2R', or '3R' if newly crossed, else None."""
        pos = self._get_pos(ticket)
        if pos is None:
            return None
        r = self._compute_r_multiple(mid)
        for milestone_r, tag in [(3.0, "3R"), (2.0, "2R"), (1.0, "1R")]:
            if r >= milestone_r and tag not in pos.r_milestones_hit:
                pos.r_milestones_hit.append(tag)
                return tag
        return None

    def _adjust_trail_for_regime(
        self,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        ticket: int | None = None,
    ) -> None:
        """Dynamically adjust trail multiplier.  Delegates to TrailStopEngine."""
        pos = self._get_pos(ticket)
        if pos is None:
            return
        self._trail_engine.adjust_trail_for_regime(pos, current_atr, regime_info)

    # Phase C: _compute_adaptive_trail_k and _compute_brain_specific_trail_scale
    # moved to TrailStopEngine.  They are called internally by the engine's
    # adjust_trail_for_regime() — manager no longer owns trail calculation logic.

    # ── Layer 2: Brain ensemble exit ────────────────────────────────────

    def evaluate_brain_exit(
        self,
        current_consensus: dict[str, Any],
        current_supporting: list[str],
        mid: float | None = None,
        ticket: int | None = None,
        *,
        kalman_velocity_bps: float | None = None,  # FIX-20260607-007: H1 Kalman velocity (bps)
    ) -> tuple[bool, str]:
        """Check if brain consensus has flipped against the entry direction.

        Three checks (in priority order):

        1. **Signal-reversal**: the FULL brain consensus direction now opposes
           the position side — immediate exit.  This catches cases where the
           aggregate brain opinion has reversed even if the original supporting
           brains haven't all flipped individually.

        2. **Support-flip**: previously-supporting brains have withdrawn
           support — requires ``flip_confirm_count`` consecutive detections
           (or immediate at ≥70% flip ratio).

        3. **EMA-filtered confidence decay**: smoothed confidence has fallen
           below entry confidence by more than ``confidence_drop_threshold``.
           EMA low-pass removes white noise while preserving trend sensitivity.

        4. **Absolute confidence floor** (FIX-20260629-187 / P1-2): EMA-smoothed
           confidence has fallen below ``confidence_floor`` regardless of drop
           magnitude.  Catches slow-drift degradation where the ensemble has
           lost conviction but each individual drop was below threshold.

        5. **Kalman velocity flip** (FIX-20260607-007): O(1) per-bar leading
           indicator — exits BEFORE price hits SL when Kalman velocity sign
           reverses.  Acts as a differential (D) term in the exit PID.

        Minimum-hold protection: during the first ``min_hold_cycles``, exits
        are suppressed unless the toxicity veto fires (price near hard SL).

        Returns (should_exit, reason).
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False, ""

        # ── Minimum-hold protection with toxicity veto ──
        if self._is_protected_period(ticket=ticket) and not self._toxicity_veto(
            mid if mid is not None else 0.0, ticket=ticket
        ):
            return False, "protected_min_hold"

        # ── 0. Kalman velocity flip (FIX-20260607-007) ──
        # Fast-path exit: when the Kalman velocity (1st derivative of price)
        # changes sign, the trend direction has reversed at the microstructure
        # level.  This is a LEADING indicator — price may still be moving in
        # the position's favor, but momentum has flipped.  Exiting here saves
        # spread + slippage vs waiting for the trail stop to fire.
        # Threshold: |velocity| > kalman_velocity_threshold_bps to reject noise.
        # DQAF-20260731-002: made per-strategy configurable (default 3.0 bps,
        # raised to 5.0 bps for XAU to reduce nervous triggering).
        _kv_threshold = self.kalman_velocity_threshold_bps
        if kalman_velocity_bps is not None and abs(kalman_velocity_bps) > _kv_threshold:
            if pos.side == "long" and kalman_velocity_bps < -_kv_threshold:
                return True, f"kalman_velocity_flip_long_v={kalman_velocity_bps:.1f}bps"
            elif pos.side == "short" and kalman_velocity_bps > _kv_threshold:
                return True, f"kalman_velocity_flip_short_v={kalman_velocity_bps:.1f}bps"

        # ── 1. Signal-reversal: full consensus opposes position ──
        consensus_dir = str(current_consensus.get("aggregated_bias", "neutral"))
        if consensus_dir not in ("long", "short", "neutral"):
            consensus_dir = "neutral"
        if consensus_dir != "neutral" and consensus_dir != pos.side:
            return True, f"signal_reversal_consensus_{consensus_dir}_vs_{pos.side}"

        entry_ids = set(pos.supporting_brain_ids)

        # ── 2. Flip check: how many previously-supporting brains flipped? ──
        flip_detected = False
        flip_ratio = 0.0
        single_brain = len(entry_ids) == 1
        if entry_ids:
            current_support_set = set(current_supporting)
            flipped = entry_ids - current_support_set
            flip_ratio = len(flipped) / len(entry_ids)
            if single_brain:
                # Single-brain strategy: require the brain to fully withdraw
                # (100% flip) — a momentary neutral is not enough.  Also
                # requires +1 extra consecutive confirmation (3 total).
                if flip_ratio >= 1.0:
                    flip_detected = True
            elif flip_ratio >= self.flip_exit_threshold:
                flip_detected = True

        # ── 3. EMA-filtered confidence decay ──
        # EMA low-pass filter removes high-frequency white noise while
        # preserving 30s sampling responsiveness.  A true reversal will
        # push the EMA below threshold within 3-4 cycles; a momentary
        # jitter is absorbed by the smoothing.
        current_score = float(current_consensus.get("consensus_score", 0))
        pos.confidence_ema = (
            pos.confidence_alpha * current_score + (1.0 - pos.confidence_alpha) * pos.confidence_ema
        )
        ema_drop = self._entry_consensus_score - pos.confidence_ema

        # ── Consecutive confirmation logic (flip only — reversal is immediate) ──
        if flip_detected:
            pos.consecutive_flips += 1
            _confirm_needed = self.flip_confirm_count + (1 if single_brain else 0)
            # Single-brain: never treat as "extreme" (100% is the only
            # possible flip_ratio for a single brain).  Always require
            # consecutive confirmation.
            if not single_brain and flip_ratio >= 0.70:
                pos.consecutive_flips = 0
                return True, f"brain_flip_extreme_{int(flip_ratio*100)}pct"
            # Exit after required consecutive flips
            if pos.consecutive_flips >= _confirm_needed:
                pos.consecutive_flips = 0
                return True, f"brain_flip_{int(flip_ratio*100)}pct_c{_confirm_needed}"
        else:
            pos.consecutive_flips = 0

        if self.confidence_decay_enabled and ema_drop > self.confidence_drop_threshold:
            return True, f"confidence_decay_ema_{ema_drop:.3f}"

        # ── P1-2 (FIX-20260629-187): p_win trajectory — absolute confidence floor ──
        # Even when the drop from entry hasn't reached confidence_drop_threshold,
        # if the EMA-smoothed confidence has fallen below the absolute floor, the
        # brain ensemble no longer has sufficient conviction.  This catches
        # slow-drift degradation where each individual drop is small but confidence
        # is now below the critical threshold (e.g. entry at 0.48, now at 0.32 —
        # a 0.16 drop that should trigger even if threshold is 0.10, but the first
        # 0.10 drop was absorbed by trend protection).
        #
        # confidence_floor=0 means disabled (backward compatible default).
        # Guard: only enforce the floor if entry confidence was above it.
        # Without this guard, a cold EMA (seeded at 0.0 when entry_consensus
        # is empty) would trigger an exit on the first evaluation cycle even
        # when current confidence is healthy.  See test_same_direction_
        # consensus_no_exit for the exact failure mode.
        if (
            self.confidence_floor > 0
            and self.confidence_decay_enabled
            and self._entry_consensus_score >= self.confidence_floor
            and pos.confidence_ema < self.confidence_floor
        ):
            return True, f"confidence_floor_{pos.confidence_ema:.3f}_lt_{self.confidence_floor:.2f}"

        return False, ""

    def should_reeval_brains(self, cycle_count: int) -> bool:
        """Return True when it's time to re-evaluate brain signals."""
        return cycle_count - self._last_brain_reeval_cycle >= self.brain_reeval_interval

    def mark_brains_reevaluated(self, cycle_count: int) -> None:
        self._last_brain_reeval_cycle = cycle_count

    def should_exit_ou_based(
        self, current_z_score: float, z_exit: float = 0.3, ticket: int | None = None
    ) -> tuple[bool, str]:
        """纯粹的均值回归平仓判定。

        只有极端偏离入场的单子（|entry_z| >= 1.5），
        才有资格因为 |current_z| < 0.3 而平仓。
        时间维度的风控交给 TimeStop 模块，此处不做 warmup。
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False, ""

        entry_z = abs(pos.entry_z_score)

        # Gate 1: 入场不极端 → 直接否决（来自非OU逻辑开仓或重启恢复的仓位）
        if entry_z < 1.5:
            return False, f"ou_ignored_low_entry_z_{entry_z:.2f}"

        # Gate 2: 当前仍处于极端 → 继续持有，等待回归
        if abs(current_z_score) >= z_exit:
            return False, "ou_waiting_for_reversion"

        # Gate 3: 从极端回归到均值 → 触发平仓
        return True, f"ou_revert_target_reached_z{abs(current_z_score):.2f}_from_{entry_z:.1f}"

    # ── Opt2: Z-score inflection entry gate (v3.2) ──

    @staticmethod
    def should_enter_inflection(
        current_z: float,
        prev_z: float | None,
        z_entry: float = 1.3,
    ) -> tuple[bool, str]:
        """Z-score must be moving back toward zero after crossing threshold.

        Long (z < -Z_ENTRY): require z > z_prev (inflecting up).
        Short (z > Z_ENTRY): require z < z_prev (inflecting down).

        Filters ~60% of signals while improving unweighted R by 34%.
        """
        if prev_z is None:
            return True, "inflection_first_bar_allowed"
        if abs(current_z) < z_entry:
            return False, f"inflection_below_threshold_z{current_z:.2f}"
        if current_z > z_entry and current_z >= prev_z:
            return False, f"inflection_short_no_peak_z{current_z:.2f}"
        if current_z < -z_entry and current_z <= prev_z:
            return False, f"inflection_long_no_trough_z{current_z:.2f}"
        return True, "inflection_confirmed"

    # ── Opt3: Time-bleed stop (v3.2) ──

    @staticmethod
    def should_exit_bleed(
        pos: ActivePosition,
        current_pnl_r: float,
        bleed_bars: int = 3,
        bleed_bars_override: int | None = None,
    ) -> tuple[bool, str]:
        """Exit if N consecutive bars have negative PnL since entry.

        Saves ~1.55R per trade vs waiting for hard_stop at -2.0R.

        bleed_bars_override: pre-close reduced window (applied externally via
        PreCloseContext.compute_effective_bleed_bars).  None → use normal value.
        """
        _eff_bars = bleed_bars_override if bleed_bars_override is not None else bleed_bars
        # DQAF-064 §3: Only append bar_pnls on integer cycle boundaries.
        # Previously this appended every main-loop tick (~30s), causing
        # bleed_stop to accumulate negative bars ~10x too fast.
        if pos.cycles_held > pos._last_bar_cycle:
            pos.bar_pnls.append(current_pnl_r)
            pos._last_bar_cycle = pos.cycles_held
        if len(pos.bar_pnls) > _eff_bars:
            pos.bar_pnls = pos.bar_pnls[-_eff_bars:]
        if len(pos.bar_pnls) >= _eff_bars and all(p < 0 for p in pos.bar_pnls):
            pos.bleed_triggered = True
            return True, f"bleed_stop_{_eff_bars}bars_neg"
        return False, ""

    # ── Knife 2: Drift Lock (v3.2) ──

    def is_direction_locked(self, direction: str, current_z: float) -> tuple[bool, str]:
        """Check if *direction* is locked by a prior mean-drift exit.

        Returns (is_locked, reason).  A lock expires when z crosses to the
        OPPOSITE side of zero, confirming the mean has relocated.

        Lock semantics:
          - "long" locked → blocked until z > +1.0 (price pushed to upside,
            shorts got hit, long opportunity is real again)
          - "short" locked → blocked until z < -1.0
        """
        unlock_target = self._drift_lock.get(direction)
        if unlock_target is None:
            return False, ""

        if direction == "long":
            if current_z >= unlock_target:
                self._drift_lock.pop("long", None)
                return False, "drift_lock_long_released"
            return True, f"drift_lock_long_active_z{current_z:.2f}_need_{unlock_target}"
        else:
            if current_z <= unlock_target:
                self._drift_lock.pop("short", None)
                return False, "drift_lock_short_released"
            return True, f"drift_lock_short_active_z{current_z:.2f}_need_{unlock_target}"

    def set_drift_lock(self, exit_direction: str, exit_pnl_r: float, exit_z: float) -> None:
        """Set drift lock after an OU exit.

        Only locks when PnL < 0 — the exit was mean-drift (MA moved toward
        price), NOT genuine price reversion.  Profit-taking exits do NOT lock.

        Lock targets:
          - Long exit → lock "long" entry until z > +1.0
          - Short exit → lock "short" entry until z < -1.0
        """
        if exit_pnl_r >= 0:
            return  # profitable exit = genuine reversion, no lock

        if exit_direction == "long":
            self._drift_lock["long"] = 1.0
        elif exit_direction == "short":
            self._drift_lock["short"] = -1.0

    def clear_drift_lock(self, direction: str | None = None) -> None:
        """Clear drift lock(s).  If *direction* is None, clear all."""
        if direction is None:
            self._drift_lock.clear()
        else:
            self._drift_lock.pop(direction, None)

    # ── Knife 1: Volume Climax Check (v3.2) ──

    @staticmethod
    def check_volume_climax(
        current_tick_volume: float,
        prev_tick_volume: float | None,
        recent_tick_volumes: list[float] | None = None,
        *,
        current_high: float = 0.0,
        current_low: float = 0.0,
        current_open: float = 0.0,
        current_close: float = 0.0,
        lookback: int = 20,
        climax_mult: float = 2.0,
        wick_ratio: float = 0.50,
    ) -> tuple[bool, str]:
        """Check if the current bar shows a distinctive volume pattern at inflection.

        Two valid patterns (either confirms genuine exhaustion, not fake-out):
          a) Volume contraction: current tick_volume < previous bar
             → selling/buying pressure exhausted.
          b) Volume climax + absorption wick: tick_volume > climax_mult × lookback
             mean AND wick > wick_ratio of total range → institutional passive
             wall absorbed the move.

        Normal volume at a z-score inflection = likely fake turnaround → skip.
        """
        if prev_tick_volume is None:
            return False, "vol_insufficient_data"

        cv = float(current_tick_volume)
        pv = float(prev_tick_volume)

        # Pattern A: Volume contraction — exhaustion
        if cv < pv:
            return True, "vol_contraction"

        # Pattern B: Volume climax + long wick — absorption
        if recent_tick_volumes and len(recent_tick_volumes) >= lookback:
            mean_vol = float(np.mean(recent_tick_volumes[-lookback:]))
            if mean_vol > 0 and cv > climax_mult * mean_vol:
                body = abs(float(current_close) - float(current_open))
                total_range = float(current_high) - float(current_low)
                if total_range > 0 and body / total_range < wick_ratio:
                    return True, "vol_climax_absorption"

        # Normal volume = fake inflection
        return False, "vol_normal"

    # ── Knife 3: Alpha Handoff (v3.2) ──

    def should_handoff_ou_to_trail(
        self,
        current_z: float,
        mid: float,
        adx: float | None = None,
        hurst: float | None = None,
        *,
        min_r_for_handoff: float = 1.0,
        min_adx: float = 25.0,
        min_hurst: float = 0.50,
        min_peak_r: float = 2.5,
    ) -> tuple[bool, str]:
        """Check if OU exit should be bypassed in favor of trailing stop.

        When |z| < 0.3 (OU says exit) but the position has strong unrealized
        profit AND the trend is still pushing in our favor, closing is premature.
        Instead, switch to trailing stop to let the trend carry further.

        Conditions:
          - PnL > +1.0R (real profit, not just z-score noise)
          - ADX > 25 OR Hurst > 0.50 OR highest_r > 2.5 (trend is real)
          - |z| < 0.3 (OU exit band — the handoff trigger zone)
        """
        pos = self._position
        if pos is None:
            return False, ""
        if pos.ou_handoff_active:
            return False, "handoff_already_active"

        r_now = self._compute_r_multiple(mid)
        if r_now < min_r_for_handoff:
            return False, f"handoff_r_too_low_r{r_now:.2f}"

        trend_strong = False
        if adx is not None and adx >= min_adx:
            trend_strong = True
        if hurst is not None and hurst >= min_hurst:
            trend_strong = True
        if pos.highest_r >= min_peak_r:
            trend_strong = True  # peak profit proves trend was real

        if not trend_strong:
            return False, "handoff_trend_too_weak"

        # Direction check: trend must be aligned with position
        if pos.side == "long" and current_z > 0:
            return False, "handoff_long_z_wrong_sign"
        if pos.side == "short" and current_z < 0:
            return False, "handoff_short_z_wrong_sign"

        return True, f"handoff_ou_to_trail_r{r_now:.2f}"

    def activate_handoff(self, mid: float) -> None:
        """Activate alpha handoff: bypass OU exit, use trailing stop instead."""
        pos = self._position
        if pos is None:
            return
        pos.ou_handoff_active = True
        pos.ou_handoff_r = self._compute_r_multiple(mid)
        # Force breakeven SL floor (entry + 0.1 ATR) so handoff doesn't give
        # back what was already earned.
        # PER_TF: be_sl offset uses bracket_atr (per-TF bracket sizing ATR)
        # so high-TF positions get a proportional lock instead of M5-micro.
        _be_atr = pos.bracket_atr or pos.entry_atr
        if not pos.breakeven_triggered and _be_atr > 0:
            if pos.side == "long":
                be_sl = pos.entry_price + 0.1 * _be_atr
                if be_sl > pos.current_sl:
                    pos.current_sl = round(be_sl, 3)
            else:
                be_sl = pos.entry_price - 0.1 * _be_atr
                if be_sl < pos.current_sl:
                    pos.current_sl = round(be_sl, 3)
            pos.breakeven_triggered = True

    # ── Z-score dynamic exit (v3.1) ──

    def should_exit_zscore_dynamic(
        self,
        current_z_score: float,
        mid: float,
        current_atr: float,
        bars_held: int | None = None,
        m1_candles: list[dict[str, float]] | None = None,
        z_entry: float = 1.3,
        z_exit: float = 0.3,
        max_hold: int = 8,
    ) -> tuple[bool, str]:
        """PnL-aware dynamic Z-score exit with toxic flow stop.

        Decision tree:
          1. |z| < z_exit AND PnL > 0 → profitable reversion → EXIT
          2. |z| < z_exit AND PnL < 0 → mean drift trap → DO NOT EXIT
             → fall through to toxic flow / time-based exit
          3. bars_held >= 6 → check toxic flow (M1 extreme momentum against position)
          4. bars_held >= max_hold → hard deadline → EXIT
          5. PnL <= -2.0 ATR any time → hard stop → EXIT

        Returns (should_exit, reason).
        """
        pos = self._position
        if pos is None:
            return False, ""

        entry_z = abs(pos.entry_z_score)
        if entry_z < z_entry:
            return False, f"z_dyn_ignored_low_entry_z_{entry_z:.2f}"

        r_now = self._compute_r_multiple(mid)
        effective_bars = bars_held if bars_held is not None else pos.cycles_held

        # ── 1. Profitable reversion ──
        if abs(current_z_score) < z_exit:
            if r_now > 0:
                return True, f"z_reversion_profit_z{abs(current_z_score):.2f}_r{r_now:.2f}"
            else:
                # Mean drift trap detected — log it
                self._log_mean_drift(entry_z, current_z_score, effective_bars, r_now, mid)
                # Fall through to toxic flow / time checks below
                # (explicitly NOT exiting here)

        # ── 2. Hard stop: PnL <= -2.0 ATR ──
        if r_now <= -2.0:
            return True, f"z_hard_stop_r{r_now:.2f}"

        # ── 3. Toxic flow stop (bars 6+) ──
        if effective_bars >= 6 and effective_bars < max_hold:
            toxic = self._detect_toxic_flow(m1_candles, pos.side, current_atr)
            if toxic:
                return True, f"z_toxic_flow_bar{effective_bars}_r{r_now:.2f}"

        # ── 4. Hard deadline ──
        if effective_bars >= max_hold:
            return True, f"z_deadline_bar{effective_bars}_r{r_now:.2f}"

        return False, "z_holding"

    # ── Toxic flow detection ──

    @staticmethod
    def _detect_toxic_flow(
        candles: list[dict[str, float]] | None,
        side: str,
        atr: float,
    ) -> bool:
        """Detect extreme one-sided momentum against the position.

        When short: 2 consecutive bullish engulfing M5 bars (body > 0.3 × ATR,
        close > previous high) signal toxic flow against the position.

        When long: 2 consecutive bearish engulfing M5 bars signal toxic flow.

        Falls back to M5 bars in `candles`; if none provided, returns False.
        """
        if not candles or len(candles) < 2:
            return False

        body_threshold = 0.3 * atr
        last_two = candles[-2:]

        bodies: list[float] = []
        for c in last_two:
            o = float(c.get("open", 0))
            cl = float(c.get("close", 0))
            bodies.append(abs(cl - o))

        if side == "short":
            # Looking for bullish engulfing: both bars close above open, second bar
            # engulfs first (higher high, lower low), bodies above threshold
            for i in range(2):
                o = float(last_two[i].get("open", 0))
                cl = float(last_two[i].get("close", 0))
                if cl <= o or bodies[i] < body_threshold:
                    return False
            # Check engulfing on second bar
            h0 = float(last_two[0].get("high", 0))
            l0 = float(last_two[0].get("low", 0))
            h1 = float(last_two[1].get("high", 0))
            l1 = float(last_two[1].get("low", 0))
            if h1 > h0 and l1 < l0:
                return True
        elif side == "long":
            # Looking for bearish engulfing: both bars close below open
            for i in range(2):
                o = float(last_two[i].get("open", 0))
                cl = float(last_two[i].get("close", 0))
                if cl >= o or bodies[i] < body_threshold:
                    return False
            h0 = float(last_two[0].get("high", 0))
            l0 = float(last_two[0].get("low", 0))
            h1 = float(last_two[1].get("high", 0))
            l1 = float(last_two[1].get("low", 0))
            if h1 > h0 and l1 < l0:
                return True

        return False

    # ── Mean drift logging ──

    def _log_mean_drift(
        self,
        entry_z: float,
        current_z: float,
        bars_held: int,
        pnl_r: float,
        mid: float,
    ) -> None:
        """Log mean drift trap event for future escape-hatch optimisation.

        Mean drift = |z| < 0.3 but PnL < 0 — the MA caught up with price,
        not a real reversion.  This is a data goldmine for optimising
        escape-hatch logic.
        """
        pos = self._position
        drift_magnitude = abs(entry_z) - abs(current_z) if pos is not None else 0.0

        record: dict[str, object] = {
            "event": "mean_drift_trap",
            "entry_z": round(entry_z, 4),
            "current_z": round(current_z, 4),
            "drift_magnitude": round(drift_magnitude, 4),
            "bars_held": bars_held,
            "pnl_r": round(pnl_r, 4),
            "mid": round(mid, 2),
        }
        if pos is not None:
            record["entry_price"] = round(pos.entry_price, 2)
            record["side"] = pos.side
            record["entry_z_score"] = round(pos.entry_z_score, 4)
            if pos.entry_atr > 0:
                record["price_move_vs_entry"] = round((mid - pos.entry_price) / pos.entry_atr, 4)

        import json as _json

        print(
            _json.dumps(record, ensure_ascii=False, default=str),
            flush=True,
        )

    # ── Layer 2.5: Meta-model multi-factor exit ────────────────────────

    def evaluate_meta_exit(
        self,
        mid: float,
        current_atr: float,
        regime_info: dict[str, Any] | None = None,
        current_consensus: dict[str, Any] | None = None,
        current_supporting: list[str] | None = None,
        ticket: int | None = None,
    ) -> ExitEvaluation | None:
        """Multi-factor exit evaluation using MetaExitEngine.

        When a trained model is available, uses ML inference for P(win).
        Otherwise falls back to heuristic scoring (PnL + time + regime +
        consensus + volatility).

        Returns ExitEvaluation if the engine recommends exit, None otherwise.
        """
        if self.meta_exit_engine is None:
            return None

        pos = self._get_pos(ticket)
        if pos is None:
            return None

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
            prev_r=round(pos.prev_r, 4),
            peak_r=round(pos.highest_r, 4),
            drawdown_r=round(max(0.0, pos.highest_r - r_now), 4),
            # Time state
            cycles_held=pos.cycles_held,
            expected_horizon=self._get_effective_horizon(),
            time_ratio=round(pos.cycles_held / max(self._get_effective_horizon(), 1), 4),
            # Regime state
            regime=reg.get("regime", "normal"),
            regime_confidence=float(reg.get("regime_confidence", 0.0)),
            trend_aligned=self._is_trend_aligned(reg, position_side=pos.side),
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

        # ── FIX-20260608-006: MetaExit shadow telemetry ──
        # Write the full ExitFeatureSnapshot + MetaExit prediction to a
        # dedicated JSONL file for future model retraining.  This closes
        # the train-serve feature gap: the training script can read this
        # file and use the SAME 20 features the runtime engine uses.
        self._write_meta_exit_telemetry(
            snap, evaluation, pos.ticket, data_dir=getattr(self, "_data_dir", "data")
        )

        # Save current R for next cycle's trajectory comparison
        pos.prev_r = round(r_now, 4)

        if evaluation.should_exit:
            return evaluation

        return None

    @staticmethod
    def _write_meta_exit_telemetry(
        snap: Any,
        evaluation: Any,
        ticket: int,
        data_dir: str = "data",
    ) -> None:
        """Write ExitFeatureSnapshot + MetaExit prediction to telemetry log.

        FIX-20260608-006: This bridges the train-serve feature gap.  The
        training script (train_exit_metamodel.py) currently uses only 8
        journal-level features.  This log records the full 20-feature
        ExitFeatureSnapshot that the runtime engine actually consumes,
        enabling future retraining with complete feature parity.

        Written to ``<data_dir>/meta_exit_snapshots.jsonl`` (append-only, JSONL).
        One line per management-cycle evaluation.  Zero impact on existing
        consumers — this is a dedicated, purpose-built training dataset.
        """
        import json as _json
        import time as _time
        from pathlib import Path as _Path

        # ── DQAF-20260615-007: Per-asset data_dir ──
        _path = _Path(data_dir) / "meta_exit_snapshots.jsonl"
        try:
            _path.parent.mkdir(parents=True, exist_ok=True)
            _record = {
                "ticket": ticket,
                "timestamp_utc": _time.time(),
                # ── Full ExitFeatureSnapshot (20 dims) ──
                "snapshot": {
                    "current_r": snap.current_r,
                    "prev_r": snap.prev_r,
                    "peak_r": snap.peak_r,
                    "drawdown_r": snap.drawdown_r,
                    "pnl_pct": snap.pnl_pct,
                    "cycles_held": snap.cycles_held,
                    "expected_horizon": snap.expected_horizon,
                    "time_ratio": snap.time_ratio,
                    "regime": snap.regime,
                    "regime_confidence": snap.regime_confidence,
                    "trend_aligned": snap.trend_aligned,
                    "atr_current": snap.atr_current,
                    "atr_entry": snap.atr_entry,
                    "atr_expansion": snap.atr_expansion,
                    "entry_consensus_score": snap.entry_consensus_score,
                    "entry_supporting_count": snap.entry_supporting_count,
                    "current_supporting_count": snap.current_supporting_count,
                    "consensus_drift": snap.consensus_drift,
                    "side": snap.side,
                    "symbol": snap.symbol,
                },
                # ── MetaExit prediction ──
                "meta_exit": {
                    "exit_urgency": evaluation.exit_urgency,
                    "should_exit": evaluation.should_exit,
                    "exit_reason": evaluation.exit_reason,
                    "p_win": evaluation.p_win,
                    "factor_breakdown": evaluation.factor_breakdown,
                },
            }
            with open(_path, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_record, ensure_ascii=False, default=str) + "\n")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass  # telemetry failure must never block trading

    @staticmethod
    def _is_trend_aligned(regime_info: dict[str, Any], *, position_side: str = "") -> bool:
        """Check if the current regime's trend direction matches position side."""
        pos_side = position_side or regime_info.get("_position_side", "")
        trend_dir = regime_info.get("trend_direction", "")
        if not pos_side or not trend_dir:
            return True  # unknown → assume aligned
        return pos_side == trend_dir

    def compute_trail_tp(
        self,
        current_atr: float,
        ticket: int | None = None,
        mid: float | None = None,
        disable_dynamic_tp: bool = False,
    ) -> float | None:
        """Return new TP on ATR contraction (tighten) or recovery (expand).

        Legacy contract: TP only moves INWARD (closer to entry) — never widens.
        TECH_DEBT-019 §3: with an RR contract (``tp_min_rr_ratio`` > 0), ATR
        recovery elastically re-expands TP outward, capped at ``initial_tp`` and
        the Proximity home-stretch gate.  §1 keeps the trailing TP distance
        >= ``min_rr`` × current SL distance (RR hard floor).

        disable_dynamic_tp: pre-close aggressive phase guard (CRITICAL CORRECTION 1).
        When True, return None immediately — keep existing TP unchanged.  Setting
        tp_mult=0.0 would compute tp_price ≈ entry_price, triggering MT5 Error
        10016 (Invalid Stops) because the price is within the broker's Stop Level.
        Boolean truncation delegates position termination to the rapidly-tightening
        trail SL instead.

        **FIX-20260713-008 (L2): SL-Anchored TP + Proximity Gate + TP Floor + Debounce**

        The pre-008 TP trail was price-blind: it anchored solely to
        ``entry_price`` and had only one trigger (ATR contraction ≤ 0.80).
        The SL trail (Chandelier) was price-aware (highest_high / lowest_low)
        with 5 layers of protection, creating a structural asymmetry where
        SL locked *more* profit while TP surrendered potential profit on the
        same position.  This fix brings structural parity:

        1. **Dynamic SL Anchor**: TP anchor follows the current SL — which is
           already price-aware via Chandelier highest_high/lowest_low.
           Pre-breakeven (SL ≤ entry): anchor = entry_price (legacy behaviour).
           Post-breakeven (SL > entry): anchor = current_sl — TP rides the
           same price-sensing rail as the SL, structurally preventing
           Bracket Inversion.

        2. **Proximity Gate** (Home-Stretch Guard): when price has covered
           ≥ ``tp_proximity_ratio`` of the entry→current-TP journey, TP
           tightening is suppressed — the runner is in the home stretch
           and the finish line must not move backward.

        3. **TP Floor**: ``tp_min_distance_atr × bracket_atr`` minimum
           distance from the anchor — the structural counterpart to the
           SL's ``max_lock_atr`` ceiling.

        4. **Min-step Debounce**: ``tp_min_step`` guards against MT5
           NO_CHANGES (10025) retries on sub-pip TP adjustments.

        Active by default (全盤激活 2026-07-13).  Set to 0.0 on the strategy's
        ``TrailPolicy`` to opt out of individual protections.
        Resolved from the position's per-strategy ``TrailPolicy``.
        """
        pos = self._get_pos(ticket)
        if pos is None or pos.entry_atr <= 0 or current_atr <= 0:
            return None

        # ── CRITICAL CORRECTION 1: Pre-close TP suppression ──
        # Boolean truncation — keep existing TP, delegate termination to trail SL.
        # NEVER compute tp_mult=0.0 → tp_price ≈ entry_price → MT5 Error 10016.
        if disable_dynamic_tp:
            return None

        # ── FIX-20260707-009: Bracket inversion pre-check ──
        if pos.current_sl > 0 and pos.current_tp > 0:
            if pos.side == "long" and pos.current_sl >= pos.current_tp:
                return 0.0  # TP yields to trailing SL
            if pos.side == "short" and pos.current_sl <= pos.current_tp:
                return 0.0

        # ── FIX-20260710-001 (L3): Profitability Gate ──
        if pos.side == "long" and pos.highest_high <= pos.entry_price:
            return None  # never profitable — leave original TP alone
        if pos.side == "short" and pos.lowest_low >= pos.entry_price:
            return None  # never profitable — leave original TP alone

        atr_ratio = current_atr / pos.entry_atr

        # ── FIX-20260709-004 (L3): bracket_atr per-TF scaling ──
        _bracket_atr = getattr(pos, "bracket_atr", 0.0) or 0.0
        _tf_scale = (
            (_bracket_atr / pos.entry_atr) if (_bracket_atr > 0 and pos.entry_atr > 0) else 1.0
        )
        _trail_mult = getattr(pos, "trail_atr_mult", self.trail_atr_mult)
        tp_distance = _trail_mult * current_atr * 1.75 * _tf_scale

        # ── FIX-20260713-008: resolve per-strategy TP trail policy ──
        # Falls back to the engine's default_policy when the position carries
        # no explicit trail_policy (e.g. legacy positions, direct registrations).
        _tp = getattr(pos, "trail_policy", None)
        if _tp is None:
            _tp = self._trail_engine.default_policy
        _proximity_ratio = getattr(_tp, "tp_proximity_ratio", 0.0)
        _min_distance_atr = getattr(_tp, "tp_min_distance_atr", 0.0)
        _min_step = getattr(_tp, "tp_min_step", 0.0)
        # TECH_DEBT-019: RR contract gate — 0.0 = disabled (structural/legacy
        # zero-change); > 0 arms the RR hard floor + elastic expansion.
        _min_rr = getattr(_tp, "tp_min_rr_ratio", 0.0)
        _elastic = _min_rr > 0

        # ── TECH_DEBT-019 §3: elastic-expansion gate ──
        # Legacy contract: ATR recovery (ratio > 0.80) → None (TP only shrinks).
        # With an RR contract (min_rr > 0), ATR recovery >= 0.85 re-expands TP
        # outward (handled in the branches); the 0.80–0.85 dead band suppresses
        # movement in BOTH directions to prevent ATR-trigger oscillation.
        if atr_ratio > 0.80 and (not _elastic or atr_ratio < _EXPAND_THRESHOLD):
            return None

        # ── FIX-20260713-008 §1: Dynamic SL Anchor ──
        # Anchor follows the current SL — which already tracks highest_high
        # (LONG) or lowest_low (SHORT) via the Chandelier trail.  When SL
        # has not yet advanced past entry (pre-breakeven / pre-trail),
        # anchor = entry_price = legacy behaviour.
        if pos.side == "long":
            _sl = pos.current_sl if pos.current_sl > 0 else pos.entry_price
            anchor = max(_sl, pos.entry_price)
        else:
            _sl = pos.current_sl if pos.current_sl > 0 else pos.entry_price
            anchor = min(_sl, pos.entry_price)

        if pos.side == "long":
            candidate = anchor + tp_distance

            # ── FIX-20260713-008 §2: Proximity Gate (Home-Stretch Guard) ──
            # Shared by tighten AND expand — the finish line must not move once
            # price has covered >= tp_proximity_ratio of the entry→TP journey.
            if _proximity_ratio > 0 and mid is not None and mid > 0:
                _journey_total = pos.current_tp - pos.entry_price  # entry→TP total distance
                _journey_covered = mid - pos.entry_price  # how far price has already run
                if _journey_total > 0 and _journey_covered > 0:
                    if (_journey_covered / _journey_total) >= _proximity_ratio:
                        return None  # home stretch — do not move the finish line

            # ── FIX-20260707-009: Bracket inversion candidate check ──
            if pos.current_sl > 0 and candidate <= pos.current_sl:
                return 0.0

            # ── FIX-20260713-008 §3: TP Floor ──
            if _min_distance_atr > 0 and _bracket_atr > 0:
                _floor = anchor + _min_distance_atr * _bracket_atr
                candidate = max(candidate, _floor)

            # ── TECH_DEBT-019 §1: RR hard floor (long) ──
            # TP distance (from entry) must stay >= min_rr × current SL distance.
            # Clamped by initial_tp so a deep SL can never push TP past the
            # opening target (training contract).  Entry-time check_minimum_rr
            # guarantees initial RR >= min_rr, so this cap rarely binds.
            if _min_rr > 0:
                _rr_floor = compute_rr_floor_price(
                    pos.side, pos.entry_price, pos.current_sl, _min_rr
                )
                if _rr_floor is not None:
                    pos.rr_floor_tp = _rr_floor
                    candidate = max(candidate, _rr_floor)
                    if pos.initial_tp > 0:
                        candidate = min(candidate, pos.initial_tp)

            # ── TECH_DEBT-019 §3: elastic expansion (long) ──
            if atr_ratio <= 0.80:
                # tighten — only inward
                if candidate < pos.current_tp:
                    # ── FIX-20260713-008 §4: Min-step Debounce ──
                    if _min_step > 0 and abs(candidate - pos.current_tp) < _min_step:
                        return None
                    return round(candidate, 3)
                return None
            # expand — only outward, never past initial_tp
            if candidate > pos.current_tp:
                if _min_step > 0 and abs(candidate - pos.current_tp) < _min_step:
                    return None
                return round(candidate, 3)
            return None
        else:
            candidate = anchor - tp_distance

            # ── FIX-20260713-008 §2: Proximity Gate (short) — shared tighten/expand ──
            if _proximity_ratio > 0 and mid is not None and mid > 0:
                _journey_total = pos.entry_price - pos.current_tp
                _journey_covered = pos.entry_price - mid
                if _journey_total > 0 and _journey_covered > 0:
                    if (_journey_covered / _journey_total) >= _proximity_ratio:
                        return None

            if pos.current_sl > 0 and candidate >= pos.current_sl:
                return 0.0

            # ── FIX-20260713-008 §3: TP Floor (short) ──
            # SHORT: lower number = further from entry = more aggressive.
            # The floor anchors a MINIMUM distance from entry — TP must NOT
            # be more aggressive (lower) than the floor.  max() picks the
            # more conservative (higher) of the two.
            if _min_distance_atr > 0 and _bracket_atr > 0:
                _floor = anchor - _min_distance_atr * _bracket_atr
                candidate = max(candidate, _floor)

            # ── TECH_DEBT-019 §1: RR hard floor (short) ──
            # The two floors pull in OPPOSITE directions for SHORT: anchor floor
            # uses max() (don't be too far from entry), RR floor uses min()
            # (TP must stay at least min_rr × SL distance from entry).  Applied
            # strictly in this order, then capped at initial_tp.
            if _min_rr > 0:
                _rr_floor = compute_rr_floor_price(
                    pos.side, pos.entry_price, pos.current_sl, _min_rr
                )
                if _rr_floor is not None:
                    pos.rr_floor_tp = _rr_floor
                    candidate = min(candidate, _rr_floor)
                    if pos.initial_tp > 0:
                        candidate = max(candidate, pos.initial_tp)

            # ── TECH_DEBT-019 §3: elastic expansion (short) ──
            if atr_ratio <= 0.80:
                # tighten — only inward (TP moves up toward entry)
                if candidate > pos.current_tp:
                    # ── FIX-20260713-008 §4: Min-step Debounce ──
                    if _min_step > 0 and abs(candidate - pos.current_tp) < _min_step:
                        return None
                    return round(candidate, 3)
                return None
            # expand — only outward (TP moves down), never past initial_tp
            if candidate < pos.current_tp:
                if _min_step > 0 and abs(candidate - pos.current_tp) < _min_step:
                    return None
                return round(candidate, 3)
            return None

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

    def should_exit_time_based(
        self,
        mid: float,
        override_horizon: int | None = None,
        override_min_r: float | None = None,
        ticket: int | None = None,
    ) -> tuple[bool, str]:
        """Gamma-parameterised EV trajectory envelope — Alpha-trajectory-aware exit.

        Replaces the old one-size-fits-all sqrt curve with a family of power-law
        progress curves keyed by strategy archetype:

            Progress = (t / T_max) ** gamma
            EV_floor(t) = start_floor + (end_target − start_floor) × Progress

        Gamma controls the early-vs-late distribution of the trajectory demand:
          - Breakout / Momentum (barrier_*):     γ = 0.5  concave — demanding early
          - Trend Following    (swing):          γ = 1.0  linear   — steady
          - Mean Reversion     (statarb_*):      γ = 2.0  convex   — lenient early, sharp late

        The hardcoded grace-period cliff (``t_ratio < 0.10``) is removed — the
        continuous curve handles early-cycle tolerance naturally through
        ``start_floor`` and ``gamma``.

        ``override_min_r`` (YAML ``min_r_for_hold``) defines the envelope
        endpoint: at expiry the position must deliver at least this R to
        justify staying in.  When unset the design R:R (TP/SL) is used.
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False, ""

        # ── P0: DQAF-20260722-001 — EV trajectory requires prior profitability ──
        # Positions that have NEVER been profitable must rely on the hard
        # stop-loss, not the EV envelope.  The ev_floor is monotonic increasing
        # (gamma∈[0.5,2.0]), so a position that never reaches r>0 is
        # MATHEMATICALLY GUARANTEED to exit at a loss — often worse than the
        # hard SL would have produced (avg −$3.96 vs SL avg −$1.13).
        # Evidence: 36 unique EV trajectory exits across BTC/XAU, 0% win rate.
        if pos.highest_r <= 0.0:
            return False, ""

        r_now = self._compute_r_multiple(mid, ticket=ticket)
        effective_horizon = (
            override_horizon
            if override_horizon is not None and override_horizon > 0
            else self._get_effective_horizon()
        )
        T_max = max(effective_horizon, 1)
        t_ratio = pos.cycles_held / T_max

        # ── 1. Alpha trajectory signature (gamma) + start tolerance ──
        _sname = (pos.strategy_name or "").lower()
        if "statarb" in _sname:
            gamma = 2.0  # convex — mean reversion needs room to converge
            start_floor = -0.8  # allow early drawdown up to −0.8R
        elif "barrier" in _sname:
            gamma = 0.5  # concave — breakout must prove itself quickly
            start_floor = -0.3  # only small tolerance for friction
        else:
            gamma = 1.0  # linear — default steady ramp
            start_floor = -0.5

        # ── 2. Envelope endpoint ──
        # Design R:R (the strategy's aspirational reward)
        _sl_dist = abs(pos.entry_price - pos.initial_sl)
        _tp_dist = abs(pos.initial_tp - pos.entry_price)
        r_target = _tp_dist / _sl_dist if _sl_dist > 1e-8 else 1.75

        # override_min_r (YAML min_r_for_hold) replaces the design R:R as the
        # curve endpoint when configured — "I only need 0.3R to justify holding"
        end_target = (
            override_min_r if (override_min_r is not None and override_min_r > 0) else r_target
        )

        # ── 3. Continuous EV floor (no grace-period cliff) ──
        progress = math.pow(min(t_ratio, 1.0), gamma)
        ev_floor = start_floor + (end_target - start_floor) * progress

        if r_now < ev_floor:
            return True, (
                f"ev_trajectory_{pos.strategy_name}_gamma{gamma}"
                f"_t{t_ratio:.0%}_r{r_now:.2f}_lt_{ev_floor:.2f}"
            )
        return False, ""

    def should_exit_hesitation(
        self,
        mid: float = 0.0,
        ticket: int | None = None,
        effective_limit_override: int | None = None,
    ) -> tuple[bool, str]:
        """Exit if position has not triggered breakeven within hesitation_cycles.

        Catches positions that never gain traction — the consensus said "trade"
        but the market did not follow through.  Returns (should_exit, reason).

        effective_limit_override: pre-close absolute time cap (applied externally
        via PreCloseContext.compute_effective_hesitation).  None → use normal value.
        Overrides ALL timeframe multipliers — remaining time dominates.

        Pillar 2 — Profit Pardon: if highest_r >= 0.15 (position had meaningful
        profit but breakeven was missed due to sampling blind spot), grant
        2× hesitation_cycles extended grace period.

        Pillar 3 — Current-Profit Guard: if the position is currently profitable
        (r_now > 0), do NOT kill it — the market IS following through, just
        slower than the breakeven threshold expects.

        Pillar 4 — Half-life Dynamic Patience (FIX-20260525-021): for mean-reversion
        strategies (statarb/OU), the static hesitation_cycles is replaced by a
        dynamic limit tied to the OU half-life at entry:
          hesitation_limit = max(12, int(entry_half_life * 0.75))
        Mean reversion is a rubber band — killing it after 30 min when the
        half-life says 4 hours is a category error (trend exit on MR position).
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False, ""

        # Determine effective hesitation limit per strategy family
        _sname = (pos.strategy_name or "").lower()
        if "statarb" in _sname and pos.entry_half_life > 0:
            _effective_limit = max(12, int(pos.entry_half_life * 0.75))
        elif self.hesitation_cycles <= 0:
            return False, ""
        else:
            _effective_limit = self.hesitation_cycles

        # ── Pre-close absolute time cap (CRITICAL CORRECTION 2) ──
        # Overrides ALL timeframe multipliers.  H4 hesitation=240 bars (20h)
        # is meaningless when only 6 bars remain before flatten.  The
        # remaining-time cap ensures H1/H4 strategies exit at the same
        # physical urgency as M5/M15.
        if effective_limit_override is not None:
            _effective_limit = min(_effective_limit, effective_limit_override)

        if pos.breakeven_triggered:
            return False, ""

        # Pillar 3: Current-profit guard — a profitable position has traction
        r_now = self._compute_r_multiple(mid, ticket=ticket)
        if r_now > 0:
            return False, ""

        # Pillar 2: Profit Pardon — highest_r >= 0.15 gets 2× extension
        if pos.highest_r >= 0.15:
            extended_cycles = _effective_limit * 2
            if pos.cycles_held < extended_cycles:
                return False, ""
            return True, (f"hesitation_{pos.cycles_held}c_pardon_expired" f"_r{pos.highest_r:.2f}")

        if pos.cycles_held >= _effective_limit:
            return True, f"hesitation_{pos.cycles_held}c_no_breakeven"
        return False, ""

    # ── Minimum hold protection with toxicity veto ──────────────────────

    def _is_protected_period(self, ticket: int | None = None) -> bool:
        """True during the first min_hold_cycles after entry.

        During protection, non-SL exits (Layer 2/2.5/3) are suppressed
        unless the toxicity veto fires.
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False
        return pos.cycles_held < self.min_hold_cycles

    def _toxicity_veto(
        self, mid: float, tick_velocity: float | None = None, ticket: int | None = None
    ) -> bool:
        """Emergency escape: override protection when market is toxic.

        Triggers when (any):
          1. Price is within 0.3 × entry_atr of the hard SL (about to stop out)
          2. Instantaneous tick velocity exceeds toxicity_velocity_mult × baseline

        Returns True when the position should NOT be protected.
        """
        pos = self._get_pos(ticket)
        if pos is None:
            return False

        # Condition 1: price approaching hard stop-loss
        if pos.side == "long":
            dist_to_sl = mid - pos.initial_sl
        else:
            dist_to_sl = pos.initial_sl - mid

        if dist_to_sl < 0.3 * pos.entry_atr:
            return True

        # Condition 2: extreme tick velocity (if available)
        if tick_velocity is not None and pos.entry_atr > 0:
            baseline_vel = pos.entry_atr * 0.1  # ~10% of entry ATR per cycle
            velocity_ratio = abs(tick_velocity) / max(baseline_vel, 0.0001)
            if velocity_ratio > self.toxicity_velocity_mult:
                return True

        return False

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

    def build_close_payload(self, reason: str = "", *, magic: int = 0) -> dict[str, Any]:
        """Return execution_payload for a close dispatch."""
        pos = self._position
        payload: dict[str, Any] = {
            "action": "close",
            "side": pos.side if pos else "long",
            "position_ticket": pos.ticket if pos else 0,
            "volume": pos.volume if pos else 0.01,
            "comment": reason,
        }
        if magic:
            payload["magic"] = magic
        if pos and pos.supporting_brain_ids:
            payload["brain_ids"] = pos.supporting_brain_ids
        return payload

    # ── Persistence ──────────────────────────────────────────────────────

    _SAVE_INTERVAL_CYCLES = 5  # persist every N cycles to limit disk I/O

    @staticmethod
    def _compute_consensus_hash(pos: ActivePosition) -> str:
        """SHA256 of sorted brain IDs + consensus keys for integrity verification."""
        h = hashlib.sha256()
        h.update(",".join(sorted(pos.supporting_brain_ids)).encode())
        h.update(json.dumps(pos.entry_consensus, sort_keys=True).encode())
        return h.hexdigest()[:16]

    def save_state(self, save_path: str | Path) -> None:
        """Persist intent-state only (v3 SSOT — strategy attribution + entry).

        MT5 is the authoritative source for physical state (price, SL, TP, volume).
        Python persists only intent-state that cannot be recovered from MT5.

        FIX-20260601-036: when all positions are gone, persist an EMPTY state
        instead of deleting the file.  A missing file causes FileNotFoundError
        in downstream scripts and monitoring dashboards.  An empty state ([])
        is a valid business state — zero positions is a deliberate condition.

        DQAF-20260616-005: previously DELETE was used to prevent stale-state
        restoration on restart.  The restart code now checks MT5 net_exposure
        independently, so an empty file is safe.  This follows the state-machine
        persistence principle: the file must always exist with valid JSON.
        """
        import json as _json
        from pathlib import Path as _Path

        p = _Path(save_path)
        if not self._positions:
            # Persist empty state — zero positions is a valid business condition
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                empty_state = {
                    "version": 3,
                    "positions": [],
                    "_primary_ticket": None,
                    "_entry_consensus_score": 0.0,
                    "_last_brain_reeval_cycle": 0,
                    "_recovery_cycle": -1,
                    "saved_at_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                }
                p.write_text(_json.dumps(empty_state, indent=2), encoding="utf-8")
            except OSError:
                pass
            return

        self._last_state_path = str(p)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            positions_payload: list[dict[str, Any]] = []
            for _ticket, pos in self._positions.items():
                positions_payload.append(
                    {
                        "ticket": pos.ticket,
                        "cycles_held": pos.cycles_held,
                        "breakeven_triggered": pos.breakeven_triggered,
                        "partial_tp_done": pos.partial_tp_triggered,
                        "entry_atr": pos.entry_atr,  # FIX-018: persist for R-multiple calc across restarts
                        "bracket_atr": pos.bracket_atr,  # FIX-20260709-004: per-TF bracket sizing ATR
                        "entry_price": pos.entry_price,  # FIX-018: needed for PnL estimation
                        # DQAF-20260621-034 Addendum: strategy-attribution intent
                        # fields — cannot be recovered from MT5, must survive
                        # cold-start to prevent policy degradation and exit
                        # disconnection (IC Mandate 刀 #3 追加修复).
                        "strategy_name": pos.strategy_name or "",
                        "supporting_brain_ids": getattr(pos, "supporting_brain_ids", []) or [],
                        "entry_consensus": getattr(pos, "entry_consensus", {}) or {},
                        "model_horizons": getattr(pos, "model_horizons", {}) or {},
                        "brain_consensus_hash": self._compute_consensus_hash(pos),
                        # TECH_DEBT-019: persist the per-strategy RR contract so a
                        # restart-surviving position keeps its tp_min_rr_ratio (the
                        # RR floor / elastic expansion would otherwise silently
                        # fall back to the 0.0 default and lose the protection).
                        "trail_policy": asdict(pos.trail_policy) if pos.trail_policy else None,
                    }
                )
            payload: dict[str, Any] = {
                "version": 3,  # SSOT intent-state only; MT5 holds physical state
                "positions": positions_payload,
                "_last_brain_reeval_cycle": self._last_brain_reeval_cycle,
                "_entry_consensus_score": self._entry_consensus_score,
                "_recovery_cycle": self._recovery_cycle,
                "_primary_ticket": self._primary_ticket,
                "saved_at_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
            import os as _os

            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _os.replace(tmp, p)
        except OSError:
            pass  # Disk write failure is non-fatal

    def load_state(
        self, save_path: str | Path, max_age_hours: float = 24.0
    ) -> ActivePosition | None:
        """Restore intent-state from JSON, if fresh enough.

        Handles v1 (single-position), v2 (multi-position full state), and
        v3 (SSOT intent-only — 4 fields per position, MT5 is physical truth).
        Returns the primary restored position (for backward compat), or None.
        """
        import json as _json
        from pathlib import Path as _Path

        p = _Path(save_path)
        if not p.exists():
            return None

        # Reject state older than max_age_hours
        try:
            age_h = (time.time() - p.stat().st_mtime) / 3600
            if age_h > max_age_hours:
                try:  # noqa: SIM105
                    p.unlink()  # clean up stale file
                except OSError:
                    pass
                return None
        except OSError:
            return None

        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            return None

        data_version = data.get("version", 1)
        is_v3 = isinstance(data_version, int) and data_version >= 3
        is_v2 = isinstance(data_version, int) and data_version >= 2

        def _build_position_full(d: dict[str, Any]) -> ActivePosition:
            """Full reconstruction (v1/v2) — all fields present in JSON."""
            return ActivePosition(
                ticket=int(d["ticket"]),
                side=str(d["side"]),
                entry_price=float(d["entry_price"]),
                volume=float(d["volume"]),
                initial_sl=float(d.get("initial_sl", d["entry_price"])),
                initial_tp=float(d.get("initial_tp", 0)),
                current_sl=float(d.get("current_sl", d["initial_sl"])),
                current_tp=float(d.get("current_tp", d.get("initial_tp", 0))),
                highest_high=float(d.get("highest_high", d["entry_price"])),
                lowest_low=float(d.get("lowest_low", d["entry_price"])),
                entry_atr=float(d.get("entry_atr", 2.0)),
                bracket_atr=float(d.get("bracket_atr", 0.0)),  # FIX-20260709-004
                entry_cycle=int(d.get("entry_cycle", 0)),
                entry_z_score=float(d.get("entry_z_score", 0.0)),
                entry_consensus=d.get("entry_consensus", {}),
                supporting_brain_ids=d.get("supporting_brain_ids", []),
                model_horizons=d.get("model_horizons", {}),
                breakeven_triggered=bool(d.get("breakeven_triggered", False)),
                trail_multiplier=float(d.get("trail_multiplier", self.trail_atr_mult)),
                r_milestones_hit=d.get("r_milestones_hit", []),
                cycles_held=int(d.get("cycles_held", 0)),
                highest_r=float(d.get("highest_r", 0.0)),
                prev_r=float(d.get("prev_r", 0.0)),
                partial_tp_triggered=bool(d.get("partial_tp_triggered", False)),
                partial_tp_r=float(d.get("partial_tp_r", 0.0)),
                partial_tp_ratio=float(d.get("partial_tp_ratio", 0.5)),
                ou_handoff_active=bool(d.get("ou_handoff_active", False)),
                ou_handoff_r=float(d.get("ou_handoff_r", 0.0)),
                strategy_name=str(d.get("strategy_name", "")),
                expected_remaining_volume=float(
                    d.get("expected_remaining_volume", d.get("volume", 0.0))
                ),
                consecutive_flips=int(d.get("consecutive_flips", 0)),
                trail_atr_mult=float(d.get("trail_atr_mult", self.trail_atr_mult)),
                trail_atr_mult_low=float(d.get("trail_atr_mult_low", self.trail_atr_mult_low)),
                trail_atr_mult_high=float(d.get("trail_atr_mult_high", self.trail_atr_mult_high)),
                breakeven_threshold_atr=float(
                    d.get("breakeven_threshold_atr", self.breakeven_threshold_atr)
                ),
                trail_policy=_policy_from_dict(d.get("trail_policy")),  # TECH_DEBT-019
            )

        def _build_position_v3(d: dict[str, Any]) -> ActivePosition | None:
            """Minimal reconstruction from v3 intent-state.

            Intent fields (strategy attribution + entry_price + entry_atr) are
            persisted.  Physical state (volume, SL, TP, side, highest/lowest)
            must be synced from MT5 by the caller (live_intent_loop.py recovery
            path and management_phase.py per-cycle sync).
            """
            ticket = int(d["ticket"])
            _strategy_from_state = str(d.get("strategy_name", ""))
            pos = ActivePosition(
                ticket=ticket,
                side="unknown",  # filled by MT5 recovery
                entry_price=float(d.get("entry_price", 0.0)),  # FIX-018: persisted since v3
                volume=0.0,  # filled by MT5 recovery
                initial_sl=0.0,
                initial_tp=0.0,
                current_sl=0.0,
                current_tp=0.0,
                highest_high=float(
                    d.get("entry_price", 0)
                ),  # FIX-017: default to entry_price, not 0
                lowest_low=float(d.get("entry_price", 0)),  # FIX-017: prevents premature breakeven
                entry_atr=float(d.get("entry_atr", 2.0)),  # FIX-018: persisted since v3
                bracket_atr=float(
                    d.get("bracket_atr", 0.0)
                ),  # FIX-20260709-004: persisted since v3
                entry_cycle=0,
                cycles_held=int(d.get("cycles_held", 0)),
                breakeven_triggered=bool(d.get("breakeven_triggered", False)),
                partial_tp_triggered=bool(d.get("partial_tp_done", False)),
                # DQAF-20260621-034 Addendum: strategy-attribution intent fields
                # restored from V3 state.  Without these, V3-recovered positions
                # lose strategy identity → fallback_unmanaged never applies →
                # exit policies degrade to empty-config defaults (IC Mandate 刀 #3).
                strategy_name=_strategy_from_state,
                supporting_brain_ids=[str(x) for x in d.get("supporting_brain_ids", [])],
                entry_consensus=dict(d.get("entry_consensus", {}) or {}),
                model_horizons={
                    str(k): int(v) for k, v in (d.get("model_horizons", {}) or {}).items()
                },
                trail_policy=_policy_from_dict(d.get("trail_policy")),  # TECH_DEBT-019
            )
            # Stash v3 fields for downstream reconciliation
            pos._v3_consensus_hash = str(d.get("brain_consensus_hash", ""))
            return pos

        if is_v3:
            position_list: list[dict[str, Any]] = data.get("positions", [])
            if not position_list:
                return None
            primary_pos = None
            for pd in position_list:
                if "ticket" not in pd:
                    continue
                pos = _build_position_v3(pd)
                if pos is None:
                    continue
                self._positions[pos.ticket] = pos
                if primary_pos is None:
                    primary_pos = pos
            self._primary_ticket = int(data.get("_primary_ticket", 0)) or (
                primary_pos.ticket if primary_pos else None
            )
            self._last_brain_reeval_cycle = int(data.get("_last_brain_reeval_cycle", -1))
            self._entry_consensus_score = float(data.get("_entry_consensus_score", 0.0))
            self._recovery_cycle = 0
            return primary_pos
        elif is_v2:
            pos_list: list[dict[str, Any]] = data.get("positions", [])
            if not pos_list:
                return None
            primary_pos = None
            for pd in pos_list:
                if not all(k in pd for k in ("ticket", "side", "entry_price", "volume")):
                    continue
                pos = _build_position_full(pd)
                self._positions[pos.ticket] = pos
                if primary_pos is None:
                    primary_pos = pos
            self._primary_ticket = int(data.get("_primary_ticket", 0)) or (
                primary_pos.ticket if primary_pos else None
            )
            self._last_brain_reeval_cycle = int(data.get("_last_brain_reeval_cycle", -1))
            self._entry_consensus_score = float(data.get("_entry_consensus_score", 0.0))
            # backward-compat: read legacy _consecutive_flips into primary position
            _legacy_flips = int(data.get("_consecutive_flips", 0))
            if primary_pos is not None and _legacy_flips:
                primary_pos.consecutive_flips = _legacy_flips
            self._recovery_cycle = 0  # always start grace period on recovery
            return primary_pos
        else:
            # v1 format: single position
            if not all(k in data for k in ("ticket", "side", "entry_price", "volume")):
                return None
            pos = _build_position_full(data)
            self._positions[pos.ticket] = pos
            self._primary_ticket = pos.ticket
            self._last_brain_reeval_cycle = int(data.get("_last_brain_reeval_cycle", -1))
            self._entry_consensus_score = float(data.get("_entry_consensus_score", 0.0))
            _legacy_flips = int(data.get("_consecutive_flips", 0))
            if _legacy_flips:
                pos.consecutive_flips = _legacy_flips
            self._recovery_cycle = 0  # always start grace period on recovery
            return pos

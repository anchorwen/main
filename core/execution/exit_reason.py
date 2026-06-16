"""Canonical exit reason taxonomy — single source of truth for all position exits.

Iron Law Phase 2 (2026-06-13): Replaces freeform string-based exit reason
classification that was scattered across 4+ files (position_manager.py,
live_cycle.py, reentry_guard.py, position_close_adapter.py, managed_close.py).

The ``ExitReason`` enum is the authoritative registry.  ``classify()``
replaces the post-hoc ``_classify_exit_reason()`` substring matcher.
Generation sites should migrate to produce enum members directly;
until then, ``classify(raw_string)`` bridges the gap.

Design:
  - 15 canonical categories (matching existing _classify_exit_reason taxonomy)
  - Each member carries cooldown tier + severity for downstream reentry logic
  - ``classify()`` is a strict classifier — unknown inputs return UNKNOWN
"""

from __future__ import annotations

from enum import Enum


class ExitReason(str, Enum):
    """Canonical exit reason for one closed position.

    Inherits from ``str`` so existing string-comparison code continues to
    work during migration.  New code should use ``is ExitReason.X`` or
    ``== ExitReason.X``.
    """

    # ── Model / signal-driven exits ─────────────────────────────────────
    BRAIN_FLIP = "brain_flip"
    """Brain ensemble flipped direction (signal_reversal, consensus_flip)."""

    MOMENTUM_PAUSE = "momentum_pause"
    """Confidence decay / EMA drop without direction flip (same-direction
    conviction dip — NOT a flip, should have lighter reentry penalty)."""

    KALMAN_FLIP = "kalman_flip"
    """Kalman trend velocity reversal (leading indicator, faster than consensus)."""

    META_EXIT = "meta_exit"
    """Multi-factor meta-model exit urgency (pnl_urgency, time_decay,
    regime_misalignment, consensus_drift, vol_expansion, ml_p_win)."""

    OU_REVERT = "ou_revert"
    """Ornstein-Uhlenbeck mean-reversion signal (z-score based)."""

    # ── Risk / protection exits ─────────────────────────────────────────
    SL_HIT = "sl_hit"
    """Stop-loss triggered (original or trailed)."""

    TP_HIT = "tp_hit"
    """Take-profit triggered (full or partial)."""

    BLEED_STOP = "bleed_stop"
    """Consecutive negative-bar PnL bleed — hard stop."""

    WATCHDOG = "watchdog"
    """Exit watchdog forced close (retry exhaustion or L2 forced liquidation)."""

    EMERGENCY_CLOSE = "emergency_close"
    """Grace-period emergency close (deeply underwater after restart)."""

    # ── Time / structural exits ─────────────────────────────────────────
    TIME_EXPIRED = "time_expired"
    """Time-based exit — max hold time reached (EV trajectory or linear phase)."""

    HESITATION = "hesitation"
    """No breakeven within N cycles — position timed out on hesitation."""

    # ── Portfolio / netting exits ───────────────────────────────────────
    NET_OUT = "net_out"
    """Net position close — opposing entry forces reduction/close."""

    # ── Unknown / external exits ────────────────────────────────────────
    UNKNOWN_CLOSE = "unknown_close"
    """Position closed outside Python control (MIA, manual, MT5 direct)."""

    UNKNOWN = "unknown"
    """Fallthrough — raw reason string did not match any known pattern."""

    # ── Metadata ────────────────────────────────────────────────────────

    @property
    def cooldown_tier(self) -> str:
        """Reentry cooldown tier for downstream reentry logic.

        Returns one of: ``"light"``, ``"medium"``, ``"heavy"``, ``"block"``.
        """
        return _COOLDOWN_TIER.get(self, "medium")

    @property
    def is_model_driven(self) -> bool:
        """True if the exit was triggered by a model/brain signal (vs mechanical)."""
        return self in _MODEL_DRIVEN

    @property
    def is_risk_driven(self) -> bool:
        """True if the exit was triggered by a risk protection (SL, bleed, watchdog)."""
        return self in _RISK_DRIVEN

    @property
    def is_structural(self) -> bool:
        """True if the exit was triggered by time/structural constraints."""
        return self in _STRUCTURAL


# ── Category sets ─────────────────────────────────────────────────────────

_MODEL_DRIVEN: set[ExitReason] = {
    ExitReason.BRAIN_FLIP,
    ExitReason.MOMENTUM_PAUSE,
    ExitReason.KALMAN_FLIP,
    ExitReason.META_EXIT,
    ExitReason.OU_REVERT,
}

_RISK_DRIVEN: set[ExitReason] = {
    ExitReason.SL_HIT,
    ExitReason.BLEED_STOP,
    ExitReason.WATCHDOG,
    ExitReason.EMERGENCY_CLOSE,
}

_STRUCTURAL: set[ExitReason] = {
    ExitReason.TP_HIT,
    ExitReason.TIME_EXPIRED,
    ExitReason.HESITATION,
}

# ── Cooldown tiers (downstream reentry guard) ────────────────────────────

_COOLDOWN_TIER: dict[ExitReason, str] = {
    ExitReason.BRAIN_FLIP: "heavy",  # direction changed → strong block
    ExitReason.MOMENTUM_PAUSE: "light",  # same direction, just conviction dip
    ExitReason.KALMAN_FLIP: "medium",  # leading indicator, may revert
    ExitReason.META_EXIT: "medium",  # multi-factor — moderate signal
    ExitReason.OU_REVERT: "light",  # mean-reversion — expected behavior
    ExitReason.SL_HIT: "heavy",  # price proved direction wrong
    ExitReason.TP_HIT: "light",  # successful exit, no penalty
    ExitReason.BLEED_STOP: "heavy",  # consecutive losses — risk signal
    ExitReason.WATCHDOG: "block",  # system forced — extreme caution
    ExitReason.EMERGENCY_CLOSE: "block",  # emergency — do not re-enter
    ExitReason.TIME_EXPIRED: "light",  # structural timeout, no signal
    ExitReason.HESITATION: "medium",  # couldn't reach breakeven
    ExitReason.NET_OUT: "medium",  # opposing entry — active decision
    ExitReason.UNKNOWN_CLOSE: "heavy",  # we don't know what happened
    ExitReason.UNKNOWN: "heavy",  # fallthrough — treat with caution
}


# ── Classification ────────────────────────────────────────────────────────


def classify(raw_reason: str | None) -> ExitReason:
    """Map a raw exit reason string to the canonical ``ExitReason``.

    This is the single authoritative classifier — it replaces the post-hoc
    ``_classify_exit_reason()`` substring matcher that previously lived in
    ``reentry_guard.py``.  All generation sites will eventually call this
    directly instead of producing freeform strings.

    Args:
        raw_reason: Freeform exit reason string from any generation site.
                    May be ``None`` for entries where the label was not set
                    (e.g. orphan cleanup, system maintenance).

    Returns:
        The canonical ``ExitReason`` enum member.  Returns ``UNKNOWN`` if
        no pattern matches or the input is ``None``.
    """
    # ── P1: Null-reference defense (DQAF-20260616-001) ──
    # label=None occurs on ~114 close events (orphan cleanup, bridge rejects).
    # Without this guard, raw_reason.lower() raises AttributeError.
    if raw_reason is None or not isinstance(raw_reason, str):
        return ExitReason.UNKNOWN

    r = raw_reason.lower()

    # ── P0: Exact-match canonical labels (DQAF-20260616-001) ─────────────────
    # "win", "loss", "breakeven" are primary exit labels produced by the
    # execution layer.  Previously they fell through to UNKNOWN because no
    # pattern matched, triggering the most conservative reentry gate
    # (confidence ≥ 0.70) and permanently deadlocking rule-based strategies
    # whose confidence is fixed at 0.50.
    #
    # Mappings per Ω architect review:
    #   "win"       → TP_HIT      (successful exit, light cooldown)
    #   "loss"      → SL_HIT      (money-losing exit, strict cooldown)
    #   "breakeven" → TIME_EXPIRED (neutral/time-based exit, light cooldown)
    if r in ("win", "loss", "breakeven"):
        if r == "win":
            return ExitReason.TP_HIT
        if r == "loss":
            return ExitReason.SL_HIT
        return ExitReason.TIME_EXPIRED  # "breakeven"

    # ── P3: Operational labels (DQAF-20260616-003) ────────────────────────
    # "close_accepted" — MT5 accepted the close order.  This is a normal
    # operational close with no negative signal.  Map to TIME_EXPIRED
    # (neutral, light cooldown) to avoid blocking reentry.
    if r == "close_accepted":
        return ExitReason.TIME_EXPIRED

    # ── P3: Orphan/system-maintenance labels (DQAF-20260616-001) ────────────
    # "auto_orphan_rejected" / "auto_orphan_stale" are produced by the orphan
    # cleanup pipeline — they are system maintenance, not strategy exits.
    # Classify as UNKNOWN_CLOSE (system-triggered, not strategy-driven) so
    # audit dashboards can distinguish them from strategy exits.
    if "auto_orphan" in r:
        return ExitReason.UNKNOWN_CLOSE

    if "brain_flip" in r or "signal_reversal" in r:
        return ExitReason.BRAIN_FLIP
    if "confidence_decay" in r or "confidence_drop" in r:
        return ExitReason.MOMENTUM_PAUSE
    if "kalman_velocity" in r:
        return ExitReason.KALMAN_FLIP
    if "sl_hit" in r or "sl_stop" in r:
        return ExitReason.SL_HIT
    if "tp_hit" in r or "take_profit" in r or "partial_tp" in r:
        return ExitReason.TP_HIT
    if (
        "meta_exit" in r
        or "pnl_urgency" in r
        or "time_decay" in r
        or "regime_misalignment" in r
        or "consensus_drift" in r
        or "vol_expansion" in r
        or "ml_p_win" in r
    ):
        return ExitReason.META_EXIT
    if "ev_trajectory" in r:
        return ExitReason.TIME_EXPIRED
    if "time_" in r or "phase" in r:
        return ExitReason.TIME_EXPIRED
    if "ou_" in r or "zscore" in r or "z_score" in r:
        return ExitReason.OU_REVERT
    if "hesitation_" in r:
        return ExitReason.HESITATION
    if "bleed_stop_" in r:
        return ExitReason.BLEED_STOP
    if "net_out" in r:
        return ExitReason.NET_OUT
    if "exit_watchdog" in r:
        return ExitReason.WATCHDOG
    if "grace_period_emergency" in r:
        return ExitReason.EMERGENCY_CLOSE
    if "mia_close" in r or "unknown_close" in r or "manual_close" in r or "manual" in r:
        return ExitReason.UNKNOWN_CLOSE
    return ExitReason.UNKNOWN


# ── Backward-compatible shim ──────────────────────────────────────────────
# During migration, generation sites still produce freeform strings.
# This thin wrapper preserves the old function signature so existing
# consumers (reentry_guard.py, audit scripts) can adopt the enum
# incrementally without breaking.


def _classify_exit_reason(raw_reason: str | None) -> str:
    """Backward-compatible shim — delegates to :func:`classify`.

    Returns the enum's ``.value`` (string) to preserve the old API.
    New code should call :func:`classify` directly and work with
    ``ExitReason`` enum members.
    """
    return classify(raw_reason).value

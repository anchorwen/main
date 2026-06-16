"""Re-entry quality guard — prevents churn after managed exits.

Distinguishes exit reasons and applies graduated re-entry criteria
rather than a blunt same-direction block.  Inspired by:

  - Lopez de Prado meta-labelling (separate direction from action quality)
  - Grinold & Kahn IC decay (signal information erodes with reuse)

Design:
  Layer 1 — exit-reason classifier (normalises raw exit reason strings)
  Layer 2 — signal-quality gate (confidence improvement + price confirmation)
  Layer 3 — tiered volume decay (grace→warning→kill for same-direction re-entries)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.execution.exit_reason import (  # noqa: F401 — re-export for tests
    _classify_exit_reason,
    classify,
)

# ── Quality gate ──────────────────────────────────────────────────────────


def check_reentry_quality(
    *,
    exit_reason_raw: str,
    exit_direction: str,
    exit_confidence: float,
    exit_price: float,
    exit_timestamp: float,
    now_timestamp: float | None = None,
    new_direction: str,
    new_confidence: float,
    mid_price: float,
    entry_half_life: float = 0.0,
    timeframe_minutes: float = 5.0,
    # ── FIX-20260605-120: per-asset threshold overrides ──
    sl_cooldown_override: float | None = None,
    sl_penalty_override: float | None = None,
    bleed_cooldown_override: float | None = None,
    bleed_penalty_override: float | None = None,
    # ── DQAF-20260616-001/P2: L3 architecture — rule-based strategy gate ──
    is_rule_based: bool = False,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for re-entering after a managed exit.

    Opposite-direction re-entries are always allowed (no guard).
    Same-direction re-entries are gated by exit-reason-specific criteria
    including minimum elapsed time, confidence improvement, and price confirmation.

    *entry_half_life* enables TTL (time-to-live) hard unlock for time-based
    categories.  When the elapsed time since exit exceeds
    ``half_life * timeframe_minutes * 2.5 * 60`` seconds, the lock is
    forcibly broken regardless of price confirmation — the mean has shifted,
    the old exit reference is stale.

    *is_rule_based* (DQAF-20260616-001/P2): when True, the strategy has no
    ML model (``brain_types: []``, ``min_valid_brains: 0``) and its confidence
    is fixed (typically 0.50).  ML confidence thresholds are meaningless for
    such strategies — applying them creates a permanent deadlock.  Instead,
    rule-based strategies use a time-based cooldown scaled to the timeframe
    (min 120s, 40% of bar period) with no confidence check.
    """
    if new_direction not in ("long", "short"):
        return False, "neutral_signal"
    if new_direction != exit_direction:
        return True, "opposite_direction"

    import time as _time

    elapsed = (now_timestamp if now_timestamp is not None else _time.time()) - exit_timestamp

    # ── Stale exit override (FIX-20260529-039) ──
    # Exits older than 24h are irrelevant — market microstructure, volatility
    # regime, and trend have all shifted.  Blocking reentry on a stale record
    # (e.g. unknown_close from 9.5 days ago) creates a permanent deadlock.
    # 24h = one full session cycle including Asia/London/NY overlap.
    _STALE_EXIT_SECONDS: float = 86400.0  # 24 hours
    if elapsed > _STALE_EXIT_SECONDS:
        return True, f"stale_exit_allowed_{elapsed:.0f}s_gt_{_STALE_EXIT_SECONDS:.0f}s"

    # ── L3 Architecture: Rule-based strategy reentry gate ──────────────────
    # DQAF-20260616-001/P2: Rule-based strategies (structural_swing_v1, etc.)
    # have fixed confidence and no ML model to improve it.  ML confidence
    # thresholds (e.g. "≥0.70") create a mathematical deadlock — the strategy
    # can NEVER satisfy them.  This is a design defect in the reentry guard:
    # it assumes all strategies have ML-derived confidence that can improve.
    #
    # Rule-based reentry uses a time-based cooldown scaled to the timeframe:
    #   cooldown = max(120s, timeframe_minutes * 60 * 0.4)
    #   M5:  120s,  M15: 360s,  M30: 720s,  H1: 1440s,  H4: 5760s
    # After the cooldown expires, the strategy's own rule engine is the
    # sole authority — no confidence check is applied.
    if is_rule_based:
        _rule_cooldown = max(120.0, timeframe_minutes * 60.0 * 0.4)
        if elapsed < _rule_cooldown:
            return (
                False,
                f"rule_based_cooldown_{elapsed:.0f}s_lt_{_rule_cooldown:.0f}s",
            )
        return True, "rule_based_reentry_allowed"

    category = classify(exit_reason_raw)

    # ── Absolute ceiling on reentry thresholds ──────────────────────────
    # Tree models (XGBoost/LightGBM) rarely output confidence > 0.82 in
    # real-market conditions.  Linear margin addition (e.g. exit_conf + 0.10)
    # can produce thresholds > 1.0 when exit confidence is extreme, creating
    # a mathematical deadlock — the model physically cannot reach the threshold.
    # This ceiling prevents that class of permanent lock-out.
    _MAX_THRESHOLD = 0.82

    if category == "brain_flip":
        # Brain flipped against us — this is the most dangerous exit category
        # for churn.  Require: minimum elapsed time, significantly stronger
        # signal, AND price confirmation of the new entry direction.
        if elapsed < 120:
            return False, f"brain_flip_too_soon_{elapsed:.0f}s_lt_120s"

        # ── FIX-20260606-127 / FIX-20260606-130: TTL hard unlock ─────────
        # FIX-127 added TTL for brain_flip (proven sl_hit TTL pattern).
        # FIX-130 recalibrates parameters for BTC model capability:
        #   TTL: 4h→2h — BTC max confidence ceiling ~0.686, floor 0.70 was
        #        unreachable → guaranteed 4h deadlock after every brain_flip.
        #   Addition: +0.10→+0.05 — narrower margin still requires real
        #        improvement but doesn't overshoot model output range.
        #   Floor: 0.70→0.65 — BTC model P99≈0.685, 0.65 is reachable.
        # After TTL expires, only basic signal quality (confidence > 0.50)
        # is required — same pattern as FIX-20260528-011.
        _brain_flip_ttl_s = max(7200, entry_half_life * timeframe_minutes * 2.5 * 60)
        if elapsed > _brain_flip_ttl_s:
            if new_confidence < 0.50:
                return (
                    False,
                    f"brain_flip_ttl_expired_low_conf_{new_confidence:.3f}",
                )
            return True, f"brain_flip_ttl_expired_{elapsed:.0f}s"

        # Within TTL window: strict logic with model-aware ceiling (FIX-130)
        _threshold = min(max(exit_confidence + 0.05, 0.65), _MAX_THRESHOLD)
        if new_confidence < _threshold:
            return (
                False,
                f"brain_flip_confidence_not_improved_{new_confidence:.3f}_need_{_threshold:.3f}",
            )
        if exit_direction == "long" and mid_price <= exit_price:
            return False, "brain_flip_price_not_confirming_long"
        if exit_direction == "short" and mid_price >= exit_price:
            return False, "brain_flip_price_not_confirming_short"
        return True, "brain_flip_reentry_confirmed"

    if category == "sl_hit":
        # TTL hard unlock: if half_life × 2.5 has elapsed since exit,
        # the mean has shifted — the old exit reference is stale and
        # continued blocking would miss new-regime trading opportunities.
        if entry_half_life > 0:
            _ttl_s = entry_half_life * timeframe_minutes * 2.5 * 60.0
            if elapsed > _ttl_s:
                return (
                    True,
                    f"sl_ttl_expired_{elapsed:.0f}s_gt_{_ttl_s:.0f}s_half_life_{entry_half_life:.1f}",
                )

        # FIX-20260605-120: per-asset thresholds via config override.
        _sl_cooldown = sl_cooldown_override if sl_cooldown_override is not None else 180
        _sl_penalty = sl_penalty_override if sl_penalty_override is not None else 0.10
        if elapsed < _sl_cooldown:
            return False, f"sl_too_soon_{elapsed:.0f}s_lt_{_sl_cooldown}s"
        _sl_threshold = min(exit_confidence + _sl_penalty, _MAX_THRESHOLD)
        if new_confidence < _sl_threshold:
            return (
                False,
                f"sl_recovery_confidence_insufficient_{new_confidence:.3f}_need_{_sl_threshold:.3f}",
            )
        if exit_direction == "long" and mid_price <= exit_price + 1.0:
            return False, "sl_recovery_price_not_confirming_long"
        if exit_direction == "short" and mid_price >= exit_price - 1.0:
            return False, "sl_recovery_price_not_confirming_short"
        return True, "sl_recovery_allowed"

    if category == "time_expired":
        # Time expiry — model's prediction window closed naturally.
        # Require minimum elapsed time AND no significant confidence decay
        # to prevent instant re-churn on the next cycle with same signal.
        if elapsed < 60:
            return False, f"time_expired_too_soon_{elapsed:.0f}s_lt_60s"
        if new_confidence < exit_confidence - 0.05:
            return (
                False,
                f"time_expired_confidence_decayed_{new_confidence:.3f}",
            )
        return True, "time_expired_refresh_allowed"

    if category == "momentum_pause":
        # Confidence decay / drop — the brain's conviction dipped below the
        # decay threshold, triggering a safety exit, but the DIRECTION did NOT
        # flip.  This is a same-direction pause, not a reversal.
        #
        # Treat leniently: as long as confidence has stabilised (not dropped
        # further), allow re-entry.  The trend's "second leg" should not be
        # blocked by the same strictness as a full brain_flip.
        if elapsed < 60:
            return False, f"momentum_pause_too_soon_{elapsed:.0f}s_lt_60s"
        if new_confidence < exit_confidence - 0.05:
            return (
                False,
                f"momentum_pause_confidence_decayed_{new_confidence:.3f}_need_{exit_confidence - 0.05:.3f}",
            )
        return True, "momentum_pause_refresh_allowed"

    if category == "tp_hit":
        # Take-profit — trend may continue, allow if confidence hasn't decayed.
        if new_confidence < exit_confidence - 0.03:
            return False, f"tp_reentry_confidence_decayed_{new_confidence:.3f}"
        return True, "tp_reentry_allowed"

    if category == "ou_revert":
        # OU mean-reversion completed — the z-score returned to normal,
        # but we must guard against infinite churn in trending markets.
        # Require minimum elapsed time, confidence improvement, AND
        # price confirmation of a new extreme forming.
        if elapsed < 120:
            return False, f"ou_revert_too_soon_{elapsed:.0f}s_lt_120s"
        _ou_threshold = min(max(exit_confidence + 0.05, 0.70), _MAX_THRESHOLD)
        if new_confidence < _ou_threshold:
            return (
                False,
                f"ou_revert_confidence_not_improved_{new_confidence:.3f}_need_{_ou_threshold:.3f}",
            )
        # For SHORT: price must be HIGHER (further from mean = new extreme)
        # For LONG: price must be LOWER (further from mean = new extreme)
        if exit_direction == "short" and mid_price <= exit_price:
            return False, "ou_revert_price_not_confirming_new_extreme_short"
        if exit_direction == "long" and mid_price >= exit_price:
            return False, "ou_revert_price_not_confirming_new_extreme_long"
        return True, "ou_revert_reentry_confirmed"

    if category == "meta_exit":
        if elapsed < 120:
            return False, f"meta_exit_too_soon_{elapsed:.0f}s_lt_120s"

        # ── FIX-20260606-127: TTL hard unlock for meta_exit ───────────────
        # Same pattern as brain_flip TTL.  meta_exit +0.05 margin is less
        # strict than brain_flip +0.10, but the same mathematical deadlock
        # applies when exit_confidence is near the model's output ceiling.
        # After TTL expires only basic signal quality is required.
        _meta_ttl_s = max(7200, entry_half_life * timeframe_minutes * 2.0 * 60)
        if elapsed > _meta_ttl_s:
            if new_confidence < 0.50:
                return (
                    False,
                    f"meta_exit_ttl_expired_low_conf_{new_confidence:.3f}",
                )
            return True, f"meta_exit_ttl_expired_{elapsed:.0f}s"

        # Within TTL window: original strict logic
        if new_confidence < exit_confidence + 0.05:
            return (
                False,
                f"meta_exit_confidence_not_improved_{new_confidence:.3f}",
            )
        return True, "meta_exit_reentry_confirmed"

    if category == "hesitation":
        # Position never reached breakeven — market did not confirm the
        # signal.  Require significantly stronger signal AND price
        # confirmation before re-entering same direction.
        if elapsed < 180:
            return False, f"hesitation_too_soon_{elapsed:.0f}s_lt_180s"

        # ── FIX-20260609-001: TTL hard unlock + _MAX_THRESHOLD ceiling ──
        # ── FIX-20260609-010: BTC model-aware calibration ─────────────────
        # FIX-001 added _MAX_THRESHOLD=0.82 and 2h TTL, but the +0.15 margin
        # with floor 0.70 still produces unreachable thresholds for BTC
        # tree-based models (LightGBM/XGBoost P99 ≈ 0.685-0.75):
        #   exit_conf=0.67 → max(0.82, 0.70)=0.82 → deadlock (150+ cycles
        #   observed 2026-06-08/09).
        # FIX-010 recalibrates to match FIX-130 brain_flip BTC parameters:
        #   margin +0.08 (was +0.15), floor 0.65 (was 0.70).
        # Ordering: brain_flip +0.05 < hesitation +0.08 < sl_hit +0.10 —
        #   hesitation is stricter than a direction-neutral confidence dip
        #   but less strict than a realised stop-loss.
        #
        # TTL (time-to-live): after 2h or 2.0 half-life cycles (whichever
        # longer), force-unlock with basic signal quality check (>0.50).
        # Same pattern as brain_flip (FIX-127/130), sl_hit (FIX-20260528-011),
        # and meta_exit (FIX-127).
        _hesitation_ttl_s = max(7200, entry_half_life * timeframe_minutes * 2.0 * 60)
        if elapsed > _hesitation_ttl_s:
            if new_confidence < 0.50:
                return (
                    False,
                    f"hesitation_ttl_expired_low_conf_{new_confidence:.3f}",
                )
            return True, f"hesitation_ttl_expired_{elapsed:.0f}s"

        # Within TTL window: confidence improvement +0.08, floor 0.65,
        # ceiling _MAX_THRESHOLD=0.82.
        # BTC worst-case: exit_conf=0.70 → 0.78 (below ceiling, rare but
        # reachable).  exit_conf=0.67 → 0.75 (P99 tail reachable).
        # FIX-20260610-007-B: margin 0.08→0.05 for faster data collection.
        # More reentries → more trades → more training samples.
        # Still bounded by _MAX_THRESHOLD (0.82) ceiling.
        _hesitation_threshold = min(max(exit_confidence + 0.05, 0.65), _MAX_THRESHOLD)
        if new_confidence < _hesitation_threshold:
            return (
                False,
                f"hesitation_confidence_not_improved_{new_confidence:.3f}_need_{_hesitation_threshold:.3f}",
            )
        # For LONG: price must be LOWER than exit (cheaper entry)
        if exit_direction == "long" and mid_price >= exit_price:
            return False, "hesitation_price_not_confirming_long"
        # For SHORT: price must be HIGHER than exit (cheaper entry)
        if exit_direction == "short" and mid_price <= exit_price:
            return False, "hesitation_price_not_confirming_short"
        return True, "hesitation_reentry_confirmed"

    if category == "bleed_stop":
        # FIX-20260605-120: per-asset thresholds via config override.
        # XAU defaults: cooldown=180s, confidence_penalty=0.10
        # BTC: cooldown=600s, confidence_penalty=0.15
        _cooldown = bleed_cooldown_override if bleed_cooldown_override is not None else 180
        _penalty = bleed_penalty_override if bleed_penalty_override is not None else 0.10
        if elapsed < _cooldown:
            return False, f"bleed_stop_too_soon_{elapsed:.0f}s_lt_{_cooldown}s"
        if new_confidence < exit_confidence + _penalty:
            return (
                False,
                f"bleed_stop_confidence_not_improved_{new_confidence:.3f}",
            )
        if exit_direction == "long" and mid_price <= exit_price + 1.0:
            return False, "bleed_stop_price_not_confirming_long"
        if exit_direction == "short" and mid_price >= exit_price - 1.0:
            return False, "bleed_stop_price_not_confirming_short"
        return True, "bleed_stop_recovery_allowed"

    # Unknown close (MIA, manual, unknown) — conservative timeout-based block.
    # FIX-20260525-024: previously fell into catch-all "unknown" permanent block.
    # Now: 900s timeout then allow with confidence check.  MIA/manual closes
    # are not necessarily negative signals — we just don't know the reason.
    if category == "unknown_close":
        if elapsed < 900:
            return False, f"unknown_close_too_soon_{elapsed:.0f}s_lt_900s"
        _unknown_threshold = min(max(exit_confidence, 0.70), _MAX_THRESHOLD)
        if new_confidence < _unknown_threshold:
            return (
                False,
                f"unknown_close_confidence_insufficient_{new_confidence:.3f}_need_{_unknown_threshold:.3f}",
            )
        return True, "unknown_close_timeout_allowed"

    # Unknown — conservative: block same-direction with timeout
    # FIX-20260525-024: previously permanent block. Now 900s timeout.
    if elapsed < 900:
        return False, f"unknown_exit_reason_blocked_{exit_reason_raw[:30]}_{elapsed:.0f}s_lt_900s"
    if new_confidence < max(exit_confidence, 0.70):
        return (
            False,
            f"unknown_exit_confidence_insufficient_{new_confidence:.3f}",
        )
    return True, "unknown_exit_timeout_allowed"


# ── Volume decay ──────────────────────────────────────────────────────────


def apply_reentry_volume_scale(
    base_volume: float,
    consecutive_same_direction: int,
    lot_step: float = 0.01,
) -> tuple[float, bool]:
    """Scale volume down for consecutive same-direction entries.

    Tiered decay (institutional):
      0         = first entry (full size)
      1         = grace period (1.0× — no volume penalty, gated by cooldown + confidence)
      2         = warning (0.5×)
      3+        = kill (0 → hard block)

    Returns (volume, should_block).  If the scaled volume rounds back
    up to the original volume due to min_lot discretization, the order
    is hard-blocked — the penalty must have real effect.
    """
    if consecutive_same_direction == 0:
        return base_volume, False

    # Tiered non-linear decay: grace → warning → kill
    if consecutive_same_direction == 1:
        scale = 1.0
    elif consecutive_same_direction == 2:
        scale = 0.5
    else:
        scale = 0.0

    if scale == 0.0:
        return 0.0, True  # 3+ consecutive same-direction → hard block

    raw_vol = base_volume * scale
    stepped_vol = max(0.01, round(raw_vol / lot_step) * lot_step)

    # Core defense: if discretization rounds back to original volume,
    # the penalty is ineffective → hard block.
    # At scale=1.0 this never fires (stepped_vol == base_volume but scale == 1.0).
    # At scale=0.5 with base=0.01: 0.005→0.01 ≥ 0.01 → HARD BLOCK on 2nd re-entry.
    # At scale=0.5 with base=0.10: 0.05 < 0.10 → passes, genuine 50% reduction.
    if stepped_vol >= base_volume and scale < 1.0:
        return 0.0, True

    return stepped_vol, False


# ── State record ──────────────────────────────────────────────────────────


@dataclass
class ExitRecord:
    """Lightweight record of a managed exit for re-entry decisions."""

    timestamp: float
    strategy_name: str
    direction: str  # "long" or "short"
    reason: str  # raw exit reason
    confidence: float  # consensus confidence at time of exit
    price: float  # mid price at exit
    ticket: int

    # Derived
    category: str = ""  # set in __post_init__

    def __post_init__(self) -> None:
        self.category = classify(self.reason)


@dataclass
class ReentryState:
    """Per-strategy re-entry tracking (stored in LiveCycleState)."""

    last_exit: ExitRecord | None = None
    consecutive_same_direction: int = 0
    last_direction: str | None = None  # None = no prior direction (was "" sentinel)

    def record_exit(self, record: ExitRecord) -> None:
        self.last_exit = record
        if record.direction == self.last_direction:
            self.consecutive_same_direction += 1
        else:
            self.consecutive_same_direction = 0
            self.last_direction = record.direction

    def check_and_record_entry(
        self,
        direction: str,
        confidence: float,
        mid: float,
        entry_half_life: float = 0.0,
        timeframe_minutes: float = 5.0,
        sl_cooldown: float | None = None,
        sl_penalty: float | None = None,
        bleed_cooldown: float | None = None,
        bleed_penalty: float | None = None,
        # ── DQAF-20260616-001/P2: L3 architecture ──
        is_rule_based: bool = False,
    ) -> tuple[bool, str, float]:
        """Check re-entry quality and return (allowed, reason, volume_scale).

        *entry_half_life* is forwarded to check_reentry_quality for TTL
        (time-to-live) hard unlock — when elapsed time since the previous
        exit exceeds half_life × timeframe_minutes × 2.5, the lock is
        forcibly broken even if price confirmation hasn't occurred.

        *is_rule_based* (DQAF-20260616-001/P2): when True, the strategy uses
        time-based cooldown instead of ML confidence thresholds (see
        :func:`check_reentry_quality` for details).
        """
        if self.last_exit is None:
            # First entry ever — always allowed
            if direction == self.last_direction:
                self.consecutive_same_direction += 1
            else:
                self.consecutive_same_direction = 0
                self.last_direction = direction
            return True, "first_entry", 1.0

        allowed, reason = check_reentry_quality(
            exit_reason_raw=self.last_exit.reason,
            exit_direction=self.last_exit.direction,
            exit_confidence=self.last_exit.confidence,
            exit_price=self.last_exit.price,
            exit_timestamp=self.last_exit.timestamp,
            new_direction=direction,
            new_confidence=confidence,
            mid_price=mid,
            entry_half_life=entry_half_life,
            timeframe_minutes=timeframe_minutes,
            sl_cooldown_override=sl_cooldown,
            sl_penalty_override=sl_penalty,
            bleed_cooldown_override=bleed_cooldown,
            bleed_penalty_override=bleed_penalty,
            is_rule_based=is_rule_based,
        )
        if not allowed:
            return False, reason, 0.0

        # FIX-20260529-039: stale exit — the last_exit is >24h old and
        # irrelevant.  Reset consecutive_same_direction to 0 before
        # incrementing, otherwise 20+ replayed bootstrap entries all in
        # the same direction cause volume_decay_blocked on the first live
        # trade.  (Bootstrap sets timestamp=now so elapsed is small; the
        # stale flag is the only reliable signal that the counter is
        # counting replays, not actual live entries.)
        if reason.startswith("stale_exit_allowed"):
            self.consecutive_same_direction = 0
            self.last_direction = None

        # Determine consecutive count for volume scaling.
        # The caller applies apply_reentry_volume_scale with the actual
        # decision volume to ensure min_lot discretization is handled.
        if direction == self.last_direction:
            self.consecutive_same_direction += 1
        else:
            self.consecutive_same_direction = 0
            self.last_direction = direction
        return True, reason, float(self.consecutive_same_direction)


# ── State helpers (for LiveCycleState integration) ────────────────────────


def ensure_reentry_state(store: dict[str, Any], strategy_name: str) -> ReentryState:
    """Get or create a ReentryState for *strategy_name* inside *store*."""
    if strategy_name not in store:
        store[strategy_name] = ReentryState()
    return store[strategy_name]

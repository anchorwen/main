"""Re-entry quality guard — prevents churn after managed exits.

Distinguishes exit reasons and applies graduated re-entry criteria
rather than a blunt same-direction block.  Inspired by:

  - Lopez de Prado meta-labelling (separate direction from action quality)
  - Grinold & Kahn IC decay (signal information erodes with reuse)

Design:
  Layer 1 — exit-reason classifier (normalises raw exit reason strings)
  Layer 2 — signal-quality gate (confidence improvement + price confirmation)
  Layer 3 — volume decay (consecutive same-direction positions shrink)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _classify_exit_reason(raw_reason: str) -> str:
    """Map a raw exit reason string to a canonical category.

    Returns one of: ``"brain_flip"``, ``"sl_hit"``, ``"tp_hit"``,
    ``"time_expired"``, ``"ou_revert"``, ``"meta_exit"``, ``"unknown"``.
    """
    r = raw_reason.lower()
    if "brain_flip" in r or "signal_reversal" in r or "confidence_drop" in r:
        return "brain_flip"
    if "sl_hit" in r or "sl_stop" in r:
        return "sl_hit"
    if "tp_hit" in r or "take_profit" in r:
        return "tp_hit"
    if "time_" in r or "phase" in r:
        return "time_expired"
    if "ou_" in r or "zscore" in r or "z_score" in r:
        return "ou_revert"
    if "meta_exit" in r:
        return "meta_exit"
    if "hesitation_" in r:
        return "hesitation"
    if "bleed_stop_" in r:
        return "bleed_stop"
    if "ev_trajectory" in r:
        return "time_expired"  # EV trajectory IS a time-based exit
    return "unknown"


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
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for re-entering after a managed exit.

    Opposite-direction re-entries are always allowed (no guard).
    Same-direction re-entries are gated by exit-reason-specific criteria
    including minimum elapsed time, confidence improvement, and price confirmation.
    """
    if new_direction not in ("long", "short"):
        return False, "neutral_signal"
    if new_direction != exit_direction:
        return True, "opposite_direction"

    import time as _time

    elapsed = (now_timestamp if now_timestamp is not None else _time.time()) - exit_timestamp
    category = _classify_exit_reason(exit_reason_raw)

    if category == "brain_flip":
        # Brain flipped against us — this is the most dangerous exit category
        # for churn.  Require: minimum elapsed time, significantly stronger
        # signal, AND price confirmation of the new entry direction.
        if elapsed < 120:
            return False, f"brain_flip_too_soon_{elapsed:.0f}s_lt_120s"
        if new_confidence < max(exit_confidence + 0.10, 0.70):
            return (
                False,
                f"brain_flip_confidence_not_improved_{new_confidence:.3f}_need_{max(exit_confidence + 0.10, 0.70):.3f}",
            )
        if exit_direction == "long" and mid_price <= exit_price:
            return False, "brain_flip_price_not_confirming_long"
        if exit_direction == "short" and mid_price >= exit_price:
            return False, "brain_flip_price_not_confirming_short"
        return True, "brain_flip_reentry_confirmed"

    if category == "sl_hit":
        # Stop-loss — strictest requirements (SL streak breaker handles most)
        if elapsed < 180:
            return False, f"sl_too_soon_{elapsed:.0f}s_lt_180s"
        if new_confidence < exit_confidence + 0.10:
            return (
                False,
                f"sl_recovery_confidence_insufficient_{new_confidence:.3f}",
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
        if new_confidence < max(exit_confidence + 0.05, 0.70):
            return (
                False,
                f"ou_revert_confidence_not_improved_{new_confidence:.3f}_need_{max(exit_confidence + 0.05, 0.70):.3f}",
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
        if new_confidence < max(exit_confidence + 0.15, 0.70):
            return (
                False,
                f"hesitation_confidence_not_improved_{new_confidence:.3f}",
            )
        # For LONG: price must be LOWER than exit (cheaper entry)
        if exit_direction == "long" and mid_price >= exit_price:
            return False, "hesitation_price_not_confirming_long"
        # For SHORT: price must be HIGHER than exit (cheaper entry)
        if exit_direction == "short" and mid_price <= exit_price:
            return False, "hesitation_price_not_confirming_short"
        return True, "hesitation_reentry_confirmed"

    if category == "bleed_stop":
        # Bleed stop — consecutive bars with negative PnL.  This is a
        # strong adverse signal — treat similarly to SL hit.
        if elapsed < 180:
            return False, f"bleed_stop_too_soon_{elapsed:.0f}s_lt_180s"
        if new_confidence < exit_confidence + 0.10:
            return (
                False,
                f"bleed_stop_confidence_not_improved_{new_confidence:.3f}",
            )
        if exit_direction == "long" and mid_price <= exit_price + 1.0:
            return False, "bleed_stop_price_not_confirming_long"
        if exit_direction == "short" and mid_price >= exit_price - 1.0:
            return False, "bleed_stop_price_not_confirming_short"
        return True, "bleed_stop_recovery_allowed"

    # Unknown — conservative: block same-direction
    return False, f"unknown_exit_reason_blocked_{exit_reason_raw[:30]}"


# ── Volume decay ──────────────────────────────────────────────────────────


def apply_reentry_volume_scale(
    base_volume: float,
    consecutive_same_direction: int,
    lot_step: float = 0.01,
) -> tuple[float, bool]:
    """Scale volume down for consecutive same-direction entries.

    0 = first entry (full size)
    1 = first re-entry (0.75×)
    2 = second re-entry (0.50×)
    3+ = blocked (0)

    Returns (volume, should_block).  If the scaled volume rounds back
    up to the original volume due to min_lot discretization, the order
    is hard-blocked — the penalty must have real effect.
    """
    if consecutive_same_direction == 0:
        return base_volume, False

    scale = max(0.0, 1.0 - (consecutive_same_direction * 0.25))
    if scale == 0.0:
        return 0.0, True  # 3+ consecutive same-direction → hard block

    raw_vol = base_volume * scale
    stepped_vol = max(0.01, round(raw_vol / lot_step) * lot_step)

    # Core defense: if discretization rounds back to original volume,
    # the penalty is ineffective → hard block.
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
        self.category = _classify_exit_reason(self.reason)


@dataclass
class ReentryState:
    """Per-strategy re-entry tracking (stored in LiveCycleState)."""

    last_exit: ExitRecord | None = None
    consecutive_same_direction: int = 0
    last_direction: str = ""

    def record_exit(self, record: ExitRecord) -> None:
        self.last_exit = record
        if record.direction == self.last_direction:
            self.consecutive_same_direction += 1
        else:
            self.consecutive_same_direction = 0
            self.last_direction = record.direction

    def check_and_record_entry(
        self, direction: str, confidence: float, mid: float
    ) -> tuple[bool, str, float]:
        """Check re-entry quality and return (allowed, reason, volume_scale)."""
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
        )
        if not allowed:
            return False, reason, 0.0

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

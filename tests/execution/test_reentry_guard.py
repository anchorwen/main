"""Tests for reentry_guard — prevents same-direction churn after managed exits."""

from __future__ import annotations

import time

from core.execution.reentry_guard import (
    ExitRecord,
    ReentryState,
    _classify_exit_reason,
    apply_reentry_volume_scale,
    check_reentry_quality,
    ensure_reentry_state,
)


class TestClassifyExitReason:
    def test_confidence_drop_is_momentum_pause(self):
        # FIX-116: confidence_drop/decay → momentum_pause (same-direction dip, not flip)
        assert _classify_exit_reason("confidence_drop_0.500") == "momentum_pause"
        assert _classify_exit_reason("confidence_drop_0.835") == "momentum_pause"
        assert _classify_exit_reason("confidence_decay_0.200") == "momentum_pause"

    def test_signal_reversal_is_brain_flip(self):
        assert _classify_exit_reason("signal_reversal_short") == "brain_flip"
        assert _classify_exit_reason("brain_flip_long") == "brain_flip"

    def test_ou_reversion_is_ou_revert(self):
        assert _classify_exit_reason("ou_reversion_z0.11") == "ou_revert"
        assert _classify_exit_reason("ou_zscore_exit") == "ou_revert"
        assert _classify_exit_reason("zscore_revert_1.5") == "ou_revert"

    def test_sl_hit(self):
        assert _classify_exit_reason("sl_hit_first") == "sl_hit"
        assert _classify_exit_reason("sl_stop_triggered") == "sl_hit"

    def test_tp_hit(self):
        assert _classify_exit_reason("tp_hit_first") == "tp_hit"
        assert _classify_exit_reason("take_profit_reached") == "tp_hit"

    def test_time_expired(self):
        assert _classify_exit_reason("time_exit_phase_3") == "time_expired"
        assert _classify_exit_reason("phase_2_exit") == "time_expired"

    def test_unknown(self):
        assert _classify_exit_reason("some_random_reason_xyz") == "unknown"


class TestCheckReentryQuality:
    # ── momentum_pause (confidence_drop/decay) — lenient same-direction reentry ──
    # FIX-116: momentum_pause has 60s cooldown (vs 120s for brain_flip),
    # -0.05 confidence tolerance (vs strict improvement for brain_flip),
    # and no price-confirmation requirement.

    def test_momentum_pause_blocks_too_soon(self):
        now = time.time()
        allowed, reason = check_reentry_quality(
            exit_reason_raw="confidence_drop_0.500",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=now - 30,
            now_timestamp=now,
            new_direction="short",
            new_confidence=0.80,
            mid_price=4730.0,
        )
        assert allowed is False
        assert "too_soon" in reason
        assert "60s" in reason

    # ── brain_flip (signal_reversal) — strict directional reversal reentry ──

    def test_brain_flip_blocks_without_confidence_improvement(self):
        # Use signal_reversal to trigger actual brain_flip category
        allowed, reason = check_reentry_quality(
            exit_reason_raw="signal_reversal_short",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.52,  # +0.02 < required max(+0.10, 0.70) = 0.70
            mid_price=4730.0,
        )
        assert allowed is False
        assert "confidence_not_improved" in reason

    def test_brain_flip_blocks_without_price_confirmation_short(self):
        allowed, reason = check_reentry_quality(
            exit_reason_raw="signal_reversal_short",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.75,  # meets max(0.60, 0.70) = 0.70
            mid_price=4732.0,  # price went UP (bad for SHORT reentry)
        )
        assert allowed is False
        assert "price_not_confirming_short" in reason

    def test_brain_flip_allows_with_confidence_and_price_confirmation(self):
        allowed, reason = check_reentry_quality(
            exit_reason_raw="signal_reversal_short",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.75,  # meets max(0.60, 0.70) = 0.70
            mid_price=4730.0,  # price went DOWN (good for SHORT reentry)
        )
        assert allowed is True
        assert "brain_flip_reentry_confirmed" in reason

    # ── ou_revert time gate ──

    def test_ou_revert_blocks_too_soon(self):
        now = time.time()
        allowed, reason = check_reentry_quality(
            exit_reason_raw="ou_reversion_z0.11",
            exit_direction="short",
            exit_confidence=0.70,
            exit_price=4731.0,
            exit_timestamp=now - 30,
            now_timestamp=now,
            new_direction="short",
            new_confidence=0.80,
            mid_price=4735.0,
        )
        assert allowed is False
        assert "too_soon" in reason
        assert "120s" in reason

    # ── ou_revert (the churn bug we fixed) ──

    def test_ou_revert_blocks_without_confidence_improvement(self):
        """OU exit require confidence +0.05 and also min 0.70."""
        allowed, reason = check_reentry_quality(
            exit_reason_raw="ou_reversion_z0.11",
            exit_direction="short",
            exit_confidence=0.70,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.71,  # +0.01 < required max(+0.05, 0.70) = 0.75
            mid_price=4735.0,
        )
        assert allowed is False
        assert "confidence_not_improved" in reason

    def test_ou_revert_blocks_without_price_confirming_new_extreme(self):
        """OU exit: SHORT requires price HIGHER than exit (further from mean)."""
        allowed, reason = check_reentry_quality(
            exit_reason_raw="ou_reversion_z0.11",
            exit_direction="short",
            exit_confidence=0.70,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.80,  # meets max(0.75, 0.70) = 0.75
            mid_price=4731.0,  # NOT higher — price didn't move further from mean
        )
        assert allowed is False
        assert "price_not_confirming_new_extreme_short" in reason

    def test_ou_revert_allows_with_confidence_and_new_extreme(self):
        """OU exit: allows re-entry when both confidence up AND price confirms."""
        allowed, reason = check_reentry_quality(
            exit_reason_raw="ou_reversion_z0.11",
            exit_direction="short",
            exit_confidence=0.70,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.80,  # meets max(0.75, 0.70) = 0.75
            mid_price=4735.0,  # HIGHER = new extreme forming
        )
        assert allowed is True
        assert "ou_revert_reentry_confirmed" in reason

    def test_ou_revert_long_requires_price_lower(self):
        """OU exit LONG: requires price LOWER than exit to confirm new extreme."""
        allowed, reason = check_reentry_quality(
            exit_reason_raw="ou_reversion_z0.11",
            exit_direction="long",
            exit_confidence=0.70,
            exit_price=4680.0,
            exit_timestamp=time.time() - 300,
            new_direction="long",
            new_confidence=0.80,  # meets max(0.75, 0.70) = 0.75
            mid_price=4685.0,  # HIGHER — should be LOWER for long extreme
        )
        assert allowed is False
        assert "price_not_confirming_new_extreme_long" in reason

    # ── opposite direction always allowed ──

    def test_opposite_direction_always_allowed(self):
        # Opposite direction bypasses ALL gates — even recent exit
        now = time.time()
        allowed, reason = check_reentry_quality(
            exit_reason_raw="confidence_drop_0.500",
            exit_direction="short",
            exit_confidence=0.80,
            exit_price=4731.0,
            exit_timestamp=now - 30,
            now_timestamp=now,
            new_direction="long",
            new_confidence=0.30,
            mid_price=4731.0,
        )
        assert allowed is True
        assert reason == "opposite_direction"

    # ── sl_hit ──

    def test_sl_hit_requires_strict_confidence(self):
        allowed, reason = check_reentry_quality(
            exit_reason_raw="sl_hit_first",
            exit_direction="long",
            exit_confidence=0.50,
            exit_price=4700.0,
            exit_timestamp=time.time() - 300,
            new_direction="long",
            new_confidence=0.55,  # +0.05 < required +0.10
            mid_price=4710.0,
        )
        assert allowed is False
        assert "confidence_insufficient" in reason

    def test_sl_hit_blocks_too_soon(self):
        now = time.time()
        allowed, reason = check_reentry_quality(
            exit_reason_raw="sl_hit_first",
            exit_direction="long",
            exit_confidence=0.50,
            exit_price=4700.0,
            exit_timestamp=now - 30,
            now_timestamp=now,
            new_direction="long",
            new_confidence=0.70,
            mid_price=4710.0,
        )
        assert allowed is False
        assert "too_soon" in reason

    # ── meta_exit time gate ──

    def test_meta_exit_blocks_too_soon(self):
        now = time.time()
        allowed, reason = check_reentry_quality(
            exit_reason_raw="meta_exit_high_vol",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=now - 30,
            now_timestamp=now,
            new_direction="short",
            new_confidence=0.65,
            mid_price=4730.0,
        )
        assert allowed is False
        assert "too_soon" in reason

    # ── neutral signal ──

    def test_neutral_direction_always_blocked(self):
        allowed, reason = check_reentry_quality(
            exit_reason_raw="confidence_drop_0.500",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="neutral",
            new_confidence=0.80,
            mid_price=4730.0,
        )
        assert allowed is False
        assert reason == "neutral_signal"


class TestReentryState:
    def test_first_entry_always_allowed(self):
        rs = ReentryState()
        allowed, reason, scale = rs.check_and_record_entry("long", 0.5, 2000.0)
        assert allowed is True
        assert reason == "first_entry"
        assert scale == 1.0

    def test_blocks_same_direction_after_brain_flip_recent(self):
        rs = ReentryState()
        rs.record_exit(
            ExitRecord(
                timestamp=time.time() - 30,
                strategy_name="statarb_dynamic",
                direction="short",
                reason="confidence_drop_0.500",
                confidence=0.50,
                price=4731.0,
                ticket=12345,
            )
        )
        # Re-entry too soon — blocked by 120s time gate
        allowed, reason, scale = rs.check_and_record_entry("short", 0.80, 4730.0)
        assert allowed is False
        assert "too_soon" in reason

    def test_allows_after_confidence_and_price_improve(self):
        rs = ReentryState()
        rs.record_exit(
            ExitRecord(
                timestamp=time.time() - 300,
                strategy_name="statarb_dynamic",
                direction="short",
                reason="confidence_drop_0.500",
                confidence=0.50,
                price=4731.0,
                ticket=12345,
            )
        )
        allowed, reason = check_reentry_quality(
            exit_reason_raw="confidence_drop_0.500",
            exit_direction="short",
            exit_confidence=0.50,
            exit_price=4731.0,
            exit_timestamp=time.time() - 300,
            new_direction="short",
            new_confidence=0.75,  # meets max(0.60, 0.70) = 0.70
            mid_price=4730.0,  # price went down (good for SHORT)
        )
        assert allowed is True


class TestVolumeDecay:
    def test_first_entry_full_volume(self):
        vol, blocked = apply_reentry_volume_scale(1.0, 0)
        assert vol == 1.0
        assert blocked is False

    def test_first_reentry_grace_period(self):
        # Tiered decay: 1st re-entry = grace period (full volume, gated by cooldown)
        vol, blocked = apply_reentry_volume_scale(1.0, 1)
        assert vol == 1.0
        assert blocked is False

    def test_second_reentry_50pct(self):
        vol, blocked = apply_reentry_volume_scale(1.0, 2)
        assert vol == 0.50
        assert blocked is False

    def test_third_reentry_blocked(self):
        vol, blocked = apply_reentry_volume_scale(1.0, 3)
        assert vol == 0.0
        assert blocked is True

        vol, blocked = apply_reentry_volume_scale(1.0, 99)
        assert vol == 0.0
        assert blocked is True


class TestEnsureReentryState:
    def test_creates_new_state_when_missing(self):
        store: dict = {}
        rs = ensure_reentry_state(store, "statarb_dynamic")
        assert isinstance(rs, ReentryState)
        assert "statarb_dynamic" in store

    def test_returns_existing_state(self):
        store: dict = {}
        rs1 = ensure_reentry_state(store, "barrier_12bar")
        rs2 = ensure_reentry_state(store, "barrier_12bar")
        assert rs1 is rs2

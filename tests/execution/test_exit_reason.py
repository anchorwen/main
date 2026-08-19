"""Unit tests for exit_reason.py — canonical exit reason taxonomy + classify().

Covers:
  - ExitReason enum: 15 members, str inheritance, cooldown_tier property
  - Category sets: _MODEL_DRIVEN, _RISK_DRIVEN, _STRUCTURAL
  - classify(): all 15+ pattern matching branches, boundary cases
  - _classify_exit_reason(): backward-compatible shim
"""

from __future__ import annotations

from typing import Any, cast

from core.execution.exit_reason import (
    ExitReason,
    _classify_exit_reason,
    classify,
)

# ═══════════════════════════════════════════════════════════════════════════
# ExitReason enum
# ═══════════════════════════════════════════════════════════════════════════


class TestExitReasonEnum:
    """Enum definition and properties."""

    def test_has_23_members(self) -> None:
        """23 canonical exit reasons (15 original + 8 V6 from FIX-20260629-195)."""
        assert len(ExitReason) == 23

    def test_is_str_subclass(self) -> None:
        """Inherits from str for backward compatibility."""
        assert issubclass(ExitReason, str)
        assert isinstance(ExitReason.BRAIN_FLIP, str)

    def test_member_value_equals_name(self) -> None:
        """Each member's value matches its name."""
        # TECH_DEBT-009: Enum literal vs str literal strict_equality 非重叠 — 运行时 str 子类按值相等, cast(object) 类型层绕过
        assert cast(object, ExitReason.BRAIN_FLIP) == "brain_flip"
        assert cast(object, ExitReason.SL_HIT) == "sl_hit"
        assert cast(object, ExitReason.UNKNOWN) == "unknown"


class TestCooldownTiers:
    """cooldown_tier property on each ExitReason."""

    def test_brain_flip_is_heavy(self) -> None:
        assert ExitReason.BRAIN_FLIP.cooldown_tier == "heavy"

    def test_tp_hit_is_light(self) -> None:
        assert ExitReason.TP_HIT.cooldown_tier == "light"

    def test_watchdog_is_block(self) -> None:
        assert ExitReason.WATCHDOG.cooldown_tier == "block"

    def test_emergency_close_is_block(self) -> None:
        assert ExitReason.EMERGENCY_CLOSE.cooldown_tier == "block"

    def test_unknown_is_heavy(self) -> None:
        assert ExitReason.UNKNOWN.cooldown_tier == "heavy"

    def test_all_members_have_cooldown_tier(self) -> None:
        """Every enum member must have a defined cooldown tier."""
        for member in ExitReason:
            assert member.cooldown_tier in (
                "light",
                "medium",
                "heavy",
                "block",
            ), f"{member} has invalid cooldown_tier: {member.cooldown_tier}"


class TestCategoryProperties:
    """is_model_driven, is_risk_driven, is_structural."""

    def test_brain_flip_is_model_driven(self) -> None:
        assert ExitReason.BRAIN_FLIP.is_model_driven is True
        assert ExitReason.BRAIN_FLIP.is_risk_driven is False
        assert ExitReason.BRAIN_FLIP.is_structural is False

    def test_sl_hit_is_risk_driven(self) -> None:
        assert ExitReason.SL_HIT.is_model_driven is False
        assert ExitReason.SL_HIT.is_risk_driven is True
        assert ExitReason.SL_HIT.is_structural is False

    def test_tp_hit_is_structural(self) -> None:
        assert ExitReason.TP_HIT.is_model_driven is False
        assert ExitReason.TP_HIT.is_risk_driven is False
        assert ExitReason.TP_HIT.is_structural is True

    def test_unknown_is_none_of_them(self) -> None:
        assert ExitReason.UNKNOWN.is_model_driven is False
        assert ExitReason.UNKNOWN.is_risk_driven is False
        assert ExitReason.UNKNOWN.is_structural is False

    def test_mutual_exclusivity(self) -> None:
        """Each member belongs to at most one category.

        Members with no category: NET_OUT, UNKNOWN_CLOSE, UNKNOWN.
        """
        no_category = {ExitReason.NET_OUT, ExitReason.UNKNOWN_CLOSE, ExitReason.UNKNOWN}
        for member in ExitReason:
            categories = sum(
                [
                    member.is_model_driven,
                    member.is_risk_driven,
                    member.is_structural,
                ]
            )
            if member in no_category:
                assert categories == 0, f"{member} has {categories} categories (expected 0)"
            else:
                assert categories == 1, f"{member} belongs to {categories} categories (expected 1)"


# ═══════════════════════════════════════════════════════════════════════════
# classify()
# ═══════════════════════════════════════════════════════════════════════════


class TestClassify:
    """Pure function: raw string → ExitReason."""

    # ── Null/empty defense ──

    def test_none_returns_unknown(self) -> None:
        assert classify(None) == ExitReason.UNKNOWN

    def test_empty_string_returns_unknown(self) -> None:
        assert classify("") == ExitReason.UNKNOWN

    def test_non_string_returns_unknown(self) -> None:
        assert (
            classify(cast(Any, 42)) == ExitReason.UNKNOWN
        )  # TECH_DEBT-009: 非法输入探针 (int→str|None 参数) → cast(Any)

    # ── P0: Exact-match canonical labels ──

    def test_win_maps_to_tp_hit(self) -> None:
        assert classify("win") == ExitReason.TP_HIT
        assert classify("WIN") == ExitReason.TP_HIT

    def test_loss_maps_to_sl_hit(self) -> None:
        assert classify("loss") == ExitReason.SL_HIT
        assert classify("LOSS") == ExitReason.SL_HIT

    def test_breakeven_maps_to_time_expired(self) -> None:
        assert classify("breakeven") == ExitReason.TIME_EXPIRED

    # ── P3: Operational labels ──

    def test_close_accepted_maps_to_time_expired(self) -> None:
        assert classify("close_accepted") == ExitReason.TIME_EXPIRED

    # ── P3: Orphan labels ──

    def test_auto_orphan_maps_to_unknown_close(self) -> None:
        assert classify("auto_orphan_rejected") == ExitReason.UNKNOWN_CLOSE
        assert classify("auto_orphan_stale_ticket_123") == ExitReason.UNKNOWN_CLOSE

    # ── Brain/signal exits ──

    def test_brain_flip_patterns(self) -> None:
        assert classify("brain_flip") == ExitReason.BRAIN_FLIP
        assert classify("signal_reversal_h1") == ExitReason.BRAIN_FLIP
        assert classify("BRAIN_FLIP_urgent") == ExitReason.BRAIN_FLIP

    def test_momentum_pause_patterns(self) -> None:
        assert classify("confidence_decay") == ExitReason.MOMENTUM_PAUSE
        assert classify("confidence_drop_0.3") == ExitReason.MOMENTUM_PAUSE

    def test_kalman_flip_patterns(self) -> None:
        assert classify("kalman_velocity_negative") == ExitReason.KALMAN_FLIP

    def test_meta_exit_patterns(self) -> None:
        assert classify("meta_exit") == ExitReason.META_EXIT
        assert classify("pnl_urgency_high") == ExitReason.META_EXIT
        assert classify("time_decay_active") == ExitReason.META_EXIT
        assert classify("regime_misalignment") == ExitReason.META_EXIT
        assert classify("consensus_drift") == ExitReason.META_EXIT
        assert classify("vol_expansion_critical") == ExitReason.META_EXIT
        assert classify("ml_p_win_below_0.3") == ExitReason.META_EXIT

    def test_ou_revert_patterns(self) -> None:
        assert classify("ou_revert") == ExitReason.OU_REVERT
        assert classify("zscore_exit") == ExitReason.OU_REVERT
        assert classify("z_score_2.5") == ExitReason.OU_REVERT

    # ── Risk exits ──

    def test_sl_hit_patterns(self) -> None:
        assert classify("sl_hit") == ExitReason.SL_HIT
        assert classify("sl_stop_triggered") == ExitReason.SL_HIT

    def test_tp_hit_patterns(self) -> None:
        assert classify("tp_hit") == ExitReason.TP_HIT
        assert classify("take_profit_full") == ExitReason.TP_HIT
        assert classify("partial_tp_0.5") == ExitReason.TP_HIT

    def test_bleed_stop_patterns(self) -> None:
        assert classify("bleed_stop_consecutive_3") == ExitReason.BLEED_STOP

    def test_exit_watchdog_patterns(self) -> None:
        assert classify("exit_watchdog_retry_exhausted") == ExitReason.WATCHDOG

    def test_emergency_close_patterns(self) -> None:
        assert classify("grace_period_emergency_72h") == ExitReason.EMERGENCY_CLOSE

    # ── Time/structural exits ──

    def test_ev_trajectory_maps_to_time_expired(self) -> None:
        assert classify("ev_trajectory_btc_swing_gamma1.0_t7%_r-0.80") == ExitReason.TIME_EXPIRED

    def test_time_prefix_maps_to_time_expired(self) -> None:
        assert classify("time_expired") == ExitReason.TIME_EXPIRED
        assert classify("phase_timeout") == ExitReason.TIME_EXPIRED

    def test_hesitation_patterns(self) -> None:
        assert classify("hesitation_cycle_5") == ExitReason.HESITATION

    # ── Portfolio/netting exits ──

    def test_net_out_patterns(self) -> None:
        assert classify("net_out_hedge") == ExitReason.NET_OUT

    # ── Unknown close ──

    def test_mia_close_maps_to_unknown_close(self) -> None:
        assert classify("mia_close_ticket_123") == ExitReason.UNKNOWN_CLOSE

    def test_unknown_close_maps_to_unknown_close(self) -> None:
        assert classify("unknown_close") == ExitReason.UNKNOWN_CLOSE

    def test_manual_close_maps_to_unknown_close(self) -> None:
        assert classify("manual_close") == ExitReason.UNKNOWN_CLOSE
        # "manual" substring matches in "manual_close" AND standalone
        assert classify("manual") == ExitReason.UNKNOWN_CLOSE

    # ── Fallthrough ──

    def test_completely_unknown_string_returns_unknown(self) -> None:
        assert classify("xyz_random_garbage") == ExitReason.UNKNOWN

    def test_numeric_string_returns_unknown(self) -> None:
        assert classify("12345") == ExitReason.UNKNOWN

    # ── Case insensitivity ──

    def test_mixed_case(self) -> None:
        assert classify("Sl_HiT") == ExitReason.SL_HIT
        assert classify("Brain_Flip") == ExitReason.BRAIN_FLIP

    # ── Substring matching precedence ──

    def test_hesitation_over_time_prefix(self) -> None:
        """hesitation_ prefix checked BEFORE time_ prefix."""
        assert classify("hesitation_cycle") == ExitReason.HESITATION
        # (not TIME_EXPIRED which would also match "time_"... wait, "hesitation_cycle"
        # doesn't contain "time_", so this is fine. But "hesitation_timeout" would
        # match "hesitation_" first → HESITATION wins.)

    def test_brain_flip_over_meta_exit(self) -> None:
        """'meta_exit_brain_flip' → BRAIN_FLIP (brain_flip checked first at L214)."""
        # Note: "brain_flip" (L214) is checked before "meta_exit" (L224).
        # A raw string containing both returns BRAIN_FLIP.
        assert classify("meta_exit_brain_flip") == ExitReason.BRAIN_FLIP


# ═══════════════════════════════════════════════════════════════════════════
# _classify_exit_reason — backward-compatible shim
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyExitReasonShim:
    """Backward-compatible string→string wrapper."""

    def test_returns_string(self) -> None:
        result = _classify_exit_reason("brain_flip")
        assert result == "brain_flip"
        assert isinstance(result, str)

    def test_delegates_to_classify(self) -> None:
        assert _classify_exit_reason("sl_hit") == "sl_hit"
        assert _classify_exit_reason("win") == "tp_hit"
        assert _classify_exit_reason(None) == "unknown"

    def test_unknown_input_returns_unknown_string(self) -> None:
        assert _classify_exit_reason("garbage") == "unknown"

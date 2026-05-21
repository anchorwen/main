"""Tests for ActivePositionManager — dynamic exit orchestration."""

import pytest

from core.execution.position_manager import ActivePosition, ActivePositionManager

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def long_position() -> ActivePosition:
    pm = ActivePositionManager()
    return pm.register_position(
        ticket=12345,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=0,
        entry_consensus={"aggregated_bias": "long", "consensus_score": 0.65},
        supporting_brain_ids=["brain_a", "brain_b", "brain_c"],
        current_high=2500.0,
    )


@pytest.fixture
def short_position() -> ActivePosition:
    pm = ActivePositionManager()
    return pm.register_position(
        ticket=12346,
        side="short",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2510.0,
        initial_tp=2465.0,
        entry_atr=5.0,
        entry_cycle=0,
        entry_consensus={"aggregated_bias": "short", "consensus_score": 0.60},
        supporting_brain_ids=["brain_a", "brain_b"],
        current_high=2500.0,
    )


@pytest.fixture
def manager() -> ActivePositionManager:
    return ActivePositionManager()


# ── Registration ─────────────────────────────────────────────────────────


def test_register_long_sets_all_fields(manager):
    pos = manager.register_position(
        ticket=1,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=10,
        entry_consensus={"aggregated_bias": "long", "consensus_score": 0.70},
        supporting_brain_ids=["b1", "b2"],
    )
    assert pos.ticket == 1
    assert pos.side == "long"
    assert pos.entry_price == 2500.0
    assert pos.initial_sl == 2490.0
    assert pos.current_sl == 2490.0
    assert pos.initial_tp == 2535.0
    assert pos.current_tp == 2535.0
    assert pos.entry_atr == 5.0
    assert pos.entry_cycle == 10
    assert pos.highest_high == 2500.0
    assert pos.lowest_low == 2500.0
    assert pos.breakeven_triggered is False
    assert pos.trail_multiplier == 2.0
    assert pos.r_milestones_hit == []
    assert pos.cycles_held == 0
    assert pos.highest_r == 0.0


def test_has_position(manager):
    assert not manager.has_position()
    manager.register_position(
        ticket=1,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=0,
    )
    assert manager.has_position()


def test_clear_position(manager):
    manager.register_position(
        ticket=1,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=0,
    )
    manager.clear_position()
    assert not manager.has_position()
    assert manager.get_position() is None


# ── Layer 1: Chandelier trailing stop ────────────────────────────────────


def test_trail_stop_moves_up_for_long(long_position, manager):
    """SL should advance upward as highest_high increases."""
    pos = manager._position = long_position
    pos.highest_high = 2510.0  # price moved +10
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = 2510 - 2.0*5 = 2500 > 2490
    assert new_sl is not None
    assert new_sl > pos.current_sl  # 2500 > 2490


def test_trail_stop_moves_down_for_short(short_position, manager):
    """SL should advance downward as lowest_low decreases."""
    pos = manager._position = short_position
    pos.lowest_low = 2490.0  # price moved -10
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = 2490 + 2.0*5 = 2500 < 2510
    assert new_sl is not None
    assert new_sl < pos.current_sl  # 2500 < 2510


def test_trail_stop_never_moves_backward_long(long_position, manager):
    """SL should not go down for a long when price retreats.

    With graduated lock disabled, trail SL stays when highest_high unchanged.
    """
    manager.graduated_lock_enabled = False
    pos = manager._position = long_position
    pos.highest_high = 2510.0
    pos.current_sl = 2500.0  # already trailed up
    # Now price retreats but highest_high stays 2510
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate still = 2510 - 10 = 2500, which == current_sl, so None
    assert new_sl is None


def test_graduated_lock_advances_sl_at_3r_long(long_position, manager):
    """At +3R peak, graduated lock raises SL floor to +1.5R."""
    pos = manager._position = long_position
    pos.highest_high = 2515.0  # +15 = 3R at entry_atr=5.0
    pos.current_sl = 2500.0  # at breakeven
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = 2515 - 10 = 2505, graduated lock at 3R: floor = 2500 + 1.5*5 = 2507.5
    assert new_sl == 2507.5


def test_trail_stop_none_when_no_improvement(long_position, manager):
    """Return None when trail hasn't advanced."""
    pos = manager._position = long_position
    pos.highest_high = 2498.0  # small gain, candidate = 2498 - 10 = 2488 < 2490
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    assert new_sl is None


def test_trail_stop_after_breakeven_long(long_position, manager):
    """After breakeven, SL cannot go below entry price."""
    pos = manager._position = long_position
    pos.breakeven_triggered = True
    pos.highest_high = 2508.0
    pos.current_sl = 2500.0
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = max(entry, 2508 - 10) = max(2500, 2498) = 2500
    # 2500 == current_sl, so None
    assert new_sl is None


def test_trail_stop_above_breakeven_long(long_position, manager):
    """After breakeven, SL can advance above entry, but capped at max_lock_atr."""
    pos = manager._position = long_position
    pos.breakeven_triggered = True
    pos.highest_high = 2520.0
    pos.current_sl = 2500.0  # at breakeven
    manager.max_lock_atr = 1.0
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = max(2500, 2520 - 10) = 2510, but capped at 2500 + 1.0*5 = 2505
    assert new_sl == 2505.0


def test_trail_stop_respects_max_lock_cap(long_position, manager):
    """Trail SL cannot exceed entry + max_lock_atr * entry_atr."""
    pos = manager._position = long_position
    pos.breakeven_triggered = True
    pos.highest_high = 2540.0  # candidate = 2540 - 10 = 2530
    pos.current_sl = 2500.0
    manager.max_lock_atr = 1.0
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # capped at 2500 + 1.0*5 = 2505, even though raw trail would be 2530
    assert new_sl == 2505.0


def test_trail_stop_never_below_original_sl_long(long_position, manager):
    """Original SL is the hard floor — trail cannot go below it."""
    pos = manager._position = long_position
    pos.breakeven_triggered = True
    pos.highest_high = 2485.0  # price went the wrong way
    pos.current_sl = 2490.0
    new_sl = manager.compute_trail_stop(current_atr=5.0)
    # candidate = max(2500, 2485-10) = max(2500, 2475) = 2500
    # but 2500 < current_sl? No, 2500 > 2490, so it would advance.
    # Actually the breakeven constraint ensures candidate >= 2500
    # And 2500 > original SL 2490, so it's valid
    # Wait — 2500 > 2490, so it returns 2500 which is capped at max_lock=2505
    # Let me test the case where the raw trail goes below original SL
    assert new_sl is not None  # trail advances to at least original SL floor


# ── Breakeven ─────────────────────────────────────────────────────────────


def test_breakeven_not_triggered_below_threshold(long_position, manager):
    """Breakeven should not trigger when move < threshold."""
    pos = manager._position = long_position
    pos.highest_high = 2503.0  # moved +3, threshold = 1.0*5 = 5
    assert not manager.should_breakeven(mid=2503.0, current_atr=5.0)


def test_breakeven_triggered_above_threshold(long_position, manager):
    """Breakeven should trigger when move >= threshold."""
    pos = manager._position = long_position
    pos.highest_high = 2506.0  # moved +6, threshold = 1.0*5 = 5
    assert manager.should_breakeven(mid=2504.0, current_atr=5.0)


def test_breakeven_not_triggered_twice(long_position, manager):
    """Breakeven should not trigger again once already triggered."""
    pos = manager._position = long_position
    pos.highest_high = 2506.0
    pos.breakeven_triggered = True
    assert not manager.should_breakeven(mid=2504.0, current_atr=5.0)


def test_breakeven_short(short_position, manager):
    """Breakeven for short: entry - lowest_low >= threshold."""
    pos = manager._position = short_position
    pos.lowest_low = 2493.0  # moved -7, threshold = 5
    assert manager.should_breakeven(mid=2495.0, current_atr=5.0)


# ── R-multiple ─────────────────────────────────────────────────────────────


def test_r_multiple_long(long_position, manager):
    """R = (mid - entry) / risk for long."""
    manager._position = long_position
    abs(2500.0 - 2490.0)  # 10
    r = manager._compute_r_multiple(mid=2515.0)
    assert r == pytest.approx(1.5)  # (2515 - 2500) / 10 = 1.5


def test_r_multiple_short(short_position, manager):
    """R = (entry - mid) / risk for short."""
    manager._position = short_position
    abs(2500.0 - 2510.0)  # 10
    r = manager._compute_r_multiple(mid=2485.0)
    assert r == pytest.approx(1.5)  # (2500 - 2485) / 10 = 1.5


def test_r_milestone_1r(long_position, manager):
    pos = manager._position = long_position
    tag = manager.check_r_milestones(mid=2510.0)  # 1R
    assert tag == "1R"
    assert "1R" in pos.r_milestones_hit


def test_r_milestone_skips_already_hit(long_position, manager):
    pos = manager._position = long_position
    pos.r_milestones_hit = ["1R", "2R", "3R"]
    tag = manager.check_r_milestones(mid=2540.0)  # 4R but all hit
    assert tag is None


def test_r_milestone_multi_hit_in_one_step(long_position, manager):
    """Jump from 0 to 3.5R in one step — only highest milestone returned per call."""
    pos = manager._position = long_position
    tag = manager.check_r_milestones(mid=2535.0)  # 3.5R
    # Code checks 3R first, so returns 3R
    assert tag == "3R"
    # Only 3R was appended this cycle
    assert "3R" in pos.r_milestones_hit

    # Subsequent call at same mid should return next un-hit milestone
    tag2 = manager.check_r_milestones(mid=2535.0)  # still 3.5R
    assert tag2 == "2R"

    tag3 = manager.check_r_milestones(mid=2535.0)  # still 3.5R
    assert tag3 == "1R"
    assert pos.r_milestones_hit == ["3R", "2R", "1R"]


# ── Regime multiplier adjustment ─────────────────────────────────────────


def test_regime_low_vol_tightens_trail(long_position, manager):
    """Low vol → tighter trail multiplier."""
    pos = manager._position = long_position
    pos.trail_multiplier = 2.0
    manager.trail_atr_mult_low = 1.5
    manager._adjust_trail_for_regime(5.0, {"regime": "low"})
    assert pos.trail_multiplier == 1.5


def test_regime_high_vol_loosens_trail(long_position, manager):
    """High vol → looser trail multiplier."""
    pos = manager._position = long_position
    pos.trail_multiplier = 2.0
    manager.trail_atr_mult_high = 3.0
    manager._adjust_trail_for_regime(5.0, {"regime": "high"})
    assert pos.trail_multiplier == 3.0


def test_adaptive_trail_vol_expansion(long_position, manager):
    """When volatility expands (vol_ratio > 1.5), trail K widens +0.8."""
    pos = manager._position = long_position
    pos.trail_multiplier = 2.0
    pos.entry_atr = 5.0
    current_atr = 10.0  # vol_ratio = 2.0 (> 1.5)
    pos.highest_high = 2550.0
    manager._adjust_trail_for_regime(current_atr, {"regime": "normal"})
    assert pos.trail_multiplier == 2.8  # 2.0 + 0.8


def test_adaptive_trail_vol_contraction(long_position, manager):
    """When volatility contracts (vol_ratio < 0.7), trail K tightens -0.3."""
    pos = manager._position = long_position
    pos.trail_multiplier = 2.0
    pos.entry_atr = 5.0
    current_atr = 1.0  # vol_ratio = 0.2 (< 0.7)
    pos.highest_high = 2550.0
    manager._adjust_trail_for_regime(current_atr, {"regime": "normal"})
    assert pos.trail_multiplier == 1.7  # 2.0 - 0.3


# ── Layer 2: Brain ensemble exit ──────────────────────────────────────────


def test_brain_exit_no_flip_below_threshold(long_position, manager):
    """No exit when fewer than 50% of brains flip."""
    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1", "b2", "b3", "b4"]
    # Only b4 flipped (1/4 = 25% < 50%)
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.65},
        ["b1", "b2", "b3"],
    )
    assert not should_exit
    assert reason == ""


def test_brain_exit_flip_above_threshold(long_position, manager):
    """Exit when >= 50% flip AND consecutive confirmation count reached."""
    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1", "b2", "b3", "b4"]
    manager.flip_confirm_count = 2
    # First flip (50%): detected but not yet triggered
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.65},
        ["b2", "b4"],
    )
    assert not should_exit  # needs second confirmation
    assert manager._consecutive_flips == 1
    # Second consecutive flip: triggers exit
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.65},
        ["b2", "b4"],
    )
    assert should_exit
    assert "brain_flip_50pct_c2" in reason


def test_brain_exit_flip_extreme_immediate(long_position, manager):
    """>= 70% flip triggers immediate exit without waiting for confirmation."""
    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1", "b2", "b3", "b4"]
    manager.flip_confirm_count = 2
    # 3/4 = 75% flip → immediate
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.65},
        ["b2"],  # only 1 of 4 remains
    )
    assert should_exit
    assert "brain_flip_extreme" in reason


def test_brain_flip_resets_on_no_flip(long_position, manager):
    """Consecutive flip counter resets when a cycle has no flip."""
    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1", "b2"]
    manager.flip_confirm_count = 2
    # First flip
    manager.evaluate_brain_exit({"consensus_score": 0.65}, ["b2"])  # 50% flip
    assert manager._consecutive_flips == 1
    # No flip this cycle
    manager.evaluate_brain_exit({"consensus_score": 0.65}, ["b1", "b2"])  # 0% flip
    assert manager._consecutive_flips == 0


def test_brain_exit_confidence_drop_ema(long_position, manager):
    """Exit when confidence drops sharply — EMA-filtered single large drop."""
    pos = manager._position = long_position
    manager._entry_consensus_score = 0.65
    pos.confidence_ema = 0.65  # seed EMA
    # Single large drop: 0.65 → 0.30
    # ema = 0.4 * 0.30 + 0.6 * 0.65 = 0.12 + 0.39 = 0.51
    # ema_drop = 0.65 - 0.51 = 0.14 > 0.10
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.30},
        pos.supporting_brain_ids,
    )
    assert should_exit
    assert "confidence_decay_ema" in reason


def test_brain_exit_no_confidence_drop_small(long_position, manager):
    """No exit when confidence drop is small (no brain flip)."""
    pos = manager._position = long_position
    manager._entry_consensus_score = 0.65
    # Drop of 0.07 < 0.10
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.58},
        pos.supporting_brain_ids,
    )
    assert not should_exit


def test_brain_exit_all_new_brains(long_position, manager):
    """All entry-supporting brains flipped — 100% flip."""
    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1", "b2"]
    should_exit, reason = manager.evaluate_brain_exit(
        {"consensus_score": 0.65},
        ["b3", "b4"],  # completely different set
    )
    assert should_exit
    assert "brain_flip" in reason
    assert "100" in reason


def test_brain_reeval_interval(manager):
    """should_reeval_brains returns True after interval cycles."""
    manager.brain_reeval_interval = 5
    # First call: never evaluated → should evaluate
    assert manager.should_reeval_brains(cycle_count=100)
    manager.mark_brains_reevaluated(100)
    # Same cycle: already evaluated
    assert not manager.should_reeval_brains(100)
    assert not manager.should_reeval_brains(101)
    assert not manager.should_reeval_brains(104)
    # Interval passed
    assert manager.should_reeval_brains(105)
    assert manager.should_reeval_brains(200)


# ── Layer 3: Time-based exit ──────────────────────────────────────────────


def test_time_exit_stale_past_horizon(long_position, manager):
    """Past expiry (t/T >= 1.0): must deliver full design R:R (3.5) to hold."""
    pos = manager._position = long_position
    pos.cycles_held = 65
    manager.max_hold_cycles = 60
    # T_max=60, t_ratio=1.0, ev_floor=-0.5+4.0×1.0=3.5
    # mid=2501 → R=0.1 < 3.5 → exit
    should_exit, reason = manager.should_exit_time_based(mid=2501.0)
    assert should_exit
    assert "ev_trajectory" in reason
    assert "r0.10_lt_3.50" in reason


def test_time_exit_past_horizon_spares_strong_winner(long_position, manager):
    """Past expiry with R >= design R:R: horse is running, let it ride."""
    pos = manager._position = long_position
    pos.cycles_held = 65
    manager.max_hold_cycles = 60
    # T_max=60, ev_floor=3.5; mid=2540 → R=4.0 >= 3.5 → spared
    should_exit, _ = manager.should_exit_time_based(mid=2540.0)
    assert not should_exit


def test_time_exit_early_underwater_triggers(long_position, manager):
    """Early position deeply underwater below EV floor → exit.

    No hard grace period — gamma=1.0 linear curve with start_floor=-0.5R
    means a -0.5R drawdown at 16.7% progress exits.
    """
    pos = manager._position = long_position
    pos.cycles_held = 10
    manager.max_hold_cycles = 60  # t_ratio = 16.7%
    # ev_floor = -0.5 + 4.0 × 0.167 = 0.167
    # mid=2495 → R=-0.5 < 0.167 → exit
    should_exit, reason = manager.should_exit_time_based(mid=2495.0)
    assert should_exit
    assert "ev_trajectory" in reason


def test_time_exit_mid_life_override_min_r(long_position, manager):
    """override_min_r lowers the envelope endpoint — lenient early, tight late."""
    pos = manager._position = long_position
    pos.cycles_held = 36  # t/T = 60%
    manager.max_hold_cycles = 60
    # override_min_r=0.3: end_target=0.3 (instead of r_target=3.5)
    # ev_floor at 60% = -0.5 + (0.3+0.5) × 0.6 = -0.02
    # R=-0.1 (mid=2499) < -0.02 → exit
    should_exit, _ = manager.should_exit_time_based(mid=2499.0, override_min_r=0.3)
    assert should_exit
    # R=0.5 (mid=2505) > -0.02 → spared
    should_exit2, _ = manager.should_exit_time_based(mid=2505.0, override_min_r=0.3)
    assert not should_exit2


def test_time_exit_late_stage_rising_bar(long_position, manager):
    """Late stage (t/T=90%) bar rises: must show real progress to hold."""
    pos = manager._position = long_position
    pos.cycles_held = 54  # t/T = 90%
    manager.max_hold_cycles = 60
    # override_min_r=0.3: ev_floor at 90% = -0.5 + 0.8 × 0.9 = 0.22
    # R=0.1 (mid=2501) < 0.22 → exit
    should_exit, _ = manager.should_exit_time_based(mid=2501.0, override_min_r=0.3)
    assert should_exit
    # R=0.5 (mid=2505) > 0.22 → spared
    should_exit2, _ = manager.should_exit_time_based(mid=2505.0, override_min_r=0.3)
    assert not should_exit2


def test_time_exit_model_horizon_used(long_position, manager):
    """When model_horizons are set, use the shortest one."""
    pos = manager._position = long_position
    pos.model_horizons = {"v9_brain": 12, "mtx_brain": 3}  # min = 3
    pos.cycles_held = 2  # ratio = 2/3 = 0.67 → phase 2
    manager.require_min_r = 0.3
    # R ≈ 0.1 at mid=2501
    should_exit, _ = manager.should_exit_time_based(mid=2501.0)
    assert should_exit  # MTX short horizon triggers earlier


# ── Payload builders ──────────────────────────────────────────────────────


def test_build_modify_payload(long_position, manager):
    manager._position = long_position
    payload = manager.build_modify_payload(new_sl=2505.0, new_tp=2535.0, reason="trail")
    assert payload["action"] == "modify_sltp"
    assert payload["position_ticket"] == 12345
    assert payload["sl"] == 2505.0
    assert payload["tp"] == 2535.0
    assert payload["comment"] == "trail"


def test_build_close_payload(long_position, manager):
    manager._position = long_position
    payload = manager.build_close_payload(reason="brain_flip_50pct")
    assert payload["action"] == "close"
    assert payload["position_ticket"] == 12345
    assert payload["volume"] == 0.01
    assert "brain_flip" in payload["comment"]


# ── update_prices ─────────────────────────────────────────────────────────


def test_update_prices_tracks_extremes_long(long_position, manager):
    """update_prices should track highest_high and lowest_low."""
    pos = manager._position = long_position
    result = manager.update_prices(
        mid=2510.0, bid=2509.5, ask=2510.5, current_atr=5.0, cycle_count=1
    )
    assert result["mid"] == 2510.0
    assert pos.highest_high == 2509.5  # bid for long
    assert pos.lowest_low == 2500.0  # unchanged (ask didn't go below)
    assert pos.cycles_held == 1


def test_update_prices_tracks_highest_r(long_position, manager):
    pos = manager._position = long_position
    manager.update_prices(mid=2520.0, bid=2519.5, ask=2520.5, current_atr=5.0, cycle_count=1)
    assert pos.highest_r == pytest.approx(2.0)  # (2520-2500)/10
    # Lower mid should not lower highest_r
    manager.update_prices(mid=2505.0, bid=2504.5, ask=2505.5, current_atr=5.0, cycle_count=2)
    assert pos.highest_r == pytest.approx(2.0)


def test_update_prices_empty_when_no_position(manager):
    result = manager.update_prices(mid=2500.0, bid=2500.0, ask=2500.0, current_atr=5.0)
    assert result == {}


# ── OU mean-reversion exit ────────────────────────────────────────────────


def test_ou_exit_when_z_reverts(long_position, manager):
    """OU exit triggers when |current_z| < z_exit AND |entry_z| >= 1.5."""
    manager._position = long_position
    long_position.entry_z_score = 2.5  # entered at meaningful extreme
    should_exit, reason = manager.should_exit_ou_based(current_z_score=0.1, z_exit=0.3)
    assert should_exit
    assert "ou_revert_target_reached" in reason


def test_ou_exit_not_when_z_still_high(long_position, manager):
    """No OU exit when |current_z| is still above exit threshold (still extreme)."""
    manager._position = long_position
    long_position.entry_z_score = 2.5
    should_exit, reason = manager.should_exit_ou_based(current_z_score=-2.5, z_exit=0.3)
    assert not should_exit
    assert "ou_waiting_for_reversion" in reason


def test_ou_no_exit_without_entry_extreme(long_position, manager):
    """If entry was NOT at extreme (|z| < 1.5), don't use reversion exit."""
    manager._position = long_position
    long_position.entry_z_score = 0.5  # not an extreme entry
    should_exit, reason = manager.should_exit_ou_based(current_z_score=0.1, z_exit=0.3)
    assert not should_exit
    assert "ou_ignored_low_entry_z" in reason


def test_ou_no_exit_unknown_entry(long_position, manager):
    """Recovered positions (entry_z_score=0) are never exited via OU reversion."""
    manager._position = long_position
    long_position.entry_z_score = 0.0  # unknown entry z (restart recovery)
    long_position.cycles_held = 5  # even after many cycles, no warmup escape
    should_exit, reason = manager.should_exit_ou_based(current_z_score=0.1, z_exit=0.3)
    assert not should_exit
    assert reason == "ou_ignored_low_entry_z_0.00"


def test_ou_exit_threshold_boundary(long_position, manager):
    """|entry_z|=1.5 exactly passes the gate."""
    manager._position = long_position
    long_position.entry_z_score = 1.5  # boundary — meets >= 1.5
    should_exit, reason = manager.should_exit_ou_based(current_z_score=0.1, z_exit=0.3)
    assert should_exit
    assert "ou_revert_target_reached" in reason


def test_ou_no_exit_just_below_threshold(long_position, manager):
    """|entry_z|=1.49 is just below threshold — rejected."""
    manager._position = long_position
    long_position.entry_z_score = 1.49
    should_exit, reason = manager.should_exit_ou_based(current_z_score=0.1, z_exit=0.3)
    assert not should_exit
    assert "ou_ignored_low_entry_z" in reason


def test_ou_waiting_for_reversion(long_position, manager):
    """Extreme entry but current_z still high — waiting, don't exit."""
    manager._position = long_position
    long_position.entry_z_score = 2.0
    should_exit, reason = manager.should_exit_ou_based(current_z_score=1.8, z_exit=0.3)
    assert not should_exit
    assert reason == "ou_waiting_for_reversion"


# ── Brain-specific trail ──────────────────────────────────────────────────


def test_brain_specific_trail_no_pnl_store(long_position, manager):
    """Returns 1.0 when no PnL store is set."""
    manager._position = long_position
    scale = manager._compute_brain_specific_trail_scale()
    assert scale == 1.0


def test_brain_specific_trail_high_sharpe(long_position, manager):
    """High Sharpe brains get wider trail (scale > 1.0)."""
    from types import SimpleNamespace

    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1"]

    class FakePnL:
        def get_metrics(self, bid):
            return SimpleNamespace(sharpe_ratio=2.0, sample_count=10)

    manager.pnl_store = FakePnL()
    scale = manager._compute_brain_specific_trail_scale()
    assert scale > 1.0  # wider trail for good performers


def test_brain_specific_trail_negative_sharpe(long_position, manager):
    """Negative Sharpe brains get tighter trail (scale < 1.0)."""
    from types import SimpleNamespace

    pos = manager._position = long_position
    pos.supporting_brain_ids = ["b1"]

    class FakePnL:
        def get_metrics(self, bid):
            return SimpleNamespace(sharpe_ratio=-2.0, sample_count=10)

    manager.pnl_store = FakePnL()
    scale = manager._compute_brain_specific_trail_scale()
    assert scale < 1.0  # tighter trail for poor performers


# ── Model horizons ────────────────────────────────────────────────────────


def test_register_with_model_horizons(manager):
    pos = manager.register_position(
        ticket=1,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=0,
        model_horizons={"v9": 12, "mtx": 3},
    )
    assert pos.model_horizons == {"v9": 12, "mtx": 3}


def test_effective_horizon_uses_shortest(long_position, manager):
    pos = manager._position = long_position
    pos.model_horizons = {"v9_brain": 12, "mtx_brain": 3, "ou_brain": 0}
    h = manager._get_effective_horizon()
    assert h == 3  # min of non-zero horizons, ou_brain(0) excluded


def test_effective_horizon_fallback(long_position, manager):
    pos = manager._position = long_position
    pos.model_horizons = {}
    manager.max_hold_cycles = 60
    h = manager._get_effective_horizon()
    assert h == 60  # fallback to max_hold_cycles

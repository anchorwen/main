"""FIX-20260709-004 (L3) — per-TF ``bracket_atr`` scaling of the trailing TP.

Regression suite for DQAF-20260709-003: the trailing-TP tightener
(``compute_trail_tp``) computed its distance from the M5-scale ``current_atr``
while the SL/TP bracket had been sized with the strategy's own-timeframe ATR
(FIX-20260706-027).  On h1/h4 swings this collapsed the take-profit from a
swing-scale target to an M5-scale one (RR 1.66 → 0.08).

The fix stores ``bracket_atr`` (the per-TF ATR that sized the bracket) on the
position and scales the trailing-TP distance by ``bracket_atr / entry_atr`` so
the tightened TP stays in the bracket's timeframe.  ``entry_atr`` is left
untouched (it remains the M5 reference used by the R-metric / ratchet / MetaExit
features), and ``bracket_atr == 0`` reproduces the exact legacy behaviour.
"""

from __future__ import annotations

import pytest

from core.execution.position_manager import ActivePositionManager


def _short(
    pm: ActivePositionManager,
    *,
    entry_atr: float,
    bracket_atr: float,
    entry_price: float = 2500.0,
    initial_sl: float = 2650.0,
    initial_tp: float = 2237.5,
    ticket: int = 1,
    was_profitable: bool = True,
):
    pos = pm.register_position(
        ticket=ticket,
        side="short",
        entry_price=entry_price,
        volume=0.01,
        initial_sl=initial_sl,
        initial_tp=initial_tp,
        entry_atr=entry_atr,
        bracket_atr=bracket_atr,
        entry_cycle=0,
        trail_atr_mult=2.0,
        current_high=entry_price,
    )
    # FIX-20260710-001: compute_trail_tp now requires the position to have been
    # profitable before tightening TP.  Simulate a brief favourable excursion
    # so the ATR-contraction gate is reachable.
    if was_profitable:
        pos.lowest_low = entry_price - (entry_atr * 1.5)  # 1.5× entry ATR below entry
        pos.highest_high = entry_price  # short: highest_high stays at entry
    return pos


def test_register_position_stores_bracket_atr() -> None:
    pm = ActivePositionManager()
    pos = _short(pm, entry_atr=5.0, bracket_atr=75.0)
    assert pos.bracket_atr == pytest.approx(75.0)


def test_register_position_bracket_atr_defaults_zero() -> None:
    """Callers that omit bracket_atr get 0.0 → legacy (M5) scale fallback."""
    pm = ActivePositionManager()
    pos = pm.register_position(
        ticket=2,
        side="short",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2510.0,
        initial_tp=2465.0,
        entry_atr=5.0,
        entry_cycle=0,
    )
    assert pos.bracket_atr == 0.0


def test_compute_trail_tp_scales_to_bracket_timeframe() -> None:
    """With bracket_atr >> entry_atr, the tightened TP distance scales up by
    the timeframe ratio instead of collapsing to the M5 scale."""
    pm = ActivePositionManager()
    _short(pm, entry_atr=5.0, bracket_atr=75.0)  # 15× TF scale

    # current_atr contracts to 3.0 → atr_ratio 0.60 ≤ 0.80 → tightening fires.
    new_tp = pm.compute_trail_tp(3.0, ticket=1)

    # tp_distance = trail_mult(2.0) × current_atr(3.0) × 1.75 × (75/5=15) = 157.5
    assert new_tp is not None
    assert new_tp == pytest.approx(2500.0 - 157.5, abs=0.05)
    # A swing-scale distance, NOT the collapsed M5 distance (10.5).
    assert (2500.0 - new_tp) > 100.0


def test_compute_trail_tp_legacy_fallback_when_no_bracket_atr() -> None:
    """bracket_atr == 0 reproduces the exact pre-fix behaviour (M5 distance)."""
    pm = ActivePositionManager()
    _short(pm, entry_atr=5.0, bracket_atr=0.0)

    new_tp = pm.compute_trail_tp(3.0, ticket=1)

    # Legacy: tp_distance = 2.0 × 3.0 × 1.75 × 1.0 = 10.5
    assert new_tp is not None
    assert new_tp == pytest.approx(2500.0 - 10.5, abs=0.05)


def test_compute_trail_tp_incident_regression_no_collapse() -> None:
    """The live XAU h4_swing incident (DQAF-20260709-003): the tightened TP
    must remain a swing-scale target, not collapse to entry−11."""
    pm = ActivePositionManager()
    _short(
        pm,
        entry_atr=4.61,  # M5 ATR stored as entry_atr
        bracket_atr=69.96,  # H4 ATR that actually sized the bracket (2.0×=139.9 SL)
        entry_price=4055.844,
        initial_sl=4195.755,  # 2.0 × H4 ATR above entry
        initial_tp=3823.46,  # 3.5 × H4 ATR below entry (RR ≈ 1.66)
    )

    new_tp = pm.compute_trail_tp(3.40, ticket=1)

    assert new_tp is not None
    tp_distance = 4055.844 - new_tp
    # Fixed distance ≈ 2.0 × 3.40 × 1.75 × (69.96/4.61) ≈ 180.6 — a real swing TP.
    assert tp_distance > 150.0
    # The collapsed (buggy) TP would sit at entry − 11.9 = 4043.9.
    assert new_tp < 3950.0
    # And the resulting reward:risk is no longer inverted (SL distance 139.9).
    assert tp_distance / 139.911 > 1.0


def test_bracket_atr_survives_save_load_round_trip(tmp_path) -> None:
    """bracket_atr is persisted (v3 intent-state) and restored across restart."""
    state_path = tmp_path / "active_position.json"
    pm = ActivePositionManager()
    _short(pm, entry_atr=4.61, bracket_atr=69.96, ticket=4103318355)
    pm.save_state(state_path)

    pm2 = ActivePositionManager()
    pm2.load_state(state_path)
    restored = pm2.get_position(ticket=4103318355)

    assert restored is not None
    assert restored.bracket_atr == pytest.approx(69.96)


def test_compute_trail_tp_suppressed_when_never_profitable() -> None:
    """FIX-20260710-001 / DQAF-20260710-001: TP tightening must be suppressed
    on positions that have NEVER seen favourable price movement.

    A SHORT position with lowest_low >= entry_price means price never went
    below entry — the position has been losing since open.  Tightening TP
    closer to entry in this state makes recovery HARDER, not easier.
    """
    pm = ActivePositionManager()
    _short(pm, entry_atr=5.0, bracket_atr=75.0, was_profitable=False)

    # ATR contracts — would trigger tightening without the gate
    new_tp = pm.compute_trail_tp(3.0, ticket=1)

    # Gate must suppress: position was never profitable
    assert new_tp is None


def test_compute_trail_tp_allowed_when_was_profitable() -> None:
    """Counter-case: with lowest_low below entry (was profitable), TP tightening
    proceeds normally when ATR contracts."""
    pm = ActivePositionManager()
    _short(pm, entry_atr=5.0, bracket_atr=75.0, was_profitable=True)

    new_tp = pm.compute_trail_tp(3.0, ticket=1)

    # Gate must allow: position saw favourable excursion
    assert new_tp is not None
    assert new_tp < 2500.0  # SHORT TP tightened inward


def test_compute_trail_tp_profitability_gate_long() -> None:
    """LONG mirror: highest_high <= entry_price → never profitable → suppress."""
    pm = ActivePositionManager()
    pos = pm.register_position(
        ticket=10,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2400.0,
        initial_tp=2700.0,
        entry_atr=5.0,
        bracket_atr=75.0,
        entry_cycle=0,
        trail_atr_mult=2.0,
        current_high=2500.0,
    )
    # Never profitable: highest_high == entry_price
    assert pos.highest_high == pytest.approx(2500.0)

    new_tp = pm.compute_trail_tp(3.0, ticket=10)
    assert new_tp is None  # Gate must suppress

    # Now simulate a profitable excursion
    pos.highest_high = 2520.0  # price went above entry
    new_tp = pm.compute_trail_tp(3.0, ticket=10)
    assert new_tp is not None  # Gate must allow
    assert new_tp > 2500.0  # LONG TP tightened inward

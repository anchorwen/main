"""Regression tests for the cross-strategy opposing-position coordinator.

DQAF-20260709-002 / FIX entry-phase: the CrossStrategyCoordinator existed since
P4-2 but was never injected into the live path (strategy_evaluator received
``cross_strategy_coordinator=None``), so its opposing-position guard never ran
and an XAU LONG (m30_swing) opened against an existing SHORT (h4_swing) — a
hedge that paid spread twice and cancelled edge.  These tests lock the guard's
block/warn/off semantics and the institutional default mode.

Statistical scope: pure unit tests of the decision object; no live data, no MT5.
"""

from __future__ import annotations

from core.execution.cross_strategy_coordinator import CrossStrategyCoordinator


def _positions(**by_strategy: str) -> dict[str, dict]:
    """Build a current_positions map: {strategy: {"direction": ...}}."""
    return {
        s: {"direction": d, "ticket": 1000 + i, "volume": 0.01}
        for i, (s, d) in enumerate(by_strategy.items())
    }


def test_block_mode_blocks_opposing_position() -> None:
    coord = CrossStrategyCoordinator(mode="block")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(h4_swing="short"),
    )
    assert res.blocked is True
    assert res.recommended_action == "block"
    assert [o.strategy_name for o in res.opposing_positions] == ["h4_swing"]


def test_incident_scenario_long_blocked_by_existing_short() -> None:
    """The exact hedge from the incident: m30_swing LONG must be blocked while
    h4_swing already holds a SHORT."""
    coord = CrossStrategyCoordinator(mode="block")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(h4_swing="short", m15_swing="short"),
    )
    assert res.blocked is True
    assert set(o.strategy_name for o in res.opposing_positions) == {"h4_swing", "m15_swing"}


def test_block_mode_allows_same_direction() -> None:
    coord = CrossStrategyCoordinator(mode="block")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(h4_swing="long"),
    )
    assert res.blocked is False
    assert res.opposing_positions == []


def test_block_mode_allows_when_no_positions() -> None:
    coord = CrossStrategyCoordinator(mode="block")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions={},
    )
    assert res.blocked is False


def test_strategy_does_not_oppose_itself() -> None:
    """A strategy holding its own opposing position is not a cross-strategy
    conflict (that is a normal flip handled by the strategy's own exit)."""
    coord = CrossStrategyCoordinator(mode="block")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(m30_swing="short"),
    )
    assert res.blocked is False


def test_warn_mode_allows_but_flags() -> None:
    coord = CrossStrategyCoordinator(mode="warn")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(h4_swing="short"),
    )
    assert res.blocked is False  # telemetry only — does NOT block
    assert res.recommended_action == "warn"
    assert res.opposing_positions  # but records the conflict


def test_off_mode_is_noop() -> None:
    coord = CrossStrategyCoordinator(mode="off")
    res = coord.check(
        pending_strategy="m30_swing",
        pending_direction="long",
        current_positions=_positions(h4_swing="short"),
    )
    assert res.blocked is False
    assert res.opposing_positions == []


def test_conflict_count_accumulates_across_calls() -> None:
    coord = CrossStrategyCoordinator(mode="block")
    for _ in range(3):
        coord.check(
            pending_strategy="m30_swing",
            pending_direction="long",
            current_positions=_positions(h4_swing="short"),
        )
    assert coord.conflict_count["m30_swing"] == 3


def test_live_cycle_config_default_mode_is_block() -> None:
    """The wiring must default to the institutional 'block' mode so the guard
    is active without any live.yaml entry (regression against the dormant param
    that shipped None)."""
    from core.runtime.live_cycle import LiveCycleConfig

    assert LiveCycleConfig.__dataclass_fields__["cross_strategy_mode"].default == "block"

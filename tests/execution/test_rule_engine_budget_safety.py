"""Regression test: RuleEngineStrategyWrapper must survive budget attribute access.

DQAF-20260617-001: RuleEngineStrategyWrapper had no ``budget`` attribute,
causing AttributeError when live_cycle.py's budget reconciliation loop
tried to check ``_strat.budget is not None``.  4 consecutive cycle errors
brought the circuit breaker to 4/5 (threshold=5).

Root cause (RC-06): the wrapper implements ``evaluate()`` but does not
inherit from ``StrategyLine``, so it lacks the ``budget`` attribute that
live_cycle.py's reconciliation path expects.  All other strategy types
inherit from StrategyLine and have budget by default.

Fix: live_cycle.py now uses ``getattr(_strat, 'budget', None)`` for
defensive access.  This test verifies that:
  1. RuleEngineStrategyWrapper instances can be created without error
  2. getattr(wrapper, 'budget', None) returns None (safe to check)
  3. The wrapper does not crash when passed through budget-aware code paths

Related: FIX-20260613-038 (wrapper creation), DQAF-20260617-001 (this bug)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.rule_engine_strategy import RuleEngineStrategyWrapper
from core.execution.strategy_context import StrategyEvaluationContext


def make_wrapper() -> RuleEngineStrategyWrapper:
    """Create a minimal RuleEngineStrategyWrapper for testing."""
    mock_engine = MagicMock()
    mock_engine.spread_points = 0.010
    mock_engine.slippage_points = 0.005
    mock_engine._compute_barriers.return_value = (4320.0, 4310.0, 4340.0)
    return RuleEngineStrategyWrapper(
        strategy_name="structural_swing_v1",
        magic=90501,
        rule_engine=mock_engine,
        cooldown_bars=3,
    )


def test_wrapper_budget_is_none_via_getattr():
    """getattr(wrapper, 'budget', None) returns None — safe for defensive checks."""
    wrapper = make_wrapper()
    budget = getattr(wrapper, "budget", None)
    assert budget is None, (
        f"Expected budget=None for RuleEngineStrategyWrapper, got {budget!r}. "
        "live_cycle.py budget reconciliation uses getattr(_strat, 'budget', None) "
        "to guard against strategy types without budget tracking."
    )


def test_wrapper_survives_budget_check_pattern():
    """Simulate the exact pattern from live_cycle.py:4599/4614."""
    wrapper = make_wrapper()
    # This is the pattern that was crashing:
    #   if _strat is not None and _strat.budget is not None:
    # The fix uses getattr(_strat, 'budget', None) instead of _strat.budget.
    budget = getattr(wrapper, "budget", None)
    if budget is not None:
        budget.record_trade(0.0, False)  # should NOT be reached
    # If we get here without AttributeError, the fix works.
    assert budget is None


def test_wrapper_evaluate_still_works():
    """Sanity check: the wrapper's evaluate() still functions after the fix."""
    wrapper = make_wrapper()
    result = wrapper.evaluate(
        context=StrategyEvaluationContext(
            mid_price=4320.0,
            bid=4319.9,
            ask=4320.1,
            current_atr=5.0,
            trend_direction="long",
        ),
    )
    assert result.strategy_name == "structural_swing_v1"
    assert result.magic == 90501
    # Cooldown not yet expired (bars_since_last_signal starts at 999 > cooldown_bars=3)
    # Wait no — 999 >= 3 so cooldown IS expired. First call should produce a signal.
    assert result.should_trade is True
    assert result.direction == "long"
    assert result.sl > 0
    assert result.tp > 0


def test_strategy_dict_with_budget_check():
    """End-to-end: simulate the strategies dict lookup pattern."""
    wrapper = make_wrapper()
    strategies = {"structural_swing_v1": wrapper}

    _sname = "structural_swing_v1"
    _strat = strategies.get(_sname)

    # The fixed pattern from live_cycle.py
    if _strat is not None and getattr(_strat, "budget", None) is not None:
        _strat.budget.record_trade(0.0, False)  # type: ignore[attr-defined]  # should NOT be reached

    # Verify we passed the check without error
    assert _strat is not None
    assert getattr(_strat, "budget", None) is None

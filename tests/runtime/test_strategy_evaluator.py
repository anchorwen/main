"""E2E smoke tests for strategy_evaluator — locking extracted behavior.

These tests serve as the safety net for Strangler Fig extraction:
each extraction from live_cycle.py must preserve the behavior verified here.

Phase 1.A: Lock the behavior of already-extracted evaluate_strategy_lines().
Phase 1.B: Each new extraction adds its own smoke test here before extraction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.runtime.strategy_evaluator import evaluate_strategy_lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mock_portfolio_risk() -> MagicMock:
    pr = MagicMock()
    pr.check_cross_strategy_risk.return_value = MagicMock(
        approved=True,
        risk_score=0.1,
        blocked_strategies=[],
        sizing_multipliers={},
    )
    return pr


def _mock_execution_queue() -> MagicMock:
    eq = MagicMock()
    eq.stage.return_value = None
    eq.drain.return_value = []
    return eq


def _minimal_regime_info() -> dict:
    return {
        "regime": "normal",
        "adx": 25.0,
        "atr": 5.0,
        "trend_direction": "long",
        "trend_strength": 0.3,
        "h1_trend_direction": "long",
        "h1_trend_strength": 0.3,
        "primary_trend": "long",
        "strategy_gates": {
            "barrier_12bar": "full",
            "micro_3bar": "full",
        },
    }


# ---------------------------------------------------------------------------
# Test 1: Fail-Closed — bootstrap_degraded blocks ALL trades
# ---------------------------------------------------------------------------
def test_bootstrap_degraded_blocks_all_trades():
    """When restart state bootstrap fails, NO trade should be permitted.

    This is the Fail-Closed safety invariant: degraded state = no trading.
    FIX-20260606-138.
    """
    result = evaluate_strategy_lines(
        strategy_lines={"barrier_12bar": MagicMock(), "micro_3bar": MagicMock()},
        feature_vector=np.zeros(40, dtype=np.float32),
        micro_feature_vector=np.zeros(9, dtype=np.float32),
        mid_price=2000.0,
        bid=1999.5,
        ask=2000.5,
        current_atr=5.0,
        regime_info=_minimal_regime_info(),
        portfolio_risk=_mock_portfolio_risk(),
        execution_queue=_mock_execution_queue(),
        tracker=MagicMock(),
        pnl_ledger=MagicMock(),
        regime_gate=None,
        current_positions={},
        bootstrap_degraded=True,
    )

    # Invariants
    assert result["trade_decisions"] == 0, "bootstrap_degraded must produce zero trades"
    assert len(result["strategy_results"]) == 2
    for sr in result["strategy_results"]:
        assert sr["should_trade"] is False
        assert sr["reason"] == "bootstrap_degraded_fail_closed"


# ---------------------------------------------------------------------------
# Test 2: Empty strategy_lines → graceful no-op
# ---------------------------------------------------------------------------
def test_empty_strategy_lines_returns_gracefully():
    """Zero strategies should not crash — returns empty result."""
    result = evaluate_strategy_lines(
        strategy_lines={},
        feature_vector=np.zeros(40, dtype=np.float32),
        micro_feature_vector=np.zeros(9, dtype=np.float32),
        mid_price=2000.0,
        bid=1999.5,
        ask=2000.5,
        current_atr=5.0,
        regime_info=_minimal_regime_info(),
        regime_gate=None,
        portfolio_risk=_mock_portfolio_risk(),
        execution_queue=_mock_execution_queue(),
        tracker=MagicMock(),
        pnl_ledger=MagicMock(),
        current_positions={},
    )

    assert result["trade_decisions"] == 0
    assert result["decisions_map"] == {}
    assert result["strategy_results"] == []


# ---------------------------------------------------------------------------
# Test 3: Return structure contract
# ---------------------------------------------------------------------------
def test_result_has_required_keys():
    """The return dict must always contain the documented keys."""
    result = evaluate_strategy_lines(
        strategy_lines={},
        feature_vector=np.zeros(40, dtype=np.float32),
        micro_feature_vector=np.zeros(9, dtype=np.float32),
        mid_price=2000.0,
        bid=1999.5,
        ask=2000.5,
        current_atr=5.0,
        regime_info=_minimal_regime_info(),
        regime_gate=None,
        portfolio_risk=_mock_portfolio_risk(),
        execution_queue=_mock_execution_queue(),
        tracker=MagicMock(),
        pnl_ledger=MagicMock(),
        current_positions={},
    )

    assert "decisions_map" in result
    assert "trade_decisions" in result
    assert "strategy_results" in result
    assert isinstance(result["trade_decisions"], int)
    assert isinstance(result["decisions_map"], dict)
    assert isinstance(result["strategy_results"], list)


# ---------------------------------------------------------------------------
# Test 4: SL streak block is respected
# ---------------------------------------------------------------------------
def test_sl_streak_block_filters_blocked_strategy():
    """Strategies in SL streak timeout should be filtered out."""
    future_time = 9999999999.0
    blocked = {"barrier_12bar": future_time}

    result = evaluate_strategy_lines(
        strategy_lines={"barrier_12bar": MagicMock()},
        feature_vector=np.zeros(40, dtype=np.float32),
        micro_feature_vector=np.zeros(9, dtype=np.float32),
        mid_price=2000.0,
        bid=1999.5,
        ask=2000.5,
        current_atr=5.0,
        regime_info=_minimal_regime_info(),
        regime_gate=None,
        sl_streak_blocked_until=blocked,
        portfolio_risk=_mock_portfolio_risk(),
        execution_queue=_mock_execution_queue(),
        tracker=MagicMock(),
        pnl_ledger=MagicMock(),
        current_positions={},
    )

    # The blocked strategy should produce a "blocked_sl_streak" result
    blocked_results = [
        r for r in result["strategy_results"] if r.get("action") == "blocked_sl_streak"
    ]
    assert len(blocked_results) == 1
    assert blocked_results[0]["strategy"] == "barrier_12bar"


# ---------------------------------------------------------------------------
# Test 5: Feature vector repair handles NaN
# ---------------------------------------------------------------------------
def test_nan_feature_vector_does_not_crash():
    """NaN in feature vector should be repaired, not crash.

    This is the first market_stress_factory-driven test — simulating
    nan_cascade scenario at the strategy evaluation boundary.
    """
    nan_vector = np.full(40, np.nan, dtype=np.float32)

    result = evaluate_strategy_lines(
        strategy_lines={},
        feature_vector=nan_vector,
        micro_feature_vector=np.zeros(9, dtype=np.float32),
        mid_price=2000.0,
        bid=1999.5,
        ask=2000.5,
        current_atr=5.0,
        regime_info=_minimal_regime_info(),
        regime_gate=None,
        portfolio_risk=_mock_portfolio_risk(),
        execution_queue=_mock_execution_queue(),
        tracker=MagicMock(),
        pnl_ledger=MagicMock(),
        current_positions={},
    )

    # Must not crash. NaN repair is logged but doesn't block empty strategy set.
    assert result["trade_decisions"] == 0

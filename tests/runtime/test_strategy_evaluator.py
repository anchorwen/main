"""E2E smoke tests for strategy_evaluator — locking extracted behavior.

These tests serve as the safety net for Strangler Fig extraction:
each extraction from live_cycle.py must preserve the behavior verified here.

Phase 1.A: Lock the behavior of already-extracted evaluate_strategy_lines().
Phase 1.B: Each new extraction adds its own smoke test here before extraction.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from core.runtime.strategy_evaluator import (
    _gods_eye_health_vol_mult,
    evaluate_strategy_lines,
)


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


# ---------------------------------------------------------------------------
# DQAF-20260806-003 Option B2: GodsEye health deadband ramp
# (IC Approved 2026-08-06) — healthy GodsEye must NOT shave volume below the
# Ω min_economic floor ("threshold resonance" fix).
# ---------------------------------------------------------------------------
class TestGodsEyeHealthDeadbandRamp:
    @staticmethod
    def _approved_portfolio_risk(volume: float = 0.02) -> MagicMock:
        """Portfolio risk mock that approves and passes adjusted_volume."""
        pr = _mock_portfolio_risk()
        pr.check.return_value = SimpleNamespace(
            verdict=SimpleNamespace(value="approved"),
            adjusted_volume=volume,
            reason="",
        )
        return pr

    def test_healthy_deadband_is_full_passthrough(self):
        """health >= 0.70 → 1.0 (no volume intervention)."""
        for health in (1.0, 0.9, 0.875, 0.70):
            assert _gods_eye_health_vol_mult(health) == 1.0, f"health={health}"

    def test_degraded_band_linear_ramp(self):
        """0.25 < health < 0.70 → continuous linear ramp 0.25x..1.0x."""
        # Endpoint just below deadband: near-full passthrough.
        assert _gods_eye_health_vol_mult(0.699) == pytest.approx(0.9981, abs=1e-3)
        # Ramp midpoint: health=0.50 → 0.25 + 0.75 × (0.25/0.45) = 0.6667.
        assert _gods_eye_health_vol_mult(0.50) == pytest.approx(
            0.25 + 0.75 * (0.25 / 0.45), abs=1e-6
        )
        # Ramp is monotonic non-decreasing across the degraded band.
        xs = [0.26, 0.35, 0.50, 0.60, 0.69]
        vals = [_gods_eye_health_vol_mult(x) for x in xs]
        assert vals == sorted(vals)

    def test_floor_clamp_preserved(self):
        """health <= 0.25 → 0.25 (pre-existing worst-case multiplier unchanged)."""
        assert _gods_eye_health_vol_mult(0.25) == 0.25
        assert _gods_eye_health_vol_mult(0.10) == 0.25
        assert _gods_eye_health_vol_mult(0.0) == 0.25

    def test_decisive_case_healthy_eye_survives_omega_gate(self):
        """E2E reproduction of the production kill: volume=0.02, GodsEye
        health=0.875 (recommended_mode='normal').  Before B2 the GodsEye
        multiplier was max(0.25, 0.875)=0.875 → 0.02×0.875=0.0175 < 0.02 →
        KILLED at the Ω min_economic gate (intent_20260806T072045Z.log).
        After B2 the healthy eye does not shave → 0.02 survives the gate.
        """
        import core.runtime.strategy_evaluator as se
        from core.execution.strategy_decision import StrategyDecision

        strategy = MagicMock()
        strategy.config = None
        strategy.evaluate.return_value = StrategyDecision(
            strategy_name="h1_swing",
            magic=93200,
            should_trade=True,
            direction="long",
            confidence=0.877,
            volume=0.02,
            sl=0.5,
            tp=1.5,
            hard_sl=1.0,
        )

        eq = _mock_execution_queue()
        eq.is_pending_open.return_value = False
        eq.is_unattributed_blocked.return_value = False

        gods_eye = SimpleNamespace(
            health_score=0.875,
            confidence_modifier=1.1,
            recommended_mode="normal",
            chop_detected=False,
            anomaly_score=0.1,
            macro_bias="up",
        )

        # Stub the OOD gateway to "ok" so the decision reaches evaluate() +
        # Cut 7 (GodsEye) — OOD is not the code under test.
        _original_ood = se._get_ood_gateway
        se._get_ood_gateway = lambda: SimpleNamespace(
            check=lambda *a, **k: SimpleNamespace(status="ok", reason="ok")
        )
        try:
            result = evaluate_strategy_lines(
                strategy_lines={"h1_swing": strategy},
                feature_vector=np.ones(40, dtype=np.float32),
                micro_feature_vector=np.ones(9, dtype=np.float32),
                mid_price=2000.0,
                bid=1999.5,
                ask=2000.5,
                current_atr=5.0,
                regime_info=_minimal_regime_info(),
                regime_gate=None,
                portfolio_risk=self._approved_portfolio_risk(volume=0.02),
                execution_queue=eq,
                tracker=MagicMock(),
                pnl_ledger=MagicMock(),
                current_positions={},
                gods_eye_verdict=gods_eye,
                base_dir="",
            )
        finally:
            se._get_ood_gateway = _original_ood

        sr = result["strategy_results"][0]
        assert (
            sr["should_trade"] is True
        ), f"Healthy GodsEye must not shave 0.02 below floor: {sr['reason']}"
        assert sr["volume"] == pytest.approx(0.02, abs=1e-9)
        assert "volume_degraded_below_economic_minimum" not in sr["reason"]

    def test_degraded_eye_still_killed_by_omega_gate(self):
        """Red-line check: a truly degraded GodsEye (health=0.30) STILL gets
        killed at the Ω gate.  B2 only removes the shave in the healthy band;
        it does NOT relax the min_economic floor (0.02) in the degraded band.
        """
        import core.runtime.strategy_evaluator as se
        from core.execution.strategy_decision import StrategyDecision

        strategy = MagicMock()
        strategy.config = None
        strategy.evaluate.return_value = StrategyDecision(
            strategy_name="h1_swing",
            magic=93200,
            should_trade=True,
            direction="long",
            confidence=0.60,
            volume=0.02,
            sl=0.5,
            tp=1.5,
            hard_sl=1.0,
        )

        eq = _mock_execution_queue()
        eq.is_pending_open.return_value = False
        eq.is_unattributed_blocked.return_value = False

        gods_eye = SimpleNamespace(
            health_score=0.30,
            confidence_modifier=1.0,
            recommended_mode="defensive",
            chop_detected=False,
            anomaly_score=0.1,
            macro_bias="neutral",
        )

        _original_ood = se._get_ood_gateway
        se._get_ood_gateway = lambda: SimpleNamespace(
            check=lambda *a, **k: SimpleNamespace(status="ok", reason="ok")
        )
        try:
            result = evaluate_strategy_lines(
                strategy_lines={"h1_swing": strategy},
                feature_vector=np.ones(40, dtype=np.float32),
                micro_feature_vector=np.ones(9, dtype=np.float32),
                mid_price=2000.0,
                bid=1999.5,
                ask=2000.5,
                current_atr=5.0,
                regime_info=_minimal_regime_info(),
                regime_gate=None,
                portfolio_risk=self._approved_portfolio_risk(volume=0.02),
                execution_queue=eq,
                tracker=MagicMock(),
                pnl_ledger=MagicMock(),
                current_positions={},
                gods_eye_verdict=gods_eye,
                base_dir="",
            )
        finally:
            se._get_ood_gateway = _original_ood

        sr = result["strategy_results"][0]
        # 0.02 × ramp(0.30) = 0.02 × 0.3333 = 0.0067 < 0.02 → KILL.
        assert sr["should_trade"] is False
        assert "volume_degraded_below_economic_minimum" in sr["reason"]

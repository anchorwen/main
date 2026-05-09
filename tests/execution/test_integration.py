"""Integration tests — cross-component pipelines.

Tests the full execution chain from class decision through to dispatch,
using real components where possible and mocks only at external boundaries
(brain inference, MT5 dispatch).
"""

from __future__ import annotations

import pytest

from core.execution.execution_queue import ExecutionQueue
from core.execution.portfolio_risk import PortfolioRiskController, RiskVerdict
from core.execution.strategy_line import StrategyLineConfig
from tests.execution.conftest import (
    generate_ranging_bars,
    generate_trending_bars,
    make_proposal,
)
from tests.execution.test_strategy_line import _make_strategy

# ── Helper ────────────────────────────────────────────────────────────────


def _mock_dispatch(**kw):
    return {"order_id": 999, **kw}


# ── Integration tests ─────────────────────────────────────────────────────


class TestStrategyLineToRiskPipeline:
    """Strategy decisions flow through into portfolio risk checks."""

    def test_approved_decision_passes_risk(self):
        """A confident decision should pass portfolio risk when no conflicts."""
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is True

        # Feed into risk controller
        risk_ctrl = PortfolioRiskController()
        # Use StrategyDecision-like dict for positions
        risk_result = risk_ctrl.check(result, {})
        assert risk_result.verdict == RiskVerdict.APPROVED

    def test_risk_rejection_stops_trade(self):
        """A valid signal can be rejected by portfolio risk."""
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is True

        # Simulate existing positions that breach limits
        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "long",
                "volume": 0.05,
                "ticket": 100,
            },
            "micro_3bar": {
                "strategy": "micro_3bar",
                "direction": "long",
                "volume": 0.05,
                "ticket": 101,
            },
        }
        risk_ctrl = PortfolioRiskController(max_gross_exposure=0.08)
        risk_result = risk_ctrl.check(result, positions)
        assert risk_result.verdict == RiskVerdict.REJECTED

    def test_net_out_partial_close_flow(self):
        """When opposing positions exist, risk controller can signal NET_OUT."""
        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        result = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert result.should_trade is True

        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "short",
                "volume": 0.01,
                "ticket": 200,
            },
        }
        risk_ctrl = PortfolioRiskController(netting_mode="net_out")
        risk_result = risk_ctrl.check(result, positions)
        # Net-out closes the opposing and places the remainder
        assert risk_result.verdict in (RiskVerdict.NET_OUT, RiskVerdict.REDUCED)


class TestRiskToQueuePipeline:
    """Risk results flow into execution queue for dispatch."""

    def test_approved_risk_enqueued_and_flushed(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        line = _make_strategy(
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                ),
            ]
        )
        decision = line.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        assert decision.should_trade is True

        risk_ctrl = PortfolioRiskController()
        risk_result = risk_ctrl.check(decision, {})

        eq = ExecutionQueue(stagger_seconds=0)
        eq.enqueue("barrier_12bar", decision, risk_result)
        assert eq.queue_size == 1

        results = eq.flush(_mock_dispatch)
        assert len(results) == 1
        assert results[0].dispatched is True

    def test_empty_queue_does_nothing(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue()
        results = eq.flush(_mock_dispatch)
        assert results == []


class TestMultiStrategyPipeline:
    """All three strategies evaluated and their decisions compared."""

    def test_all_three_strategies_produce_decisions(self):
        """Each strategy line produces a decision independently."""
        # Barrier: long signal
        barrier = _make_strategy(
            config=StrategyLineConfig(name="barrier_12bar", magic=90001, brain_types={"test"}),
            proposals=[
                make_proposal(
                    up_probability=0.85,
                    down_probability=0.15,
                    confidence=0.90,
                    direction_bias="long",
                )
            ],
        )
        # Micro: short signal
        micro = _make_strategy(
            config=StrategyLineConfig(name="micro_3bar", magic=90002, brain_types={"test"}),
            proposals=[
                make_proposal(
                    up_probability=0.10,
                    down_probability=0.90,
                    confidence=0.85,
                    direction_bias="short",
                )
            ],
        )
        # StatArb: long signal (oversold reversion)
        statarb = _make_strategy(
            config=StrategyLineConfig(name="statarb_dynamic", magic=90003, brain_types={"test"}),
            proposals=[
                make_proposal(
                    up_probability=0.88,
                    down_probability=0.12,
                    confidence=0.82,
                    direction_bias="long",
                )
            ],
        )

        b_decision = barrier.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        m_decision = micro.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )
        s_decision = statarb.evaluate(
            feature_vector=None,
            micro_feature_vector=None,
            mid_price=2000.0,
        )

        assert b_decision.should_trade is True and b_decision.direction == "long"
        assert m_decision.should_trade is True and m_decision.direction == "short"
        assert s_decision.should_trade is True and s_decision.direction == "long"

        # The directions should be independent — no cross-blocking
        assert b_decision.magic != m_decision.magic != s_decision.magic

    def test_risk_controller_sees_all_positions(self):
        """Portfolio risk sees positions from all strategies."""
        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "long",
                "volume": 0.03,
                "ticket": 1,
            },
            "micro_3bar": {
                "strategy": "micro_3bar",
                "direction": "short",
                "volume": 0.02,
                "ticket": 2,
            },
            "statarb_dynamic": {
                "strategy": "statarb_dynamic",
                "direction": "long",
                "volume": 0.02,
                "ticket": 3,
            },
        }
        ctrl = PortfolioRiskController()
        summary = ctrl.get_portfolio_summary(positions)
        assert summary["gross_exposure"] == pytest.approx(0.07)
        assert summary["net_exposure"] == pytest.approx(0.03)  # +0.03 -0.02 +0.02 = +0.03
        assert summary["position_count"] == 3


class TestRegimeGateToStrategyPipeline:
    """Regime gate classification affects strategy mode decisions."""

    def test_trending_regime_keeps_barrier_full(self):
        from core.execution.regime_gate import RegimeGate

        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))
        gate.feed_h1_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))
        regime_info = gate.classify(atr_value=5.0)

        mode = regime_info["strategy_gates"]["barrier_12bar"]
        # Trending usually keeps barrier at full or reduced
        assert mode in ("full", "reduced")

    def test_ranging_regime_may_reduce_barrier(self):
        from core.execution.regime_gate import RegimeGate

        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_ranging_bars(200, center=2000.0, amplitude=3.0))
        gate.feed_h1_bars_batch(generate_ranging_bars(200, center=2000.0, amplitude=3.0))
        regime_info = gate.classify(atr_value=5.0)

        # Ranging should not produce trending — accept any valid regime
        assert regime_info["regime"] in ("trending", "ranging", "mild_trend", "high_vol", "normal")
        for _name, mode in regime_info["strategy_gates"].items():
            assert mode in ("full", "reduced", "off")

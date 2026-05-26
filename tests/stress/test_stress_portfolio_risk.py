"""Stress tests — extreme portfolio states for PortfolioRiskController."""

from __future__ import annotations

from core.execution.portfolio_risk import PortfolioRiskController, RiskVerdict
from core.execution.strategy_line import StrategyDecision


def _make_decision(
    strategy: str = "barrier_12bar",
    direction: str = "long",
    volume: float = 0.02,
    magic: int = 90001,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_name=strategy,
        magic=magic,
        should_trade=True,
        direction=direction,
        confidence=0.75,
        volume=volume,
        sl=1990.0,
        tp=2017.5,
        hard_sl=1985.0,
    )


class TestStressAllOpposing:
    def test_all_positions_opposing_new_trade(self):
        """All 3 existing positions short, new trade long — netting should handle."""
        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "short",
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
                "direction": "short",
                "volume": 0.02,
                "ticket": 3,
            },
        }
        ctrl = PortfolioRiskController(netting_mode="net_out")
        dec = _make_decision(direction="long", volume=0.01)
        result = ctrl.check(dec, positions)
        # Should not crash — netting handles multi-opposing
        assert result.verdict in (RiskVerdict.REDUCED, RiskVerdict.NET_OUT, RiskVerdict.REJECTED)


class TestStressMaxedOut:
    def test_gross_exposure_maxed_rejects_new(self):
        """When portfolio is maxed, any new trade is rejected."""
        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "long",
                "volume": 0.05,
                "ticket": 1,
            },
            "micro_3bar": {
                "strategy": "micro_3bar",
                "direction": "long",
                "volume": 0.05,
                "ticket": 2,
            },
        }
        ctrl = PortfolioRiskController(max_gross_exposure=0.10)
        dec = _make_decision(volume=0.01)
        result = ctrl.check(dec, positions)
        assert result.verdict == RiskVerdict.REJECTED

    def test_all_same_direction_full_limit(self):
        """All 3 slots long, adding a 4th long → rejected by concentration."""
        positions = {
            "barrier_12bar": {
                "strategy": "barrier_12bar",
                "direction": "long",
                "volume": 0.01,
                "ticket": 1,
            },
            "barrier_12bar_meta": {
                "strategy": "barrier_12bar_meta",
                "direction": "long",
                "volume": 0.01,
                "ticket": 2,
            },
            "barrier_H1": {
                "strategy": "barrier_H1",
                "direction": "long",
                "volume": 0.01,
                "ticket": 3,
            },
        }
        ctrl = PortfolioRiskController(max_same_direction=2)
        dec = _make_decision(strategy="barrier_unknown", direction="long", volume=0.01)
        result = ctrl.check(dec, positions)
        assert result.verdict == RiskVerdict.REJECTED


class TestStressEmptyEdgeCases:
    def test_empty_positions_always_approved(self):
        ctrl = PortfolioRiskController()
        dec = _make_decision(volume=0.05)
        result = ctrl.check(dec, {}, current_price=2000.0)
        assert result.verdict == RiskVerdict.APPROVED

    def test_zero_volume_decision(self):
        """Zero-volume trade shouldn't break checks."""
        ctrl = PortfolioRiskController()
        dec = _make_decision(volume=0.0)
        result = ctrl.check(dec, {})
        assert result.verdict in (RiskVerdict.APPROVED, RiskVerdict.REJECTED)

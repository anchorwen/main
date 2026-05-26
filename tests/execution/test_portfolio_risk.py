"""Tests for core/execution/portfolio_risk.py — cross-strategy risk limits."""

from __future__ import annotations

import pytest

from core.execution.portfolio_risk import (
    PortfolioRiskController,
    RiskVerdict,
)
from core.execution.strategy_line import StrategyDecision


def _make_decision(
    strategy: str = "barrier_12bar",
    magic: int = 90001,
    direction: str = "long",
    volume: float = 0.02,
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


def _make_position(
    strategy: str = "barrier_12bar",
    direction: str = "long",
    volume: float = 0.02,
    ticket: int = 12345,
) -> dict:
    return {"strategy": strategy, "direction": direction, "volume": volume, "ticket": ticket}


class TestPortfolioRiskController:
    def test_default_config(self):
        ctrl = PortfolioRiskController()
        assert ctrl.max_gross == 0.10
        assert ctrl.max_net == 0.05
        assert ctrl.max_same_dir == 2
        assert ctrl.netting_mode == "allow_coexist"

    # ── Gross exposure ──
    def test_gross_exposure_exceeded_rejects(self):
        ctrl = PortfolioRiskController(max_gross_exposure=0.10)
        positions = {
            "barrier_12bar": _make_position(volume=0.06),
            "micro_3bar": _make_position(strategy="micro_3bar", volume=0.03),
        }
        # gross notional = 0.11 * 100 * 2000 = 22,000 > 20,000 = max_gross
        dec = _make_decision(strategy="statarb_dynamic", volume=0.02)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "gross_exposure" in result.reason

    def test_gross_exposure_within_limit_approves(self):
        ctrl = PortfolioRiskController(max_gross_exposure=0.10, max_net_exposure=0.10)
        positions = {
            "micro_3bar": _make_position(strategy="micro_3bar", volume=0.02),
        }
        dec = _make_decision(strategy="barrier_12bar", volume=0.02)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict != RiskVerdict.REJECTED

    # ── Net exposure ──
    def test_net_exposure_long_exceeded(self):
        ctrl = PortfolioRiskController(max_net_exposure=0.05)
        positions = {
            "barrier_12bar": _make_position(volume=0.04, direction="long"),
        }
        # net notional = 0.06 * 100 * 2000 = 12,000 > 10,000 = max_net
        dec = _make_decision(strategy="statarb_dynamic", direction="long", volume=0.02)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "net_exposure" in result.reason

    def test_net_exposure_short_reduces_net(self):
        ctrl = PortfolioRiskController(max_net_exposure=0.10)
        positions = {
            "barrier_12bar": _make_position(volume=0.04, direction="long"),
        }
        # net = 0.04 long, short 0.01 → 0.03, within 0.10 limit (statarb is not in positions)
        dec = _make_decision(strategy="statarb_dynamic", direction="short", volume=0.01)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict in (RiskVerdict.APPROVED, RiskVerdict.NET_OUT, RiskVerdict.REDUCED)

    def test_net_exposure_short_causes_negative_net(self):
        ctrl = PortfolioRiskController(max_net_exposure=0.05)
        positions = {
            "barrier_12bar": _make_position(volume=0.02, direction="long"),
        }
        # net = +0.02, new short 0.04 → -0.02, |−0.02| ≤ 0.05 (statarb is not in positions)
        dec = _make_decision(strategy="statarb_dynamic", direction="short", volume=0.04)
        result = ctrl.check(dec, positions, current_price=2000.0)
        # May be rejected by netting or approved, but NOT rejected by net exposure
        if result.verdict == RiskVerdict.REJECTED:
            assert "net_exposure" not in result.reason

    # ── Same-direction concentration ──
    def test_same_direction_limit_reached(self):
        ctrl = PortfolioRiskController(max_same_direction=2)
        # Same-family strategies so per-family concentration check triggers
        positions = {
            "barrier_12bar": _make_position(
                strategy="barrier_12bar", direction="long", volume=0.02
            ),
            "barrier_12bar_meta": _make_position(
                strategy="barrier_12bar_meta", direction="long", volume=0.02
            ),
        }
        dec = _make_decision(strategy="barrier_H1", direction="long", volume=0.01)
        result = ctrl.check(dec, positions)
        assert result.verdict == RiskVerdict.REJECTED
        assert "direction_concentration" in result.reason

    def test_different_direction_not_counted(self):
        ctrl = PortfolioRiskController(max_same_direction=2)
        positions = {
            "barrier_12bar": _make_position(direction="long"),
            "micro_3bar": _make_position(strategy="micro_3bar", direction="long"),
        }
        # Same strategy (barrier) already in short wouldn't count, but barrier has existing long
        dec = _make_decision(strategy="statarb_dynamic", direction="short", volume=0.01)
        result = ctrl.check(dec, positions)
        # Different direction: not rejected by concentration
        if result.verdict == RiskVerdict.REJECTED:
            assert "direction_concentration" not in result.reason

    def test_same_strategy_duplicate_rejected(self):
        ctrl = PortfolioRiskController(max_same_direction=2)
        positions = {
            "barrier_12bar": _make_position(strategy="barrier_12bar", direction="short"),
        }
        # Same strategy already has a position — duplicate check blocks new entry
        dec = _make_decision(strategy="barrier_12bar", direction="short", volume=0.01)
        result = ctrl.check(dec, positions)
        assert result.verdict == RiskVerdict.REJECTED
        assert "duplicate_strategy" in result.reason

    # ── Netting mode ──
    def test_net_out_reduces_opposing_larger(self):
        ctrl = PortfolioRiskController(netting_mode="net_out")
        positions = {
            "barrier_12bar": _make_position(
                strategy="barrier_12bar", direction="short", volume=0.03, ticket=999
            ),
        }
        dec = _make_decision(strategy="micro_3bar", direction="long", volume=0.01)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict == RiskVerdict.REDUCED
        assert result.net_out_ticket == 999
        assert result.adjusted_volume == pytest.approx(0.02)

    def test_net_out_closes_opposing_smaller(self):
        ctrl = PortfolioRiskController(netting_mode="net_out")
        positions = {
            "barrier_12bar": _make_position(
                strategy="barrier_12bar", direction="short", volume=0.01, ticket=777
            ),
        }
        dec = _make_decision(strategy="micro_3bar", direction="long", volume=0.03)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict == RiskVerdict.NET_OUT
        assert result.net_out_ticket == 777
        assert result.adjusted_volume == pytest.approx(0.02)

    def test_no_opposite_positions_approved(self):
        ctrl = PortfolioRiskController()
        positions = {
            "micro_3bar": _make_position(strategy="micro_3bar", direction="long"),
        }
        dec = _make_decision(strategy="statarb_dynamic", direction="long", volume=0.01)
        result = ctrl.check(dec, positions, current_price=2000.0)
        assert result.verdict == RiskVerdict.APPROVED

    # ── Portfolio summary ──
    def test_portfolio_summary_correct(self):
        ctrl = PortfolioRiskController()
        positions = {
            "a": _make_position(strategy="a", direction="long", volume=0.03),
            "b": _make_position(strategy="b", direction="short", volume=0.01),
        }
        summary = ctrl.get_portfolio_summary(positions)
        assert summary["gross_exposure"] == pytest.approx(0.04)
        assert summary["net_exposure"] == pytest.approx(0.02)
        assert summary["position_count"] == 2
        assert "a" in summary["strategies_active"]
        assert "b" in summary["strategies_active"]

    def test_empty_portfolio_summary(self):
        ctrl = PortfolioRiskController()
        summary = ctrl.get_portfolio_summary({})
        assert summary["gross_exposure"] == 0.0
        assert summary["net_exposure"] == 0.0
        assert summary["position_count"] == 0
        assert summary["strategies_active"] == []

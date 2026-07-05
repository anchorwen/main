"""TDD unit tests for PortfolioNettingGate — 6 scenarios + edge cases.

Covers the institutional netting contract:
  S1: Unanimous LONG → all pass through
  S2: Unanimous SHORT → all pass through
  S3: Opposing LONG+SHORT, net≈0 → swallow ALL (mode=swallow)
  S4: 2 LONG + 1 SHORT, net>threshold → swallow SHORT only (mode=reduce)
  S5: Single strategy → identity pass-through
  S6: All neutral / should_trade=False → pass-through
  E1: Empty queue → no-op
  E2: Disabled config → pass-through
  E3: warn mode → no decisions modified
  E4: vote_weight=0 → excluded from power computation
  E5: Zero confidence → zero power
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.execution.portfolio_netting import (
    PortfolioNettingConfig,
    PortfolioNettingGate,
)

# ── Test decision stub ────────────────────────────────────────────────────


@dataclass
class StubDecision:
    """Minimal stub that mimics StrategyDecision fields used by netting."""

    direction: str = "neutral"
    confidence: float = 0.5
    volume: float = 0.01
    should_trade: bool = True
    vote_weight: float = 1.0
    reason: str = "test"


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_long(
    conf: float = 0.6, vol: float = 0.02, vw: float = 1.0, trade: bool = True
) -> StubDecision:
    return StubDecision(
        direction="long", confidence=conf, volume=vol, vote_weight=vw, should_trade=trade
    )


def _make_short(
    conf: float = 0.6, vol: float = 0.02, vw: float = 1.0, trade: bool = True
) -> StubDecision:
    return StubDecision(
        direction="short", confidence=conf, volume=vol, vote_weight=vw, should_trade=trade
    )


def _make_neutral(conf: float = 0.5, trade: bool = False) -> StubDecision:
    return StubDecision(direction="neutral", confidence=conf, should_trade=trade)


# ── S1: Unanimous LONG ────────────────────────────────────────────────────


def test_unanimous_long_passes_through():
    """All strategies agree LONG → no netting, all survive."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    decisions = [
        ("strat_a", _make_long(conf=0.65, vol=0.02)),
        ("strat_b", _make_long(conf=0.55, vol=0.01)),
    ]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "dispatch"
    assert result.direction == "long"
    assert result.net_ratio == 1.0  # unanimous
    assert result.swallowed == []
    assert set(result.survivors) == {"strat_a", "strat_b"}
    assert all(d.should_trade for _, d in decisions)


# ── S2: Unanimous SHORT ───────────────────────────────────────────────────


def test_unanimous_short_passes_through():
    """All strategies agree SHORT → no netting, all survive."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    decisions = [
        ("strat_c", _make_short(conf=0.70, vol=0.03)),
    ]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "dispatch"
    assert result.direction == "short"
    assert result.net_ratio == 1.0
    assert result.swallowed == []


# ── S3: Opposing LONG+SHORT, net≈0 → swallow ALL ──────────────────────────


def test_opposing_balanced_swallows_all():
    """btc_swing SHORT + btc_swing_h1 LONG with similar conviction → both swallowed."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    long_dec = _make_long(conf=0.65, vol=0.02)  # power = 1.0 × 0.65 × 0.02 = 0.013
    short_dec = _make_short(conf=0.60, vol=0.02)  # power = 1.0 × 0.60 × 0.02 = 0.012
    decisions = [
        ("btc_swing_h1", long_dec),
        ("btc_swing", short_dec),
    ]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    # net_power = 0.013 - 0.012 = 0.001, gross_power = 0.025
    # net_ratio = 0.001 / 0.025 = 0.04 < 0.20 → swallow
    assert result.action == "swallow"
    assert result.direction == "neutral"
    assert result.net_ratio < 0.20
    assert set(result.swallowed) == {"btc_swing_h1", "btc_swing"}
    assert result.survivors == []

    # Both decisions must be disabled
    assert not long_dec.should_trade
    assert not short_dec.should_trade
    assert "portfolio_netted_swallow" in long_dec.reason
    assert "portfolio_netted_swallow" in short_dec.reason


# ── S4: 2 LONG + 1 SHORT, net > threshold → swallow SHORT only ────────────


def test_dominant_long_swallows_minority_short():
    """Three strategies, 2 LONG + 1 SHORT → LONG dominant → SHORT swallowed."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    long_a = _make_long(conf=0.70, vol=0.02)  # power = 0.014
    long_b = _make_long(conf=0.65, vol=0.02)  # power = 0.013
    short_c = _make_short(conf=0.50, vol=0.01)  # power = 0.005
    decisions = [
        ("strat_a", long_a),
        ("strat_b", long_b),
        ("strat_c", short_c),
    ]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    # long_power = 0.027, short_power = 0.005, net = 0.022
    # gross = 0.032, net_ratio = 0.688 ≥ 0.20 → reduce (swallow SHORT only)
    assert result.action == "reduce"
    assert result.direction == "long"
    assert result.net_ratio >= 0.20
    assert set(result.swallowed) == {"strat_c"}
    assert set(result.survivors) == {"strat_a", "strat_b"}

    # LONG decisions still active
    assert long_a.should_trade
    assert long_b.should_trade
    # SHORT decision swallowed
    assert not short_c.should_trade
    assert "portfolio_netted_reduce" in short_c.reason


# ── S5: Single strategy → identity pass-through ───────────────────────────


def test_single_strategy_passes_through():
    """Single strategy with no opposing votes → pass-through."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=True))
    decisions = [("lone_strat", _make_long(conf=0.55, vol=0.01))]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "dispatch"
    assert result.direction == "long"
    assert result.net_ratio == 1.0
    assert result.swallowed == []
    assert decisions[0][1].should_trade


# ── S6: All neutral + should_trade=False → pass-through ───────────────────


def test_all_neutral_passes_through():
    """No active directional decisions → no netting needed."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=True))
    decisions = [
        ("strat_a", _make_neutral(trade=False)),
        ("strat_b", _make_neutral(trade=False)),
    ]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "dispatch"
    assert result.reason == "no_directional_signals"
    assert result.swallowed == []
    assert set(result.survivors) == {"strat_a", "strat_b"}


# ── E1: Empty queue ───────────────────────────────────────────────────────


def test_empty_queue_returns_identity():
    """Empty list → pass-through with neutral result."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=True))
    decisions: list[tuple[str, StubDecision]] = []
    filtered, result = gate.net(decisions, symbol="BTCUSDc")

    assert filtered == []
    assert result.action == "dispatch"
    assert result.reason == "netting_disabled_or_empty"


# ── E2: Disabled config ───────────────────────────────────────────────────


def test_disabled_gate_passes_through():
    """Gate disabled → all decisions pass through unchanged."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=False))
    long_dec = _make_long(conf=0.60)
    short_dec = _make_short(conf=0.60)
    decisions = [("a", long_dec), ("b", short_dec)]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "dispatch"
    assert result.reason == "netting_disabled_or_empty"
    assert long_dec.should_trade  # untouched
    assert short_dec.should_trade  # untouched


# ── E3: warn mode ─────────────────────────────────────────────────────────


def test_warn_mode_does_not_block():
    """warn mode → telemetry only, no decisions modified."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="warn")
    )
    long_dec = _make_long(conf=0.60, vol=0.02)
    short_dec = _make_short(conf=0.55, vol=0.02)
    decisions = [("a", long_dec), ("b", short_dec)]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    assert result.action == "warn"
    assert result.swallowed == []
    assert long_dec.should_trade
    assert short_dec.should_trade


# ── E4: vote_weight=0 → excluded from power ───────────────────────────────


def test_zero_vote_weight_excluded():
    """vote_weight=0 → power=0, effectively neutral."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=True, netting_threshold=0.20))
    muted_long = _make_long(conf=0.80, vol=0.10, vw=0.0)  # power = 0
    active_short = _make_short(conf=0.50, vol=0.01, vw=1.0)  # power = 0.005
    decisions = [("muted", muted_long), ("active", active_short)]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    # Only active_short has power → unanimous SHORT
    assert result.action == "dispatch"
    assert result.direction == "short"
    assert result.net_ratio == 1.0
    assert muted_long.should_trade
    assert active_short.should_trade


# ── E5: should_trade=False decisions ignored ──────────────────────────────


def test_non_trading_decisions_ignored():
    """Decisions with should_trade=False are treated as neutral."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    blocked_long = _make_long(conf=0.60, trade=False)
    active_short = _make_short(conf=0.55, vol=0.02)
    decisions = [("blocked", blocked_long), ("active", active_short)]
    _, result = gate.net(decisions, symbol="BTCUSDc")

    # Only active_short counts → unanimous SHORT
    assert result.action == "dispatch"
    assert result.direction == "short"
    assert not blocked_long.should_trade  # was already False
    assert active_short.should_trade  # not swallowed by netting


# ── Property: net_ratio bounds ────────────────────────────────────────────


def test_net_ratio_in_zero_one_range():
    """net_ratio is always in [0, 1]."""
    gate = PortfolioNettingGate(PortfolioNettingConfig(enabled=True))
    # Perfectly balanced
    d1 = _make_long(conf=0.50, vol=0.02)
    d2 = _make_short(conf=0.50, vol=0.02)
    _, r1 = gate.net([("a", d1), ("b", d2)])
    assert 0.0 <= r1.net_ratio <= 1.0

    # Extreme imbalance
    d3 = _make_long(conf=0.90, vol=0.10)
    d4 = _make_short(conf=0.30, vol=0.01)
    _, r2 = gate.net([("c", d3), ("d", d4)])
    assert 0.0 <= r2.net_ratio <= 1.0


# ── Property: counters ────────────────────────────────────────────────────


def test_swallow_counter_increments():
    """swallow_count tracks actual swallow events."""
    gate = PortfolioNettingGate(
        PortfolioNettingConfig(enabled=True, netting_threshold=0.20, mode="swallow")
    )
    # First cycle: balanced → swallow
    gate.net([("a", _make_long(conf=0.55, vol=0.02)), ("b", _make_short(conf=0.55, vol=0.02))])
    assert gate.netting_count == 1
    assert gate.swallow_count == 1
    assert gate.reduce_count == 0

    # Second cycle: unbalanced → reduce
    gate.net([("a", _make_long(conf=0.80, vol=0.10)), ("b", _make_short(conf=0.30, vol=0.01))])
    assert gate.netting_count == 2
    assert gate.swallow_count == 1
    assert gate.reduce_count == 1


# ── Config validation ─────────────────────────────────────────────────────


def test_invalid_mode_raises():
    """Invalid mode string raises ValueError."""
    with pytest.raises(ValueError, match="Invalid netting mode"):
        PortfolioNettingConfig(mode="block")

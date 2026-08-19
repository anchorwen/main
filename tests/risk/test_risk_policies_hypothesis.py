"""Property-based tests for risk policies (Tier 1 — Capital Path).

Phase 3: Each policy class receives hypothesis bombardment.
All policies are pure logic — no MT5, no I/O, no network.
Target: ≥85% line / ≥75% branch coverage on core/risk/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.contracts.enums import RiskDecisionStatus


# ---------------------------------------------------------------------------
# Mock intent — duck-type compatible with DecisionIntent
# ---------------------------------------------------------------------------
@dataclass
class MockIntent:
    symbol: str = "XAUUSDc"
    side: str = "buy"
    volume: float = 0.01
    _is_open: bool = True
    intent_id: str = "mock-intent-001"
    action: Any = None

    def is_open_intent(self) -> bool:
        return self._is_open

    def is_passive(self) -> bool:
        return False

    @classmethod
    def close_intent(cls, **kwargs: Any) -> MockIntent:
        return cls(_is_open=False, **kwargs)


# ---------------------------------------------------------------------------
# Mock control_snapshot for ModePolicy
# ---------------------------------------------------------------------------
@dataclass
class MockMode:
    value: str


@dataclass
class MockModeState:
    current_mode: MockMode


@dataclass
class MockControlSnapshot:
    mode_state: MockModeState

    @classmethod
    def with_mode(cls, mode: str) -> MockControlSnapshot:
        return cls(mode_state=MockModeState(current_mode=MockMode(mode)))


# ============================================================================
# PositionLimitPolicy
# ============================================================================
@given(
    current_count=st.integers(0, 30),
    max_positions=st.integers(1, 20),
    is_open=st.booleans(),
)
@settings(max_examples=200)
def test_position_limit_policy_invariants(
    current_count: int, max_positions: int, is_open: bool
) -> None:
    """PositionLimitPolicy must DENY when count >= max for open intents."""
    from core.risk.risk_policies import PositionLimitPolicy

    policy = PositionLimitPolicy(max_open_positions=max_positions)
    intent = MockIntent(_is_open=is_open)
    snapshot = MagicMock()
    ctx = {"open_position_count": current_count}

    result = policy.evaluate(intent, snapshot, ctx)

    if is_open and current_count >= max_positions:
        assert result["status"] == RiskDecisionStatus.DENY
        assert "open_position_limit_exceeded" in result["reason"]
    else:
        assert result["status"] == RiskDecisionStatus.ALLOW
        assert result["tier"] == "position"


# ============================================================================
# DrawdownPolicy
# ============================================================================
@given(
    current_dd=st.floats(0.0, 20.0, allow_nan=False, allow_infinity=False),
    max_dd=st.floats(1.0, 15.0, allow_nan=False, allow_infinity=False),
    is_open=st.booleans(),
)
@settings(max_examples=300)
def test_drawdown_policy_invariants(current_dd: float, max_dd: float, is_open: bool) -> None:
    """DrawdownPolicy: 3-tier system — DENY, ALLOW_LIMITED, ALLOW."""
    from core.risk.risk_policies import DrawdownPolicy

    policy = DrawdownPolicy(max_drawdown_pct=max_dd)
    intent = MockIntent(_is_open=is_open)
    snapshot = MagicMock()
    ctx = {"current_drawdown_pct": current_dd}

    result = policy.evaluate(intent, snapshot, ctx)

    if current_dd >= max_dd:
        # Tier 1: hard breach
        if is_open:
            assert result["status"] == RiskDecisionStatus.DENY
        else:
            assert result["status"] == RiskDecisionStatus.FORCE_REDUCE
            assert result.get("constraint", {}).get("force_reduce_only") is True
    elif current_dd >= max_dd * 0.8:
        # Tier 2: warning zone
        assert (
            result["status"] == RiskDecisionStatus.ALLOW_LIMITED
        ), f"dd={current_dd:.2f}%, max={max_dd}%, expected ALLOW_LIMITED, got {result['status']}"
        assert result["constraint"]["max_risk_fraction"] == 0.5
    else:
        # Tier 3: safe
        assert result["status"] == RiskDecisionStatus.ALLOW


@given(
    current_dd=st.floats(0.0, 50.0, allow_nan=False),
    max_dd=st.floats(0.01, 50.0, allow_nan=False),
)
@settings(max_examples=100)
def test_drawdown_policy_never_exceeds_max_dd_deny(current_dd: float, max_dd: float) -> None:
    """When current_dd < max_dd * 0.8, must be ALLOW (clean path)."""
    import math

    from core.risk.risk_policies import DrawdownPolicy

    if not math.isfinite(current_dd) or not math.isfinite(max_dd):
        return  # skip NaN/Inf

    if current_dd < max_dd * 0.8:
        policy = DrawdownPolicy(max_drawdown_pct=max_dd)
        intent = MockIntent(_is_open=True)
        result = policy.evaluate(intent, MagicMock(), {"current_drawdown_pct": current_dd})
        assert (
            result["status"] == RiskDecisionStatus.ALLOW
        ), f"dd={current_dd} < 0.8×max={max_dd} should be ALLOW, got {result['status']}"


# ============================================================================
# ExposurePolicy
# ============================================================================
@given(
    current_exp=st.floats(0.0, 2_000_000.0, allow_nan=False, allow_infinity=False),
    proposed_exp=st.floats(0.0, 500_000.0, allow_nan=False, allow_infinity=False),
    max_notional=st.floats(100_000.0, 2_000_000.0, allow_nan=False, allow_infinity=False),
    is_open=st.booleans(),
)
@settings(max_examples=200)
def test_exposure_policy_invariants(
    current_exp: float, proposed_exp: float, max_notional: float, is_open: bool
) -> None:
    """ExposurePolicy: DENY when (current + proposed) >= max, only for opens."""
    from core.risk.risk_policies import ExposurePolicy

    policy = ExposurePolicy(max_notional=max_notional)
    intent = MockIntent(_is_open=is_open)
    ctx = {
        "current_notional_exposure": current_exp,
        "proposed_notional_exposure": proposed_exp,
    }

    result = policy.evaluate(intent, MagicMock(), ctx)

    if is_open and (current_exp + proposed_exp) >= max_notional:
        assert result["status"] == RiskDecisionStatus.DENY
        assert "notional_exposure_exceeded" in result["reason"]
    else:
        assert result["status"] == RiskDecisionStatus.ALLOW


# ============================================================================
# ConcentrationPolicy
# ============================================================================
@given(
    symbol_count=st.integers(0, 10),
    max_per_symbol=st.integers(1, 8),
    is_open=st.booleans(),
    symbol_name=st.sampled_from(["XAUUSDc", "BTCUSDc", "EURUSDc"]),
)
@settings(max_examples=200)
def test_concentration_policy_invariants(
    symbol_count: int, max_per_symbol: int, is_open: bool, symbol_name: str
) -> None:
    """ConcentrationPolicy: DENY when positions_per_symbol[symbol] >= max."""
    from core.risk.risk_policies import ConcentrationPolicy

    policy = ConcentrationPolicy(max_per_symbol=max_per_symbol)
    intent = MockIntent(symbol=symbol_name, _is_open=is_open)
    ctx = {"positions_per_symbol": {symbol_name: symbol_count}}

    result = policy.evaluate(intent, MagicMock(), ctx)

    if is_open and symbol_count >= max_per_symbol:
        assert result["status"] == RiskDecisionStatus.DENY
        assert symbol_name in result["reason"]
    else:
        assert result["status"] == RiskDecisionStatus.ALLOW


# ============================================================================
# ModePolicy
# ============================================================================
@given(mode=st.sampled_from(["active", "halted", "liquidation_only", "observe_only", "cautious"]))
@settings(max_examples=50)
def test_mode_policy_known_modes(mode: str) -> None:
    """ModePolicy must handle all 5 known modes without crashing."""
    from core.risk.risk_policies import ModePolicy

    policy = ModePolicy()
    intent = MockIntent(_is_open=True)
    snapshot = MockControlSnapshot.with_mode(mode)

    result = policy.evaluate(intent, snapshot, {})

    assert result["status"] is not None
    assert result["tier"] == "mode"

    # Mode-specific assertions
    if mode == "halted":
        assert result["status"] == RiskDecisionStatus.DENY
    elif mode == "liquidation_only":
        assert result["status"] == RiskDecisionStatus.DENY  # open intent
    elif mode == "observe_only":
        assert result["status"] == RiskDecisionStatus.DEFER
    elif mode == "cautious":
        assert result["status"] == RiskDecisionStatus.ALLOW_LIMITED
        assert result["constraint"]["max_risk_fraction"] == 0.5
    elif mode == "active":
        assert result["status"] == RiskDecisionStatus.ALLOW


def test_mode_policy_liquidation_allows_close_intent() -> None:
    """liquidation_only mode: DENY for opens, LIQUIDATE_ONLY for closes."""
    from core.risk.risk_policies import ModePolicy

    policy = ModePolicy()
    close_intent = MockIntent.close_intent()
    snapshot = MockControlSnapshot.with_mode("liquidation_only")

    result = policy.evaluate(close_intent, snapshot, {})

    assert result["status"] == RiskDecisionStatus.LIQUIDATE_ONLY
    assert "close_allowed" in result["reason"]


# ============================================================================
# RiskEvaluationService — policy chaining
# ============================================================================
def test_risk_service_picks_most_restrictive() -> None:
    """The most restrictive policy result must dominate."""
    from core.risk.risk_evaluation_service import RiskEvaluationService
    from core.risk.risk_policies import DrawdownPolicy, PositionLimitPolicy

    svc = RiskEvaluationService(
        [
            PositionLimitPolicy(max_open_positions=1),
            DrawdownPolicy(max_drawdown_pct=10.0),
        ]
    )

    intent = MockIntent(_is_open=True, intent_id="test-1")
    snapshot = MockControlSnapshot.with_mode("active")
    ctx = {
        "open_position_count": 10,  # triggers DENY from PositionLimit
        "current_drawdown_pct": 5.0,  # triggers ALLOW_LIMITED from Drawdown
    }

    verdict = svc.evaluate(intent, snapshot, context=ctx)

    # PositionLimit says DENY (most restrictive), Drawdown says ALLOW_LIMITED
    # Verdict should be DENY
    assert verdict.status == RiskDecisionStatus.DENY


def test_risk_service_default_policies() -> None:
    """Default construction includes ModePolicy + PositionLimitPolicy."""
    from core.risk.risk_evaluation_service import RiskEvaluationService

    svc = RiskEvaluationService()  # no explicit policies
    assert len(svc._policies) >= 2


# ============================================================================
# Strategy contracts — Signal validation
# ============================================================================
def test_signal_rejects_invalid_side() -> None:
    """Signal with side='foobar' must raise ValueError."""
    from datetime import UTC, datetime

    from core.strategies.contracts import Signal

    with pytest.raises(ValueError, match="side"):
        Signal(
            schema_version="signal.v1",
            signal_id="s1",
            strategy_id="st1",
            symbol="XAUUSDc",
            side="foobar",
            strength=0.5,
            confidence=0.5,
            generated_at=datetime.now(UTC),
        )


def test_signal_rejects_out_of_bounds_strength() -> None:
    """Signal with strength=1.5 must raise ValueError."""
    from datetime import UTC, datetime

    from core.strategies.contracts import Signal

    with pytest.raises(ValueError, match="strength"):
        Signal(
            schema_version="signal.v1",
            signal_id="s1",
            strategy_id="st1",
            symbol="XAUUSDc",
            side="buy",
            strength=1.5,
            confidence=0.5,
            generated_at=datetime.now(UTC),
        )


def test_signal_rejects_negative_confidence() -> None:
    """Signal with confidence=-0.1 must raise ValueError."""
    from datetime import UTC, datetime

    from core.strategies.contracts import Signal

    with pytest.raises(ValueError, match="confidence"):
        Signal(
            schema_version="signal.v1",
            signal_id="s1",
            strategy_id="st1",
            symbol="XAUUSDc",
            side="buy",
            strength=0.5,
            confidence=-0.1,
            generated_at=datetime.now(UTC),
        )


def test_signal_accepts_valid_input() -> None:
    """Valid Signal must be constructed without error."""
    from datetime import UTC, datetime

    from core.strategies.contracts import Signal

    s = Signal(
        schema_version="signal.v1",
        signal_id="s1",
        strategy_id="st1",
        symbol="XAUUSDc",
        side="buy",
        strength=0.8,
        confidence=0.75,
        generated_at=datetime.now(UTC),
        reason="test",
    )
    assert s.side == "buy"
    assert s.strength == 0.8
    assert s.confidence == 0.75


@given(
    strength=st.floats(0.0, 1.0),
    confidence=st.floats(0.0, 1.0),
    side=st.sampled_from(["buy", "sell", "hold", "flat"]),
)
@settings(max_examples=200)
def test_signal_valid_range_never_rejects(strength: float, confidence: float, side: str) -> None:
    """Any combination of valid inputs must construct successfully."""
    from datetime import UTC, datetime

    from core.strategies.contracts import Signal

    # Must not raise
    Signal(
        schema_version="signal.v1",
        signal_id="s1",
        strategy_id="st1",
        symbol="XAUUSDc",
        side=side,
        strength=strength,
        confidence=confidence,
        generated_at=datetime.now(UTC),
    )

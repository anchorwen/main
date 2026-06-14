"""Unit tests for strategy_builder — partition brains into strategy instances.

Tests the already-extracted build_strategy_lines() function.
Phase 1.B: Lock behavior before further live_cycle extraction.
Tier 1 target: >=85% line / >=75% branch coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Mock config helper
# ---------------------------------------------------------------------------
def _make_config(**overrides: object) -> SimpleNamespace:
    """Create a minimal LiveCycleConfig-compatible namespace for testing."""
    defaults: dict[str, object] = {
        "symbol": "XAUUSDc",
        "strategy_configs": {},
        "sl_atr_mult": 2.0,
        "tp_atr_mult": 3.5,
        "volume": 0.01,
        "confidence_threshold": 0.40,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Brain factory
# ---------------------------------------------------------------------------
def _brain(
    brain_id: str = "test_brain",
    contract_group: str = "barrier_12bar",
    status: str = "live",
    brain_type: str = "xgboost_v9",
    vote_weight: float = 1.0,
    training_contract: str = "survival_barrier",
) -> dict:
    """Create a minimal brain info dict for strategy_builder."""
    return {
        "brain_id": brain_id,
        "contract_group": contract_group,
        "status": status,
        "brain_type": brain_type,
        "vote_weight": vote_weight,
        "training_contract": training_contract,
    }


# ============================================================================
# Test: Empty inputs
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_empty_brains_returns_empty_strategies(mock_get_asset: MagicMock) -> None:
    """Zero brains should produce zero strategies — no crash."""
    from core.runtime.strategy_builder import build_strategy_lines

    mock_get_asset.return_value = SimpleNamespace(contract_size=100)
    config = _make_config()
    result = build_strategy_lines([], config)
    assert result == {}


# ============================================================================
# Test: Frozen brains excluded
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_frozen_brain_excluded_from_voting(mock_get_asset: MagicMock) -> None:
    """Frozen or retired brains must be excluded before strategy building."""
    from core.runtime.strategy_builder import build_strategy_lines

    mock_get_asset.return_value = SimpleNamespace(contract_size=100)
    config = _make_config(
        strategy_configs={
            "barrier_12bar": {"enabled": True, "base_volume": 0.01},
        }
    )
    brains = [
        _brain("b1", status="frozen"),
        _brain("b2", status="retired"),
        _brain("b3", status="candidate"),
    ]
    # All barrier brains are frozen/retired/candidate — candidate keeps them alive
    # but frozen+retired are removed before group assignment.
    # With just candidate brains remaining, barrier should still build if > 0 candidates.
    result = build_strategy_lines(brains, config)

    # After frozen/retired filtering, only b3 (candidate) remains
    # barrier_12bar should be present since 1 brain is enough
    assert len(result) >= 1
    # Verify frozen brains were filtered (they aren't in the result's brain list)
    barrier = result.get("barrier_12bar")
    if barrier is not None:
        brain_ids_in_strategy = [b["brain_id"] for b in barrier.brains]
        assert "b1" not in brain_ids_in_strategy, "frozen brain must be excluded"
        assert "b2" not in brain_ids_in_strategy, "retired brain must be excluded"


# ============================================================================
# Test: Contract mismatch mutes vote weight
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_warn_contract_mismatch_sets_vote_weight_zero(mock_get_asset: MagicMock) -> None:
    """Brain with wrong training contract gets vote_weight=0."""
    from core.runtime.strategy_builder import _warn_contract_mismatch

    brain_info = _brain("mismatched_brain", training_contract="wrong_contract")
    _warn_contract_mismatch(
        brain_info,
        strategy_name="barrier_12bar",
        required_contracts={"barrier_12bar": "survival_barrier"},
    )
    assert brain_info["vote_weight"] == 0.0
    assert brain_info["_contract_muted"] is True


# ============================================================================
# Test: Contract OK does not mute
# ============================================================================
def test_warn_contract_mismatch_passes_matching_contract() -> None:
    """Brain with correct contract keeps its vote_weight."""
    from core.runtime.strategy_builder import _warn_contract_mismatch

    brain_info = _brain("good_brain", training_contract="survival_barrier_v9")
    _warn_contract_mismatch(
        brain_info,
        strategy_name="barrier_12bar",
        required_contracts={"barrier_12bar": "survival_barrier"},
    )
    # "survival_barrier" is in "survival_barrier_v9" → should pass
    assert brain_info.get("_contract_muted") is None


# ============================================================================
# Test: Disabled strategy clears brain group
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_disabled_strategy_excluded(mock_get_asset: MagicMock) -> None:
    """Strategy with enabled:false must not appear in result."""
    from core.runtime.strategy_builder import build_strategy_lines

    mock_get_asset.return_value = SimpleNamespace(contract_size=100)
    config = _make_config(
        strategy_configs={
            "barrier_12bar": {"enabled": False, "base_volume": 0.01},
        }
    )
    brains = [_brain("b1")]
    result = build_strategy_lines(brains, config)
    # Disabled strategy — barrier_12bar should not appear
    assert "barrier_12bar" not in result


# ============================================================================
# Test: Unknown contract group logged but not crashed
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_unknown_contract_group_does_not_crash(mock_get_asset: MagicMock) -> None:
    """Brain with unknown contract_group must not crash the builder."""
    from core.runtime.strategy_builder import build_strategy_lines

    mock_get_asset.return_value = SimpleNamespace(contract_size=100)
    config = _make_config()
    brains = [_brain("b1", contract_group="nonexistent_group")]
    result = build_strategy_lines(brains, config)
    # Should not crash. Unknown groups produce no strategies.
    assert isinstance(result, dict)


# ============================================================================
# Test: Rule-engine strategies (StructuralSwingV1)
# ============================================================================
@patch("core.runtime.strategy_builder.get_asset")
def test_rule_engine_strategy_created(mock_get_asset: MagicMock) -> None:
    """Rule-engine config without ML brains creates RuleEngineStrategyWrapper."""
    from core.runtime.strategy_builder import build_strategy_lines

    mock_get_asset.return_value = SimpleNamespace(contract_size=100)
    config = _make_config(
        strategy_configs={
            "structural_swing_v1": {
                "rule_engine": "structural_swing_v1",
                "enabled": True,
                "magic": 90501,
                "base_volume": 0.01,
                "sl": {"base_atr_mult": 3.0},
                "tp": {"base_atr_mult": 1.5},
                "exit": {"time_exit_cycles": 12},
                "spread_points": 30,
                "slippage_points": 10,
                "cooldown_bars": 3,
                "max_positions_per_direction": 1,
            }
        }
    )
    result = build_strategy_lines([], config)

    assert "structural_swing_v1" in result
    strategy = result["structural_swing_v1"]
    assert strategy is not None
    # RuleEngineStrategyWrapper stores engine as .engine (not .rule_engine)
    assert strategy.engine is not None
    assert strategy.engine.sl_mult == 3.0
    assert strategy.engine.tp_mult == 1.5

"""TECH_DEBT-006 (DQAF-20260803-001 / IC 最高执行令) — per-symbol minimum economic volume.

Verifies the per-symbol MIN_ECONOMIC floor:
- BTC derives its own legal floor 0.01 (removes structural plant-state)
- XAU keeps 2×lot_step = 0.02 (status quo preserved)
- explicit live.yaml config override wins
- strategy_builder static cross-symbol validation warns (not raises) on
  strategies whose base_volume sits below the floor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.execution.strategy_line import StrategyLineConfig
from tests.mock_kit.config_factory import TEST_BASE_DIR

# ── resolved_min_economic_volume derivation ────────────────────────────────


def test_btc_derives_own_floor_001() -> None:
    """BTC (base_volume 0.01, lot_step 0.01) must resolve to 0.01 —
    the IC ruling floor that removes the structural plant-state."""
    cfg = StrategyLineConfig(
        name="btc_swing_h1_v2",
        magic=90412,
        brain_types={"lightgbm_v1"},
        base_dir=TEST_BASE_DIR,
        symbol="BTCUSDc",
        base_volume=0.01,
        lot_step=0.01,
        contract_size=1.0,  # ASSET_REGISTRY BTCUSDc (Defense 1)
    )
    assert cfg.resolved_min_economic_volume == 0.01


def test_xau_keeps_two_lot_step() -> None:
    """XAU (lot_step 0.01) must resolve to 0.02 — preserves FIX-20260730-010
    status quo; the floor was calibrated for XAU's cost structure."""
    cfg = StrategyLineConfig(
        name="m30_swing",
        magic=90003,
        brain_types={"xgboost_v9"},
        base_dir=TEST_BASE_DIR,
        symbol="XAUUSDc",
        base_volume=0.05,
        lot_step=0.01,
    )
    assert cfg.resolved_min_economic_volume == 0.02


def test_explicit_config_wins_over_derivation() -> None:
    """Explicit min_economic_volume in live.yaml overrides symbol-aware default."""
    cfg = StrategyLineConfig(
        name="btc_swing_h1_v2",
        magic=90412,
        brain_types={"lightgbm_v1"},
        base_dir=TEST_BASE_DIR,
        symbol="BTCUSDc",
        base_volume=0.01,
        lot_step=0.01,
        contract_size=1.0,  # ASSET_REGISTRY BTCUSDc (Defense 1)
        min_economic_volume=0.03,
    )
    assert cfg.resolved_min_economic_volume == 0.03


def test_btc_floor_independent_of_base_volume_scale() -> None:
    """BTC floor is max(lot_step, base_volume): a larger base_volume must not
    silently lower the floor below lot_step granularity."""
    cfg = StrategyLineConfig(
        name="btc_swing_h4",
        magic=90415,
        brain_types={"xgboost_v9"},
        base_dir=TEST_BASE_DIR,
        symbol="BTCUSDc",
        base_volume=0.05,
        lot_step=0.01,
        contract_size=1.0,  # ASSET_REGISTRY BTCUSDc (Defense 1)
    )
    assert cfg.resolved_min_economic_volume == 0.05  # max(0.01, 0.05)


# ── strategy_builder static cross-symbol validation ─────────────────────────


def test_builder_static_validation_warns_not_raises() -> None:
    """A strategy whose base_volume < per-symbol floor must log a warning, not
    raise — the floor may be intentional per IC ruling (warning-only gate)."""
    from unittest.mock import MagicMock

    from core.execution.swing_strategy import SwingStrategy
    from core.runtime.strategy_builder import _validate_min_economic_floors

    below_floor = StrategyLineConfig(
        name="structural_below_floor",
        magic=99999,
        brain_types={"lightgbm_v1"},
        base_dir=TEST_BASE_DIR,
        symbol="BTCUSDc",
        base_volume=0.005,  # deliberately below BTC floor 0.01
        lot_step=0.01,
        contract_size=1.0,  # ASSET_REGISTRY BTCUSDc (Defense 1)
    )
    ok_strategy = StrategyLineConfig(
        name="ok_strategy",
        magic=99998,
        brain_types={"lightgbm_v1"},
        base_dir=TEST_BASE_DIR,
        symbol="BTCUSDc",
        base_volume=0.01,
        lot_step=0.01,
        contract_size=1.0,  # ASSET_REGISTRY BTCUSDc (Defense 1)
    )
    dummy_brain = {
        "brain_id": "test_brain",
        "contract_group": "btc_swing_h1_v2",
        "status": "probation",
        "brain_type": "lightgbm_v1",
        "vote_weight": 1.0,
        "training_contract": "swing",
    }

    strategies: dict[str, object] = {
        "structural_below_floor": SwingStrategy(
            below_floor, [dummy_brain], budget=SimpleNamespace()
        ),
        "ok_strategy": SwingStrategy(ok_strategy, [dummy_brain], budget=SimpleNamespace()),
    }

    # Healthy strategy passes silently; below-floor strategy triggers exactly
    # one warning — and never raises.  (logger.warning uses lazy %-interpolation,
    # so under mock the args remain the format string + format args; asserting
    # call_count verifies the below-floor strategy alone was flagged.)
    from core.runtime import strategy_builder

    mock_warn = MagicMock()
    with patch.object(strategy_builder.logger, "warning", mock_warn):
        _validate_min_economic_floors(strategies)

    assert mock_warn.call_count == 1
    _fmt, _args = mock_warn.call_args.args[0], mock_warn.call_args.args[1:]
    assert "structural_below_floor" in _args[0]  # the strategy name passed as arg

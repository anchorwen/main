"""Shared fixtures for runtime-layer tests (Tier 1 — capital path).

These fixtures support tests for live_cycle, strategy_builder,
strategy_evaluator, signal_pipeline, and extracted domain services.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Placeholder — real fixtures imported from mock_kit once Phase 0.2 is done
# ---------------------------------------------------------------------------
# from tests.mock_kit.brain_factory import create_mock_brain_adapter
# from tests.mock_kit.config_factory import create_minimal_live_config
# from tests.mock_kit.data_factory import create_synthetic_ohlc_bars
# from tests.mock_kit.market_stress_factory import generate_flash_crash

"""Shared Mock Kit for institutional-grade testing.

Provides standardized test doubles across four domains:
- brain_factory: Mock brain adapters, TestProposal, signal generators
- data_factory: Synthetic OHLC bars, feature vectors, regime info
- config_factory: StrategyLineConfig presets, minimal live config
- market_stress_factory: 9 extreme market scenarios for toxicity testing
- time_travel_guard: Look-ahead bias detection (proxy + context manager)

All factories use deterministic seeds and controlled random state.
No factory touches live data/ or data_btc/ directories.
"""

from tests.mock_kit.brain_factory import (
    TestProposal,
    create_mock_brain_adapter,
    make_proposal,
)
from tests.mock_kit.config_factory import (
    create_minimal_live_config,
    create_strategy_line_config,
    create_strategy_magic_map,
)
from tests.mock_kit.data_factory import (
    create_mock_feature_vector,
    create_mock_regime_info,
    create_synthetic_ohlc_bars,
    generate_random_walk_bars,
    generate_ranging_bars,
    generate_trending_bars,
)
from tests.mock_kit.time_travel_guard import (
    TimeTravelAccess,
    TimeTravelGuard,
    TimeTravelProxy,
    TimeTravelViolation,
)

__all__ = [
    # brain_factory
    "TestProposal",
    "create_mock_brain_adapter",
    "make_proposal",
    # config_factory
    "create_minimal_live_config",
    "create_strategy_line_config",
    "create_strategy_magic_map",
    # data_factory
    "create_mock_feature_vector",
    "create_mock_regime_info",
    "create_synthetic_ohlc_bars",
    "generate_random_walk_bars",
    "generate_ranging_bars",
    "generate_trending_bars",
    # time_travel_guard
    "TimeTravelAccess",
    "TimeTravelGuard",
    "TimeTravelProxy",
    "TimeTravelViolation",
]

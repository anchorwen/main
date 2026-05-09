"""Strategy type constants — enum and magic number mappings.

Centralises strategy identity so that string and integer literals are not
scattered across config files, strategy modules, and tests.
"""

from __future__ import annotations

from enum import Enum


class StrategyType(str, Enum):
    """Names of the three independent strategy lines."""

    BARRIER_12BAR = "barrier_12bar"
    MICRO_3BAR = "micro_3bar"
    STATARB_DYNAMIC = "statarb_dynamic"


# Magic numbers sent to MT5 as order identifiers
MAGIC_BARRIER: int = 90001
MAGIC_MICRO: int = 90002
MAGIC_STATARB: int = 90003

# Lookup from strategy name to magic
STRATEGY_NAME_TO_MAGIC: dict[str, int] = {
    StrategyType.BARRIER_12BAR.value: MAGIC_BARRIER,
    StrategyType.MICRO_3BAR.value: MAGIC_MICRO,
    StrategyType.STATARB_DYNAMIC.value: MAGIC_STATARB,
}

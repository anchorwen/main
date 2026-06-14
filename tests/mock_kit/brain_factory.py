"""Mock brain adapters, TestProposal, and signal generators.

Extracted from tests/execution/conftest.py — the canonical TestProposal
and mock brain adapter used across execution and engine tests.

Usage:
    from tests.mock_kit.brain_factory import (
        TestProposal, create_mock_brain_adapter, make_proposal,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Mock brain adapter
# ---------------------------------------------------------------------------
def create_mock_brain_adapter() -> Any:
    """Returns a simple mock brain adapter with call recording.

    Usage:
        adapter = create_mock_brain_adapter()
        adapter.infer.return_value = mock_signal   # set expected output
        adapter.get_signal.return_value = mock_signal
        ...
        assert len(adapter.infer.calls) == expected_count
    """
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.infer = MagicMock(return_value=None)
    adapter.get_signal = MagicMock(return_value=None)
    adapter.brain_id = "mock_brain_01"
    adapter.brain_type = "xgboost_v9"
    object.__setattr__(adapter, "calls", [])
    return adapter


# ---------------------------------------------------------------------------
# TestProposal — mutable BrainSignal-compatible stand-in
# ---------------------------------------------------------------------------
@dataclass
class TestProposal:
    """Mutable BrainSignal-compatible class for tests.

    Has all BrainSignal attributes + vote_weight for weighted-voting tests.
    Also carries legacy dict attrs for backward compat during migration.
    """

    brain_id: str = "test_brain_01"
    direction: str = "long"
    confidence: float = 0.80
    raw_score: float = 0.0
    fallback: bool = False
    runtime_ms: float = 0.0
    vote_weight: float = 1.0

    # Legacy dict attrs (backward compat with tests still using old interface)
    prediction: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)


def make_proposal(
    *,
    brain_id: str = "test_brain_01",
    up_probability: float = 0.75,
    down_probability: float = 0.25,
    confidence: float = 0.80,
    direction_bias: str = "long",
    vote_weight: float = 1.0,
    fallback_used: bool = False,
    event_time: datetime | None = None,
) -> TestProposal:
    """Build a BrainSignal-compatible TestProposal with controlled values.

    Args:
        brain_id: Unique brain identifier.
        up_probability: P(up) from model output [0, 1].
        down_probability: P(down) from model output [0, 1].
        confidence: Model confidence score [0, 1].
        direction_bias: "long" or "short".
        vote_weight: Parliament vote weight for this brain.
        fallback_used: Whether the brain used a fallback path.
        event_time: Event timestamp (defaults to None = use current time in test).

    Returns:
        TestProposal with prediction and health dicts populated.
    """
    return TestProposal(
        brain_id=brain_id,
        direction=direction_bias,
        confidence=confidence,
        raw_score=max(up_probability, down_probability),
        fallback=fallback_used,
        vote_weight=vote_weight,
        prediction={
            "up_probability": up_probability,
            "down_probability": down_probability,
            "confidence": confidence,
            "direction_bias": direction_bias,
        },
        health={"fallback_used": fallback_used},
    )

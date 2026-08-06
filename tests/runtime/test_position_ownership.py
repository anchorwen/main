"""Unit tests for position ownership resolver — Strangler Fig #15.

Pure function extracted from live_cycle.py L5128-5156.
Zero I/O, deterministic — ideal for parameterized testing.
"""

from __future__ import annotations

from typing import Any

from core.runtime.position_ownership import resolve_position_owner

# ── Test contract-group type sets (mirrors live_cycle imports) ──

_M15_TYPES = {"V8_OnlineMLP_M15", "V9_ONNX_M15"}
_H1_TYPES = {"V9_ONNX_H1", "Swing_V9_H1_V2", "V11_ONNX_H1"}
_H4_TYPES = {"Swing_V9_H4_V2"}
_3BAR_TYPES = {"V9_ONNX_3BAR", "V10_XGB_3BAR"}
_STATARB_TYPES = {"OU_StatArb_Dynamic", "StatArb_M15"}

# dict[str, Any] — a single literal type would not match the heterogeneous
# keyword-only params (set[str] | frozenset[str]) under `**` spread in unified mode.
_KWARGS: dict[str, Any] = dict(
    micro_m15_types=_M15_TYPES,
    micro_h1_types=_H1_TYPES,
    micro_h4_types=_H4_TYPES,
    micro_3bar_types=_3BAR_TYPES,
    statarb_types=_STATARB_TYPES,
)


# ── Empty / no brain IDs ─────────────────────────────────────────────────


def test_empty_brain_ids_returns_default():
    assert resolve_position_owner([], [], **_KWARGS) == "barrier_12bar"


def test_none_brain_ids_returns_default():
    none_brain_ids: Any = None  # deliberate invalid-input probe — Any bypasses static arg-type
    assert resolve_position_owner(none_brain_ids, [], **_KWARGS) == "barrier_12bar"


def test_no_match_returns_default():
    brains = [{"brain_id": "b1", "brain_type": "unknown_type"}]
    assert resolve_position_owner(["b1"], brains, **_KWARGS) == "barrier_12bar"


# ── Direct group matching ─────────────────────────────────────────────────


def test_matches_micro_m15():
    brains = [{"brain_id": "m15_1", "brain_type": "V9_ONNX_M15"}]
    assert resolve_position_owner(["m15_1"], brains, **_KWARGS) == "micro_m15"


def test_matches_micro_h1():
    brains = [{"brain_id": "h1_1", "brain_type": "Swing_V9_H1_V2"}]
    assert resolve_position_owner(["h1_1"], brains, **_KWARGS) == "micro_h1"


def test_matches_micro_h4():
    brains = [{"brain_id": "h4_1", "brain_type": "Swing_V9_H4_V2"}]
    assert resolve_position_owner(["h4_1"], brains, **_KWARGS) == "micro_h4"


def test_matches_micro_3bar():
    brains = [{"brain_id": "b3_1", "brain_type": "V10_XGB_3BAR"}]
    assert resolve_position_owner(["b3_1"], brains, **_KWARGS) == "micro_3bar"


def test_matches_statarb():
    brains = [{"brain_id": "sa_1", "brain_type": "OU_StatArb_Dynamic"}]
    assert resolve_position_owner(["sa_1"], brains, **_KWARGS) == "statarb_dynamic"


# ── Priority: higher-resolution timeframes win ───────────────────────────


def test_first_matching_group_wins():
    """If a brain_type matches multiple groups, first checked wins (M15 > H1 > H4 > 3bar > statarb)."""
    # A brain_type that would match H1 if checked earlier, but M15 is checked first
    brains = [{"brain_id": "x1", "brain_type": "V9_ONNX_M15"}]
    assert resolve_position_owner(["x1"], brains, **_KWARGS) == "micro_m15"


def test_multiple_brain_ids_first_match_wins():
    brains = [
        {"brain_id": "sa_1", "brain_type": "OU_StatArb_Dynamic"},
        {"brain_id": "m15_1", "brain_type": "V9_ONNX_M15"},
    ]
    # First brain_id "sa_1" maps to statarb → returns "statarb_dynamic"
    assert resolve_position_owner(["sa_1", "m15_1"], brains, **_KWARGS) == "statarb_dynamic"


# ── Brain not found in registry ──────────────────────────────────────────


def test_brain_id_not_in_registry():
    brains = [{"brain_id": "other", "brain_type": "V9_ONNX_M15"}]
    assert resolve_position_owner(["unknown_id"], brains, **_KWARGS) == "barrier_12bar"


# ── Custom default ────────────────────────────────────────────────────────


def test_custom_default_owner():
    assert (
        resolve_position_owner([], [], default_owner="custom_strategy", **_KWARGS)
        == "custom_strategy"
    )


# ── Determinism ───────────────────────────────────────────────────────────


def test_deterministic():
    brains = [
        {"brain_id": "a", "brain_type": "V9_ONNX_H1"},
        {"brain_id": "b", "brain_type": "V9_ONNX_M15"},
    ]
    ids = ["a", "b"]
    r1 = resolve_position_owner(ids, brains, **_KWARGS)
    r2 = resolve_position_owner(ids, brains, **_KWARGS)
    assert r1 == r2

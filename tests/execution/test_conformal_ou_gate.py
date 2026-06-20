"""Characterization tests for apply_conformal_ou_gate() — FIX-20260620-016.

Tests the extracted OU signal quality gate function that replaces the inline
ConformalOU + MetaFilter gate block formerly in strategy_line.evaluate().

Coverage targets:
  - Gate rejection path (gate_diag structure verification)
  - Gate pass-through path
  - COLD phase exploration bypass (force_min_volume)
  - MetaFilter fallback path
  - Exception handling (fail_open_guard)
  - Neither gate available → pass through
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.conformal_ou_gate import apply_conformal_ou_gate
from core.execution.strategy_decision import StrategyDecision

# ── Shared fixtures ──────────────────────────────────────────────────────────


def _make_decision(**kwargs: object) -> StrategyDecision:
    """Minimal make_decision() stub matching StrategyLine._make_decision() signature."""
    return StrategyDecision(
        strategy_name="test_strategy",
        magic=9999,
        should_trade=bool(kwargs.get("should_trade", False)),
        direction=str(kwargs.get("direction", "neutral")),
        confidence=float(kwargs.get("confidence", 0.0)),  # type: ignore[arg-type]
        volume=float(kwargs.get("volume", 0.0)),  # type: ignore[arg-type]
        sl=float(kwargs.get("sl", 0.0)),  # type: ignore[arg-type]
        tp=float(kwargs.get("tp", 0.0)),  # type: ignore[arg-type]
        hard_sl=float(kwargs.get("hard_sl", 0.0)),  # type: ignore[arg-type]
        brain_ids=list(kwargs.get("brain_ids", [])),  # type: ignore[arg-type]
        supporting_count=int(kwargs.get("supporting_count", 0)),  # type: ignore[arg-type]
        total_count=int(kwargs.get("total_count", 0)),  # type: ignore[arg-type]
        regime_mode=str(kwargs.get("regime_mode", "full")),
        reason=str(kwargs.get("reason", "")),
        gate_diag=dict(kwargs.get("gate_diag", {})),  # type: ignore[arg-type]
    )


def _default_kwargs() -> dict:
    """Return the minimum kwargs needed for apply_conformal_ou_gate()."""
    return {
        "strategy_name": "statarb_dynamic",
        "conformal_ou_gate": None,
        "meta_filter_gate": None,
        "proposals": [],
        "trend_strength": 0.3,
        "feature_vector": None,
        "micro_feature_dict": None,
        "direction": "long",
        "confidence": 0.6,
        "brain_ids": ["brain_001"],
        "support_count": 3,
        "total_count": 5,
        "regime_gate_mode": "full",
        "make_decision": _make_decision,
    }


# ── Conformal OU Gate rejection ──────────────────────────────────────────────


def test_ou_gate_rejects_returns_blocked_decision():
    """OU gate score below threshold → blocked decision with proper gate_diag."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    ou_result = {
        "passed": False,
        "score": 0.15,
        "threshold": 0.35,
        "force_min_volume": False,
        "reason": "score_0.1500_lt_threshold_0.3500",
        "features": {
            "z_score": -1.8,
            "z_entry": 3.9,
            "z_depth": 0.46,
            "z_depth_q": 0.35,
            "half_life": 45.0,
            "max_half_life": 20.0,
            "hl_q": 0.12,
            "theta": 0.003,
            "theta_min": 0.005,
            "theta_q": 0.15,
            "adx": 27.0,
            "adx_q": 0.68,
            "vel_q": 0.72,
            "ou_confidence": 0.45,
        },
    }
    mock_gate.filter.return_value = ou_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    # Should return a blocked decision
    assert blocked is not None
    assert isinstance(blocked, StrategyDecision)
    assert blocked.should_trade is False
    assert blocked.volume == 0.0
    assert blocked.reason == "score_0.1500_lt_threshold_0.3500"

    # gate_diag must be populated with all physics features
    assert blocked.gate_diag["gate"] == "conformal_ou"
    assert blocked.gate_diag["composite_score"] == 0.15
    assert blocked.gate_diag["threshold"] == 0.35
    assert blocked.gate_diag["z_score"] == -1.8
    assert blocked.gate_diag["z_entry"] == 3.9
    assert blocked.gate_diag["z_depth_q"] == 0.35
    assert blocked.gate_diag["half_life"] == 45.0
    assert blocked.gate_diag["hl_q"] == 0.12
    assert blocked.gate_diag["theta"] == 0.003
    assert blocked.gate_diag["theta_q"] == 0.15
    assert blocked.gate_diag["adx"] == 27.0
    assert blocked.gate_diag["adx_q"] == 0.68
    assert blocked.gate_diag["vel_q"] == 0.72

    # ou_result is propagated for downstream _last_ou_result
    assert raw_ou is ou_result

    # Verify filter was called with correct args
    mock_gate.filter.assert_called_once()
    call_kwargs = mock_gate.filter.call_args.kwargs
    assert call_kwargs["strategy_name"] == "statarb_dynamic"
    assert call_kwargs["adx_value"] == 15.0 + 0.3 * 40.0  # adx_approx


def test_ou_gate_passes_returns_none():
    """OU gate score above threshold → None (proceed)."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    ou_result = {
        "passed": True,
        "score": 0.55,
        "threshold": 0.35,
        "force_min_volume": False,
        "reason": "ok",
        "features": {"z_score": -2.5, "z_entry": 3.9},
    }
    mock_gate.filter.return_value = ou_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is None  # No block
    assert raw_ou is ou_result  # ou_result still propagated


def test_ou_gate_cold_exploration_bypass():
    """COLD phase force_min_volume=True → gate rejection overridden, proceed."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    ou_result = {
        "passed": False,  # Gate says no
        "score": 0.10,
        "threshold": 0.20,
        "force_min_volume": True,  # COLD phase: explore!
        "reason": "score_0.1000_lt_threshold_0.2000",
        "features": {},
    }
    mock_gate.filter.return_value = ou_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is None  # COLD bypass — don't block
    assert raw_ou is ou_result  # ou_result propagated for volume override


def test_ou_gate_exception_blocks_trade():
    """OU gate raises exception → fail_open_guard → blocked decision."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    mock_gate.filter.side_effect = RuntimeError("OU physics computation failed")

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    assert blocked.should_trade is False
    assert blocked.reason == "ou_gate_exception_blocked"
    assert blocked.gate_diag == {}  # No gate_diag on exception
    assert raw_ou is None  # No ou_result on exception


# ── MetaFilter fallback ──────────────────────────────────────────────────────


def test_metafilter_fallback_rejects():
    """When OU gate unavailable, MetaFilter rejects → blocked."""
    mock_mf = MagicMock()
    mock_mf.is_loaded = True
    mf_result = {"passed": False, "reason": "meta_filter_score_below_threshold"}
    mock_mf.filter.return_value = mf_result

    kwargs = _default_kwargs()
    kwargs["meta_filter_gate"] = mock_mf
    kwargs["feature_vector"] = [0.1] * 47  # dummy feature vector

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    assert blocked.should_trade is False
    assert blocked.reason == "meta_filter_score_below_threshold"
    assert raw_ou is None  # No ou_result from MetaFilter path

    mock_mf.filter.assert_called_once()


def test_metafilter_fallback_passes():
    """When OU gate unavailable, MetaFilter passes → proceed."""
    mock_mf = MagicMock()
    mock_mf.is_loaded = True
    mf_result = {"passed": True, "reason": "ok"}
    mock_mf.filter.return_value = mf_result

    kwargs = _default_kwargs()
    kwargs["meta_filter_gate"] = mock_mf
    kwargs["feature_vector"] = [0.1] * 47

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is None
    assert raw_ou is None


def test_metafilter_fallback_skips_when_feature_vector_none():
    """MetaFilter fallback requires feature_vector — skipped when None."""
    mock_mf = MagicMock()
    mock_mf.is_loaded = True

    kwargs = _default_kwargs()
    kwargs["meta_filter_gate"] = mock_mf
    kwargs["feature_vector"] = None  # No features → skip MetaFilter

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is None  # Pass through
    assert raw_ou is None
    mock_mf.filter.assert_not_called()


def test_metafilter_exception_blocks_trade():
    """MetaFilter raises exception → blocked."""
    mock_mf = MagicMock()
    mock_mf.is_loaded = True
    mock_mf.filter.side_effect = RuntimeError("MetaFilter model crash")

    kwargs = _default_kwargs()
    kwargs["meta_filter_gate"] = mock_mf
    kwargs["feature_vector"] = [0.1] * 47

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    assert blocked.should_trade is False
    assert blocked.reason == "meta_filter_gate_exception_blocked"
    assert raw_ou is None


# ── Neither gate available ───────────────────────────────────────────────────


def test_neither_gate_available_passes_through():
    """No gates configured → pass through (None, None)."""
    kwargs = _default_kwargs()
    # Both gates are None by default

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is None
    assert raw_ou is None


def test_ou_gate_available_but_not_loaded_falls_back_to_metafilter():
    """OU gate exists but not loaded → falls back to MetaFilter."""
    mock_ou = MagicMock()
    mock_ou.is_loaded = False  # Not loaded → skip

    mock_mf = MagicMock()
    mock_mf.is_loaded = True
    mf_result = {"passed": False, "reason": "mf_blocked"}
    mock_mf.filter.return_value = mf_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_ou
    kwargs["meta_filter_gate"] = mock_mf
    kwargs["feature_vector"] = [0.1] * 47

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    assert blocked.reason == "mf_blocked"  # MetaFilter blocked it
    mock_ou.filter.assert_not_called()  # OU gate never called
    mock_mf.filter.assert_called_once()


# ── gate_diag regression: empty features → empty dict ────────────────────────


def test_ou_gate_rejection_with_empty_features_produces_empty_gate_diag():
    """When features dict is empty, gate_diag should be empty (not crash)."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    ou_result = {
        "passed": False,
        "score": 0.05,
        "threshold": 0.35,
        "force_min_volume": False,
        "reason": "score_0.0500_lt_threshold_0.3500",
        "features": {},  # Empty features
    }
    mock_gate.filter.return_value = ou_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, raw_ou = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    assert blocked.gate_diag == {}  # Empty when features are empty
    assert blocked.reason == "score_0.0500_lt_threshold_0.3500"


# ── Schema hash verification (C-05 log contract) ─────────────────────────────


def test_gate_diag_schema_keys():
    """gate_diag dict must contain exactly 14 well-known keys (schema hash contract)."""
    mock_gate = MagicMock()
    mock_gate.is_loaded = True
    ou_result = {
        "passed": False,
        "score": 0.22,
        "threshold": 0.35,
        "force_min_volume": False,
        "reason": "score_low",
        "features": {
            "z_score": -1.5,
            "z_entry": 3.9,
            "z_depth": 0.38,
            "z_depth_q": 0.28,
            "half_life": 30.0,
            "max_half_life": 20.0,
            "hl_q": 0.25,
            "theta": 0.004,
            "theta_min": 0.005,
            "theta_q": 0.22,
            "adx": 25.0,
            "adx_q": 0.75,
            "vel_q": 0.80,
            "ou_confidence": 0.50,
        },
    }
    mock_gate.filter.return_value = ou_result

    kwargs = _default_kwargs()
    kwargs["conformal_ou_gate"] = mock_gate

    blocked, _ = apply_conformal_ou_gate(**kwargs)

    assert blocked is not None
    gd = blocked.gate_diag

    # Schema contract: 14 keys (FIX-20260620-016)
    expected_keys = {
        "gate",
        "composite_score",
        "threshold",
        "z_score",
        "z_entry",
        "z_depth_q",
        "half_life",
        "hl_q",
        "theta",
        "theta_q",
        "adx",
        "adx_q",
        "vel_q",
    }
    assert set(gd.keys()) == expected_keys, f"Schema mismatch: {set(gd.keys())} != {expected_keys}"

    # Type verification: all values must be JSON-serializable scalars
    for key, value in gd.items():
        assert isinstance(value, str | float | int | type(None)), (
            f"gate_diag['{key}'] = {value!r} (type={type(value).__name__}) — "
            f"expected JSON-serializable scalar"
        )

"""Unit tests for trend volume guards — Strangler Fig #16.

Pure functions extracted from strategy_line.py.
Zero I/O, deterministic — ideal for parameterized testing.
"""

from __future__ import annotations

from core.execution.trend_volume_guard import (
    check_minimum_rr,
    compute_counter_trend_volume_mult,
)

# ── compute_counter_trend_volume_mult ─────────────────────────────────────


def test_ct_no_regime_info():
    assert compute_counter_trend_volume_mult("statarb_dynamic", "long", None) == 1.0


def test_ct_neutral_direction():
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic", "neutral", {"regime_gate": {"h1_adx": 30.0, "primary_trend": "long"}}
        )
        == 1.0
    )


def test_ct_with_trend_no_penalty():
    """With-trend trade — no penalty."""
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic", "long", {"regime_gate": {"h1_adx": 30.0, "primary_trend": "long"}}
        )
        == 1.0
    )


def test_ct_counter_trend_penalty_applied():
    """Counter-trend with ADX above threshold — 0.70 penalty."""
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic",
            "short",
            {"regime_gate": {"h1_adx": 30.0, "primary_trend": "long"}},
            penalise_threshold=20.0,
        )
        == 0.70
    )


def test_ct_counter_trend_below_threshold():
    """Counter-trend but ADX below penalise threshold — no penalty."""
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic",
            "short",
            {"regime_gate": {"h1_adx": 15.0, "primary_trend": "long"}},
            penalise_threshold=20.0,
        )
        == 1.0
    )


def test_ct_statarb_m15_penalised():
    assert (
        compute_counter_trend_volume_mult(
            "statarb_m15",
            "long",
            {"regime_gate": {"h1_adx": 30.0, "primary_trend": "short"}},
            penalise_threshold=20.0,
        )
        == 0.70
    )


def test_ct_non_ou_strategy_no_penalty():
    """Non-statarb strategies are not in _CT_VOLUME_ELIGIBLE — no penalty."""
    assert (
        compute_counter_trend_volume_mult(
            "barrier_12bar", "short", {"regime_gate": {"h1_adx": 30.0, "primary_trend": "long"}}
        )
        == 1.0
    )


def test_ct_zero_adx():
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic", "short", {"regime_gate": {"h1_adx": 0.0, "primary_trend": "long"}}
        )
        == 1.0
    )


def test_ct_primary_neutral():
    """If primary trend is neutral, no penalty regardless of direction."""
    assert (
        compute_counter_trend_volume_mult(
            "statarb_dynamic",
            "short",
            {"regime_gate": {"h1_adx": 30.0, "primary_trend": "neutral"}},
        )
        == 1.0
    )


# ── check_minimum_rr ──────────────────────────────────────────────────────


def test_rr_above_minimum():
    """RR=2.0 passes with min_rr=1.2."""
    assert check_minimum_rr(100.0, 95.0, 110.0, min_rr_ratio=1.2) is True


def test_rr_below_minimum():
    """RR=1.1 fails with min_rr=1.2."""
    assert check_minimum_rr(100.0, 97.0, 103.3, min_rr_ratio=1.2) is False


def test_rr_exact_minimum():
    """RR=1.2 exactly meets min_rr=1.2."""
    assert check_minimum_rr(100.0, 95.0, 106.0, min_rr_ratio=1.2) is True


def test_rr_zero_sl_distance():
    """SL at entry price → sl_dist=0 → fails."""
    assert check_minimum_rr(100.0, 100.0, 110.0) is False


def test_rr_large_values():
    """Works with BTC-scale prices."""
    assert check_minimum_rr(65000.0, 64950.0, 65100.0, min_rr_ratio=1.5) is True


def test_rr_short_trade():
    """Short trade: SL above, TP below."""
    # Short: entry=100, sl=102 (2 above), tp=96 (4 below) → RR=2.0
    assert check_minimum_rr(100.0, 102.0, 96.0, min_rr_ratio=1.2) is True

"""Characterization tests for resolve_p_win() — FIX-20260620-017.

Tests the 7-step p_win resolution chain extracted from strategy_line.evaluate():
  1. Cold explore → 0.50
  2. MetaFilter → Platt-calibrated P(TP|signal)
  3. Rolling WR → median win rate from PnL store
  4. Brain confidence → monotonic fallback
  5. MetaFilter absent → elevated floor, disables UCB
  6. UCB elastic floor → confidence-based lift
  7. Regime + Z-strength adjustments → penalties
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.pwin_chain import PWinResolution, resolve_p_win

# ── Shared fixtures ──────────────────────────────────────────────────────────


def _default_kwargs() -> dict:
    """Minimum kwargs for resolve_p_win()."""
    return {
        "is_cold_explore": False,
        "meta_p_win": None,
        "pnl_store": None,
        "brains": [],
        "direction": "long",
        "confidence": 0.55,
        "strategy_name": "statarb_dynamic",
        "meta_filter": None,
        "min_p_win": 0.45,
        "regime_info": None,
        "entry_z_score": 2.5,
    }


# ── Step 1: Cold explore → 0.50 ──────────────────────────────────────────────


def test_cold_explore_forces_neutral_p_win():
    """Cold explore > all else — p_win=0.50 regardless of MetaFilter."""
    kwargs = _default_kwargs()
    kwargs["is_cold_explore"] = True
    kwargs["meta_p_win"] = 0.65  # MetaFilter says high confidence

    res = resolve_p_win(**kwargs)

    assert res.p_win == 0.50
    assert res.p_win_source == "cold_explore_neutral"
    assert not res.p_win_degraded  # Cold explore is NOT degraded (intentional)


# ── Step 2: MetaFilter → Platt-calibrated ────────────────────────────────────


def test_metafilter_primary_source():
    """MetaFilter p_win is the primary source when available."""
    kwargs = _default_kwargs()
    kwargs["meta_p_win"] = 0.62

    res = resolve_p_win(**kwargs)

    assert res.p_win == 0.62
    assert res.p_win_source == "meta_filter"
    assert not res.p_win_degraded


# ── Step 3: Rolling WR → PnL store ───────────────────────────────────────────


def test_rolling_wr_fallback_with_metafilter_present():
    """MetaFilter present + PnL → rolling_wr (source stays clean)."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.48
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    kwargs["meta_filter"] = MagicMock()  # MetaFilter present

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "rolling_wr"
    assert res.p_win == 0.48
    assert not res.meta_filter_absent


def test_rolling_wr_absent_metafilter_renames_source():
    """MetaFilter absent → rolling_wr → source renamed to rolling_wr_no_metafilter."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.52
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    # meta_filter=None → absent → source renamed

    res = resolve_p_win(**kwargs)

    assert res.meta_filter_absent
    assert res.p_win_source == "rolling_wr_no_metafilter"
    assert res.p_win == 0.52


# ── Step 4: Brain confidence → monotonic fallback ────────────────────────────


def test_brain_confidence_fallback_when_no_data():
    """No MetaFilter, no PnL store → brain confidence fallback."""
    kwargs = _default_kwargs()
    kwargs["confidence"] = 0.55
    # No meta_filter, no pnl_store → neutral_default → fail_closed → brain_confidence

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "brain_confidence"
    # confidence 0.55 → p_win = 0.40 + 0.55 * 0.20 = 0.51
    assert 0.50 <= res.p_win <= 0.52
    assert res.p_win_degraded  # brain_confidence IS degraded


def test_brain_confidence_fallback_rolling_wr_low():
    """Rolling WR ≤ 0.40 triggers brain confidence fallback."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.35  # Below 0.40 → fail_closed
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    kwargs["confidence"] = 0.60

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "brain_confidence"
    assert res.p_win_degraded


# ── Step 5: MetaFilter absent → elevated floor ───────────────────────────────


def test_metafilter_absent_elevates_floor():
    """MetaFilter absent + rolling_wr → elevated floor, source renamed."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.52  # Above 0.40, so NOT fail_closed
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    kwargs["min_p_win"] = 0.45
    # meta_filter is None → triggers MetaFilter absent

    res = resolve_p_win(**kwargs)

    assert res.meta_filter_absent
    assert res.meta_absent_floor == 0.50  # max(0.45, 0.50) = 0.50
    assert res.p_win_source == "rolling_wr_no_metafilter"
    assert not res.p_win_degraded  # p_win=0.52 > 0.40, NOT degraded


def test_metafilter_present_no_absent_flag():
    """When MetaFilter IS available, meta_filter_absent=False."""
    kwargs = _default_kwargs()
    kwargs["meta_filter"] = MagicMock()  # MetaFilter present

    res = resolve_p_win(**kwargs)

    assert not res.meta_filter_absent


# ── Step 6: UCB elastic floor ────────────────────────────────────────────────


def test_ucb_elastic_floor_lifts_rolling_wr():
    """Rolling WR between 0.40 and min_p_win → UCB elastic floor lifts it."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.42  # 0.40 < 0.42 < min_p_win(0.45)
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    kwargs["min_p_win"] = 0.45
    kwargs["confidence"] = 0.60
    kwargs["meta_filter"] = MagicMock()  # MetaFilter present → UCB active

    res = resolve_p_win(**kwargs)

    # UCB elastic floor should lift p_win
    assert res.p_win_source == "ucb_elastic_floor"
    # elastic_p_win = 0.45 - 0.05 + 0.60 * 0.10 = 0.46
    # max(0.42, 0.46) = 0.46
    assert res.p_win >= 0.45


def test_ucb_elastic_floor_inactive_when_p_win_above_min():
    """When rolling WR ≥ min_p_win, UCB elastic floor NOT triggered."""
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50  # ≥50 to bypass small-N degradation (DQAF-20260722-002)
    mock_metric.win_rate = 0.50  # Above min_p_win(0.45)
    mock_store.get_metrics.return_value = mock_metric

    kwargs = _default_kwargs()
    kwargs["pnl_store"] = mock_store
    kwargs["brains"] = [{"brain_id": "brain_001"}]
    kwargs["min_p_win"] = 0.45
    kwargs["meta_filter"] = MagicMock()

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "rolling_wr"  # UCB not triggered
    assert res.p_win == 0.50


# ── Step 7: Regime + Z-strength adjustments ──────────────────────────────────


def test_non_statarb_skips_adjustments():
    """Non-OU strategies skip regime and z-strength adjustments."""
    kwargs = _default_kwargs()
    kwargs["strategy_name"] = "barrier_12bar"
    kwargs["meta_p_win"] = 0.55

    res = resolve_p_win(**kwargs)

    assert res.p_win == 0.55  # Unchanged
    assert res.p_win_source == "meta_filter"


# ── Degraded detection ───────────────────────────────────────────────────────


def test_neutral_default_is_degraded():
    """Pure default (no data) → degraded=True."""
    kwargs = _default_kwargs()
    # No meta_filter, no pnl_store → falls through to neutral_default → brain_confidence

    res = resolve_p_win(**kwargs)

    assert res.p_win_degraded  # brain_confidence → degraded


def test_cold_explore_not_degraded():
    """Cold explore is intentional exploration — NOT degraded."""
    kwargs = _default_kwargs()
    kwargs["is_cold_explore"] = True

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "cold_explore_neutral"
    assert not res.p_win_degraded


# ── DQAF-20260802-002: Zero-vote brain exclusion ─────────────────────────────
# IC Ruling: Voting Boundary == EV Boundary.  A brain with vote_weight=0
# (muted / observation-only) must not contribute its WR to the p_win pool.


def test_cold_explore_governance_excludes_zero_vote_brain():
    """Zero-vote brain (V4_LGB vote=0.0) must NOT anchor the cold_explore pool.

    TRIO pool median([0.41, 0.4948, 0.4141]) = 0.4141 (the zero-vote floor).
    After exclusion: median([0.41, 0.4948]) = 0.4524.
    """
    gov = {
        "brain_states": {
            "BTC_Swing_M5_Binary": {
                "status": "probation",
                "vote_weight": 1.0,
                "performance_metrics": {"win_rate": 0.4948, "total_trades": 97},
            },
            "BTC_Swing_V4": {
                "status": "live",
                "vote_weight": 1.0,
                "performance_metrics": {"win_rate": 0.41, "total_trades": 100},
            },
            "BTC_Swing_V4_LGB": {
                "status": "probation",
                "vote_weight": 0.0,  # ZERO-VOTE — muted / observation-only
                "performance_metrics": {"win_rate": 0.4141, "total_trades": 99},
            },
        }
    }
    kwargs = _default_kwargs()
    kwargs.update(
        {
            "is_cold_explore": True,
            "strategy_name": "btc_swing",
            "brains": [
                {"brain_id": "BTC_Swing_M5_Binary"},
                {"brain_id": "BTC_Swing_V4"},
                {"brain_id": "BTC_Swing_V4_LGB"},
            ],
            "live_brain_ids": {"BTC_Swing_M5_Binary", "BTC_Swing_V4", "BTC_Swing_V4_LGB"},
            "governance_state": gov,
        }
    )

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "cold_explore_governance"
    assert res.p_win == 0.4524  # median of VOTING brains only — NOT 0.4141
    assert res.p_win != 0.4141


def test_rolling_wr_pool_excludes_zero_vote_brain():
    """Zero-vote brain excluded from the rolling_wr pool AND sample significance.

    If V4_LGB's 50 samples entered, total_n=50 → rolling_wr (WR=0.4141).
    Excluded: total_n=0 → brain_confidence_small_n override (0.51).  A muted
    brain's WR must never be a valid EV estimate source.
    """
    mock_store = MagicMock()
    mock_metric = MagicMock()
    mock_metric.sample_count = 50
    mock_metric.win_rate = 0.4141
    mock_store.get_metrics.return_value = mock_metric

    gov = {
        "brain_states": {
            "BTC_Swing_V4_LGB": {
                "status": "probation",
                "vote_weight": 0.0,
                "performance_metrics": {"win_rate": 0.4141, "total_trades": 99},
            },
        }
    }
    kwargs = _default_kwargs()
    kwargs.update(
        {
            "is_cold_explore": False,
            "strategy_name": "btc_swing",
            "pnl_store": mock_store,
            "brains": [{"brain_id": "BTC_Swing_V4_LGB"}],
            "live_brain_ids": {"BTC_Swing_V4_LGB"},
            "governance_state": gov,
            "meta_filter": MagicMock(),
            "confidence": 0.55,
        }
    )

    res = resolve_p_win(**kwargs)

    assert res.p_win_source == "brain_confidence_small_n"  # V4_LGB samples excluded
    assert res.p_win == 0.51  # 0.40 + 0.55 * 0.20 — zero-vote WR did NOT enter


# ── Type validation ──────────────────────────────────────────────────────────


def test_p_win_resolution_is_dataclass():
    """PWinResolution is a proper dataclass with all expected fields."""
    res = resolve_p_win(**_default_kwargs())

    assert isinstance(res, PWinResolution)
    assert hasattr(res, "p_win")
    assert hasattr(res, "p_win_source")
    assert hasattr(res, "p_win_degraded")
    assert hasattr(res, "meta_filter_absent")
    assert hasattr(res, "meta_absent_floor")
    assert isinstance(res.p_win, float)
    assert isinstance(res.p_win_source, str)
    assert isinstance(res.p_win_degraded, bool)
    assert isinstance(res.meta_filter_absent, bool)
    assert isinstance(res.meta_absent_floor, float)

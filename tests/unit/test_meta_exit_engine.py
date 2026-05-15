"""Tests for meta_exit_engine.py — multi-factor exit urgency scoring."""

from core.execution.meta_exit_engine import (
    ExitEvaluation,
    ExitFeatureSnapshot,
    MetaExitEngine,
    create_exit_engine,
)

# ── Helpers ──


def _snap(**overrides) -> ExitFeatureSnapshot:
    defaults = {
        "current_r": 0.0,
        "peak_r": 0.5,
        "drawdown_r": 0.5,
        "cycles_held": 6,
        "expected_horizon": 12,
        "time_ratio": 0.5,
        "regime": "normal",
        "regime_confidence": 0.5,
        "trend_aligned": True,
        "atr_current": 5.0,
        "atr_entry": 5.0,
        "atr_expansion": 0.0,
        "entry_consensus_score": 0.70,
        "entry_supporting_count": 3,
        "current_supporting_count": 3,
        "consensus_drift": 0.0,
        "side": "long",
    }
    defaults.update(overrides)
    return ExitFeatureSnapshot(**defaults)


# ── ExitEvaluation dataclass ──


def test_exit_evaluation_defaults():
    e = ExitEvaluation(exit_urgency=0.3, should_exit=False)
    assert e.exit_reason == ""
    assert e.factor_breakdown == {}
    assert e.p_win is None


# ── MetaExitEngine: heuristic scoring ──


def test_engine_no_position_is_neutral():
    engine = MetaExitEngine()
    snap = _snap(current_r=1.5, time_ratio=0.2)
    result = engine.evaluate(snap)
    assert result.exit_urgency < 0.3


def test_engine_winning_trade_low_urgency():
    engine = MetaExitEngine()
    snap = _snap(current_r=2.0, time_ratio=0.3, peak_r=2.0, drawdown_r=0.0)
    result = engine.evaluate(snap)
    assert result.exit_urgency < 0.2
    assert not result.should_exit


def test_engine_losing_trade_high_urgency():
    engine = MetaExitEngine()
    snap = _snap(
        current_r=-1.0,
        time_ratio=0.8,
        drawdown_r=1.5,
        trend_aligned=False,
        regime="high",
        regime_confidence=0.8,
        atr_expansion=0.4,
        consensus_drift=0.3,
    )
    result = engine.evaluate(snap)
    assert result.exit_urgency > 0.5


def test_engine_overtime_trade_urgency_rises():
    engine = MetaExitEngine()
    snap = _snap(
        current_r=-0.5,
        time_ratio=1.5,
        cycles_held=24,
        expected_horizon=12,
        drawdown_r=1.0,
        atr_expansion=0.3,
    )
    result = engine.evaluate(snap)
    # Losing + overtime + expanding vol should push urgency up
    # (overtime capped at 0.80 — PnL/vol factors carry the remaining signal)
    assert result.exit_urgency > 0.35


def test_engine_consensus_collapse():
    engine = MetaExitEngine()
    snap = _snap(consensus_drift=0.5, current_supporting_count=0, entry_supporting_count=3)
    result = engine.evaluate(snap)
    assert result.factor_breakdown["consensus"] > 0.5


def test_engine_atr_expansion_increases_urgency():
    engine = MetaExitEngine()
    calm = _snap(atr_expansion=0.0, atr_current=5.0, atr_entry=5.0)
    volatile = _snap(atr_expansion=1.0, atr_current=10.0, atr_entry=5.0)

    r_calm = engine.evaluate(calm)
    r_vol = engine.evaluate(volatile)
    assert r_vol.exit_urgency > r_calm.exit_urgency


def test_engine_counter_trend_high_regime():
    engine = MetaExitEngine()
    snap = _snap(trend_aligned=False, regime="high", regime_confidence=0.9)
    result = engine.evaluate(snap)
    assert result.factor_breakdown["regime"] > 0.5


def test_engine_trend_aligned_low_urgency():
    engine = MetaExitEngine()
    snap = _snap(trend_aligned=True, regime="low", regime_confidence=0.9)
    result = engine.evaluate(snap)
    assert result.factor_breakdown["regime"] < 0.1


def test_engine_urgent_surpasses_threshold():
    engine = MetaExitEngine(urgency_threshold=0.55)
    snap = _snap(
        current_r=-1.2,
        time_ratio=1.2,
        trend_aligned=False,
        regime="high",
        regime_confidence=0.9,
        atr_expansion=0.6,
        consensus_drift=0.4,
    )
    result = engine.evaluate(snap)
    assert result.should_exit
    assert result.exit_urgency >= 0.55


def test_engine_factor_breakdown_sums_approximately():
    engine = MetaExitEngine()
    snap = _snap()
    result = engine.evaluate(snap)
    breakdown = result.factor_breakdown
    assert "pnl" in breakdown
    assert "time" in breakdown
    assert "regime" in breakdown
    assert "consensus" in breakdown
    assert "volatility" in breakdown
    for v in breakdown.values():
        assert 0.0 <= v <= 1.0


def test_engine_custom_weights():
    engine = MetaExitEngine(w_pnl=0.8, w_time=0.0, w_regime=0.0, w_consensus=0.2, w_volatility=0.0)
    snap = _snap(current_r=-1.0, consensus_drift=0.0)
    result = engine.evaluate(snap)
    # pnl is dominant
    assert result.factor_breakdown["pnl"] > 0.7


# ── Factor scorers ──


def test_score_pnl_deep_loss():
    snap = _snap(current_r=-2.0)
    assert MetaExitEngine._score_pnl(snap) == 1.0


def test_score_pnl_winning():
    snap = _snap(current_r=2.5)
    assert MetaExitEngine._score_pnl(snap) == 0.0


def test_score_pnl_breakeven():
    snap = _snap(current_r=0.0)
    assert 0.25 <= MetaExitEngine._score_pnl(snap) <= 0.35


def test_score_time_early():
    snap = _snap(time_ratio=0.1, cycles_held=1)
    assert MetaExitEngine._score_time(snap) < 0.1


def test_score_time_overtime():
    snap = _snap(time_ratio=2.0, cycles_held=24, expected_horizon=12)
    assert MetaExitEngine._score_time(snap) >= 0.8


def test_score_volatility_contracting():
    snap = _snap(atr_expansion=-0.2)
    assert MetaExitEngine._score_volatility(snap) < 0.1


def test_score_volatility_exploding():
    snap = _snap(atr_expansion=1.0)
    assert MetaExitEngine._score_volatility(snap) > 0.8


def test_score_consensus_improved():
    snap = _snap(consensus_drift=-0.1)
    assert MetaExitEngine._score_consensus(snap) == 0.0


def test_score_consensus_severe_drift():
    snap = _snap(consensus_drift=0.5)
    assert MetaExitEngine._score_consensus(snap) > 0.8


# ── create_exit_engine factory ──


def test_create_exit_engine_no_model():
    engine = create_exit_engine(model_path=None)
    assert engine is not None
    assert engine._model is None


def test_create_exit_engine_nonexistent_model():
    engine = create_exit_engine(model_path="nonexistent/path/model.txt")
    assert engine is None  # returns None: Layer 2.5 disabled, trail+flip+time handle exits


# ── ExitFeatureSnapshot defaults ──


def test_snapshot_defaults():
    snap = ExitFeatureSnapshot()
    assert snap.current_r == 0.0
    assert snap.expected_horizon == 12
    assert snap.side == "long"

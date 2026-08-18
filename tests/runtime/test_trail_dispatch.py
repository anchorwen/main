"""TECH_DEBT-019 — trail_dispatch RR coupling integration tests (FIX-20260819-001).

Zero-coverage breakout: compute_and_dispatch_trail previously had no direct
test.  These pin the dispatch-time behaviour of the three-mechanism RR contract
(end-to-end: real ActivePositionManager + captured dispatch):

  Blueprint ① RR hard floor  — dispatched pair always satisfies RR >= min_rr.
  Blueprint ② SL_Volatility_Trail — ATR contraction that tightens TP ALSO
    tightens SL by the same ratio (symmetric coupling).
  Blueprint ③ elastic expansion — handled in compute_trail_tp (unit level).

The critical zero-change guarantee: min_rr == 0 → SL never moves via the
volatility trail and the RR guard never clamps.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.execution.position_manager import ActivePosition, ActivePositionManager
from core.execution.trail_stop_engine import TrailPolicy
from core.runtime.trail_dispatch import compute_and_dispatch_trail


def _rr_policy(min_rr: float) -> TrailPolicy:
    return TrailPolicy(
        trail_atr_mult=2.0,
        trail_atr_mult_low=1.5,
        trail_atr_mult_high=3.0,
        breakeven_threshold_atr=1.0,
        trail_activation_atr=1.0,
        min_trail_mult=0.8,
        max_lock_atr=1.5,
        tp_min_rr_ratio=min_rr,
    )


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(exit_min_step=0.01, base_dir=str(tmp_path))


def _short_pos(
    pm: ActivePositionManager,
    *,
    ticket: int,
    min_rr: float,
    current_tp: float | None = None,
) -> ActivePosition:
    """Replicates the live 4500875936 geometry (SHORT, 150-pt SL, 15× bracket
    scale).  cycles_held=0 → Chandelier trail is skipped by min_hold_cycles so
    any SL movement is attributable to the SL_Volatility_Trail (or nothing)."""
    pos = pm.register_position(
        ticket=ticket,
        side="short",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2650.0,
        initial_tp=2237.5,
        entry_atr=5.0,
        bracket_atr=75.0,
        entry_cycle=0,
        trail_atr_mult=2.0,
        current_high=2500.0,
        trail_policy=_rr_policy(min_rr),
    )
    # Profitable (passes compute_trail_tp's gate) but too shallow to arm the
    # profit ratchet (peak 0.2R < ratchet_arm_r 1.0) or fire breakeven.
    pos.lowest_low = 2499.0
    if current_tp is not None:
        pos.current_tp = current_tp
    return pos


def _capture_dispatch():
    """Return (record, mock_fn).  mock_fn records each dispatch call."""
    record = []

    def _dispatch(
        config, pos, final_sl, final_tp, *, reason="", brain_ids=None, strategy_name="", state=None
    ):
        record.append(
            {
                "sl": final_sl,
                "tp": final_tp,
                "reason": reason,
                "strategy": strategy_name,
            }
        )

    return record, _dispatch


class TestSLVolatilityTrail:
    """Blueprint ② — symmetric SL tightening at dispatch time."""

    def test_atr_contraction_tightens_sl_alongside_tp(self, tmp_path) -> None:
        """atr_ratio 0.60 tightens TP 2237.5→2372.5 AND SL 2650→2590 (same
        ratio), preserving RR = (2500−2372.5)/(2590−2500) = 1.42 ≥ 0.85."""
        pm = ActivePositionManager()
        _short_pos(pm, ticket=4500875936, min_rr=0.85)
        record, dispatch = _capture_dispatch()

        compute_and_dispatch_trail(
            config=_config(tmp_path),
            pos=pm.get_position(4500875936),
            pm=pm,
            state=SimpleNamespace(),
            mid=2495.0,
            current_atr=3.0,  # ratio 0.60 ≤ 0.80
            strategy_name="h1_swing",
            dispatch_modify_trail_fn=dispatch,
        )

        assert len(record) == 1
        call = record[0]
        # SL tightened symmetrically (2650 → 2590) — the fix under test.
        assert call["sl"] == pytest.approx(2590.0, abs=0.01)
        # TP pinned at the RR floor (not the bracket floor 2387.5).
        assert call["tp"] == pytest.approx(2372.5, abs=0.01)
        assert "sl_vol_trail" in call["reason"].split("+")
        # RR invariant holds for the DISPATCHED pair.
        rr = (2500.0 - call["tp"]) / (call["sl"] - 2500.0)
        assert rr >= 0.85

    def test_min_rr_zero_leaves_sl_untouched(self, tmp_path) -> None:
        """Zero-change lock: without an RR contract the SL must NOT move via the
        volatility trail — only the legacy TP tightening dispatches."""
        pm = ActivePositionManager()
        _short_pos(pm, ticket=8801, min_rr=0.0)
        record, dispatch = _capture_dispatch()

        compute_and_dispatch_trail(
            config=_config(tmp_path),
            pos=pm.get_position(8801),
            pm=pm,
            state=SimpleNamespace(),
            mid=2495.0,
            current_atr=3.0,  # ratio 0.60 — would arm the trail under min_rr>0
            strategy_name="structural_swing_v1",
            dispatch_modify_trail_fn=dispatch,
        )

        assert len(record) == 1
        call = record[0]
        # SL unchanged (legacy bracket floor TP 2387.5 only).
        assert call["sl"] == pytest.approx(2650.0, abs=1e-9)
        assert call["tp"] == pytest.approx(2387.5, abs=0.01)
        assert "sl_vol_trail" not in call["reason"].split("+")
        assert "tp_rr_floor" not in call["reason"].split("+")

    def test_elastic_expansion_does_not_arm_sl_vol_trail(self, tmp_path) -> None:
        """Outward TP movement (expansion) must NEVER arm the SL volatility
        trail — only genuine ATR-contraction tightening does."""
        pm = ActivePositionManager()
        pos = _short_pos(pm, ticket=8802, min_rr=0.85)
        # previously tightened; ATR now recovers → ratio 0.90 ≥ 0.85 → expand.
        pos.current_tp = 2400.0
        pos.current_sl = 2590.0
        record, dispatch = _capture_dispatch()

        compute_and_dispatch_trail(
            config=_config(tmp_path),
            pos=pos,
            pm=pm,
            state=SimpleNamespace(),
            mid=2495.0,
            current_atr=4.5,  # ratio 0.90
            strategy_name="h1_swing",
            dispatch_modify_trail_fn=dispatch,
        )

        if record:
            assert "sl_vol_trail" not in record[0]["reason"].split("+")
        # SL must not have been tightened below the expanded 2590 baseline.
        assert pos.current_sl == pytest.approx(2590.0, abs=1e-9)


class TestRRFloorDispatchGuard:
    """Blueprint ① — dispatch-time RR assertion (registry-mandated final clamp)."""

    def test_heals_collapsed_tp_at_dead_band(self, tmp_path) -> None:
        """A surviving position whose TP already collapsed (RR 0.37) — while
        compute_trail_tp sits in the 0.80–0.85 hysteresis band (returns None) —
        is pulled back to the RR floor at dispatch time.  This is the guard's
        self-heal role: it must clamp even when the TP engine is silent."""
        pm = ActivePositionManager()
        pos = _short_pos(pm, ticket=8803, min_rr=0.85, current_tp=2445.0)
        record, dispatch = _capture_dispatch()

        compute_and_dispatch_trail(
            config=_config(tmp_path),
            pos=pos,
            pm=pm,
            state=SimpleNamespace(),
            mid=2495.0,
            current_atr=4.1,  # ratio 0.82 → dead band → compute_trail_tp None
            strategy_name="h1_swing",
            dispatch_modify_trail_fn=dispatch,
        )

        assert len(record) == 1
        call = record[0]
        assert "tp_rr_floor" in call["reason"].split("+")
        # TP clamped to entry − 0.85×150 = 2372.5.
        assert call["tp"] == pytest.approx(2372.5, abs=0.01)
        rr = (2500.0 - call["tp"]) / (call["sl"] - 2500.0)
        assert rr >= 0.85

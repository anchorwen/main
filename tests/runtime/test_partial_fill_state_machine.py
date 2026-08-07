"""Regression tests for the Partial Fill State Machine (IC 2026-08-07 裁决 2b).

FIX-20260807-003: a full-close intent that only partially fills leaves a NAKED
residual.  The management loop must detect it, sync the tracked volume to the
residual, and RE-SEND the close on the next cycle — never blind-wait it out for
``PENDING_CLOSE_MAX_CYCLES`` (the "50-minute" trap that left ticket residuals
untracked and unmanaged).

Pins three converged helpers:
  * ``_probe_mt5_residual``  — MT5 ground-truth volume probe (0.0 = gone/unreachable)
  * ``_is_partial_fill``     — residual < full-close target ⇒ a partial fill is owed
  * ``_finalize_close_dispatch`` — dispatch-success finalize keeps a partial-fill
    residual tracked (pending lock LEFT ACTIVE) vs clears a full close
and ``ActivePositionManager.sync_position_volume`` (pos.volume lowered,
expected_remaining_volume kept at the full-close target).
"""

from __future__ import annotations

from types import SimpleNamespace

from core.execution.position_manager import ActivePositionManager
from core.runtime.management_phase import (
    _finalize_close_dispatch,
    _is_partial_fill,
    _probe_mt5_residual,
)


def _make_pm() -> ActivePositionManager:
    return ActivePositionManager(
        trail_atr_mult=2.0,
        breakeven_threshold_atr=1.0,
        trail_activation_atr=0.3,
    )


def _make_pos(ticket: int = 4454299643, volume: float = 0.02) -> SimpleNamespace:
    """Minimal tracked-position stand-in for the helper functions."""
    pos = SimpleNamespace()
    pos.ticket = ticket
    pos.volume = volume
    pos.expected_remaining_volume = volume  # full-close target
    return pos


class _Mt5Worker:
    """Fake MT5 worker returning a fixed position list from positions_get()."""

    def __init__(self, positions: list) -> None:
        self._positions = positions

    def positions_get(self, ticket: int | None = None) -> list:
        return self._positions


def _mt5_vol(residual: float) -> _Mt5Worker:
    """MT5 worker reporting a single position holding *residual* volume."""
    if residual <= 0:
        return _Mt5Worker([])
    return _Mt5Worker([SimpleNamespace(volume=residual)])


# ── _probe_mt5_residual ────────────────────────────────────────────────────


class TestProbeMt5Residual:
    def test_none_worker_returns_zero(self) -> None:
        assert _probe_mt5_residual(None, 4454299643) == 0.0

    def test_position_gone_returns_zero(self) -> None:
        assert _probe_mt5_residual(_mt5_vol(0.0), 4454299643) == 0.0

    def test_partial_residual_returned(self) -> None:
        assert _probe_mt5_residual(_mt5_vol(0.01), 4454299643) == 0.01

    def test_worker_exception_returns_zero(self) -> None:
        class _Broken:
            def positions_get(self, ticket=None):
                raise RuntimeError("MT5 IPC down")

        assert _probe_mt5_residual(_Broken(), 4454299643) == 0.0


# ── _is_partial_fill ───────────────────────────────────────────────────────


class TestIsPartialFill:
    def test_residual_below_target_is_partial(self) -> None:
        pos = _make_pos(volume=0.02)
        assert _is_partial_fill(0.01, pos) is True

    def test_residual_equal_to_target_is_not_partial(self) -> None:
        # Dispatch never took (MT5 still holds the full volume) → stays under
        # the flood guard's blind-wait, NOT the partial-fill re-dispatch.
        pos = _make_pos(volume=0.02)
        assert _is_partial_fill(0.02, pos) is False

    def test_zero_residual_is_not_partial(self) -> None:
        pos = _make_pos(volume=0.02)
        assert _is_partial_fill(0.0, pos) is False

    def test_falls_back_to_volume_when_expected_zero(self) -> None:
        pos = _make_pos(volume=0.02)
        pos.expected_remaining_volume = 0.0
        assert _is_partial_fill(0.01, pos) is True


# ── _finalize_close_dispatch ───────────────────────────────────────────────


class TestFinalizeCloseDispatch:
    def test_partial_fill_keeps_position_tracked(self) -> None:
        """Residual < target → position stays tracked at residual volume,
        pending lock NOT released (next cycle re-dispatches), position NOT
        cleared.  This is what ends the 50-minute blind wait."""
        pm = _make_pm()
        pm.register_position(
            ticket=4454299643,
            side="long",
            entry_price=4700.0,
            volume=0.02,
            initial_sl=4600.0,
            initial_tp=4800.0,
            entry_atr=5.0,
            entry_cycle=1,
        )
        pos = pm.get_position(4454299643)
        assert pos is not None
        pm.mark_pending_close(4454299643, 1)
        state = SimpleNamespace(known_open_tickets={})

        _finalize_close_dispatch(pm=pm, state=state, pos=pos, mt5_worker=_mt5_vol(0.01))

        # Position still tracked, volume synced to the MT5 residual.
        assert pm.has_position(4454299643)
        assert pos.volume == 0.01
        # expected_remaining_volume stays at the FULL-close target so the
        # next-cycle detection still sees a residual re-close is owed.
        assert pos.expected_remaining_volume == 0.02
        # Pending lock STILL ACTIVE → L1926 re-dispatches next cycle.
        assert pm.is_pending_close(4454299643, 2) is True

    def test_full_close_clears_position(self) -> None:
        """MT5 shows zero residual → position cleared exactly as before."""
        pm = _make_pm()
        pm.register_position(
            ticket=4454299643,
            side="long",
            entry_price=4700.0,
            volume=0.02,
            initial_sl=4600.0,
            initial_tp=4800.0,
            entry_atr=5.0,
            entry_cycle=1,
        )
        pos = pm.get_position(4454299643)
        assert pos is not None
        pm.mark_pending_close(4454299643, 1)
        state = SimpleNamespace(known_open_tickets={})

        _finalize_close_dispatch(pm=pm, state=state, pos=pos, mt5_worker=_mt5_vol(0.0))

        assert not pm.has_position(4454299643)


# ── ActivePositionManager.sync_position_volume ─────────────────────────────


class TestSyncPositionVolume:
    def test_lowers_volume_keeps_expected_target(self) -> None:
        pm = _make_pm()
        pm.register_position(
            ticket=4454299643,
            side="long",
            entry_price=4700.0,
            volume=0.02,
            initial_sl=4600.0,
            initial_tp=4800.0,
            entry_atr=5.0,
            entry_cycle=1,
        )
        pm.sync_position_volume(4454299643, 0.01)
        pos = pm.get_position(4454299643)
        assert pos is not None
        assert pos.volume == 0.01
        assert pos.expected_remaining_volume == 0.02

    def test_non_positive_volume_ignored(self) -> None:
        pm = _make_pm()
        pm.register_position(
            ticket=4454299643,
            side="long",
            entry_price=4700.0,
            volume=0.02,
            initial_sl=4600.0,
            initial_tp=4800.0,
            entry_atr=5.0,
            entry_cycle=1,
        )
        pm.sync_position_volume(4454299643, 0.0)
        pos = pm.get_position(4454299643)
        assert pos is not None
        assert pos.volume == 0.02

    def test_unknown_ticket_noop(self) -> None:
        pm = _make_pm()
        pm.sync_position_volume(999999, 0.01)  # must not raise
